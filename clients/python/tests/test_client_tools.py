from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from llm_gateway_client import (
    CallResult,
    Completed,
    GatewayClient,
    ToolCall,
    continuation_of,
    function_output,
    tool,
)

pytestmark = pytest.mark.anyio
SCHEMA = {
    "type": "object",
    "properties": {"day": {"type": "string"}},
    "required": ["day"],
    "additionalProperties": False,
}
CALL = {
    "call_id": "call_1",
    "name": "get_events",
    "arguments": '{"day":"today"}',
    "arguments_json": {"day": "today"},
    "arguments_error": None,
}
CONTINUATION = {"provider": "stub", "model": "stub-echo", "items": [{"type": "function_call"}]}
RESULT = {
    "provider_call_id": "t-1",
    "status": "completed",
    "provider": "stub",
    "model": "stub-echo",
    "model_alias": "stub-echo",
    "definition_digest": "a" * 64,
    "output_text": "",
    "tool_calls": [CALL],
    "continuation": CONTINUATION,
    "usage": {"input_tokens": 3, "output_tokens": 1},
}


def test_tool_definition() -> None:
    assert tool("get_events", SCHEMA, description="Events for a day.") == {
        "type": "function",
        "name": "get_events",
        "parameters": SCHEMA,
        "strict": True,
        "description": "Events for a day.",
    }
    assert "description" not in tool("get_events", SCHEMA, strict=False)


def test_function_output_sends_text_or_json() -> None:
    assert function_output("call_1", "standup") == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "standup",
    }
    assert function_output("call_1", {"events": ["standup"]})["output"] == '{"events": ["standup"]}'


def test_continuation_round_trips_unchanged() -> None:
    result = CallResult.model_validate(RESULT)

    assert result.tool_calls == [ToolCall.model_validate(CALL)]
    assert continuation_of(result) == {"type": "continuation", **CONTINUATION}


def test_result_without_tool_calls_has_no_continuation() -> None:
    plain = CallResult.model_validate({**RESULT, "tool_calls": [], "continuation": None})

    with pytest.raises(ValueError, match="no continuation"):
        continuation_of(plain)


def test_results_from_older_gateways_still_parse() -> None:
    older = {k: v for k, v in RESULT.items() if k not in ("tool_calls", "continuation")}

    result = CallResult.model_validate(older)

    assert (result.tool_calls, result.continuation) == ([], None)


async def test_tool_options_are_sent_and_stream_tool_calls_are_typed() -> None:
    bodies: list[dict[str, Any]] = []
    sse = (
        f"event: tool_call\ndata: {json.dumps(CALL)}\n\n"
        f"event: response.completed\ndata: {json.dumps(RESULT)}\n\n"
    ).encode()

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("stream"):
            return httpx2.Response(200, content=sse, headers={"content-type": "text/event-stream"})
        return httpx2.Response(200, json=RESULT)

    gateway = GatewayClient(
        "http://gateway",
        "lgw.dev.key",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    await gateway.responses.create(
        provider_call_id="t-1",
        operation="demo",
        input="hi",
        profile="chat",
        tools=[tool("get_events", SCHEMA)],
        tool_choice="required",
        parallel_tool_calls=False,
    )
    events = [
        event
        async for event in gateway.responses.stream(
            provider_call_id="t-2",
            operation="demo",
            input="hi",
            profile="chat",
            tools=[tool("get_events", SCHEMA)],
        )
    ]

    assert bodies[0]["tools"][0]["name"] == "get_events"
    assert (bodies[0]["tool_choice"], bodies[0]["parallel_tool_calls"]) == ("required", False)
    assert events[0] == ToolCall.model_validate(CALL)
    assert isinstance(events[1], Completed) and events[1].result.tool_calls[0].call_id == "call_1"
