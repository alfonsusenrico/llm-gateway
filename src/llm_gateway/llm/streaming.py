"""Streamed calls: a producer task that always runs to settlement, and an SSE consumer.

The producer reads the provider stream, forwards gateway events, and settles the
call exactly once. A client that disconnects only detaches the consumer; the call
still finishes, is charged its real usage, and is stored for replay.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from llm_gateway.calls.records import Allowance, CallRecord
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.llm.providers import (
    FunctionCall,
    ProviderFailure,
    ProviderSuccess,
    StreamRefusal,
    StreamStarted,
    StreamText,
)
from llm_gateway.llm.request import ResponseRequest
from llm_gateway.llm.service import (
    CallDeps,
    PreparedCall,
    finish_call,
    keep_running,
    prepare_call,
    replay_body,
    tool_call_body,
)
from llm_gateway.projects.contract import ContractV2

logger = logging.getLogger(__name__)
KEEP_ALIVE_SECONDS = 15.0
_QUEUE_SIZE = 256
GatewayEvent = tuple[str, dict[str, Any]]
_INTERNAL_ERROR = {"code": "internal_error", "status": 500, "title": "Internal error"}


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def problem_event(error: CallRejectedError) -> dict[str, Any]:
    data: dict[str, Any] = {"code": error.code, "status": error.status, "title": error.title}
    if error.detail:
        data["detail"] = error.detail
    if error.retry_after_seconds is not None:
        data["retry_after_seconds"] = error.retry_after_seconds
    return data


class _Channel:
    """A bounded queue whose producer stops waiting once the consumer is gone."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[GatewayEvent | None] = asyncio.Queue(_QUEUE_SIZE)
        self.detached = False

    async def put(self, item: GatewayEvent | None) -> None:
        if not self.detached:
            await self._queue.put(item)

    async def get(self, timeout: float) -> GatewayEvent | None:
        return await asyncio.wait_for(self._queue.get(), timeout)

    def detach(self) -> None:
        self.detached = True
        # Free a producer blocked on a full queue.
        while not self._queue.empty():
            self._queue.get_nowait()


@dataclass
class StreamPlan:
    """What to send: a live stream, or recorded events of an ended call."""

    events: list[GatewayEvent]
    channel: _Channel | None
    allowance: Allowance | None


async def _produce(
    deps: CallDeps, prepared: PreparedCall, request: ResponseRequest, channel: _Channel
) -> None:
    started = time.monotonic()
    accepted = False
    result: ProviderSuccess | ProviderFailure | None = None
    try:
        async with aclosing(prepared.adapter.execute_stream(prepared.resolved, request)) as events:
            async for event in events:
                if isinstance(event, StreamStarted):
                    accepted = True
                    await channel.put(
                        (
                            "response.started",
                            {
                                "provider_call_id": request.provider_call_id,
                                "provider": prepared.resolved.provider,
                                "model": prepared.resolved.model,
                            },
                        )
                    )
                elif isinstance(event, StreamText):
                    await channel.put(("text.delta", {"delta": event.delta}))
                elif isinstance(event, StreamRefusal):
                    await channel.put(("refusal.delta", {"delta": event.delta}))
                elif isinstance(event, FunctionCall):
                    await channel.put(("tool_call", tool_call_body(event)))
                else:
                    result = event
                    break
    except Exception as exc:  # an adapter defect; the call may have run
        logger.error("Adapter stream failed unexpectedly: %s", type(exc).__name__)
        result = ProviderFailure("execution_unknown", "adapter_error")
    if result is None:
        result = ProviderFailure("execution_unknown", "stream_ended")
    try:
        body = await finish_call(deps, prepared, request, result, started)
    except CallRejectedError as error:
        # Before acceptance the route still answers problem JSON with its status.
        await channel.put(("error" if accepted else "rejected", problem_event(error)))
    except Exception as exc:
        # The consumer must still end, or it would wait on keep-alives forever.
        logger.error("Streamed call settlement failed: %s", type(exc).__name__)
        await channel.put(("error" if accepted else "internal", _INTERNAL_ERROR))
    else:
        await channel.put(("response.completed", body))
    finally:
        await channel.put(None)


async def start_stream(
    deps: CallDeps,
    *,
    project_id: str,
    environment: str,
    contract: ContractV2,
    request: ResponseRequest,
    request_bytes: int,
) -> StreamPlan:
    """Admit and start the call; a refusal before acceptance raises CallRejectedError."""
    prepared = await prepare_call(
        deps,
        project_id=project_id,
        environment=environment,
        contract=contract,
        request=request,
        request_bytes=request_bytes,
    )
    if isinstance(prepared, CallRecord):
        try:
            body = await replay_body(deps, prepared)
        except CallRejectedError as error:
            if error.code == "call_result_expired":
                raise
            return StreamPlan([("error", problem_event(error))], None, None)
        return StreamPlan([("response.completed", body)], None, None)
    channel = _Channel()
    keep_running(_produce(deps, prepared, request, channel))
    first = await channel.get(timeout=deps.settings.llm_call_timeout_seconds + 60)
    if first is not None and first[0] == "internal":
        raise RuntimeError("streamed call settlement failed")
    if first is not None and first[0] == "rejected":
        data = first[1]
        raise CallRejectedError(
            data["code"],
            detail=data.get("detail"),
            retry_after_seconds=data.get("retry_after_seconds"),
        )
    events = [first] if first is not None else []
    return StreamPlan(events, channel, prepared.admission.allowance)


async def sse_body(plan: StreamPlan) -> AsyncGenerator[bytes]:
    """The SSE response body; on client disconnect only the consumer stops."""
    try:
        for name, data in plan.events:
            yield sse(name, data)
        if plan.channel is None or (
            plan.events and plan.events[-1][0] in ("error", "response.completed")
        ):
            return
        while True:
            try:
                item = await plan.channel.get(timeout=KEEP_ALIVE_SECONDS)
            except TimeoutError:
                yield b": keep-alive\n\n"
                continue
            if item is None:
                return
            yield sse(*item)
    finally:
        if plan.channel is not None:
            plan.channel.detach()
