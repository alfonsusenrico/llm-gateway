"""Pinned GPU-only RapidOCR HTTP contract with bounded, content-free failures."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, TypeGuard

import httpx2

from llm_gateway.llm.providers import ProviderFailure
from llm_gateway.ocr.models import OcrBlock, OcrRequest, ResolvedOcr


@dataclass(frozen=True, slots=True)
class OcrSuccess:
    text: str
    blocks: list[OcrBlock]


async def _json(response: httpx2.Response, limit: int) -> dict[str, Any]:
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError("response_limit")
    body = json.loads(data)
    if not isinstance(body, dict):
        raise ValueError("response_shape")
    return body


def _number(value: Any) -> TypeGuard[int | float]:
    return type(value) in {int, float} and math.isfinite(value)


def _success(body: dict[str, Any], resolved: ResolvedOcr) -> OcrSuccess:
    spec, image = resolved.entry.ocr, resolved.image
    if (
        spec is None
        or body.get("model") != resolved.entry.model
        or body.get("definition_digest") != spec.service_definition_digest
        or type(body.get("width")) is not int
        or body["width"] != image.width
        or type(body.get("height")) is not int
        or body["height"] != image.height
        or body.get("coordinate_system") != "original_image_pixels"
        or not isinstance(body.get("text"), str)
        or not isinstance(body.get("blocks"), list)
    ):
        raise ValueError("identity")
    blocks = body["blocks"]
    if len(blocks) > spec.max_blocks:
        raise ValueError("blocks")
    parsed = []
    total = 0
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            raise ValueError("block")
        confidence, box = block.get("confidence"), block.get("box")
        if (
            not _number(confidence)
            or not 0 <= confidence <= 1
            or not isinstance(box, list)
            or len(box) != 4
        ):
            raise ValueError("block")
        for point in box:
            if (
                not isinstance(point, list)
                or len(point) != 2
                or not all(_number(v) for v in point)
                or not -1 <= point[0] <= image.width + 1
                or not -1 <= point[1] <= image.height + 1
            ):
                raise ValueError("box")
        total += len(block["text"].encode("utf-8"))
        if total > spec.max_text_bytes:
            raise ValueError("text_limit")
        parsed.append(OcrBlock(text=block["text"], confidence=confidence, box=box))
    if body["text"] != "\n".join(block.text for block in parsed):
        raise ValueError("text")
    return OcrSuccess(body["text"], parsed)


def _failure(status: int, body: dict[str, Any]) -> ProviderFailure:
    error = body.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if status == 503 and code in {"service_busy", "gpu_unavailable"}:
        return ProviderFailure("temporary_overload", "ocr_not_executed")
    if status in {413, 422} and code in {
        "image_invalid",
        "image_too_large",
        "image_dimensions_exceeded",
        "request_invalid",
        "request_too_large",
    }:
        return ProviderFailure("invalid_request", "ocr_input_rejected")
    if status == 404 and code == "model_not_found":
        return ProviderFailure("configuration_mismatch", "ocr_identity_mismatch")
    return ProviderFailure("execution_unknown", "ocr_response_failed")


class OcrAdapter:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ):
        self._base_url, self._timeout, self._transport = base_url, timeout_seconds, transport

    async def _metadata(
        self, client: httpx2.AsyncClient, resolved: ResolvedOcr
    ) -> ProviderFailure | None:
        values = []
        for path in ["/health/ready", "/v1/models"]:
            async with client.stream("GET", path) as response:
                if response.status_code != 200:
                    return ProviderFailure(
                        "configuration_mismatch"
                        if 300 <= response.status_code < 400
                        else "temporary_overload",
                        "ocr_metadata_unavailable",
                    )
                values.append(await _json(response, 256_000))
        ready, listing = values
        providers = ["CUDAExecutionProvider"] * 3
        if (
            ready.get("status") != "ready"
            or ready.get("service") != "ocr"
            or ready.get("device") != "cuda"
            or ready.get("providers") != providers
            or not isinstance(listing.get("data"), list)
        ):
            return ProviderFailure("configuration_mismatch", "ocr_identity_mismatch")
        matches = [
            m
            for m in listing["data"]
            if isinstance(m, dict) and m.get("alias") == resolved.entry.model
        ]
        spec = resolved.entry.ocr
        if len(matches) != 1 or spec is None:
            return ProviderFailure("configuration_mismatch", "ocr_identity_mismatch")
        model = matches[0]
        native = {
            k: v for k, v in model.items() if k not in {"device", "providers", "definition_digest"}
        }
        digest = hashlib.sha256(
            json.dumps(native, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        if (
            model.get("device") != "cuda"
            or model.get("providers") != providers
            or digest != spec.service_definition_digest
            or model.get("definition_digest") != digest
        ):
            return ProviderFailure("configuration_mismatch", "ocr_identity_mismatch")
        return None

    async def execute(
        self, resolved: ResolvedOcr, request: OcrRequest
    ) -> OcrSuccess | ProviderFailure:
        sent = False
        try:
            async with (
                asyncio.timeout(self._timeout),
                httpx2.AsyncClient(
                    base_url=self._base_url,
                    timeout=httpx2.Timeout(self._timeout, connect=5),
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                ) as client,
            ):
                failure = await self._metadata(client, resolved)
                if failure is not None:
                    return failure
                sent = True
                async with client.stream(
                    "POST",
                    "/v1/ocr",
                    json={
                        "model": resolved.entry.model,
                        "media_type": request.media_type,
                        "data_base64": request.data_base64,
                    },
                ) as response:
                    body = await _json(response, 16_000_000)
                    if response.status_code != 200:
                        return _failure(response.status_code, body)
                    return _success(body, resolved)
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout):
            return ProviderFailure("connection_not_established", "ocr_connection_failed")
        except (
            httpx2.HTTPError,
            TimeoutError,
            ValueError,
            TypeError,
            RecursionError,
            OverflowError,
        ):
            return ProviderFailure(
                "execution_unknown" if sent else "temporary_overload", "ocr_response_failed"
            )
