from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from llm_gateway.catalogue.model import get_catalogue
from llm_gateway.llm.openai_adapter import OpenAIAdapter, create_openai_client
from llm_gateway.llm.providers import ProviderFailure, ProviderSuccess
from llm_gateway.llm.request import ResponseRequest
from llm_gateway.llm.resolve import ResolvedCall

pytestmark = pytest.mark.anyio
LUNA = get_catalogue().model("openai-gpt-6-luna")
assert LUNA is not None
RESOLVED = ResolvedCall(
    entry=LUNA,
    reasoning_effort="low",
    temperature=None,
    max_output_tokens=500,
    prompt_cache_key="alpha:summarise",
)
SECRET_PROMPT = "the-confidential-prompt"


def _request(**overrides: Any) -> ResponseRequest:
    data: dict[str, Any] = {
        "provider_call_id": "call-1",
        "operation": "summarise",
        "model": "openai-gpt-6-luna",
        "input": SECRET_PROMPT,
    }
    data.update(overrides)
    return ResponseRequest.model_validate(data)


def _success_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "resp_123",
        "object": "response",
        "created_at": 1_760_000_000,
        "model": "gpt-6-luna",
        "status": "completed",
        "service_tier": "default",
        "output": [
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Hello there.", "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 2000,
            "input_tokens_details": {"cached_tokens": 1024, "cache_write_tokens": 0},
            "output_tokens": 500,
            "output_tokens_details": {"reasoning_tokens": 120},
            "total_tokens": 2500,
        },
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }
    body.update(overrides)
    return body


def _adapter(handler: Callable[[httpx2.Request], httpx2.Response]) -> OpenAIAdapter:
    client = create_openai_client(
        "sk-test-not-real",
        30,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return OpenAIAdapter(client)


def _answer(status: int, body: Any, headers: dict[str, str] | None = None):
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status, json=body, headers={"x-request-id": "req_9", **(headers or {})}
        )

    return handler


async def test_request_body_and_headers() -> None:
    captured: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(request)
        return httpx2.Response(200, json=_success_body(), headers={"x-request-id": "req_9"})

    request = _request(
        instructions="Be brief.",
        text_format={
            "type": "json_schema",
            "name": "summary",
            "schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "strict": True,
        },
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Describe"},
                    {
                        "type": "input_image",
                        "media_type": "image/png",
                        "data_base64": "aGVsbG8=",
                        "detail": "low",
                    },
                    {
                        "type": "input_file",
                        "media_type": "application/pdf",
                        "data_base64": "aGVsbG8=",
                        "filename": "a.pdf",
                    },
                ],
            }
        ],
    )

    await _adapter(handler).execute(RESOLVED, request)

    sent = json.loads(captured[0].content)
    assert captured[0].url.path == "/v1/responses"
    assert captured[0].headers["X-Client-Request-Id"] == "call-1"
    assert sent["store"] is False
    assert sent["model"] == "gpt-6-luna"
    assert sent["max_output_tokens"] == 500
    assert sent["prompt_cache_key"] == "alpha:summarise"
    assert sent["reasoning"] == {"effort": "low"}
    assert sent["instructions"] == "Be brief."
    assert "temperature" not in sent
    assert sent["text"]["format"]["type"] == "json_schema"
    assert sent["text"]["format"]["strict"] is True
    assert sent["input"][0]["content"] == [
        {"type": "input_text", "text": "Describe"},
        {"type": "input_image", "image_url": "data:image/png;base64,aGVsbG8=", "detail": "low"},
        {
            "type": "input_file",
            "file_data": "data:application/pdf;base64,aGVsbG8=",
            "filename": "a.pdf",
        },
    ]


async def test_success_extracts_text_usage_tier_and_ids() -> None:
    result = await _adapter(_answer(200, _success_body())).execute(RESOLVED, _request())

    assert isinstance(result, ProviderSuccess)
    assert (result.status, result.output_text, result.refusal) == (
        "completed",
        "Hello there.",
        None,
    )
    assert result.usage.input_tokens == 2000
    assert result.usage.cached_input_tokens == 1024
    assert result.usage.output_tokens == 500
    assert result.reasoning_tokens == 120
    assert (result.service_tier, result.provider_response_id) == ("default", "resp_123")
    assert result.provider_request_id == "req_9"


async def test_refusal_and_incomplete_are_reported() -> None:
    body = _success_body(
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
        output=[
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "incomplete",
                "content": [{"type": "refusal", "refusal": "I can't help with that."}],
            }
        ],
    )

    result = await _adapter(_answer(200, body)).execute(RESOLVED, _request())

    assert isinstance(result, ProviderSuccess)
    assert (result.status, result.incomplete_reason) == ("incomplete", "max_output_tokens")
    assert result.refusal == "I can't help with that."


def _error(code: str | None, kind: str = "invalid_request_error") -> dict[str, Any]:
    return {
        "error": {
            "message": f"Bad thing about {SECRET_PROMPT}",
            "type": kind,
            "code": code,
            "param": None,
        }
    }


@pytest.mark.parametrize(
    ("status", "body", "headers", "category", "code", "retry_after"),
    [
        (
            429,
            _error("slow_down", "rate_limit_error"),
            {"retry-after": "7"},
            "temporary_throttle",
            "slow_down",
            7,
        ),
        (
            429,
            _error("credit_balance_exhausted", "insufficient_quota"),
            {},
            "account_blocked",
            "credit_balance_exhausted",
            None,
        ),
        (
            429,
            _error("project_spend_limit_exceeded"),
            {},
            "account_blocked",
            "project_spend_limit_exceeded",
            None,
        ),
        (401, _error("invalid_api_key"), {}, "authentication", "invalid_api_key", None),
        (403, _error(None, "permission_error"), {}, "authentication", "permission_error", None),
        (400, _error("invalid_value"), {}, "invalid_request", "invalid_value", None),
        (404, _error("model_not_found"), {}, "invalid_request", "model_not_found", None),
        (
            503,
            _error("server_is_overloaded", "service_unavailable_error"),
            {"retry-after": "3"},
            "temporary_overload",
            "server_is_overloaded",
            3,
        ),
        (500, _error(None, "server_error"), {}, "temporary_overload", "server_error", None),
        (504, _error(None, "server_error"), {}, "execution_unknown", "server_error", None),
    ],
)
async def test_status_errors_are_classified(
    status: int,
    body: dict[str, Any],
    headers: dict[str, str],
    category: str,
    code: str,
    retry_after: int | None,
) -> None:
    result = await _adapter(_answer(status, body, headers)).execute(RESOLVED, _request())

    assert isinstance(result, ProviderFailure)
    assert (result.category, result.provider_code, result.retry_after_seconds) == (
        category,
        code,
        retry_after,
    )


async def test_connect_error_means_not_sent() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    result = await _adapter(handler).execute(RESOLVED, _request())

    assert isinstance(result, ProviderFailure)
    assert result.category == "connection_not_established"


async def test_read_timeout_means_execution_unknown() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    result = await _adapter(handler).execute(RESOLVED, _request())

    assert isinstance(result, ProviderFailure)
    assert result.category == "execution_unknown"


async def test_failed_status_is_provider_failed_with_usage() -> None:
    body = _success_body(
        status="failed", error={"code": "server_error", "message": "boom"}, output=[]
    )

    result = await _adapter(_answer(200, body)).execute(RESOLVED, _request())

    assert isinstance(result, ProviderFailure)
    assert (result.category, result.provider_code) == ("provider_failed", "server_error")
    assert result.usage is not None and result.usage.input_tokens == 2000


async def test_unparseable_success_body_is_execution_unknown() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, content=b"<html>proxy error</html>", headers={"content-type": "text/html"}
        )

    result = await _adapter(handler).execute(RESOLVED, _request())

    assert isinstance(result, ProviderFailure)
    assert result.category == "execution_unknown"


async def test_provider_messages_never_reach_logs(caplog: pytest.LogCaptureFixture) -> None:
    from llm_gateway.logging_setup import limit_library_logging

    limit_library_logging()
    with caplog.at_level(logging.DEBUG):
        await _adapter(_answer(400, _error("invalid_value"))).execute(RESOLVED, _request())
        await _adapter(_answer(200, _success_body())).execute(RESOLVED, _request())

    assert SECRET_PROMPT not in caplog.text
    assert "sk-test-not-real" not in caplog.text
