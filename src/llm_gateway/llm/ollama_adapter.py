"""Native, GPU-enforced Ollama; no status-only retries or provider message leakage."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator
from typing import Any, cast

import httpx2

from llm_gateway.catalogue.pricing import TokenUsage
from llm_gateway.llm.local_rules import native_messages
from llm_gateway.llm.providers import (
    ProviderFailure,
    ProviderSuccess,
    StreamEvent,
    StreamStarted,
    StreamText,
)
from llm_gateway.llm.request import ResponseRequest
from llm_gateway.llm.resolve import ResolvedCall

_MAX_RESPONSE_BYTES = 1_000_000
_CONFIRMED_REFUSALS = frozenset({"service_busy", "gpu_unavailable", "gpu_model_load_failed"})
_NOT_SENT = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)


async def _json(response: httpx2.Response) -> dict[str, Any]:
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data.extend(chunk)
        if len(data) > _MAX_RESPONSE_BYTES:
            raise ValueError("response_limit")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("response_shape")
    return value


def _refusal(status: int, body: dict[str, Any]) -> ProviderFailure:
    error = body.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if status == 503 and code in _CONFIRMED_REFUSALS:
        return ProviderFailure("temporary_overload", str(code))
    if status == 422 and code in {
        "hardware_options_prohibited",
        "request_limits_exceeded",
        "request_invalid",
    }:
        return ProviderFailure("invalid_request", "local_request_rejected")
    # In particular, gpu_execution_unverified can be emitted after completion.
    return ProviderFailure("execution_unknown", "local_response_failed")


def _success(
    body: dict[str, Any], resolved: ResolvedCall, output: str | None = None
) -> ProviderSuccess:
    message = body.get("message")
    if (
        body.get("model") != resolved.model
        or body.get("done") is not True
        or body.get("done_reason") not in {"stop", "length"}
        or not isinstance(message, dict)
        or message.get("role") != "assistant"
        or not isinstance(message.get("content"), str)
        or message.get("tool_calls")
        or message.get("thinking")
    ):
        raise ValueError("terminal_shape")
    message["content"].encode("utf-8")
    counts = [body.get("prompt_eval_count"), body.get("eval_count")]
    if any(type(value) is not int or value < 0 for value in counts):
        raise ValueError("usage_shape")
    incomplete = body["done_reason"] == "length"
    return ProviderSuccess(
        status="incomplete" if incomplete else "completed",
        output_text=message["content"] if output is None else output,
        refusal=None,
        incomplete_reason="max_output_tokens" if incomplete else None,
        usage=TokenUsage(cast(int, counts[0]), cast(int, counts[1])),
        reasoning_tokens=None,
        service_tier="standard",
        provider_response_id=None,
        provider_request_id=None,
    )


class OllamaAdapter:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        transport: httpx2.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def _client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=httpx2.Timeout(self._timeout_seconds, connect=5),
            follow_redirects=False,
            trust_env=False,
        )

    async def _metadata(
        self, client: httpx2.AsyncClient, resolved: ResolvedCall
    ) -> ProviderFailure | None:
        local = resolved.entry.local
        if local is None:
            return ProviderFailure("configuration_mismatch", "local_contract_missing")
        values = []
        for method, path, payload in (
            ("GET", "/health/ready", None),
            ("GET", "/api/version", None),
            ("GET", "/api/tags", None),
            ("POST", "/api/show", {"model": resolved.model}),
        ):
            async with client.stream(method, path, json=payload) as response:
                if response.status_code != 200:
                    if 300 <= response.status_code < 400:
                        return ProviderFailure("configuration_mismatch", "local_redirect")
                    return ProviderFailure("temporary_overload", "local_metadata_unavailable")
                values.append(await _json(response))
        ready, version, tags, details = values
        models = tags.get("models")
        if not isinstance(models, list):
            return ProviderFailure("configuration_mismatch", "local_identity_mismatch")
        matches = [
            model
            for model in models
            if isinstance(model, dict) and model.get("name") == resolved.model
        ]
        template, system = details.get("template"), details.get("system", "")
        if (
            ready.get("status") != "ready"
            or ready.get("execution") != "gpu_only"
            or version.get("version") != local.runtime_version
            or len(matches) != 1
            or matches[0].get("digest") != local.artifact_digest
            or not isinstance(template, str)
            or not isinstance(system, str)
            or hashlib.sha256(template.encode()).hexdigest() != local.template_digest
            or hashlib.sha256(system.encode()).hexdigest() != local.system_digest
        ):
            return ProviderFailure("configuration_mismatch", "local_identity_mismatch")
        return None

    @staticmethod
    def _body(resolved: ResolvedCall, request: ResponseRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": resolved.model,
            "messages": native_messages(request),
            "stream": stream,
            "options": {
                "num_ctx": resolved.entry.context_window_tokens,
                "num_predict": resolved.max_output_tokens,
                "temperature": resolved.temperature,
            },
        }
        if request.text_format.type == "json_schema":
            body["format"] = request.text_format.json_schema
        return body

    async def execute(
        self, resolved: ResolvedCall, request: ResponseRequest
    ) -> ProviderSuccess | ProviderFailure:
        sent = False
        try:
            async with asyncio.timeout(self._timeout_seconds), self._client() as client:
                failure = await self._metadata(client, resolved)
                if failure is not None:
                    return failure
                body = self._body(resolved, request, stream=False)
                sent = True
                async with client.stream("POST", "/api/chat", json=body) as response:
                    result = await _json(response)
                    if response.status_code != 200:
                        return _refusal(response.status_code, result)
                    return _success(result, resolved)
        except _NOT_SENT:
            return ProviderFailure("connection_not_established", "local_connection_failed")
        except (httpx2.HTTPError, TimeoutError, ValueError, TypeError, RecursionError):
            return ProviderFailure(
                "execution_unknown" if sent else "temporary_overload", "local_response_failed"
            )

    async def execute_stream(
        self, resolved: ResolvedCall, request: ResponseRequest
    ) -> AsyncGenerator[StreamEvent]:
        sent = False
        response = None
        deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        try:
            async with self._client() as client:
                failure = await asyncio.wait_for(
                    self._metadata(client, resolved), self._timeout_seconds
                )
                if failure is not None:
                    yield failure
                    return
                body = self._body(resolved, request, stream=True)
                sent = True
                outgoing = client.build_request("POST", "/api/chat", json=body)
                response = await asyncio.wait_for(
                    client.send(outgoing, stream=True),
                    max(0, deadline - asyncio.get_running_loop().time()),
                )
                try:
                    if response.status_code != 200:
                        error = await asyncio.wait_for(
                            _json(response), max(0, deadline - asyncio.get_running_loop().time())
                        )
                        yield _refusal(response.status_code, error)
                        return
                    yield StreamStarted()
                    pending = bytearray()
                    used = 0
                    parts: list[str] = []
                    chunks = response.aiter_bytes()
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                anext(chunks), max(0, deadline - asyncio.get_running_loop().time())
                            )
                        except StopAsyncIteration:
                            break
                        used += len(chunk)
                        if used > _MAX_RESPONSE_BYTES:
                            raise ValueError("response_limit")
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, remainder = pending.partition(b"\n")
                            pending = bytearray(remainder)
                            if not line.strip():
                                continue
                            event = json.loads(line)
                            if not isinstance(event, dict) or "error" in event:
                                raise ValueError("stream_error")
                            message = event.get("message")
                            if (
                                event.get("model") != resolved.model
                                or type(event.get("done")) is not bool
                                or not isinstance(message, dict)
                                or message.get("role") != "assistant"
                                or not isinstance(message.get("content"), str)
                                or message.get("tool_calls")
                                or message.get("thinking")
                            ):
                                raise ValueError("stream_shape")
                            text = message["content"]
                            text.encode("utf-8")
                            if text:
                                parts.append(text)
                                yield StreamText(text)
                            if event["done"]:
                                yield _success(event, resolved, "".join(parts))
                                return
                    # Native streams require newline-delimited terminal events.
                    yield ProviderFailure("execution_unknown", "local_stream_ended")
                finally:
                    await response.aclose()
        except _NOT_SENT:
            yield ProviderFailure("connection_not_established", "local_connection_failed")
        except (httpx2.HTTPError, TimeoutError, ValueError, TypeError, RecursionError):
            yield ProviderFailure(
                "execution_unknown" if sent else "temporary_overload", "local_stream_failed"
            )
