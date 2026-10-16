"""One OCR call through shared admission, settlement and encrypted replay."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import cast

from llm_gateway.calls.admission import find_call
from llm_gateway.calls.execution import ExecutionPlan, PreparedExecution, prepare_execution
from llm_gateway.calls.records import AdmissionRefusedError, CallRecord
from llm_gateway.calls.settlement import Outcome, settle
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.llm.providers import ProviderFailure, failure_policy
from llm_gateway.llm.service import CallDeps, CallResponse, keep_running, replay_body
from llm_gateway.ocr.adapter import OcrAdapter
from llm_gateway.ocr.models import (
    OcrRequest,
    OcrResult,
    ResolvedOcr,
    resolve_ocr,
    validate_image,
)
from llm_gateway.projects.contract import ContractV2

logger = logging.getLogger(__name__)


async def run_ocr(
    deps: CallDeps,
    *,
    project_id: str,
    environment: str,
    request: OcrRequest,
    request_bytes: int,
) -> CallResponse:
    previous = await find_call(deps.pool, project_id, environment, request.provider_call_id)
    if previous is not None:
        if previous.task != "ocr" or previous.request_fingerprint != request.fingerprint():
            raise CallRejectedError("idempotency_conflict")
        if previous.state == "admitted":
            raise AdmissionRefusedError("call_in_progress", status=409, retry_after_seconds=1)
        if previous.state != "retryable_failure":
            return CallResponse(await replay_body(deps, previous), None)
    image = await validate_image(request)

    def resolve_current(contract: ContractV2) -> ExecutionPlan[ResolvedOcr]:
        resolved = resolve_ocr(request, image, contract, deps.catalogue)
        return ExecutionPlan(resolved, resolved.entry, resolved.settings_digest, 0)

    prepared = await prepare_execution(
        deps,
        project_id=project_id,
        environment=environment,
        task="ocr",
        request=request,
        request_bytes=request_bytes,
        resolve=resolve_current,
    )
    if isinstance(prepared, CallRecord):
        return CallResponse(await replay_body(deps, prepared), None)
    body = await asyncio.shield(keep_running(_execute(deps, prepared, request)))
    return CallResponse(body, prepared.admission.allowance)


async def _execute(
    deps: CallDeps, prepared: PreparedExecution[ResolvedOcr], request: OcrRequest
) -> dict:
    started = time.monotonic()
    adapter = cast(OcrAdapter, prepared.adapter)
    try:
        result = await adapter.execute(prepared.plan.resolved, request)
    except Exception as exc:
        logger.error("OCR adapter failed: %s", type(exc).__name__)
        result = ProviderFailure("execution_unknown", "ocr_adapter_error")
    entry, admission = prepared.plan.entry, prepared.admission
    elapsed = round((time.monotonic() - started) * 1000)
    if isinstance(result, ProviderFailure):
        policy = failure_policy(result)
        await settle(
            deps.pool,
            deps.settings,
            deps.catalogue,
            admission.call_key,
            Outcome(policy.settled_state, result.usage, failure_code=policy.problem_code),
            actor=deps.actor,
        )
        logger.info(
            "OCR call=%s outcome=%s latency_ms=%s",
            admission.call_key,
            policy.problem_code,
            elapsed,
        )
        raise CallRejectedError(policy.problem_code, retry_after_seconds=policy.retry_after_seconds)
    resolved = prepared.plan.resolved
    body = OcrResult(
        provider_call_id=request.provider_call_id,
        status="completed",
        provider=entry.provider,
        model=entry.model,
        model_alias=entry.alias,
        definition_digest=entry.definition_digest,
        media_type=request.media_type,
        width=resolved.image.width,
        height=resolved.image.height,
        coordinate_system="original_image_pixels",
        exif_rotation=False,
        text=result.text,
        blocks=result.blocks,
        cost_usd=None,
        replayed=False,
    ).model_dump(mode="json")
    await settle(
        deps.pool,
        deps.settings,
        deps.catalogue,
        admission.call_key,
        Outcome("completed", None, service_tier_used="standard"),
        actor=deps.actor,
        completed_result=(deps.results, body),
    )
    logger.info(
        "OCR call=%s outcome=completed blocks=%s latency_ms=%s",
        admission.call_key,
        len(result.blocks),
        elapsed,
    )
    return body
