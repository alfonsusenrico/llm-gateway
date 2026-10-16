from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from llm_gateway_client import (
    Completed,
    ExecutionUnknownError,
    GatewayClient,
    InvalidRequestError,
    RefusalDelta,
    RetryableError,
    RetryPolicy,
    Started,
    TextDelta,
)

pytestmark = pytest.mark.anyio
RESULT = {
    "provider_call_id": "s-1",
    "status": "completed",
    "provider": "stub",
    "model": "stub-echo",
    "model_alias": "stub-echo",
    "definition_digest": "a" * 64,
    "output_text": "stub: hi there",
    "usage": {"input_tokens": 3, "output_tokens": 3},
    "cost_usd": "0.00",
    "replayed": False,
}
STARTED = {"provider_call_id": "s-1", "provider": "stub", "model": "stub-echo"}


def sse(*events: tuple[str, dict[str, Any]]) -> bytes:
    return b"".join(f"event: {n}\ndata: {json.dumps(d)}\n\n".encode() for n, d in events)


class Breaking(httpx2.AsyncByteStream):
    def __init__(self, first: bytes) -> None:
        self.first = first

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.first
        raise httpx2.ReadError("connection lost mid-stream")


def streamed(body: bytes | httpx2.AsyncByteStream) -> httpx2.Response:
    headers = {"content-type": "text/event-stream", "X-Concurrency-Limit": "2"}
    if isinstance(body, bytes):
        return httpx2.Response(200, content=body, headers=headers)
    return httpx2.Response(200, stream=body, headers=headers)


def problem(status: int, code: str) -> httpx2.Response:
    return httpx2.Response(status, json={"code": code, "title": code}, headers={"Retry-After": "2"})


class Script:
    def __init__(self, *answers: httpx2.Response | Exception) -> None:
        self.answers = list(answers)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def client(script: Script, sleeps: list[float], **policy: Any) -> GatewayClient:
    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return GatewayClient(
        "http://gateway",
        "lgw.dev.key",
        retry_policy=RetryPolicy(**policy),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(script)),
        sleep=record_sleep,
    )


async def collect(gateway: GatewayClient) -> list[Any]:
    return [
        event
        async for event in gateway.responses.stream(
            provider_call_id="s-1", operation="demo", input="hi there", profile="chat"
        )
    ]


async def test_stream_yields_typed_events() -> None:
    script = Script(
        streamed(
            sse(
                ("response.started", STARTED),
                ("text.delta", {"delta": "stub: "}),
                ("refusal.delta", {"delta": "no"}),
                ("text.delta", {"delta": "hi there"}),
                ("response.completed", RESULT),
            )
            + b": keep-alive\n\n"
        )
    )
    gateway = client(script, [])

    events = await collect(gateway)

    assert isinstance(events[0], Started) and events[0].provider_call_id == "s-1"
    assert [e.delta for e in events if isinstance(e, TextDelta)] == ["stub: ", "hi there"]
    assert [e.delta for e in events if isinstance(e, RefusalDelta)] == ["no"]
    assert isinstance(events[-1], Completed)
    assert events[-1].result.usage.output_tokens == 3
    assert script.bodies[0]["stream"] is True and script.bodies[0]["profile"] == "chat"
    assert gateway.concurrency_limit == 2


async def test_refusal_before_start_is_retried_like_a_direct_call() -> None:
    sleeps: list[float] = []
    script = Script(
        problem(503, "provider_unavailable"),
        streamed(sse(("response.started", STARTED), ("response.completed", RESULT))),
    )

    events = await collect(client(script, sleeps))

    assert isinstance(events[-1], Completed) and len(sleeps) == 1
    assert sleeps[0] >= 2


async def test_invalid_request_raises_without_retry() -> None:
    script = Script(problem(422, "request_invalid"))

    with pytest.raises(InvalidRequestError):
        await collect(client(script, []))
    assert len(script.bodies) == 1


async def test_error_event_raises_its_typed_error() -> None:
    script = Script(
        streamed(
            sse(
                ("response.started", STARTED),
                ("text.delta", {"delta": "stub: "}),
                ("error", {"code": "execution_unknown", "status": 502, "title": "unknown"}),
            )
        )
    )

    with pytest.raises(ExecutionUnknownError):
        await collect(client(script, []))
    assert len(script.bodies) == 1  # never resubmitted


async def test_dropped_stream_resubmits_and_ends_with_the_replay() -> None:
    sleeps: list[float] = []
    replayed = {**RESULT, "replayed": True}
    script = Script(
        streamed(Breaking(sse(("response.started", STARTED), ("text.delta", {"delta": "stub"})))),
        problem(409, "call_in_progress"),
        streamed(sse(("response.completed", replayed))),
    )

    events = await collect(client(script, sleeps))

    assert [type(e).__name__ for e in events] == ["Started", "TextDelta", "Completed"]
    assert events[-1].result.replayed is True
    assert events[-1].result.output_text == "stub: hi there"
    assert {body["provider_call_id"] for body in script.bodies} == {"s-1"}
    assert len(sleeps) == 2


async def test_stream_ending_without_its_final_event_is_resubmitted() -> None:
    replayed = {**RESULT, "replayed": True}
    script = Script(
        streamed(sse(("response.started", STARTED))),
        streamed(sse(("response.completed", replayed))),
    )

    events = await collect(client(script, []))

    assert isinstance(events[-1], Completed) and events[-1].result.replayed is True


async def test_retry_budget_bounds_resubmission() -> None:
    script = Script(
        streamed(Breaking(sse(("response.started", STARTED)))),
        problem(409, "call_in_progress"),
    )

    with pytest.raises(RetryableError) as raised:
        await collect(client(script, [], max_attempts=2))
    assert raised.value.code == "call_in_progress"
