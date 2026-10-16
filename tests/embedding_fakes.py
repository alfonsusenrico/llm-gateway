"""Synthetic embedding HTTP service with no model or GPU dependencies."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import httpx2

from llm_gateway.catalogue.model import ModelEntry, get_catalogue


def local_model() -> tuple[ModelEntry, dict[str, Any]]:
    entry = get_catalogue().models["local-minilm-l6-v2-v1"]
    assert entry.embedding is not None
    metadata = dict(
        alias=entry.model,
        dimensions=384,
        max_tokens=256,
        normalized=True,
        revision=entry.embedding.revision,
        query_prefix="",
        document_prefix="",
        pooling="mean",
        overflow="reject",
        artifact_files={"model": "a" * 64},
    )
    digest = hashlib.sha256(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    metadata.update(device="cuda", definition_digest=digest)
    entry = entry.model_copy(
        update={
            "alias": "embedding-test",
            "embedding": entry.embedding.model_copy(update={"service_definition_digest": digest}),
        }
    )
    return entry, metadata


class EmbeddingService:
    def __init__(self, metadata: dict[str, Any] | None = None):
        self.metadata = metadata
        self.requests: list[httpx2.Request] = []
        self.generations = 0
        self.mode = "success"
        self.hold: asyncio.Event | None = None
        self.delay = 0.0

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path == "/health/ready":
            return httpx2.Response(
                200, json={"status": "ready", "service": "embeddings", "device": "cuda"}
            )
        if request.url.path == "/v1/models":
            assert self.metadata is not None
            metadata = {
                **self.metadata,
                **({"device": "cpu"} if self.mode == "metadata_mismatch" else {}),
            }
            return httpx2.Response(200, json={"object": "list", "data": [metadata]})
        assert request.url.path == "/v1/embeddings"
        self.generations += 1
        if self.hold is not None:
            await self.hold.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == "connect":
            raise httpx2.ConnectError("PRIVATE_PROVIDER_TEXT", request=request)
        if self.mode == "timeout":
            raise httpx2.ReadTimeout("PRIVATE_PROVIDER_TEXT", request=request)
        if self.mode in {
            "gpu_unavailable",
            "service_busy",
            "input_token_limit_exceeded",
            "raw_503",
            "insufficient_quota",
            "rate_limit",
        }:
            status = (
                422
                if self.mode == "input_token_limit_exceeded"
                else (429 if self.mode in {"insufficient_quota", "rate_limit"} else 503)
            )
            return httpx2.Response(
                status, json={"error": {"code": self.mode, "message": "PRIVATE_PROVIDER_TEXT"}}
            )
        body = json.loads(request.content)
        texts = [body["input"]] if isinstance(body["input"], str) else body["input"]
        dims = body.get("dimensions", 384)
        result: dict[str, Any] = dict(
            object="list",
            model=body["model"],
            data=[
                dict(object="embedding", index=i, embedding=[1.0] + [0.0] * (dims - 1))
                for i in range(len(texts))
            ],
            usage={"prompt_tokens": 2 * len(texts), "total_tokens": 2 * len(texts)},
        )
        if self.metadata is not None:
            result.update(
                definition_digest=self.metadata["definition_digest"],
                revision=self.metadata["revision"],
                dimensions=dims,
                normalized=True,
                input_type=body["input_type"],
            )
        match self.mode:
            case "missing_usage":
                result.pop("usage")
            case "negative_usage":
                result["usage"] = {"prompt_tokens": -1, "total_tokens": -1}
            case "usage_over_bound":
                result["usage"] = {"prompt_tokens": 999999, "total_tokens": 999999}
            case "wrong_model":
                result["model"] = "other"
            case "bad_revision":
                result["revision"] = "b" * 40
            case "bad_dimensions":
                result["data"][0]["embedding"].pop()
            case "nan":
                result["data"][0]["embedding"][0] = float("nan")
            case "bool":
                result["data"][0]["embedding"][0] = True
            case "zero":
                result["data"][0]["embedding"][0] = 0.0
            case "index":
                result["data"][0]["index"] = len(texts)
            case "duplicate":
                result["data"][1]["index"] = 0
            case "reversed":
                result["data"].reverse()
            case "malformed":
                return httpx2.Response(200, content=b'{"incomplete"')
        return httpx2.Response(200, content=json.dumps(result).encode())
