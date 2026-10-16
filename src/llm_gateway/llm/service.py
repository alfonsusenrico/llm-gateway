"""One model call end to end: resolve, admit, execute once, settle, store, respond."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from jsonschema import Draft202012Validator
from psycopg_pool import AsyncConnectionPool

from llm_gateway.calls.execution import ExecutionPlan, prepare_execution
from llm_gateway.calls.records import Admitted, Allowance, CallRecord
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
from llm_gateway.notifier.events import dedupe_key, raise_alert
from llm_gateway.projects.contract import ContractV2
from llm_gateway.projects.credentials import Credential
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
    adapters: dict[str, Callable[[Credential | None], object]]
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
    contract: ContractV2,
    request: ResponseRequest,
    request_bytes: int,
) -> PreparedCall | CallRecord:
    """Resolve and admit, or return the record of a call ID that already ended."""

    def resolve_current(current: ContractV2) -> ExecutionPlan[ResolvedCall]:
        resolved = resolve(
            request,
            project_id=project_id,
            environment=environment,
            contract=current,
            catalogue=deps.catalogue,
            default_max_output_tokens=deps.settings.default_max_output_tokens,
        )
        return ExecutionPlan(
            resolved, resolved.entry, resolved.settings_digest, resolved.max_output_tokens
        )

    prepared = await prepare_execution(
        deps,
        project_id=project_id,
        environment=environment,
        task="llm",
        request=request,
        request_bytes=request_bytes,
        resolve=resolve_current,
    )
    if isinstance(prepared, CallRecord):
        return prepared
    return PreparedCall(
        prepared.plan.resolved,
        cast(ProviderAdapter, prepared.adapter),
        prepared.admission,
        prepared.pair,
    )


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
    contract: ContractV2,
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
                key=dedupe_key("provider_account_blocked", prepared.pair, resolved.provider),
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
        completed_result=(deps.results, body),
    )
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


def _structured_output(
    request: ResponseRequest, result: ProviderSuccess, resolved: ResolvedCall
) -> tuple[Any, str | None]:
    if request.text_format.type != "json_schema" or result.refusal is not None:
        return None, None
    if not result.output_text.strip():
        return None, "empty_output"
    try:
        parsed = json.loads(result.output_text)
        if resolved.entry.local is not None:
            # Python's decoder also accepts non-JSON numbers and lone UTF-16
            # surrogates. Neither can be emitted safely as our JSON result.
            json.dumps(parsed, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError):
        return None, "invalid_json"
    if not isinstance(parsed, dict):
        return None, "not_json_object"
    if resolved.entry.local is not None:
        schema = request.text_format.json_schema
        if schema is None:
            return None, "schema_mismatch"
        try:
            if not Draft202012Validator(schema).is_valid(parsed):
                return None, "schema_mismatch"
        except Exception:
            return None, "schema_mismatch"
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
        "model_alias": resolved.entry.alias,
        "definition_digest": resolved.entry.definition_digest,
        "items": list(result.output_items),
    }


def _body(
    request: ResponseRequest, resolved: ResolvedCall, result: ProviderSuccess
) -> dict[str, Any]:
    output_json, structured_error = _structured_output(request, result, resolved)
    usage = result.usage
    return {
        "provider_call_id": request.provider_call_id,
        "status": result.status,
        "provider": resolved.provider,
        "model": resolved.model,
        "model_alias": resolved.entry.alias,
        "definition_digest": resolved.entry.definition_digest,
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
