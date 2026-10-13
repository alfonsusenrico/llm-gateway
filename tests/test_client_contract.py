"""The Python client against the real gateway application, in the same revision."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from llm_gateway_client import (
    Completed,
    ExecutionUnknownError,
    GatewayClient,
    InvalidRequestError,
    RetryableError,
    TextDelta,
    continuation_of,
    function_output,
    json_schema,
    tool,
)
from llm_gateway_client.errors import KNOWN_CODES
from psycopg_pool import AsyncConnectionPool

from llm_gateway.api.routes_allocation import GrantDirectory
from tests.test_auth_api import Gateway
from tests.test_responses_api import Calls

pytestmark = [pytest.mark.anyio, pytest.mark.db]
GATEWAY_SOURCE = Path(__file__).parents[1] / "src" / "llm_gateway"
# Codes the gateway can emit that the client deliberately treats as generic errors.
GENERIC_CODES = frozenset({"http_error"})
STUB = {"provider": "stub", "model": "stub-echo"}


class DropFirstResponse(httpx2.AsyncBaseTransport):
    """Lets the gateway finish the first call, then loses its response, like a dropped link."""

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self.inner = inner
        self.dropped = False

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self.inner.handle_async_request(request)
        if not self.dropped:
            self.dropped = True
            raise httpx2.ReadError("connection dropped after the gateway answered")
        return response


class _BreakAfterTwoEvents(httpx2.AsyncByteStream):
    def __init__(self, inner: httpx2.AsyncByteStream) -> None:
        self.inner = inner

    async def __aiter__(self) -> AsyncIterator[bytes]:
        body = b"".join([chunk async for chunk in self.inner])
        yield b"".join(frame + b"\n\n" for frame in body.split(b"\n\n")[:2])
        raise httpx2.ReadError("connection dropped mid-stream")


class DropFirstStream(httpx2.AsyncBaseTransport):
    """Delivers two events of the first stream, then loses the connection."""

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self.inner = inner
        self.dropped = False

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self.inner.handle_async_request(request)
        if self.dropped:
            return response
        self.dropped = True
        assert isinstance(response.stream, httpx2.AsyncByteStream)
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_BreakAfterTwoEvents(response.stream),
        )


async def _no_wait(_seconds: float) -> None:
    return None


def _client(calls: Calls, env: str = "development", transport: Any = None) -> GatewayClient:
    asgi = httpx2.ASGITransport(app=calls.gateway.app)
    return GatewayClient(
        "http://gateway",
        calls.keys[env].raw,
        http_client=httpx2.AsyncClient(transport=transport(asgi) if transport else asgi),
        sleep=_no_wait,
    )


@pytest.fixture
async def calls(migrated_pool: AsyncConnectionPool) -> AsyncIterator[Calls]:
    gateway = Gateway(migrated_pool)
    async with gateway.client:
        calls = Calls(gateway)
        await calls.setup()
        gateway.app.state.grant_directory = GrantDirectory(migrated_pool, ttl_seconds=0)
        yield calls


async def test_call_and_json_answer(calls: Calls) -> None:
    client = _client(calls)

    text = await client.responses.create(
        provider_call_id="c1", operation="demo", model=STUB, input="hello"
    )
    shaped = await client.responses.create(
        provider_call_id="c2",
        operation="demo",
        model=STUB,
        input="hello",
        text_format=json_schema("answer", {"type": "object"}),
        stub={"reply": {"answer": 42}},
    )

    assert text.output_text == "stub: hello"
    assert shaped.output_json == {"answer": 42}
    assert client.concurrency_limit == 2  # the development grant


async def test_lost_response_is_recovered_by_replay(calls: Calls) -> None:
    client = _client(calls, transport=DropFirstResponse)

    result = await client.responses.create(
        provider_call_id="lost", operation="demo", model=STUB, input="hello"
    )

    assert result.replayed is True
    assert [row[:2] for row in await calls.ledger()] == [("lost", "completed")]


async def test_streamed_call_through_the_app(calls: Calls) -> None:
    client = _client(calls)

    events = [
        event
        async for event in client.responses.stream(
            provider_call_id="s1", operation="demo", model=STUB, input="one two three"
        )
    ]

    assert "".join(e.delta for e in events if isinstance(e, TextDelta)) == "stub: one two three"
    assert isinstance(events[-1], Completed)
    assert events[-1].result.output_text == "stub: one two three"


async def test_streamed_error_event_raises_through_the_app(calls: Calls) -> None:
    client = _client(calls)

    with pytest.raises(ExecutionUnknownError):
        async for _event in client.responses.stream(
            provider_call_id="s2",
            operation="demo",
            model=STUB,
            input="one two three four",
            stub={"scenario": "fail_mid_stream"},
        ):
            pass


async def test_dropped_stream_is_recovered_by_replay(calls: Calls) -> None:
    client = _client(calls, transport=DropFirstStream)

    events = [
        event
        async for event in client.responses.stream(
            provider_call_id="s3", operation="demo", model=STUB, input="one two three"
        )
    ]

    assert [type(e).__name__ for e in events] == ["Started", "TextDelta", "Completed"]
    assert isinstance(events[-1], Completed)
    assert events[-1].result.replayed is True
    assert events[-1].result.output_text == "stub: one two three"
    assert [row[:2] for row in await calls.ledger()] == [("s3", "completed")]


async def test_two_turn_tool_loop_with_the_helpers(calls: Calls) -> None:
    client = _client(calls)
    events_tool = tool(
        "get_events",
        {
            "type": "object",
            "properties": {"day": {"type": "string"}},
            "required": ["day"],
            "additionalProperties": False,
        },
    )

    first = await client.responses.create(
        provider_call_id="loop-1",
        operation="demo",
        model=STUB,
        input="What is on today?",
        tools=[events_tool],
        stub={"tool_calls": [{"name": "get_events", "arguments": {"day": "today"}}]},
    )
    call = first.tool_calls[0]
    second = await client.responses.create(
        provider_call_id="loop-2",
        operation="demo",
        model=STUB,
        input=[continuation_of(first), function_output(call.call_id, ["standup"])],
        tools=[events_tool],
    )

    assert (call.name, call.arguments_json) == ("get_events", {"day": "today"})
    assert second.output_text == 'stub: ["standup"]'
    assert second.tool_calls == []


@pytest.mark.parametrize(
    ("scenario", "error"),
    [
        ("overloaded", RetryableError),
        ("rate_limited", RetryableError),
        ("connection_failed", RetryableError),
        ("timeout_after_send", ExecutionUnknownError),
        ("invalid_request", InvalidRequestError),
    ],
)
async def test_failures_raise_their_typed_errors(
    calls: Calls, scenario: str, error: type[Exception]
) -> None:
    client = _client(calls)

    with pytest.raises(error):
        await client.responses.create(
            provider_call_id=f"s-{scenario}",
            operation="demo",
            model=STUB,
            input="hello",
            stub={"scenario": scenario},
        )


async def test_unknown_execution_is_attempted_once(calls: Calls) -> None:
    client = _client(calls)

    with pytest.raises(ExecutionUnknownError):
        await client.responses.create(
            provider_call_id="once",
            operation="demo",
            model=STUB,
            input="hello",
            stub={"scenario": "timeout_after_send"},
        )

    async with calls.gateway.pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT count(*) FROM call_events e JOIN calls c USING (call_key) "
            "WHERE c.provider_call_id = 'once' AND e.to_state = 'admitted'"
        )
        assert await cursor.fetchone() == (1,)


async def test_production_key_and_stub_is_an_invalid_request(calls: Calls) -> None:
    client = _client(calls, env="production")

    with pytest.raises(InvalidRequestError) as raised:
        await client.responses.create(
            provider_call_id="p1", operation="demo", model=STUB, input="hello"
        )

    assert raised.value.code == "stub_not_allowed"


async def test_allocation_catalogue_and_identity(calls: Calls) -> None:
    client = _client(calls)

    allocation = await client.allocation()
    models = await client.catalogue()
    identity = await client.whoami()

    assert (allocation.project_id, allocation.environment) == ("alpha", "development")
    assert [m.model for m in models] == ["gpt-6-luna", "stub-echo"]
    assert (identity.project_id, identity.environment) == ("alpha", "development")


def _gateway_codes() -> set[str]:
    """Every problem code the gateway can emit: its code tables plus each raise site."""
    from llm_gateway.api.routes_responses import _ADMISSION_TITLES
    from llm_gateway.llm.errors import PROBLEM_STATUS

    codes = set(PROBLEM_STATUS) | set(_ADMISSION_TITLES)
    raise_site = re.compile(
        r'(?:GatewayError|AdmissionRefusedError|CallRejectedError)\(\s*"([a-z_]+)"|code="([a-z_]+)"'
    )
    for path in GATEWAY_SOURCE.rglob("*.py"):
        for match in raise_site.finditer(path.read_text(encoding="utf-8")):
            codes.add(match.group(1) or match.group(2))
    http_codes = re.search(
        r"codes = \{([^}]*)\}", (GATEWAY_SOURCE / "api" / "problems.py").read_text()
    )
    assert http_codes is not None
    codes.update(re.findall(r'"([a-z_]+)"', http_codes.group(1)))
    return codes


def test_every_gateway_problem_code_is_mapped_by_the_client() -> None:
    codes = _gateway_codes()

    assert {"execution_unknown", "budget_exhausted", "key_invalid", "call_in_progress"} <= codes
    unmapped = sorted(codes - KNOWN_CODES - GENERIC_CODES)
    assert unmapped == []
