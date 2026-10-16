"""Synthetic native HTTP service, independent of GPU availability and real models."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2

from llm_gateway.catalogue.model import ModelEntry, get_catalogue


def local_entry() -> ModelEntry:
    original = get_catalogue().models["ollama-qwen2.5-3b-instruct-q4km-v1"]
    assert original.local is not None
    return original.model_copy(
        update={
            "alias": "local-test",
            "local": original.local.model_copy(
                update={
                    "artifact_digest": "a" * 64,
                    "template_digest": hashlib.sha256(b"synthetic-template").hexdigest(),
                    "system_digest": hashlib.sha256(b"synthetic-system").hexdigest(),
                }
            ),
        }
    )


class NativeStream(httpx2.AsyncByteStream):
    def __init__(self, final: dict[str, Any], *, mode: str, delay: float = 0):
        self.final, self.mode, self.delay = final, mode, delay
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        event = {
            "model": self.final["model"],
            "done": False,
            "message": {"role": "assistant", "content": "Local "},
        }
        yield json.dumps(event).encode() + b"\n"
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == "stream_break":
            raise httpx2.ReadError("PRIVATE_PROVIDER_TEXT")
        if self.mode == "no_terminal":
            return
        if self.mode == "malformed_stream":
            yield b'{"bad"\n'
            return
        final = {**self.final, "message": {"role": "assistant", "content": "answer."}}
        yield json.dumps(final).encode() + b"\n"

    async def aclose(self) -> None:
        self.closed = True


class LocalService:
    def __init__(self, entry: ModelEntry):
        self.entry = entry
        self.mode = "success"
        self.mismatch: str | None = None
        self.requests: list[httpx2.Request] = []
        self.generations = 0
        self.output = "Local answer."
        self.delay = 0.0
        self.streams: list[NativeStream] = []
        self.hold: asyncio.Event | None = None

    def success(self) -> dict[str, Any]:
        return {
            "model": self.entry.model,
            "done": True,
            "done_reason": "stop",
            "message": {"role": "assistant", "content": self.output},
            "prompt_eval_count": 7,
            "eval_count": 3,
        }

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        local = self.entry.local
        assert local is not None
        path = request.url.path
        if path == "/health/ready":
            return httpx2.Response(
                200,
                json={
                    "status": "ready",
                    "execution": "cpu" if self.mismatch == "gpu" else "gpu_only",
                },
            )
        if path == "/api/version":
            return httpx2.Response(
                200,
                json={"version": "other" if self.mismatch == "version" else local.runtime_version},
            )
        if path == "/api/tags":
            return httpx2.Response(
                200,
                json={
                    "models": [
                        {
                            "name": self.entry.model,
                            "digest": "b" * 64
                            if self.mismatch == "model"
                            else local.artifact_digest,
                        }
                    ]
                },
            )
        if path == "/api/show":
            return httpx2.Response(
                200,
                json={
                    "template": "other" if self.mismatch == "template" else "synthetic-template",
                    "system": "synthetic-system",
                },
            )
        assert path == "/api/chat"
        self.generations += 1
        if self.mode == "connect":
            raise httpx2.ConnectError("PRIVATE_PROVIDER_TEXT", request=request)
        if self.mode == "read_timeout":
            raise httpx2.ReadTimeout("PRIVATE_PROVIDER_TEXT", request=request)
        if self.mode in {
            "service_busy",
            "gpu_unavailable",
            "gpu_model_load_failed",
            "gpu_execution_unverified",
        }:
            return httpx2.Response(
                503, json={"error": {"code": self.mode, "message": "PRIVATE_PROVIDER_TEXT"}}
            )
        if self.mode == "raw_503":
            return httpx2.Response(503, json={"error": "PRIVATE_PROVIDER_TEXT"})
        if self.mode == "redirect":
            return httpx2.Response(307, headers={"Location": "http://untrusted.test"}, json={})
        if self.hold is not None:
            await self.hold.wait()
        if self.delay and not json.loads(request.content).get("stream"):
            await asyncio.sleep(self.delay)
        if self.mode == "malformed":
            return httpx2.Response(200, content=b"PRIVATE_PROVIDER_TEXT")
        if self.mode == "oversize":
            return httpx2.Response(200, content=b"x" * 1_000_001)
        body = self.success()
        if self.mode == "missing_usage":
            body.pop("prompt_eval_count")
        if self.mode == "incomplete":
            body["done_reason"] = "length"
        if json.loads(request.content).get("stream"):
            stream = NativeStream(body, mode=self.mode, delay=self.delay)
            self.streams.append(stream)
            return httpx2.Response(200, stream=stream)
        return httpx2.Response(200, json=body)
