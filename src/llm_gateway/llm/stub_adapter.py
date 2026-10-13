"""The zero-cost stub provider: scripted answers and scripted failures, no network."""

from __future__ import annotations

import json

from llm_gateway.catalogue.pricing import TokenUsage
from llm_gateway.llm.providers import FailureCategory, ProviderFailure, ProviderSuccess
from llm_gateway.llm.request import Message, ResponseRequest, TextPart
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
    for message in reversed(request.input):
        if message.role == "user":
            return _message_text(message)
    return ""


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
        if scenario == "invalid_json":
            text = "this is not json"
        elif wants_json:
            text = json.dumps(reply if isinstance(reply, dict) else {})
        elif isinstance(reply, str):
            text = reply
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
        )
