"""Request helpers and response models. Responses ignore unknown fields, so newer
gateways stay compatible; requests are validated by the gateway itself."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Usage(_Lenient):
    input_tokens: int
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int
    reasoning_tokens: int | None = None


class CallResult(_Lenient):
    provider_call_id: str
    status: Literal["completed", "incomplete"]
    provider: str
    model: str
    service_tier: str | None = None
    output_text: str | None = None
    output_json: dict[str, Any] | None = None
    refusal: str | None = None
    incomplete_reason: str | None = None
    structured_output_error: str | None = None
    usage: Usage
    cost_usd: str | None = None
    provider_response_id: str | None = None
    replayed: bool = False


class Allowance(_Lenient):
    requests_per_minute: int
    concurrency: int
    monthly_budget_usd: str
    per_call_cost_ceiling_usd: str


class AllocationReason(_Lenient):
    dimension: str
    code: str


class Allocation(_Lenient):
    project_id: str
    environment: str
    state: str
    requested: Allowance
    granted: Allowance
    reasons: list[AllocationReason]
    profiles: list[str]


class CatalogueModel(_Lenient):
    provider: str
    model: str
    context_window_tokens: int
    max_output_tokens: int
    reasoning_efforts: list[str]
    default_reasoning_effort: str | None = None
    temperature: str
    inputs: list[str]
    service_tiers: list[str]
    prices: dict[str, dict[str, Any]]


class Identity(_Lenient):
    project_id: str
    display_name: str
    environment: str
    contract_version: int


def _bytes(data: bytes | str | Path) -> bytes:
    return Path(data).read_bytes() if isinstance(data, (str, Path)) else data


def text(value: str) -> dict[str, Any]:
    return {"type": "input_text", "text": value}


def image(
    data: bytes | str | Path,
    media_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"],
    *,
    detail: Literal["low", "high", "auto", "original"] | None = None,
) -> dict[str, Any]:
    """An image part from bytes or a file path."""
    part: dict[str, Any] = {
        "type": "input_image",
        "media_type": media_type,
        "data_base64": base64.b64encode(_bytes(data)).decode("ascii"),
    }
    if detail is not None:
        part["detail"] = detail
    return part


def pdf(data: bytes | str | Path, filename: str) -> dict[str, Any]:
    """A PDF part from bytes or a file path."""
    return {
        "type": "input_file",
        "media_type": "application/pdf",
        "data_base64": base64.b64encode(_bytes(data)).decode("ascii"),
        "filename": filename,
    }


def message(
    role: Literal["user", "assistant", "developer"], *parts: dict[str, Any] | str
) -> dict[str, Any]:
    content = [text(part) if isinstance(part, str) else part for part in parts]
    return {"role": role, "content": content}


def json_schema(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """A strict JSON schema output format; the parsed answer arrives in `output_json`."""
    return {"type": "json_schema", "name": name, "schema": schema, "strict": True}


class Started(_Lenient):
    """The provider accepted the streamed call."""

    provider_call_id: str
    provider: str
    model: str


class TextDelta(_Lenient):
    delta: str


class RefusalDelta(_Lenient):
    delta: str


class Completed(_Lenient):
    """The final answer; after a resubmission its text is the authoritative one."""

    result: CallResult


StreamEvent = Started | TextDelta | RefusalDelta | Completed
