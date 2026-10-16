"""OpenAI Responses API adapter.

Facts from the provider review (official docs, 2026-10-13): `store` defaults to
true, so it is always sent as false; there is no idempotency key, so the call ID
travels as `X-Client-Request-Id` for tracing only; the SDK retries on its own by
default, so retries are off and the gateway's failure policy decides.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any

import httpx2
import openai
from openai import AsyncOpenAI
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseIncompleteEvent,
    ResponseOutputItemDoneEvent,
    ResponseRefusalDeltaEvent,
    ResponseTextDeltaEvent,
)
from pydantic import SecretStr

from llm_gateway.catalogue.pricing import TokenUsage
from llm_gateway.llm.providers import (
    FunctionCall,
    ProviderFailure,
    ProviderSuccess,
    StreamEvent,
    StreamRefusal,
    StreamStarted,
    StreamText,
)
from llm_gateway.llm.request import (
    Continuation,
    FilePart,
    FunctionCallOutput,
    ImagePart,
    InputItem,
    Message,
    NamedToolChoice,
    ResponseRequest,
    TextPart,
)
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


def _input_items(item: InputItem) -> list[dict[str, Any]]:
    if isinstance(item, Message):
        return [_message(item)]
    if isinstance(item, FunctionCallOutput):
        return [{"type": "function_call_output", "call_id": item.call_id, "output": item.output}]
    if isinstance(item, Continuation):
        # Reasoning, calls, and messages go back untouched, as the docs require
        # for tool loops without stored state.
        return list(item.items)
    raise TypeError(f"unexpected input item {type(item).__name__}")


def build_request(resolved: ResolvedCall, request: ResponseRequest) -> dict[str, Any]:
    """The Responses API body for this call; every field comes from validated input."""
    body: dict[str, Any] = {
        "model": resolved.model,
        "input": (
            request.input
            if isinstance(request.input, str)
            else [part for item in request.input for part in _input_items(item)]
        ),
        "store": False,
        "max_output_tokens": resolved.max_output_tokens,
    }
    if resolved.entry.prompt_cache:
        body["prompt_cache_key"] = resolved.prompt_cache_key
    if request.instructions is not None:
        body["instructions"] = request.instructions
    if resolved.reasoning_effort is not None:
        body["reasoning"] = {"effort": resolved.reasoning_effort}
    if resolved.temperature is not None:
        body["temperature"] = resolved.temperature
    if request.tools:
        body["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "parameters": tool.parameters,
                "strict": tool.strict,
                **({"description": tool.description} if tool.description is not None else {}),
            }
            for tool in request.tools
        ]
    if isinstance(request.tool_choice, NamedToolChoice):
        body["tool_choice"] = {"type": "function", "name": request.tool_choice.name}
    elif request.tool_choice is not None:
        body["tool_choice"] = request.tool_choice
    if request.parallel_tool_calls is not None:
        body["parallel_tool_calls"] = request.parallel_tool_calls
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
    allowed = _ACCOUNT_CODES | {
        "server_error",
        "server_is_overloaded",
        "rate_limit_exceeded",
        "invalid_api_key",
        "invalid_request_error",
        "invalid_parameter",
        "model_not_found",
        "slow_down",
        "permission_error",
        "invalid_value",
    }
    return value if isinstance(value, str) and value in allowed else None


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


def _function_calls(items: list[dict[str, Any]]) -> tuple[FunctionCall, ...]:
    return tuple(
        FunctionCall(str(item["call_id"]), str(item["name"]), str(item.get("arguments", "")))
        for item in items
        if item.get("type") == "function_call"
    )


def result_from_response(
    response: Response,
    request_id: str | None,
    streamed_items: list[dict[str, Any]] | None = None,
) -> ProviderSuccess | ProviderFailure:
    """The outcome of a finished Responses API object, streamed or not.

    A stream passes the items from `output_item.done`, whose reasoning content is
    complete; otherwise the response's own output list is used.
    """
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
        # queued, in_progress, cancelled: not a finished answer.
        return ProviderFailure(
            "execution_unknown", _safe_code(response.status), None, None, request_id
        )
    output_text, refusal = _texts(response)
    items = (
        streamed_items
        if streamed_items
        else [item.to_dict(mode="json") for item in response.output or []]
    )
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
        function_calls=_function_calls(items),
        output_items=tuple(items),
    )


def classify_stream_error(code: str | None, *, accepted: bool) -> ProviderFailure:
    """An `error` event: before acceptance it says why the call never ran."""
    safe = _safe_code(code)
    if accepted:
        return ProviderFailure("provider_failed", safe)
    if code in _ACCOUNT_CODES:
        return ProviderFailure("account_blocked", safe)
    if code in ("rate_limit_exceeded", "slow_down"):
        return ProviderFailure("temporary_throttle", safe)
    if code in ("server_error", "server_is_overloaded"):
        return ProviderFailure("temporary_overload", safe)
    return ProviderFailure("provider_failed", safe)


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
        return result_from_response(response, request_id)

    async def execute_stream(
        self, resolved: ResolvedCall, request: ResponseRequest
    ) -> AsyncGenerator[StreamEvent]:
        """Stream the call. Once OpenAI answered 200 the request was accepted, so any
        later loss is `execution_unknown` unless an explicit event says otherwise."""
        try:
            stream = await self._client.responses.create(
                **build_request(resolved, request),
                stream=True,
                stream_options={"include_obfuscation": False},
                extra_headers={"X-Client-Request-Id": request.provider_call_id},
            )
        except openai.APIStatusError as exc:
            yield classify_status_error(exc)
            return
        except openai.APIConnectionError as exc:
            yield classify_connection_error(exc)
            return
        request_id = stream.response.headers.get("x-request-id")
        accepted = False
        done_items: dict[int, dict[str, Any]] = {}
        try:
            async for event in stream:
                if isinstance(event, ResponseCreatedEvent):
                    accepted = True
                    yield StreamStarted(request_id)
                elif isinstance(event, ResponseOutputItemDoneEvent):
                    item = event.item.to_dict(mode="json")
                    done_items[event.output_index] = item
                    for call in _function_calls([item]):
                        yield call
                elif isinstance(event, ResponseTextDeltaEvent):
                    yield StreamText(event.delta)
                elif isinstance(event, ResponseRefusalDeltaEvent):
                    yield StreamRefusal(event.delta)
                elif isinstance(
                    event, ResponseCompletedEvent | ResponseIncompleteEvent | ResponseFailedEvent
                ):
                    items = [done_items[index] for index in sorted(done_items)]
                    yield result_from_response(event.response, request_id, items)
                    return
                elif isinstance(event, ResponseErrorEvent):
                    yield classify_stream_error(event.code, accepted=accepted)
                    return
        except openai.APIStatusError as exc:
            yield (
                classify_status_error(exc)
                if not accepted
                else ProviderFailure(
                    "execution_unknown", _safe_code(exc.code), None, None, request_id
                )
            )
            return
        except openai.APIConnectionError as exc:
            # Checked before APIError, its base class: a lost or timed-out stream.
            yield (
                ProviderFailure("execution_unknown", type(exc).__name__, None, None, request_id)
                if accepted
                else classify_connection_error(exc)
            )
            return
        except openai.APIError as exc:
            # The SDK raises for a payload with a top-level error object.
            yield classify_stream_error(getattr(exc, "code", None), accepted=accepted)
            return
        except Exception as exc:
            logger.warning("OpenAI stream interrupted: %s", type(exc).__name__)
            yield ProviderFailure("execution_unknown", type(exc).__name__, None, None, request_id)
            return
        finally:
            await stream.close()
        # The stream ended without a terminal event: the outcome is unknown.
        yield ProviderFailure("execution_unknown", "stream_ended", None, None, request_id)


class CredentialOpenAIAdapter:
    """A captured credential belongs to one execution, including a detached stream."""

    def __init__(self, secret: SecretStr, timeout_seconds: int) -> None:
        self._secret = secret
        self._timeout_seconds = timeout_seconds

    async def execute(
        self, resolved: ResolvedCall, request: ResponseRequest
    ) -> ProviderSuccess | ProviderFailure:
        async with create_openai_client(
            self._secret.get_secret_value(), self._timeout_seconds
        ) as client:
            return await OpenAIAdapter(client).execute(resolved, request)

    async def execute_stream(
        self, resolved: ResolvedCall, request: ResponseRequest
    ) -> AsyncGenerator[StreamEvent]:
        async with (
            create_openai_client(self._secret.get_secret_value(), self._timeout_seconds) as client,
            aclosing(OpenAIAdapter(client).execute_stream(resolved, request)) as events,
        ):
            async for event in events:
                yield event
