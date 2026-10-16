"""Strict single-image OCR contract and bounded CPU decoding before admission."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import threading
import warnings
from dataclasses import dataclass
from typing import Any, Literal

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from llm_gateway.catalogue.model import ALIAS_PATTERN, Catalogue, ModelEntry
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.llm.request import CALL_ID_PATTERN, OPERATION_PATTERN
from llm_gateway.projects.contract import ContractV2

MediaType = Literal["image/png", "image/jpeg"]
_DECODE_SLOT = threading.BoundedSemaphore(1)


class OcrRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    provider_call_id: str = Field(pattern=CALL_ID_PATTERN)
    operation: str = Field(pattern=OPERATION_PATTERN)
    model: str | None = Field(default=None, pattern=ALIAS_PATTERN)
    profile: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    media_type: MediaType
    data_base64: str = Field(min_length=4, max_length=10_666_668)

    @model_validator(mode="after")
    def _selection(self) -> OcrRequest:
        if (self.model is None) == (self.profile is None):
            raise ValueError("select exactly one model or profile")
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json", exclude_unset=True), sort_keys=True).encode()
        ).hexdigest()


class OcrBlock(BaseModel):
    text: str
    confidence: float
    box: list[list[float]]


class OcrResult(BaseModel):
    provider_call_id: str
    status: Literal["completed"]
    provider: str
    model: str
    model_alias: str
    definition_digest: str
    media_type: MediaType
    width: int
    height: int
    coordinate_system: Literal["original_image_pixels"]
    exif_rotation: Literal[False]
    text: str
    blocks: list[OcrBlock]
    cost_usd: str | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class ImageInfo:
    width: int
    height: int
    byte_count: int


def _decode(request: OcrRequest) -> ImageInfo:
    # Ownership stays in this thread even if the awaiting request is cancelled.
    if not _DECODE_SLOT.acquire(blocking=False):
        raise CallRejectedError("provider_unavailable", retry_after_seconds=1)
    try:
        raw = base64.b64decode(request.data_base64, validate=True)
        if len(raw) > 8_000_000:
            raise ValueError("image size")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw), formats=["PNG", "JPEG"]) as image:
                width, height = image.size
                if (
                    image.format != {"image/png": "PNG", "image/jpeg": "JPEG"}[request.media_type]
                    or getattr(image, "n_frames", 1) != 1
                    or not 1 <= width <= 4096
                    or not 1 <= height <= 4096
                    or width * height > 12_000_000
                ):
                    raise ValueError("image bounds")
                image.verify()
            # JPEG verification alone does not decode its compressed pixel stream.
            with Image.open(io.BytesIO(raw), formats=["PNG", "JPEG"]) as image:
                image.load()
        return ImageInfo(width, height, len(raw))
    except (
        ValueError,
        OSError,
        SyntaxError,
        binascii.Error,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise CallRejectedError(
            "invalid_model_settings",
            detail="A valid single PNG or JPEG within image limits is required.",
        ) from None
    finally:
        _DECODE_SLOT.release()


async def validate_image(request: OcrRequest) -> ImageInfo:
    return await asyncio.to_thread(_decode, request)


@dataclass(frozen=True, slots=True)
class ResolvedOcr:
    entry: ModelEntry
    image: ImageInfo

    @property
    def settings_digest(self) -> str:
        return hashlib.sha256(b"single-image;original-pixels;no-exif-rotation;v1").hexdigest()


def ocr_schema(entry: ModelEntry) -> dict[str, Any]:
    spec = entry.ocr
    if spec is None:
        raise ValueError("OCR contract missing")
    schema = OcrRequest.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    props = schema["properties"]
    props.pop("profile")
    props["model"] = {"type": "string", "const": entry.alias}
    props["data_base64"]["maxLength"] = ((spec.max_image_bytes + 2) // 3) * 4
    schema["required"] = [*schema["required"], "model"]
    schema["x-semantic-constraints"] = spec.model_dump(mode="json")
    return schema


def resolve_ocr(
    request: OcrRequest, image: ImageInfo, contract: ContractV2, catalogue: Catalogue
) -> ResolvedOcr:
    profile = contract.profiles.get(request.profile) if request.profile is not None else None
    if request.profile is not None and profile is None:
        raise CallRejectedError("profile_not_found")
    entry = catalogue.model(profile.model if profile else request.model or "")
    if entry is None:
        raise CallRejectedError("model_not_in_catalogue")
    if entry.task != "ocr" or entry.ocr is None:
        raise CallRejectedError("model_task_mismatch")
    from llm_gateway.catalogue.schemas import setting_errors

    supplied = request.model_dump(mode="json", exclude_unset=True)
    supplied.pop("profile", None)
    supplied["model"] = entry.alias
    inherited = profile.parameters.model_dump(exclude_none=True) if profile else {}
    errors = setting_errors(entry, {**inherited, **supplied})
    if errors:
        raise CallRejectedError("invalid_model_settings", detail="; ".join(errors))
    spec = entry.ocr
    if (
        image.byte_count > spec.max_image_bytes
        or max(image.width, image.height) > spec.max_side
        or image.width * image.height > spec.max_pixels
        or request.media_type not in spec.media_types
    ):
        raise CallRejectedError("invalid_model_settings")
    return ResolvedOcr(entry, image)
