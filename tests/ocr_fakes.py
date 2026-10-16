"""Synthetic single-image native OCR service without inference dependencies."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from typing import Any

import httpx2
from PIL import Image

from llm_gateway.catalogue.model import ModelEntry, get_catalogue
from llm_gateway.ocr.models import OcrRequest

ALIAS = "local-rapidocr-ppocrv4-v1"


def image_data(format: str = "PNG", size: tuple[int, int] = (100, 40), **options) -> bytes:
    stream = io.BytesIO()
    with Image.new("RGB", size, "white") as image:
        image.save(stream, format=format, **options)
    return stream.getvalue()


def request(**options: Any) -> OcrRequest:
    return OcrRequest.model_validate(
        {
            "provider_call_id": "ocr-1",
            "operation": "demo",
            "model": ALIAS,
            "media_type": "image/png",
            "data_base64": base64.b64encode(image_data()).decode(),
            **options,
        }
    )


def local_model() -> tuple[ModelEntry, dict[str, Any]]:
    entry = get_catalogue().models[ALIAS]
    assert entry.ocr is not None
    spec = entry.ocr
    metadata: dict[str, Any] = dict(
        alias=entry.model,
        artifact_files={"model": "a" * 64},
        coordinate_system=spec.coordinate_system,
        engine=spec.engine,
        engine_version=spec.engine_version,
        exif_rotation=False,
        inputs=list(spec.media_types),
        max_image_bytes=spec.max_image_bytes,
        max_pixels=spec.max_pixels,
        max_side=spec.max_side,
        runtime=spec.runtime,
        runtime_version=spec.runtime_version,
    )
    digest = hashlib.sha256(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    metadata.update(
        device="cuda", providers=["CUDAExecutionProvider"] * 3, definition_digest=digest
    )
    return entry.model_copy(
        update={
            "alias": "ocr-test",
            "ocr": spec.model_copy(update={"service_definition_digest": digest}),
        }
    ), metadata


class OcrService:
    def __init__(self, metadata: dict[str, Any]):
        self.metadata = metadata
        self.requests: list[httpx2.Request] = []
        self.generations = 0
        self.mode = "success"
        self.hold: asyncio.Event | None = None
        self.delay = 0.0

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path == "/health/ready":
            ready: dict[str, Any] = dict(
                status="ready",
                service="ocr",
                device="cuda",
                providers=["CUDAExecutionProvider"] * 3,
            )
            if self.mode == "ready_cpu":
                ready["device"] = "cpu"
            if self.mode == "ready_503":
                return httpx2.Response(503, json={"error": {"message": "PRIVATE_PROVIDER_TEXT"}})
            if self.mode == "redirect":
                return httpx2.Response(307, headers={"Location": "http://elsewhere"})
            return httpx2.Response(200, json=ready)
        if request.url.path == "/v1/models":
            metadata = self.metadata.copy()
            if self.mode == "metadata_cpu":
                metadata["providers"] = ["CPUExecutionProvider"] * 3
            if self.mode == "metadata_mismatch":
                metadata["artifact_files"] = {"model": "b" * 64}
            if self.mode == "metadata_hash":
                metadata["definition_digest"] = "b" * 64
            return httpx2.Response(200, json={"object": "list", "data": [metadata]})
        assert request.url.path == "/v1/ocr"
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
            "raw_503",
            "image_invalid",
            "inference_failed",
            "model_not_found",
        }:
            status = {"image_invalid": 422, "inference_failed": 500, "model_not_found": 404}.get(
                self.mode, 503
            )
            return httpx2.Response(
                status, json={"error": {"code": self.mode, "message": "PRIVATE_PROVIDER_TEXT"}}
            )
        body = json.loads(request.content)
        with Image.open(io.BytesIO(base64.b64decode(body["data_base64"]))) as image:
            width, height = image.size
        block: dict[str, Any] = dict(
            text="Synthetic text", confidence=0.99, box=[[1, 1], [50, 1], [50, 20], [1, 20]]
        )
        result: dict[str, Any] = dict(
            model=body["model"],
            definition_digest=self.metadata["definition_digest"],
            width=width,
            height=height,
            coordinate_system="original_image_pixels",
            text=block["text"],
            blocks=[block],
        )
        match self.mode:
            case "blank":
                result.update(text="", blocks=[])
            case "wrong_model":
                result["model"] = "other"
            case "wrong_digest":
                result["definition_digest"] = "b" * 64
            case "wrong_width":
                result["width"] += 1
            case "width_bool":
                result["width"] = True
            case "coordinates":
                result["coordinate_system"] = "rotated_pixels"
            case "text_mismatch":
                result["text"] = "different"
            case "confidence_bool":
                block["confidence"] = True
            case "confidence_nan":
                block["confidence"] = float("nan")
            case "confidence_range":
                block["confidence"] = 1.1
            case "box_bool":
                block["box"][0][0] = True
            case "box_inf":
                block["box"][0][0] = float("inf")
            case "box_range":
                block["box"][0][0] = width + 2
            case "box_shape":
                block["box"].pop()
            case "text_limit":
                block["text"] = "x" * 1_000_001
            case "block_limit":
                result["blocks"] *= 2049
            case "malformed":
                return httpx2.Response(200, content=b'{"unfinished"')
            case "body_limit":
                return httpx2.Response(200, content=b"x" * 16_000_001)
        return httpx2.Response(200, content=json.dumps(result).encode())
