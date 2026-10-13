"""The zero-cost stub provider: scripted answers and scripted failures, no network."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from llm_gateway.catalogue.pricing import TokenUsage
from llm_gateway.llm.providers import (
    FailureCategory,
    FunctionCall,
    ProviderFailure,
    ProviderSuccess,
    StreamEvent,
    StreamStarted,
    StreamText,
)
from llm_gateway.llm.request import (
    FunctionCallOutput,
    Message,
    ResponseRequest,
    StubToolCall,
    TextPart,
)
from llm_gateway.llm.resolve import ResolvedCall

_MAX_ECHO_CHARS = 2_000
_BYTES_PER_TOKEN = 4
_FAILURES: dict[str, FailureCategory] = {
    "rate_limited": "temporary_throttle",
    "overloaded": "temporary_overload",
    "connection_failed": "connection_not_established",
    "timeout_after_send": "execution_unknown",
    "invalid_request": "invalid_request",
}


def _last_user_text(request: ResponseRequest) -> str:
    if isinstance(request.input, str):
        return request.input
    for message in reversed(request.messages()):
        if message.role == "user":
            return _message_text(message)
    return ""


def _function_outputs(request: ResponseRequest) -> list[str]:
    if isinstance(request.input, str):
        return []
    return [item.output for item in request.input if isinstance(item, FunctionCallOutput)]


def _scripted_calls(
    calls: list[StubToolCall], answered: int
) -> tuple[tuple[FunctionCall, ...], tuple[dict[str, Any], ...]]:
    """Scripted calls, with placeholder items shaped like a provider's output.

    Call IDs continue after the outputs already answered, so they stay unique in a chain.
    """
    function_calls = tuple(
        FunctionCall(
            f"stub_call_{index}",
            call.name,
            call.arguments if isinstance(call.arguments, str) else json.dumps(call.arguments),
        )
        for index, call in enumerate(calls, start=answered + 1)
    )
    items: list[dict[str, Any]] = [{"type": "reasoning", "id": "stub_rs_1", "summary": []}]
    items += [
        {
            "type": "function_call",
            "id": f"stub_fc_{index}",
            "call_id": call.call_id,
            "name": call.name,
            "arguments": call.arguments,
            "status": "completed",
        }
        for index, call in enumerate(function_calls, start=1)
    ]
    return function_calls, tuple(items)


def _message_text(message: Message) -> str:
    if isinstance(message.content, str):
        return message.content
    return " ".join(part.text for part in message.content if isinstance(part, TextPart))


class StubAdapter:
    async def execute(
        self, resolved: ResolvedCall, request: ResponseRequest
    ) -> ProviderSuccess | ProviderFailure:
        script = request.stub
        scenario = script.scenario if script else None
        if scenario in _FAILURES:
            return ProviderFailure(_FAILURES[scenario], f"stub_{scenario}", 1)
        wants_json = request.text_format.type == "json_schema"
        reply = script.reply if script else None
        outputs = _function_outputs(request)
        function_calls: tuple[FunctionCall, ...] = ()
        items: tuple[dict[str, Any], ...] = ()
        if script and script.tool_calls:
            function_calls, items = _scripted_calls(script.tool_calls, len(outputs))
            text = ""
        elif scenario == "invalid_json":
            text = "this is not json"
        elif wants_json:
            text = json.dumps(reply if isinstance(reply, dict) else {})
        elif isinstance(reply, str):
            text = reply
        elif outputs:
            text = f"stub: {outputs[-1]}"[:_MAX_ECHO_CHARS]
        else:
            text = f"stub: {_last_user_text(request)}"[:_MAX_ECHO_CHARS]
        request_bytes = len(request.model_dump_json(exclude_none=True))
        return ProviderSuccess(
            status="incomplete" if scenario == "incomplete" else "completed",
            output_text=text,
            refusal=None,
            incomplete_reason="max_output_tokens" if scenario == "incomplete" else None,
            usage=TokenUsage(
                input_tokens=max(1, request_bytes // _BYTES_PER_TOKEN),
                output_tokens=max(1, len(text) // _BYTES_PER_TOKEN),
            ),
            reasoning_tokens=None,
            service_tier="standard",
            provider_response_id=f"stub-{request.provider_call_id}",
            provider_request_id=None,
            function_calls=function_calls,
            output_items=items,
        )

    async def execute_stream(
        self, resolved: ResolvedCall, request: ResponseRequest
    ) -> AsyncIterator[StreamEvent]:
        """The same answers word by word; `fail_mid_stream` breaks after two words."""
        result = await self.execute(resolved, request)
        if isinstance(result, ProviderFailure):
            yield result  # every scripted failure happens before acceptance
            return
        yield StreamStarted()
        for call in result.function_calls:
            yield call
        words = result.output_text.split(" ") if result.output_text else []
        failing = request.stub is not None and request.stub.scenario == "fail_mid_stream"
        for index, word in enumerate(words):
            if failing and index == 2:
                yield ProviderFailure("execution_unknown", "stub_fail_mid_stream")
                return
            yield StreamText(word if index == 0 else f" {word}")
        yield result
