"""Bounded native HTTP embedding adapters; no inference retries or provider prose."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

import httpx2
from pydantic import SecretStr

from llm_gateway.catalogue.pricing import TokenUsage
from llm_gateway.embeddings.models import EmbeddingRequest, EmbeddingVector, ResolvedEmbedding
from llm_gateway.llm.providers import ProviderFailure


@dataclass(frozen=True, slots=True)
class EmbeddingSuccess:
    vectors: list[EmbeddingVector]
    usage: TokenUsage


async def _json(response: httpx2.Response, limit: int = 8_000_000) -> dict[str, Any]:
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError("response_limit")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("response_shape")
    return result


def _success(
    body: dict[str, Any], resolved: ResolvedEmbedding, request: EmbeddingRequest, local: bool
) -> EmbeddingSuccess:
    spec = resolved.entry.embedding
    if spec is None or body.get("object") != "list" or body.get("model") != resolved.entry.model:
        raise ValueError("identity")
    if local and (
        body.get("definition_digest") != spec.service_definition_digest
        or body.get("revision") != spec.revision
        or body.get("dimensions") != resolved.dimensions
        or body.get("normalized") is not True
        or body.get("input_type") != resolved.input_type
    ):
        raise ValueError("identity")
    data, usage = body.get("data"), body.get("usage")
    if (
        not isinstance(data, list)
        or len(data) != len(request.texts())
        or not isinstance(usage, dict)
    ):
        raise ValueError("shape")
    prompt, total = usage.get("prompt_tokens"), usage.get("total_tokens")
    ceiling = (
        len(request.texts()) * resolved.entry.context_window_tokens
        if local
        else resolved.input_token_upper_bound
    )
    if (
        type(prompt) is not int
        or type(total) is not int
        or not len(data) <= prompt <= ceiling
        or total != prompt
    ):
        raise ValueError("usage")
    vectors = {}
    for item in data:
        if not isinstance(item, dict) or item.get("object") != "embedding":
            raise ValueError("vector_shape")
        index, vector = item.get("index"), item.get("embedding")
        if (
            type(index) is not int
            or not 0 <= index < len(data)
            or index in vectors
            or not isinstance(vector, list)
            or len(vector) != resolved.dimensions
            or any(type(v) not in {int, float} or not math.isfinite(v) for v in vector)
        ):
            raise ValueError("vector_shape")
        values = [float(v) for v in vector]
        if abs(math.fsum(v * v for v in values) - 1) > 0.002:
            raise ValueError("vector_norm")
        vectors[index] = EmbeddingVector(index=index, embedding=values)
    return EmbeddingSuccess([vectors[index] for index in range(len(data))], TokenUsage(prompt, 0))


def _failure(status: int, body: dict[str, Any], local: bool) -> ProviderFailure:
    error = body.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if local:
        if status == 503 and code in {"service_busy", "gpu_unavailable"}:
            return ProviderFailure("temporary_overload", "embedding_not_executed")
        if status in {413, 422} and code in {
            "input_invalid",
            "input_too_large",
            "input_token_limit_exceeded",
            "request_invalid",
            "request_too_large",
        }:
            return ProviderFailure("invalid_request", "embedding_input_rejected")
        if status == 404 and code == "model_not_found":
            return ProviderFailure("configuration_mismatch", "embedding_identity_mismatch")
    else:
        if status in {401, 403}:
            return ProviderFailure("authentication", "embedding_access_refused")
        if status == 429:
            if code in {
                "insufficient_quota",
                "credit_balance_exhausted",
                "organization_spend_limit_exceeded",
                "project_spend_limit_exceeded",
                "organization_usage_limit_exceeded",
            }:
                return ProviderFailure("account_blocked", "embedding_account_blocked")
            return ProviderFailure("temporary_throttle", "embedding_rate_limited")
        if status in {400, 404, 422}:
            return ProviderFailure("invalid_request", "embedding_input_rejected")
    return ProviderFailure("execution_unknown", "embedding_response_failed")


class EmbeddingAdapter:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        *,
        local: bool,
        secret: SecretStr | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ):
        self._base_url, self._timeout, self._local = base_url, timeout_seconds, local
        self._secret, self._transport = secret, transport

    async def _metadata(
        self, client: httpx2.AsyncClient, resolved: ResolvedEmbedding
    ) -> ProviderFailure | None:
        values = []
        for path in ["/health/ready", "/v1/models"]:
            async with client.stream("GET", path) as response:
                if response.status_code != 200:
                    return ProviderFailure(
                        "configuration_mismatch"
                        if 300 <= response.status_code < 400
                        else "temporary_overload",
                        "embedding_metadata_unavailable",
                    )
                values.append(await _json(response, 256_000))
        ready, listing = values
        if (
            ready.get("status") != "ready"
            or ready.get("device") != "cuda"
            or ready.get("service") != "embeddings"
            or not isinstance(listing.get("data"), list)
        ):
            return ProviderFailure("configuration_mismatch", "embedding_identity_mismatch")
        matches = [
            m
            for m in listing["data"]
            if isinstance(m, dict) and m.get("alias") == resolved.entry.model
        ]
        spec = resolved.entry.embedding
        if len(matches) != 1 or spec is None:
            return ProviderFailure("configuration_mismatch", "embedding_identity_mismatch")
        model = matches[0]
        native = {k: v for k, v in model.items() if k not in {"device", "definition_digest"}}
        digest = hashlib.sha256(
            json.dumps(native, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        if (
            model.get("device") != "cuda"
            or digest != spec.service_definition_digest
            or model.get("definition_digest") != digest
            or model.get("revision") != spec.revision
            or model.get("dimensions") != spec.dimensions
            or model.get("normalized") is not True
            or model.get("max_tokens") != resolved.entry.context_window_tokens
            or model.get("query_prefix") != spec.query_prefix
            or model.get("document_prefix") != ""
            or model.get("pooling") != spec.pooling
            or model.get("overflow") != "reject"
        ):
            return ProviderFailure("configuration_mismatch", "embedding_identity_mismatch")
        return None

    async def execute(
        self, resolved: ResolvedEmbedding, request: EmbeddingRequest
    ) -> EmbeddingSuccess | ProviderFailure:
        sent = False
        headers = {}
        if not self._local:
            if self._secret is None:
                return ProviderFailure("configuration_mismatch", "embedding_credential_missing")
            headers = {
                "Authorization": "Bearer " + self._secret.get_secret_value(),
                "X-Client-Request-Id": request.provider_call_id,
            }
        try:
            async with (
                asyncio.timeout(self._timeout),
                httpx2.AsyncClient(
                    base_url=self._base_url,
                    timeout=httpx2.Timeout(self._timeout, connect=5),
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                    headers=headers,
                ) as client,
            ):
                if self._local:
                    failure = await self._metadata(client, resolved)
                    if failure is not None:
                        return failure
                body: dict[str, Any] = {
                    "model": resolved.entry.model,
                    "input": request.input,
                    "encoding_format": "float",
                }
                if self._local:
                    body["input_type"] = resolved.input_type
                else:
                    body["dimensions"] = resolved.dimensions
                sent = True
                # Absolute endpoint path also supports an operator-provided URL prefix.
                path = "/v1/embeddings"
                async with client.stream("POST", path, json=body) as response:
                    result = await _json(response)
                    if response.status_code != 200:
                        return _failure(response.status_code, result, self._local)
                    return _success(result, resolved, request, self._local)
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout):
            return ProviderFailure("connection_not_established", "embedding_connection_failed")
        except (
            httpx2.HTTPError,
            TimeoutError,
            ValueError,
            TypeError,
            RecursionError,
            OverflowError,
        ):
            return ProviderFailure(
                "execution_unknown" if sent else "temporary_overload", "embedding_response_failed"
            )
