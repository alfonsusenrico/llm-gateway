from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx2
import pytest
from psycopg_pool import AsyncConnectionPool

from llm_gateway.llm import streaming
from llm_gateway.llm.openai_adapter import OpenAIAdapter, create_openai_client
from llm_gateway.llm.providers import (
    ProviderFailure,
    ProviderSuccess,
    StreamStarted,
    StreamText,
)
from llm_gateway.llm.request import ResponseRequest
from llm_gateway.llm.stub_adapter import StubAdapter
from tests.test_openai_adapter import RESOLVED, _request, _success_body
from tests.test_responses_api import Calls, FakeOpenAI, _luna_success, calls_context

pytestmark = pytest.mark.anyio


@pytest.fixture
async def calls(migrated_pool: AsyncConnectionPool) -> AsyncIterator[Calls]:
    async with calls_context(migrated_pool) as context:
        yield context


def _sse(*events: dict[str, Any]) -> bytes:
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
    )


def _created() -> dict[str, Any]:
    body = _success_body(status="in_progress", output=[], usage=None)
    return {"type": "response.created", "sequence_number": 0, "response": body}


def _delta(text: str, n: int) -> dict[str, Any]:
    return {
        "type": "response.output_text.delta",
        "sequence_number": n,
        "item_id": "msg_1",
        "output_index": 0,
        "content_index": 0,
        "delta": text,
        "logprobs": [],
    }


def _terminal(kind: str = "response.completed", **body: Any) -> dict[str, Any]:
    return {"type": kind, "sequence_number": 9, "response": _success_body(**body)}


class _Breaking(httpx2.AsyncByteStream):
    """Sends some SSE bytes, then loses the connection."""

    def __init__(self, first: bytes) -> None:
        self.first = first

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.first
        raise httpx2.ReadError("connection lost mid-stream")


def _adapter(body: bytes | httpx2.AsyncByteStream, status: int = 200) -> OpenAIAdapter:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        headers = {"content-type": "text/event-stream", "x-request-id": "req_s"}
        if isinstance(body, bytes):
            return httpx2.Response(status, content=body, headers=headers)
        return httpx2.Response(status, stream=body, headers=headers)

    client = create_openai_client(
        "sk-test", 30, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    )
    return OpenAIAdapter(client)


async def _collect(adapter: Any, request: ResponseRequest | None = None) -> list[Any]:
    return [event async for event in adapter.execute_stream(RESOLVED, request or _request())]


async def test_stream_yields_started_deltas_and_the_final_result() -> None:
    events = await _collect(
        _adapter(_sse(_created(), _delta("Hello ", 1), _delta("there.", 2), _terminal()))
    )

    assert isinstance(events[0], StreamStarted) and events[0].provider_request_id == "req_s"
    assert [e.delta for e in events if isinstance(e, StreamText)] == ["Hello ", "there."]
    final = events[-1]
    assert isinstance(final, ProviderSuccess)
    assert (final.output_text, final.usage.input_tokens) == ("Hello there.", 2000)


async def test_incomplete_and_failed_terminals() -> None:
    incomplete = await _collect(
        _adapter(
            _sse(
                _created(),
                _terminal(
                    "response.incomplete",
                    status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                ),
            )
        )
    )
    failed = await _collect(
        _adapter(
            _sse(
                _created(),
                _terminal(
                    "response.failed",
                    status="failed",
                    error={"code": "server_error", "message": "x"},
                    output=[],
                ),
            )
        )
    )

    assert (
        isinstance(incomplete[-1], ProviderSuccess)
        and incomplete[-1].incomplete_reason == "max_output_tokens"
    )
    assert isinstance(failed[-1], ProviderFailure) and failed[-1].category == "provider_failed"


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("rate_limit_exceeded", "temporary_throttle"),
        ("server_is_overloaded", "temporary_overload"),
        ("credit_balance_exhausted", "account_blocked"),
        ("something_new", "provider_failed"),
    ],
)
async def test_error_event_before_acceptance_is_classified(code: str, category: str) -> None:
    events = await _collect(
        _adapter(
            _sse(
                {"type": "error", "sequence_number": 0, "code": code, "message": "m", "param": None}
            )
        )
    )

    assert len(events) == 1 and isinstance(events[0], ProviderFailure)
    assert events[0].category == category


async def test_error_event_after_acceptance_is_provider_failed() -> None:
    events = await _collect(
        _adapter(
            _sse(
                _created(),
                _delta("Hel", 1),
                {
                    "type": "error",
                    "sequence_number": 2,
                    "code": "server_error",
                    "message": "m",
                    "param": None,
                },
            )
        )
    )

    assert isinstance(events[-1], ProviderFailure) and events[-1].category == "provider_failed"


async def test_connection_lost_mid_stream_is_execution_unknown() -> None:
    events = await _collect(_adapter(_Breaking(_sse(_created(), _delta("Hel", 1)))))

    assert isinstance(events[0], StreamStarted)
    assert isinstance(events[-1], ProviderFailure) and events[-1].category == "execution_unknown"


async def test_stream_without_a_terminal_event_is_execution_unknown() -> None:
    events = await _collect(_adapter(_sse(_created(), _delta("Hel", 1))))

    assert isinstance(events[-1], ProviderFailure)
    assert (events[-1].category, events[-1].provider_code) == ("execution_unknown", "stream_ended")


async def test_refusal_at_create_keeps_its_status_category() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "slow",
                "type": "rate_limit_error",
                "code": "slow_down",
                "param": None,
            }
        }
    ).encode()
    events = await _collect(_adapter(body, status=429))

    assert len(events) == 1 and isinstance(events[0], ProviderFailure)
    assert events[0].category == "temporary_throttle"


async def test_stub_streams_words_and_can_fail_mid_stream() -> None:
    stub_request = ResponseRequest.model_validate(
        {
            "provider_call_id": "s",
            "operation": "demo",
            "model": {"provider": "stub", "model": "stub-echo"},
            "input": "one two three four",
        }
    )
    ok = await _collect(StubAdapter(), stub_request)
    broken = await _collect(
        StubAdapter(),
        ResponseRequest.model_validate(
            {**stub_request.model_dump(), "stub": {"scenario": "fail_mid_stream"}}
        ),
    )

    assert "".join(e.delta for e in ok if isinstance(e, StreamText)) == "stub: one two three four"
    assert isinstance(ok[-1], ProviderSuccess)
    assert sum(isinstance(e, StreamText) for e in broken) == 2
    assert isinstance(broken[-1], ProviderFailure) and broken[-1].category == "execution_unknown"


def _events(text: str) -> list[tuple[str, Any]]:
    parsed = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n") if not line.startswith(":"))
        if "event" in lines:
            parsed.append((lines["event"], json.loads(lines["data"])))
    return parsed


@pytest.mark.db
async def test_streamed_call_end_to_end(calls: Calls) -> None:
    response = await calls.post(stream=True, input="alpha beta gamma")

    events = _events(response.text)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["X-Concurrency-Limit"] == "2"
    assert next(name for name, _ in events) == "response.started"
    assert (
        "".join(d["delta"] for name, d in events if name == "text.delta")
        == "stub: alpha beta gamma"
    )
    assert events[-1][0] == "response.completed"
    assert events[-1][1]["output_text"] == "stub: alpha beta gamma"
    assert (await calls.ledger())[0][:2] == ("call-1", "completed")


@pytest.mark.db
async def test_refusal_before_acceptance_is_problem_json(calls: Calls) -> None:
    response = await calls.post(stream=True, stub={"scenario": "overloaded"})

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "provider_unavailable"


@pytest.mark.db
async def test_failure_after_acceptance_is_an_error_event(calls: Calls) -> None:
    response = await calls.post(
        stream=True, input="one two three four", stub={"scenario": "fail_mid_stream"}
    )

    events = _events(response.text)
    assert response.status_code == 200
    assert events[-1] == (
        "error",
        {
            "code": "execution_unknown",
            "status": 502,
            "title": events[-1][1]["title"],
            "detail": "Provider code: stub_fail_mid_stream.",
        },
    )
    assert (await calls.ledger())[0][1] == "execution_unknown"


@pytest.mark.db
async def test_settlement_error_ends_the_stream_with_an_internal_error(
    calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_finish(*_args: Any) -> dict[str, Any]:
        raise RuntimeError("database gone")

    monkeypatch.setattr(streaming, "finish_call", broken_finish)

    with anyio.fail_after(5):
        response = await calls.post(stream=True, input="one two")

    events = _events(response.text)
    assert events[-1] == (
        "error",
        {"code": "internal_error", "status": 500, "title": "Internal error"},
    )


@pytest.mark.db
async def test_replays_are_one_event_streams(calls: Calls) -> None:
    await calls.post(stream=True)
    await calls.post(
        provider_call_id="lost", stream=True, input="a b c d", stub={"scenario": "fail_mid_stream"}
    )

    completed = _events((await calls.post(stream=True)).text)
    unknown = _events(
        (
            await calls.post(
                provider_call_id="lost",
                stream=True,
                input="a b c d",
                stub={"scenario": "fail_mid_stream"},
            )
        ).text
    )
    direct = (await calls.post()).json()

    assert [name for name, _ in completed] == ["response.completed"]
    assert completed[0][1]["replayed"] is True
    assert [name for name, _ in unknown] == ["error"]
    assert unknown[0][1]["code"] == "execution_unknown"
    assert direct["replayed"] is True  # stream and direct share one call ID


@pytest.mark.db
async def test_disconnected_client_still_settles_and_can_replay(calls: Calls) -> None:
    slow = FakeOpenAI(_luna_success(), delay=0.05)
    calls.gateway.app.state.call_deps.adapters["openai"] = slow
    loaded = await calls.gateway.authenticator.directory.load("alpha")
    assert loaded is not None and loaded.contract is not None
    request = ResponseRequest.model_validate(
        {
            "provider_call_id": "gone",
            "operation": "demo",
            "model": {"provider": "openai", "model": "gpt-6-luna"},
            "input": "hi",
            "stream": True,
        }
    )
    plan = await streaming.start_stream(
        calls.gateway.app.state.call_deps,
        project_id="alpha",
        environment="production",
        contract=loaded.contract,
        request=request,
        request_bytes=100,
    )
    body = streaming.sse_body(plan)
    await body.__anext__()  # the client reads one event, then disappears
    await body.aclose()
    await anyio.sleep(0.5)

    assert ("gone", "completed") in [row[:2] for row in await calls.ledger()]
    replay = await calls.post(
        "production",
        provider_call_id="gone",
        model={"provider": "openai", "model": "gpt-6-luna"},
        input="hi",
        operation="demo",
    )
    assert replay.json()["replayed"] is True
    assert replay.json()["output_text"] == "Hello there."


@pytest.mark.db
async def test_quiet_streams_send_keep_alives(
    calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(streaming, "KEEP_ALIVE_SECONDS", 0.05)
    calls.gateway.app.state.call_deps.adapters["openai"] = FakeOpenAI(_luna_success(), delay=0.2)

    response = await calls.post(
        "production", stream=True, model={"provider": "openai", "model": "gpt-6-luna"}
    )

    assert ": keep-alive" in response.text
    assert _events(response.text)[-1][0] == "response.completed"


def test_sse_frames() -> None:
    assert (
        streaming.sse("text.delta", {"delta": "hi"})
        == b'event: text.delta\ndata: {"delta":"hi"}\n\n'
    )
