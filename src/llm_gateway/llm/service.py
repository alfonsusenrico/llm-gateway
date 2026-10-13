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
    FunctionCall,
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


@dataclass(frozen=True, slots=True)
class PreparedCall:
    resolved: ResolvedCall
    adapter: ProviderAdapter
    admission: Admitted
    pair: str


async def prepare_call(
    deps: CallDeps,
    *,
    project_id: str,
    environment: str,
    contract: ContractV1,
    request: ResponseRequest,
    request_bytes: int,
) -> PreparedCall | CallRecord:
    """Resolve and admit, or return the record of a call ID that already ended."""
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
        return admission.call
    return PreparedCall(resolved, adapter, admission, f"{project_id}/{environment}")


def keep_running(coroutine: Any) -> asyncio.Task[Any]:
    """Run to the end even if the request that started it is cancelled."""
    task = asyncio.get_running_loop().create_task(coroutine)
    _running.add(task)
    task.add_done_callback(_running.discard)
    return task


async def run_call(
    deps: CallDeps,
    *,
    project_id: str,
    environment: str,
    contract: ContractV1,
    request: ResponseRequest,
    request_bytes: int,
) -> CallResponse:
    prepared = await prepare_call(
        deps,
        project_id=project_id,
        environment=environment,
        contract=contract,
        request=request,
        request_bytes=request_bytes,
    )
    if isinstance(prepared, CallRecord):
        return CallResponse(await replay_body(deps, prepared), None)
    body = await asyncio.shield(keep_running(_execute(deps, prepared, request)))
    return CallResponse(body, prepared.admission.allowance)


async def replay_body(deps: CallDeps, call: CallRecord) -> dict[str, Any]:
    """The stored answer of an ended call, or its recorded problem raised again."""
    if call.state == "completed":
        body = await deps.results.load(call.call_key)
        if body is None:
            raise CallRejectedError("call_result_expired")
        return {**body, "replayed": True}
    code = call.failure_code if call.failure_code in PROBLEM_STATUS else "execution_unknown"
    raise CallRejectedError(code)


async def _execute(
    deps: CallDeps, prepared: PreparedCall, request: ResponseRequest
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = await prepared.adapter.execute(prepared.resolved, request)
    except Exception as exc:  # an adapter defect; the request may have been sent
        logger.error("Adapter failed unexpectedly: %s", type(exc).__name__)
        result = ProviderFailure("execution_unknown", "adapter_error")
    return await finish_call(deps, prepared, request, result, started)


async def finish_call(
    deps: CallDeps,
    prepared: PreparedCall,
    request: ResponseRequest,
    result: ProviderSuccess | ProviderFailure,
    started: float,
) -> dict[str, Any]:
    """Settle the outcome, store a completed body, and return it; raise a failure's problem."""
    latency_ms = round((time.monotonic() - started) * 1000)
    admission, resolved = prepared.admission, prepared.resolved
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
            prepared.pair,
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
        prepared.pair,
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


def tool_call_body(call: FunctionCall) -> dict[str, Any]:
    """A tool call as projects see it, with the arguments parsed when they are valid."""
    try:
        parsed = json.loads(call.arguments)
    except ValueError:
        parsed, error = None, "invalid_json"
    else:
        error = None if isinstance(parsed, dict) else "not_json_object"
        parsed = parsed if isinstance(parsed, dict) else None
    return {
        "call_id": call.call_id,
        "name": call.name,
        "arguments": call.arguments,
        "arguments_json": parsed,
        "arguments_error": error,
    }


def _continuation(resolved: ResolvedCall, result: ProviderSuccess) -> dict[str, Any] | None:
    # Only a turn that asks for tools needs its items back; a final answer starts over.
    if not result.function_calls:
        return None
    return {
        "provider": resolved.provider,
        "model": resolved.model,
        "items": list(result.output_items),
    }


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
        "tool_calls": [tool_call_body(call) for call in result.function_calls],
        "continuation": _continuation(resolved, result),
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
