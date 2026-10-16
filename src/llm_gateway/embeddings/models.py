"""Strict embedding request/result types and immutable resolved settings."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from llm_gateway.catalogue.model import ALIAS_PATTERN, Catalogue, ModelEntry
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.llm.request import CALL_ID_PATTERN, OPERATION_PATTERN
from llm_gateway.projects.contract import ContractV2


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    provider_call_id: str = Field(pattern=CALL_ID_PATTERN)
    operation: str = Field(pattern=OPERATION_PATTERN)
    model: str | None = Field(default=None, pattern=ALIAS_PATTERN)
    profile: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    input: str | list[str]
    encoding_format: Literal["float"] = "float"
    dimensions: int | None = Field(default=None, ge=1, le=3072)
    input_type: Literal["query", "document"] | None = None

    @model_validator(mode="after")
    def _bounded(self) -> EmbeddingRequest:
        if (self.model is None) == (self.profile is None):
            raise ValueError("select exactly one model or profile")
        texts = self.texts()
        if not 1 <= len(texts) <= 64 or any(not text.strip() for text in texts):
            raise ValueError("input must contain 1 to 64 nonempty texts")
        try:
            if sum(len(text.encode("utf-8")) for text in texts) > 200_000:
                raise ValueError("input byte limit exceeded")
        except UnicodeError:
            raise ValueError("input must be valid UTF-8") from None
        return self

    def texts(self) -> list[str]:
        return [self.input] if isinstance(self.input, str) else self.input

    def fingerprint(self) -> str:
        body = self.model_dump(mode="json", exclude_unset=True)
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class EmbeddingVector(BaseModel):
    index: int
    embedding: list[float]


class EmbeddingUsage(BaseModel):
    input_tokens: int
    total_tokens: int


class EmbeddingResult(BaseModel):
    provider_call_id: str
    status: Literal["completed"]
    provider: str
    model: str
    model_alias: str
    definition_digest: str
    dimensions: int
    normalized: bool
    input_type: Literal["query", "document"] | None
    data: list[EmbeddingVector]
    usage: EmbeddingUsage
    cost_usd: str | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class ResolvedEmbedding:
    entry: ModelEntry
    dimensions: int
    input_type: Literal["query", "document"] | None
    input_token_upper_bound: int

    @property
    def settings_digest(self) -> str:
        value = {
            "dimensions": self.dimensions,
            "input_type": self.input_type,
            "encoding_format": "float",
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def embedding_schema(entry: ModelEntry) -> dict[str, Any]:
    spec = entry.embedding
    if spec is None:
        raise ValueError("embedding contract missing")
    schema = EmbeddingRequest.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    props = schema["properties"]
    props.pop("profile")
    props["model"] = {"type": "string", "const": entry.alias}
    schema["required"] = [*schema["required"], "model"]
    props["input"] = {
        "anyOf": [
            {"type": "string", "minLength": 1},
            {
                "type": "array",
                "minItems": 1,
                "maxItems": spec.max_batch_size,
                "items": {"type": "string", "minLength": 1},
            },
        ]
    }
    if spec.variable_dimensions:
        props["dimensions"] = {
            "type": "integer",
            "minimum": 1,
            "maximum": spec.dimensions,
            "default": spec.dimensions,
        }
    else:
        props.pop("dimensions")
    if spec.input_types:
        props["input_type"] = {
            "type": "string",
            "enum": list(spec.input_types),
            "default": "document",
        }
    else:
        props.pop("input_type")
    schema["x-semantic-constraints"] = {
        "max_input_bytes": spec.max_input_bytes,
        "nonempty": "Whitespace-only text is rejected.",
        "token_limit": (
            "Local tokenizer rejects overflow before encoding."
            if entry.execution == "local"
            else "UTF-8 bytes per input must fit the input token limit; aggregate bytes reserve tokens."
        ),
        "max_input_tokens": entry.context_window_tokens,
        "max_batch_tokens": spec.max_batch_tokens,
    }
    return schema


def resolve_embedding(
    request: EmbeddingRequest, contract: ContractV2, catalogue: Catalogue, environment: str
) -> ResolvedEmbedding:
    profile = contract.profiles.get(request.profile) if request.profile is not None else None
    if request.profile is not None and profile is None:
        raise CallRejectedError("profile_not_found")
    entry = catalogue.model(profile.model if profile else request.model or "")
    if entry is None:
        raise CallRejectedError("model_not_in_catalogue")
    if entry.task != "embedding" or entry.embedding is None:
        raise CallRejectedError("model_task_mismatch")
    if entry.development_only and environment == "production":
        raise CallRejectedError("stub_not_allowed")
    from llm_gateway.catalogue.schemas import setting_errors

    supplied = request.model_dump(mode="json", exclude_unset=True)
    supplied.pop("profile", None)
    supplied["model"] = entry.alias
    inherited = profile.parameters.model_dump(exclude_none=True) if profile else {}
    effective = {**inherited, **supplied}
    errors = setting_errors(entry, effective)
    if errors:
        raise CallRejectedError("invalid_model_settings", detail="; ".join(errors))
    spec = entry.embedding
    sizes = [len(text.encode("utf-8")) for text in request.texts()]
    total = sum(sizes)
    if len(sizes) > spec.max_batch_size or total > spec.max_input_bytes:
        raise CallRejectedError("invalid_model_settings")
    if entry.billing != "zero" and (
        max(sizes) > entry.context_window_tokens or total > spec.max_batch_tokens
    ):
        raise CallRejectedError(
            "invalid_model_settings", detail="Input exceeds the safe embedding token bound."
        )
    dimensions = effective.get("dimensions", spec.dimensions)
    input_type = effective.get("input_type", "document" if spec.input_types else None)
    # A local service also bounds the serialized native body before JSON parsing.
    if entry.execution == "local":
        body = {
            "model": entry.model,
            "input": request.input,
            "input_type": input_type,
            "encoding_format": "float",
        }
        if len(json.dumps(body, ensure_ascii=False).encode()) > 300_000:
            raise CallRejectedError("invalid_model_settings")
    return ResolvedEmbedding(entry, dimensions, input_type, total)
