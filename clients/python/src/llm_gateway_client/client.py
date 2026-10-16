"""Async and sync clients. Both resubmit the same call ID only when that is safe."""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx2

from llm_gateway_client import __version__
from llm_gateway_client.errors import GatewayError, RetryableError, error_from_problem
from llm_gateway_client.models import (
    Allocation,
    CallResult,
    Completed,
    Identity,
    ModelDetails,
    ModelSummary,
    RefusalDelta,
    Started,
    StreamEvent,
    TextDelta,
    ToolCall,
)

_TRANSPORT_RETRY_SECONDS = 1.0
_DEFAULT_TIMEOUT = httpx2.Timeout(240.0, connect=10.0)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How long one call may keep retrying before the error goes to the caller."""

    max_attempts: int = 4
    max_total_seconds: float = 90.0
    jitter: float = 0.2


def _call_body(
    provider_call_id: str, operation: str, input: Any, extra: dict[str, Any]
) -> dict[str, Any]:
    body = {"provider_call_id": provider_call_id, "operation": operation, "input": input}
    body.update({key: value for key, value in extra.items() if value is not None})
    return body


def _retry_after(response: httpx2.Response) -> float | None:
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


def _error(response: httpx2.Response) -> GatewayError:
    try:
        problem = response.json()
    except ValueError:
        problem = None
    if not isinstance(problem, dict) or "code" not in problem:
        if response.status_code >= 500:
            # No problem body: something between client and gateway failed.
            return RetryableError(
                "gateway_unreachable",
                status=response.status_code,
                retry_after_seconds=_TRANSPORT_RETRY_SECONDS,
            )
        return GatewayError("unexpected_response", status=response.status_code)
    return error_from_problem(response.status_code, problem, _retry_after(response))


def _concurrency_limit(response: httpx2.Response) -> int | None:
    try:
        return max(1, int(response.headers["X-Concurrency-Limit"]))
    except (KeyError, ValueError):
        return None


def _next_wait(
    error: GatewayError, attempt: int, deadline: float, policy: RetryPolicy
) -> float | None:
    """Seconds to wait before resubmitting, or None when the error must be raised."""
    if not isinstance(error, RetryableError):
        return None
    wait = max(1.0, error.retry_after_seconds or 1.0) * (1 + random.random() * policy.jitter)  # noqa: S311  # jitter, not security
    if attempt >= policy.max_attempts or time.monotonic() + wait > deadline:
        return None
    return wait


class _AsyncLimiter:
    """Concurrency limit that follows the latest X-Concurrency-Limit from the gateway."""

    def __init__(self) -> None:
        self._limit = 1
        self._active = 0
        self._changed = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._changed:
            await self._changed.wait_for(lambda: self._active < self._limit)
            self._active += 1

    async def release(self, new_limit: int | None) -> None:
        async with self._changed:
            self._active -= 1
            if new_limit is not None:
                self._limit = new_limit
            self._changed.notify_all()

    @property
    def limit(self) -> int:
        return self._limit


async def _sse_events(response: httpx2.Response) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    name, data = "", ""
    async for line in response.aiter_lines():
        if not line:
            if name:
                yield name, json.loads(data or "{}")
            name, data = "", ""
        elif line.startswith(":"):
            continue  # keep-alive comment
        elif line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            data += line[len("data: ") :]


class _Responses:
    def __init__(self, client: GatewayClient) -> None:
        self._client = client

    async def stream(
        self,
        *,
        provider_call_id: str,
        operation: str,
        input: str | list[dict[str, Any]],
        **options: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a call. If the connection drops, the same call ID is resubmitted and
        the stream ends with the replayed `Completed`, whose text is authoritative."""
        body = _call_body(provider_call_id, operation, input, {**options, "stream": True})
        async for event in self._client._stream_call(body):
            yield event

    async def create(
        self,
        *,
        provider_call_id: str,
        operation: str,
        input: str | list[dict[str, Any]],
        profile: str | None = None,
        model: str | None = None,
        instructions: str | None = None,
        text_format: dict[str, Any] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        temperature: float | None = None,
        prompt_cache_key: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, str] | None = None,
        parallel_tool_calls: bool | None = None,
        stub: dict[str, Any] | None = None,
    ) -> CallResult:
        body = _call_body(
            provider_call_id,
            operation,
            input,
            {
                "profile": profile,
                "model": model,
                "instructions": instructions,
                "text_format": text_format,
                "max_output_tokens": max_output_tokens,
                "reasoning_effort": reasoning_effort,
                "temperature": temperature,
                "prompt_cache_key": prompt_cache_key,
                "tools": tools,
                "tool_choice": tool_choice,
                "parallel_tool_calls": parallel_tool_calls,
                "stub": stub,
            },
        )
        return CallResult.model_validate(await self._client._post_call(body))


class GatewayClient:
    """Async client. One instance per process; it sizes its concurrency to the grant."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        retry_policy: RetryPolicy | None = None,
        http_client: httpx2.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http_client or httpx2.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": f"llm-gateway-client/{__version__}",
        }
        self._policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._limiter = _AsyncLimiter()
        self.responses = _Responses(self)
        self.models = _Models(self)

    async def __aenter__(self) -> GatewayClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def concurrency_limit(self) -> int:
        return self._limiter.limit

    async def _post_call(self, body: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + self._policy.max_total_seconds
        attempt = 0
        while True:
            attempt += 1
            await self._limiter.acquire()
            new_limit = None
            try:
                response = await self._http.post(
                    f"{self._base_url}/v1/responses", json=body, headers=self._headers
                )
                new_limit = _concurrency_limit(response)
            except httpx2.TransportError:
                # The gateway may have finished the call; resubmitting the same ID
                # replays it or reports call_in_progress, never a second execution.
                error: GatewayError = RetryableError(
                    "gateway_unreachable", retry_after_seconds=_TRANSPORT_RETRY_SECONDS
                )
            else:
                if response.status_code == 200:
                    return response.json()
                error = _error(response)
            finally:
                await self._limiter.release(new_limit)
            wait = _next_wait(error, attempt, deadline, self._policy)
            if wait is None:
                raise error
            await self._sleep(wait)

    async def _stream_call(self, body: dict[str, Any]) -> AsyncIterator[StreamEvent]:
        deadline = time.monotonic() + self._policy.max_total_seconds
        attempt = 0
        started = False
        while True:
            attempt += 1
            await self._limiter.acquire()
            new_limit = None
            error: GatewayError
            try:
                async with self._http.stream(
                    "POST", f"{self._base_url}/v1/responses", json=body, headers=self._headers
                ) as response:
                    new_limit = _concurrency_limit(response)
                    if response.status_code != 200:
                        await response.aread()
                        error = _error(response)
                    else:
                        async for name, data in _sse_events(response):
                            if name == "response.started" and not started:
                                started = True
                                yield Started.model_validate(data)
                            elif name == "text.delta":
                                yield TextDelta.model_validate(data)
                            elif name == "refusal.delta":
                                yield RefusalDelta.model_validate(data)
                            elif name == "tool_call":
                                yield ToolCall.model_validate(data)
                            elif name == "response.completed":
                                yield Completed(result=CallResult.model_validate(data))
                                return
                            elif name == "error":
                                raise error_from_problem(
                                    int(data.get("status") or 502),
                                    data,
                                    data.get("retry_after_seconds"),
                                )
                        # The stream ended without its final event: resubmit the call ID.
                        error = RetryableError(
                            "gateway_unreachable", retry_after_seconds=_TRANSPORT_RETRY_SECONDS
                        )
            except httpx2.TransportError:
                error = RetryableError(
                    "gateway_unreachable", retry_after_seconds=_TRANSPORT_RETRY_SECONDS
                )
            finally:
                await self._limiter.release(new_limit)
            wait = _next_wait(error, attempt, deadline, self._policy)
            if wait is None:
                raise error
            await self._sleep(wait)

    async def _get(self, path: str) -> Any:
        response = await self._http.get(f"{self._base_url}{path}", headers=self._headers)
        if response.status_code != 200:
            raise _error(response)
        return response.json()

    async def allocation(self) -> Allocation:
        return Allocation.model_validate(await self._get("/v1/allocation"))

    async def whoami(self) -> Identity:
        return Identity.model_validate(await self._get("/v1/projects/self"))


class _SyncLimiter:
    def __init__(self) -> None:
        self._limit = 1
        self._active = 0
        self._changed = threading.Condition()

    def acquire(self) -> None:
        with self._changed:
            self._changed.wait_for(lambda: self._active < self._limit)
            self._active += 1

    def release(self, new_limit: int | None) -> None:
        with self._changed:
            self._active -= 1
            if new_limit is not None:
                self._limit = new_limit
            self._changed.notify_all()


class _SyncResponses:
    def __init__(self, client: SyncGatewayClient) -> None:
        self._client = client

    def create(
        self, *, provider_call_id: str, operation: str, input: Any, **options: Any
    ) -> CallResult:
        body = _call_body(provider_call_id, operation, input, options)
        return CallResult.model_validate(self._client._post_call(body))


class SyncGatewayClient:
    """Blocking client with the same retry and concurrency rules as `GatewayClient`."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        retry_policy: RetryPolicy | None = None,
        http_client: httpx2.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http_client or httpx2.Client(timeout=_DEFAULT_TIMEOUT)
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": f"llm-gateway-client/{__version__}",
        }
        self._policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._limiter = _SyncLimiter()
        self.responses = _SyncResponses(self)
        self.models = _SyncModels(self)

    def __enter__(self) -> SyncGatewayClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _post_call(self, body: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + self._policy.max_total_seconds
        attempt = 0
        while True:
            attempt += 1
            self._limiter.acquire()
            new_limit = None
            try:
                response = self._http.post(
                    f"{self._base_url}/v1/responses", json=body, headers=self._headers
                )
                new_limit = _concurrency_limit(response)
            except httpx2.TransportError:
                error: GatewayError = RetryableError(
                    "gateway_unreachable", retry_after_seconds=_TRANSPORT_RETRY_SECONDS
                )
            else:
                if response.status_code == 200:
                    return response.json()
                error = _error(response)
            finally:
                self._limiter.release(new_limit)
            wait = _next_wait(error, attempt, deadline, self._policy)
            if wait is None:
                raise error
            self._sleep(wait)

    def _get(self, path: str) -> Any:
        response = self._http.get(f"{self._base_url}{path}", headers=self._headers)
        if response.status_code != 200:
            raise _error(response)
        return response.json()

    def allocation(self) -> Allocation:
        return Allocation.model_validate(self._get("/v1/allocation"))

    def whoami(self) -> Identity:
        return Identity.model_validate(self._get("/v1/projects/self"))


class _Models:
    def __init__(self, client: GatewayClient) -> None:
        self._client = client

    async def list(self, *, task: str | None = None) -> list[ModelSummary]:
        from urllib.parse import urlencode

        path = "/v1/models" + ("?" + urlencode({"task": task}) if task else "")
        data = await self._client._get(path)
        return [ModelSummary.model_validate(item) for item in data["models"]]

    async def get(self, alias: str) -> ModelDetails:
        from urllib.parse import quote

        return ModelDetails.model_validate(
            await self._client._get("/v1/models/" + quote(alias, safe=""))
        )


class _SyncModels:
    def __init__(self, client: SyncGatewayClient) -> None:
        self._client = client

    def list(self, *, task: str | None = None) -> list[ModelSummary]:
        from urllib.parse import urlencode

        path = "/v1/models" + ("?" + urlencode({"task": task}) if task else "")
        return [ModelSummary.model_validate(item) for item in self._client._get(path)["models"]]

    def get(self, alias: str) -> ModelDetails:
        from urllib.parse import quote

        return ModelDetails.model_validate(self._client._get("/v1/models/" + quote(alias, safe="")))
