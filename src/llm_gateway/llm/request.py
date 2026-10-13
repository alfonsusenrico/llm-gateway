"""The `POST /v1/responses` request: strict, provider-neutral, never passed through raw."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CALL_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,160}$"
OPERATION_PATTERN = r"^[a-z][a-z0-9_]{1,63}$"
StubScenario = Literal[
    "rate_limited",
    "overloaded",
    "connection_failed",
    "timeout_after_send",
    "invalid_request",
    "incomplete",
    "invalid_json",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _valid_base64(value: str) -> str:
    try:
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("must be standard base64") from exc
    return value


class TextPart(_Strict):
    type: Literal["input_text"]
    text: str = Field(min_length=1)


class ImagePart(_Strict):
    type: Literal["input_image"]
    media_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    data_base64: str = Field(min_length=4)
    detail: Literal["low", "high", "auto", "original"] | None = None

    _check_data = field_validator("data_base64")(_valid_base64)


class FilePart(_Strict):
    type: Literal["input_file"]
    media_type: Literal["application/pdf"]
    data_base64: str = Field(min_length=4)
    filename: str = Field(min_length=1, max_length=255)

    _check_data = field_validator("data_base64")(_valid_base64)


Part = Annotated[TextPart | ImagePart | FilePart, Field(discriminator="type")]


class Message(_Strict):
    role: Literal["user", "assistant", "developer"]
    content: str | list[Part] = Field(min_length=1)


class TextFormat(_Strict):
    type: Literal["text", "json_schema"] = "text"
    name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    # "schema" shadows a BaseModel attribute, so it is aliased.
    json_schema: dict[str, Any] | None = Field(default=None, alias="schema")
    strict: Literal[True] | None = None

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _schema_fields(self) -> TextFormat:
        is_schema = self.type == "json_schema"
        if is_schema and (self.name is None or self.json_schema is None or self.strict is None):
            raise ValueError("json_schema needs name, schema, and strict: true")
        if not is_schema and (self.name or self.json_schema or self.strict):
            raise ValueError("name, schema, and strict apply only to json_schema")
        return self


class ModelRef(_Strict):
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=160)


class StubScript(_Strict):
    reply: str | dict[str, Any] | None = None
    scenario: StubScenario | None = None


class ResponseRequest(_Strict):
    provider_call_id: str = Field(pattern=CALL_ID_PATTERN)
    operation: str = Field(pattern=OPERATION_PATTERN)
    profile: str | None = Field(default=None, min_length=1, max_length=40)
    model: ModelRef | None = None
    input: str | list[Message] = Field(min_length=1)
    instructions: str | None = Field(default=None, max_length=100_000)
    text_format: TextFormat = TextFormat()
    max_output_tokens: int | None = Field(default=None, ge=1, le=128_000)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=32)
    temperature: float | None = Field(default=None, ge=0, le=2)
    prompt_cache_key: str | None = Field(default=None, min_length=1, max_length=64)
    stub: StubScript | None = None

    @model_validator(mode="after")
    def _one_model_source(self) -> ResponseRequest:
        if (self.profile is None) == (self.model is None):
            raise ValueError("give exactly one of profile or model")
        if isinstance(self.input, list) and len(self.input) > 200:
            raise ValueError("input may hold at most 200 messages")
        return self

    def input_types(self) -> set[str]:
        types = {"text"}
        if isinstance(self.input, list):
            for message in self.input:
                if isinstance(message.content, list):
                    for part in message.content:
                        if isinstance(part, ImagePart):
                            types.add("image")
                        elif isinstance(part, FilePart):
                            types.add("file")
        return types

    def fingerprint(self) -> str:
        """SHA-256 of the canonical request, to tell a resubmission from a different call."""
        canonical = json.dumps(
            self.model_dump(mode="json", exclude_none=True, by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
