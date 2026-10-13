"""One model call end to end: resolve, admit, execute once, settle, store, respond."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from psycopg_pool import AsyncConnectionPool

from llm_gateway.calls.admission import admit
from llm_gateway.calls.records import Admitted, Allowance, AlreadyFinished, CallRecord, CallRequest
from llm_gateway.calls.settlement import Outcome, settle
from llm_gateway.catalogue.model import Catalogue
from llm_gateway.llm.errors import PROBLEM_STATUS, CallRejectedError
from llm_gateway.llm.providers import (
    ProviderAdapter,
    ProviderFailure,
    ProviderSuccess,
    failure_policy,
)
from llm_gateway.llm.request import ResponseRequest
from llm_gateway.llm.resolve import ResolvedCall, resolve
from llm_gateway.llm.results import ResultStore
from llm_gateway.money import format_usd
from llm_gateway.notifier.events import dedupe_key, raise_alert
from llm_gateway.projects.contract import ContractV1
from llm_gateway.settings import Settings

logger = logging.getLogger(__name__)
# Calls keep running to settlement even when the client disconnects.
_running: set[asyncio.Task[dict[str, Any]]] = set()


@dataclass(frozen=True, slots=True)
class CallDeps:
    pool: AsyncConnectionPool
    settings: Settings
    catalogue: Catalogue
    results: ResultStore
    adapters: dict[str, ProviderAdapter]
    actor: str


@dataclass(frozen=True, slots=True)
class CallResponse:
    body: dict[str, Any]
    allowance: Allowance | None  # None for a replay, which admits nothing


async def run_call(
    deps: CallDeps,
    *,
    project_id: str,
    environment: str,
    contract: ContractV1,
    request: ResponseRequest,
    request_bytes: int,
) -> CallResponse:
    resolved = resolve(
        request,
        project_id=project_id,
        environment=environment,
        contract=contract,
        catalogue=deps.catalogue,
        default_max_output_tokens=deps.settings.default_max_output_tokens,
    )
    adapter = deps.adapters.get(resolved.provider)
    if adapter is None:
        raise CallRejectedError("provider_not_configured")
    admission = await admit(
        deps.pool,
        deps.settings,
        deps.catalogue,
        CallRequest(
            project_id=project_id,
            environment=environment,
            provider_call_id=request.provider_call_id,
            request_fingerprint=request.fingerprint(),
            operation=request.operation,
            provider=resolved.provider,
            model=resolved.model,
            service_tier="standard",
            request_bytes=request_bytes,
            max_output_tokens=resolved.max_output_tokens,
        ),
        actor=deps.actor,
    )
    if isinstance(admission, AlreadyFinished):
        return await _replay(deps, admission.call)
    task = asyncio.get_running_loop().create_task(
        _execute(deps, adapter, admission, resolved, request, project_id, environment)
    )
    _running.add(task)
    task.add_done_callback(_running.discard)
    body = await asyncio.shield(task)
    return CallResponse(body, admission.allowance)


async def _replay(deps: CallDeps, call: CallRecord) -> CallResponse:
    if call.state == "completed":
        body = await deps.results.load(call.call_key)
        if body is None:
            raise CallRejectedError("call_result_expired")
        return CallResponse({**body, "replayed": True}, None)
    code = call.failure_code if call.failure_code in PROBLEM_STATUS else "execution_unknown"
    raise CallRejectedError(code)


async def _execute(
    deps: CallDeps,
    adapter: ProviderAdapter,
    admission: Admitted,
    resolved: ResolvedCall,
    request: ResponseRequest,
    project_id: str,
    environment: str,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = await adapter.execute(resolved, request)
    except Exception as exc:  # an adapter defect; the request may have been sent
        logger.error("Adapter failed unexpectedly: %s", type(exc).__name__)
        result = ProviderFailure("execution_unknown", "adapter_error")
    latency_ms = round((time.monotonic() - started) * 1000)
    pair = f"{project_id}/{environment}"
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
        _log(
            admission.call_key,
            pair,
            request,
            resolved,
            policy.problem_code,
            None,
            None,
            latency_ms,
            result.provider_request_id,
        )
        if policy.alert:
            await raise_alert(
                deps.pool,
                event_type="provider_account_blocked",
                severity="critical",
                summary=(
                    f"{resolved.provider} refused calls for an account reason "
                    f"({result.provider_code or result.category}); calls fail until it is fixed."
                ),
                key=dedupe_key("provider_account_blocked", resolved.provider),
                dedupe_window_seconds=deps.settings.notifier_dedupe_window_seconds,
            )
        detail = f"Provider code: {result.provider_code}." if result.provider_code else None
        raise CallRejectedError(
            policy.problem_code, detail=detail, retry_after_seconds=policy.retry_after_seconds
        )
    body = _body(request, resolved, result)
    settled = await settle(
        deps.pool,
        deps.settings,
        deps.catalogue,
        admission.call_key,
        Outcome(
            "completed",
            result.usage,
            reasoning_tokens=result.reasoning_tokens,
            service_tier_used=result.service_tier,
        ),
        actor=deps.actor,
    )
    body["cost_usd"] = (
        format_usd(settled.cost_micro_usd) if settled.cost_micro_usd is not None else None
    )
    await deps.results.save(admission.call_key, body)
    _log(
        admission.call_key,
        pair,
        request,
        resolved,
        "completed",
        result.usage,
        settled.cost_micro_usd,
        latency_ms,
        result.provider_request_id,
    )
    return body


def _structured_output(request: ResponseRequest, result: ProviderSuccess) -> tuple[Any, str | None]:
    if request.text_format.type != "json_schema" or result.refusal is not None:
        return None, None
    if not result.output_text.strip():
        return None, "empty_output"
    try:
        parsed = json.loads(result.output_text)
    except ValueError:
        return None, "invalid_json"
    if not isinstance(parsed, dict):
        return None, "not_json_object"
    return parsed, None


def _body(
    request: ResponseRequest, resolved: ResolvedCall, result: ProviderSuccess
) -> dict[str, Any]:
    output_json, structured_error = _structured_output(request, result)
    usage = result.usage
    return {
        "provider_call_id": request.provider_call_id,
        "status": result.status,
        "provider": resolved.provider,
        "model": resolved.model,
        "service_tier": result.service_tier,
        "output_text": result.output_text,
        "output_json": output_json,
        "refusal": result.refusal,
        "incomplete_reason": result.incomplete_reason,
        "structured_output_error": structured_error,
        "usage": {
            "input_tokens": usage.input_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": result.reasoning_tokens,
        },
        "cost_usd": None,
        "provider_response_id": result.provider_response_id,
        "replayed": False,
    }


def _log(
    call_key: int,
    pair: str,
    request: ResponseRequest,
    resolved: ResolvedCall,
    outcome: str,
    usage: Any,
    cost: int | None,
    latency_ms: int,
    provider_request_id: str | None,
) -> None:
    # Identifiers, counts, and timings only; never prompts, outputs, or provider prose.
    logger.info(
        "call=%d pair=%s operation=%s model=%s/%s outcome=%s input_tokens=%s "
        "output_tokens=%s cost_micro_usd=%s latency_ms=%d provider_request_id=%s",
        call_key,
        pair,
        request.operation,
        resolved.provider,
        resolved.model,
        outcome,
        usage.input_tokens if usage else None,
        usage.output_tokens if usage else None,
        cost,
        latency_ms,
        provider_request_id,
    )
