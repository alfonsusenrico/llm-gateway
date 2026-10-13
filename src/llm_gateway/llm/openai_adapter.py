"""OpenAI Responses API adapter.

Facts from the provider review (official docs, 2026-10-13): `store` defaults to
true, so it is always sent as false; there is no idempotency key, so the call ID
travels as `X-Client-Request-Id` for tracing only; the SDK retries on its own by
default, so retries are off and the gateway's failure policy decides.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx2
import openai
from openai import AsyncOpenAI
from openai.types.responses import Response

from llm_gateway.catalogue.pricing import TokenUsage
from llm_gateway.llm.providers import ProviderFailure, ProviderSuccess
from llm_gateway.llm.request import FilePart, ImagePart, Message, ResponseRequest, TextPart
from llm_gateway.llm.resolve import ResolvedCall

# Billing and spend refusals: retrying never restores access.
_ACCOUNT_CODES = frozenset(
    {
        "insufficient_quota",
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    }
)
# Failures before any byte of the request reached OpenAI.
_NOT_SENT = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)
_CONNECT_TIMEOUT_SECONDS = 10.0
_MAX_CODE_CHARS = 64
logger = logging.getLogger(__name__)


def create_openai_client(
    api_key: str, timeout_seconds: int, http_client: httpx2.AsyncClient | None = None
) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=api_key,
        max_retries=0,
        timeout=httpx2.Timeout(timeout_seconds, connect=_CONNECT_TIMEOUT_SECONDS),
        http_client=http_client,
    )


def _part(part: TextPart | ImagePart | FilePart) -> dict[str, Any]:
    if isinstance(part, TextPart):
        return {"type": "input_text", "text": part.text}
    if isinstance(part, ImagePart):
        item: dict[str, Any] = {
            "type": "input_image",
            "image_url": f"data:{part.media_type};base64,{part.data_base64}",
        }
        if part.detail is not None:
            item["detail"] = part.detail
        return item
    return {
        "type": "input_file",
        "file_data": f"data:{part.media_type};base64,{part.data_base64}",
        "filename": part.filename,
    }


def _message(message: Message) -> dict[str, Any]:
    if isinstance(message.content, str):
        return {"role": message.role, "content": message.content}
    return {"role": message.role, "content": [_part(part) for part in message.content]}


def build_request(resolved: ResolvedCall, request: ResponseRequest) -> dict[str, Any]:
    """The Responses API body for this call; every field comes from validated input."""
    body: dict[str, Any] = {
        "model": resolved.model,
        "input": (
            request.input
            if isinstance(request.input, str)
            else [_message(message) for message in request.input]
        ),
        "store": False,
        "max_output_tokens": resolved.max_output_tokens,
        "prompt_cache_key": resolved.prompt_cache_key,
    }
    if request.instructions is not None:
        body["instructions"] = request.instructions
    if resolved.reasoning_effort is not None:
        body["reasoning"] = {"effort": resolved.reasoning_effort}
    if resolved.temperature is not None:
        body["temperature"] = resolved.temperature
    text_format = request.text_format
    if text_format.type == "json_schema":
        body["text"] = {
            "format": {
                "type": "json_schema",
                "name": text_format.name,
                "schema": text_format.json_schema,
                "strict": True,
            }
        }
    return body


def _safe_code(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:_MAX_CODE_CHARS]


def _retry_after(response: httpx2.Response | None) -> int | None:
    if response is None:
        return None
    raw = response.headers.get("retry-after", "")
    try:
        return max(0, round(float(raw)))
    except ValueError:
        return None


def classify_status_error(exc: openai.APIStatusError) -> ProviderFailure:
    status = exc.status_code
    code = _safe_code(exc.code) or _safe_code(exc.type)
    request_id = exc.request_id
    retry_after = _retry_after(exc.response)
    is_account = exc.code in _ACCOUNT_CODES or exc.type == "insufficient_quota"
    if status in (429, 403) and is_account:
        return ProviderFailure("account_blocked", code, None, None, request_id)
    if status in (401, 403):
        return ProviderFailure("authentication", code, None, None, request_id)
    if status == 429:
        return ProviderFailure("temporary_throttle", code, retry_after, None, request_id)
    if status in (409, 500, 502, 503):
        # A conflict or server error answered before completion: safe to resubmit.
        return ProviderFailure("temporary_overload", code, retry_after, None, request_id)
    if 400 <= status < 500 and status != 408:
        return ProviderFailure("invalid_request", code, None, None, request_id)
    # 408, 504, and anything else: the request may have run.
    return ProviderFailure("execution_unknown", code, None, None, request_id)


def classify_connection_error(exc: openai.APIConnectionError) -> ProviderFailure:
    cause: BaseException | None = exc.__cause__
    while cause is not None and not isinstance(cause, httpx2.TransportError):
        cause = cause.__cause__
    if isinstance(cause, _NOT_SENT):
        return ProviderFailure("connection_not_established", type(cause).__name__)
    return ProviderFailure(
        "execution_unknown", type(cause).__name__ if cause else type(exc).__name__
    )


def _usage(usage: Any) -> tuple[TokenUsage, int | None]:
    if usage is None:
        return TokenUsage(0, 0), None
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    token_usage = TokenUsage(
        input_tokens=int(usage.input_tokens or 0),
        output_tokens=int(usage.output_tokens or 0),
        cached_input_tokens=int(getattr(input_details, "cached_tokens", 0) or 0),
        cache_write_tokens=int(getattr(input_details, "cache_write_tokens", 0) or 0),
    )
    reasoning = getattr(output_details, "reasoning_tokens", None)
    return token_usage, int(reasoning) if reasoning is not None else None


def _texts(response: Any) -> tuple[str, str | None]:
    texts: list[str] = []
    refusals: list[str] = []
    for item in response.output or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", None) or []:
            kind = getattr(part, "type", None)
            if kind == "output_text":
                texts.append(part.text)
            elif kind == "refusal":
                refusals.append(part.refusal)
    return "".join(texts), ("\n".join(refusals) if refusals else None)


class OpenAIAdapter:
    def __init__(self, client: AsyncOpenAI) -> None:
        self._client = client

    async def execute(
        self, resolved: ResolvedCall, request: ResponseRequest
    ) -> ProviderSuccess | ProviderFailure:
        try:
            raw = await self._client.responses.with_raw_response.create(
                **build_request(resolved, request),
                extra_headers={"X-Client-Request-Id": request.provider_call_id},
            )
            response = raw.parse()
        except openai.APIStatusError as exc:
            return classify_status_error(exc)
        except openai.APIConnectionError as exc:
            return classify_connection_error(exc)
        except openai.APIResponseValidationError:
            return ProviderFailure("execution_unknown", "invalid_provider_response")
        request_id = raw.headers.get("x-request-id")
        if not isinstance(response, Response):
            # A non-JSON success (for example, a proxy page): the call may have run.
            return ProviderFailure(
                "execution_unknown", "invalid_provider_response", None, None, request_id
            )
        usage, reasoning = _usage(response.usage)
        if response.status == "failed":
            error = response.error
            return ProviderFailure(
                "provider_failed",
                _safe_code(getattr(error, "code", None)),
                None,
                usage if response.usage is not None else None,
                request_id,
            )
        if response.status not in ("completed", "incomplete"):
            # queued, in_progress, cancelled: not a finished answer from a direct call.
            return ProviderFailure(
                "execution_unknown", _safe_code(response.status), None, None, request_id
            )
        output_text, refusal = _texts(response)
        details = response.incomplete_details
        return ProviderSuccess(
            status=response.status,
            output_text=output_text,
            refusal=refusal,
            incomplete_reason=getattr(details, "reason", None) if details else None,
            usage=usage,
            reasoning_tokens=reasoning,
            service_tier=response.service_tier,
            provider_response_id=response.id,
            provider_request_id=request_id,
        )
