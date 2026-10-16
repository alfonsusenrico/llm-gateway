"""One embedding call through shared admission, settlement and encrypted replay."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import cast

from llm_gateway.calls.execution import ExecutionPlan, PreparedExecution, prepare_execution
from llm_gateway.calls.records import CallRecord
from llm_gateway.calls.settlement import Outcome, settle
from llm_gateway.embeddings.adapter import EmbeddingAdapter
from llm_gateway.embeddings.models import (
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingUsage,
    ResolvedEmbedding,
    resolve_embedding,
)
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.llm.providers import ProviderFailure, failure_policy
from llm_gateway.llm.service import CallDeps, CallResponse, keep_running, replay_body
from llm_gateway.notifier.events import dedupe_key, raise_alert
from llm_gateway.projects.contract import ContractV2

logger = logging.getLogger(__name__)


async def run_embedding(
    deps: CallDeps,
    *,
    project_id: str,
    environment: str,
    request: EmbeddingRequest,
    request_bytes: int,
) -> CallResponse:
    def resolve_current(contract: ContractV2) -> ExecutionPlan[ResolvedEmbedding]:
        resolved = resolve_embedding(request, contract, deps.catalogue, environment)
        return ExecutionPlan(
            resolved, resolved.entry, resolved.settings_digest, 0, resolved.input_token_upper_bound
        )

    prepared = await prepare_execution(
        deps,
        project_id=project_id,
        environment=environment,
        task="embedding",
        request=request,
        request_bytes=request_bytes,
        resolve=resolve_current,
    )
    if isinstance(prepared, CallRecord):
        return CallResponse(await replay_body(deps, prepared), None)
    body = await asyncio.shield(keep_running(_execute(deps, prepared, request)))
    return CallResponse(body, prepared.admission.allowance)


async def _execute(
    deps: CallDeps, prepared: PreparedExecution[ResolvedEmbedding], request: EmbeddingRequest
) -> dict:
    started = time.monotonic()
    adapter = cast(EmbeddingAdapter, prepared.adapter)
    try:
        result = await adapter.execute(prepared.plan.resolved, request)
    except Exception as exc:
        logger.error("Embedding adapter failed: %s", type(exc).__name__)
        result = ProviderFailure("execution_unknown", "embedding_adapter_error")
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
            "Embedding call=%s outcome=%s latency_ms=%s",
            admission.call_key,
            policy.problem_code,
            elapsed,
        )
        if policy.alert:
            await raise_alert(
                deps.pool,
                event_type="provider_account_blocked",
                severity="critical",
                summary=f"Embedding provider account access is blocked for {prepared.pair} ({entry.provider}).",
                key=dedupe_key("provider_account_blocked", prepared.pair, entry.provider),
                dedupe_window_seconds=deps.settings.notifier_dedupe_window_seconds,
            )
        raise CallRejectedError(policy.problem_code, retry_after_seconds=policy.retry_after_seconds)
    resolved = prepared.plan.resolved
    body = EmbeddingResult(
        provider_call_id=request.provider_call_id,
        status="completed",
        provider=entry.provider,
        model=entry.model,
        model_alias=entry.alias,
        definition_digest=entry.definition_digest,
        dimensions=resolved.dimensions,
        normalized=True,
        input_type=resolved.input_type,
        data=result.vectors,
        usage=EmbeddingUsage(
            input_tokens=result.usage.input_tokens, total_tokens=result.usage.input_tokens
        ),
        cost_usd=None,
        replayed=False,
    ).model_dump(mode="json")
    await settle(
        deps.pool,
        deps.settings,
        deps.catalogue,
        admission.call_key,
        Outcome("completed", result.usage, service_tier_used="standard"),
        actor=deps.actor,
        completed_result=(deps.results, body),
    )
    logger.info(
        "Embedding call=%s outcome=completed input_tokens=%s latency_ms=%s",
        admission.call_key,
        result.usage.input_tokens,
        elapsed,
    )
    return body
