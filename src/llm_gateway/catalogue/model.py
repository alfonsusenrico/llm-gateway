"""The model catalogue and price schedule: reviewed data files, validated strictly.

Prices change only in a reviewed change that adds a dated entry; history is
never rewritten. An unknown price is reported as unknown, never as zero.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from llm_gateway.catalogue.adapters import ADAPTERS, Task

DATA_DIR = Path(__file__).parent
Tier = Literal["standard", "flex"]
InputType = Literal["text", "image", "file"]
TemperatureRule = Literal["always", "never", "only_with_effort_none"]
ALIAS_PATTERN = r"^[a-z][a-z0-9._-]{1,159}$"
NonNegativeUsd = Annotated[Decimal, Field(ge=0, le=1000)]


class CatalogueError(ValueError):
    """The catalogue or price files are invalid; the gateway must not start."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LocalLlmContract(_Strict):
    protocol: Literal["ollama-gpu-native-v1"]
    runtime_version: Literal["0.5.11"]
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    template_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    system_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_fixed_bytes: int = Field(ge=256, le=4096)
    prompt_message_bytes: int = Field(ge=128, le=4096)
    default_temperature: Literal[0] = 0


class OcrContract(_Strict):
    protocol: Literal["rapidocr-gpu-native-v1"]
    service_definition_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine: Literal["rapidocr-onnxruntime"]
    engine_version: Literal["1.4.4"]
    runtime: Literal["onnxruntime-gpu"]
    runtime_version: Literal["1.20.2"]
    media_types: tuple[Literal["image/png", "image/jpeg"], ...]
    max_image_bytes: int = Field(ge=1, le=8_000_000)
    max_pixels: int = Field(ge=1, le=12_000_000)
    max_side: int = Field(ge=1, le=4096)
    max_blocks: int = Field(ge=1, le=2048)
    max_text_bytes: int = Field(ge=1, le=1_000_000)
    coordinate_system: Literal["original_image_pixels"]
    exif_rotation: Literal[False]


class EmbeddingContract(_Strict):
    dimensions: int = Field(ge=1, le=3072)
    variable_dimensions: bool = False
    normalized: Literal[True] = True
    max_batch_size: int = Field(ge=1, le=64)
    max_input_bytes: int = Field(ge=1, le=200_000)
    max_batch_tokens: int = Field(ge=1, le=300_000)
    input_types: tuple[Literal["query", "document"], ...] = ()
    query_prefix: str = Field(default="", max_length=512)
    pooling: Literal["mean", "cls", "provider"]
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    service_definition_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


# Canonical field sets belong to a schema version. Adding fields in a later
# version must not change an already registered alias's normalized definition.
_V1_FIELDS = frozenset(
    [
        "schema_version",
        "alias",
        "task",
        "adapter",
        "deployment",
        "execution",
        "billing",
        "provider",
        "model",
        "context_window_tokens",
        "max_output_tokens",
        "reasoning_efforts",
        "default_reasoning_effort",
        "temperature",
        "inputs",
        "service_tiers",
        "function_tools",
        "development_only",
        "streaming",
        "structured_output",
        "prompt_cache",
    ]
)


class ModelEntry(_Strict):
    schema_version: Literal[1, 2, 3, 4] = 1
    alias: str = Field(pattern=ALIAS_PATTERN)
    task: Task = "llm"
    adapter: str
    deployment: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    execution: Literal["remote", "local", "stub"]
    billing: Literal["token", "zero"]
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    model: str = Field(min_length=1, max_length=160)
    context_window_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(ge=0)
    reasoning_efforts: tuple[str, ...] = ()
    default_reasoning_effort: str | None = None
    temperature: TemperatureRule
    inputs: tuple[InputType, ...] = Field(min_length=1)
    service_tiers: tuple[Tier, ...] = Field(min_length=1)
    function_tools: bool = False
    development_only: bool
    streaming: bool = True
    structured_output: bool = True
    prompt_cache: bool = True
    documentation: tuple[str, ...] = ()
    local: LocalLlmContract | None = None
    embedding: EmbeddingContract | None = None
    ocr: OcrContract | None = None

    @model_validator(mode="after")
    def _default_effort_is_allowed(self) -> ModelEntry:
        if (
            self.default_reasoning_effort is not None
            and self.default_reasoning_effort not in self.reasoning_efforts
        ):
            raise ValueError("default_reasoning_effort must be one of reasoning_efforts")
        adapter = ADAPTERS.get(self.adapter)
        if adapter is None:
            raise ValueError("adapter is not registered")
        if (self.task, self.provider, self.execution) != (
            adapter.task,
            adapter.provider,
            adapter.execution,
        ):
            raise ValueError("adapter does not support the declared execution identity")
        for name, enabled, supported in (
            ("reasoning_efforts", bool(self.reasoning_efforts), adapter.reasoning),
            ("function_tools", self.function_tools, adapter.tools),
            ("streaming", self.streaming, adapter.streaming),
            ("structured_output", self.structured_output, adapter.structured_output),
            ("prompt_cache", self.prompt_cache, adapter.prompt_cache),
        ):
            if enabled and not supported:
                raise ValueError(f"{name} is not supported by the adapter")
        if (self.billing == "zero") != (self.execution in {"local", "stub"}):
            raise ValueError("local/stub execution must explicitly declare zero provider fee")
        if self.task == "llm" and self.max_output_tokens < 1:
            raise ValueError("LLM output limit must be positive")
        if self.task != "ocr" and self.context_window_tokens < 1:
            raise ValueError("text tasks require a positive input token limit")
        if self.task != "ocr" and self.ocr is not None:
            raise ValueError("OCR contract requires OCR task")
        if self.task == "ocr":
            if (
                self.schema_version != 4
                or self.ocr is None
                or self.local is not None
                or self.embedding is not None
                or self.deployment != "ocr"
                or self.inputs != ("image",)
                or self.service_tiers != ("standard",)
                or self.temperature != "never"
                or self.context_window_tokens != 0
                or self.max_output_tokens != 0
                or self.ocr.media_types != ("image/png", "image/jpeg")
            ):
                raise ValueError("OCR requires a schema-4 image execution contract")
        elif self.task == "embedding":
            spec = self.embedding
            if self.schema_version != 3 or spec is None or self.local is not None:
                raise ValueError("embedding requires a schema-3 embedding contract")
            if self.inputs != ("text",) or self.service_tiers != ("standard",):
                raise ValueError("embedding supports text and standard tier only")
            if self.max_output_tokens != 0 or self.temperature != "never":
                raise ValueError("embedding has no generated tokens or temperature")
            if self.adapter == "local-embeddings":
                if (
                    self.deployment != "embeddings"
                    or spec.variable_dimensions
                    or not spec.revision
                    or not spec.service_definition_digest
                    or spec.input_types != ("query", "document")
                    or spec.pooling == "provider"
                ):
                    raise ValueError("invalid local embedding identity")
            elif (
                spec.revision is not None
                or spec.service_definition_digest is not None
                or spec.input_types
                or spec.query_prefix
                or spec.pooling != "provider"
            ):
                raise ValueError("invalid OpenAI embedding identity")
        elif self.embedding is not None:
            raise ValueError("embedding contract requires embedding task")
        elif self.adapter == "ollama-native":
            if self.schema_version != 2 or self.local is None or self.deployment != "ollama":
                raise ValueError("ollama-native requires a schema-2 local deployment contract")
            if self.inputs != ("text",) or self.service_tiers != ("standard",):
                raise ValueError("ollama-native supports text and the standard tier only")
            if self.temperature != "always":
                raise ValueError("ollama-native requires explicit temperature support")
            if self.context_window_tokens > 4096 or self.max_output_tokens > 1024:
                raise ValueError("ollama-native limits exceed its execution contract")
            if self.max_output_tokens >= self.context_window_tokens:
                raise ValueError("local context must leave room for input")
        elif self.schema_version != 1 or self.local is not None:
            raise ValueError("local execution contract requires ollama-native")
        return self

    @property
    def definition_digest(self) -> str:
        value = json.dumps(
            self.model_dump(
                mode="json",
                include=set(_V1_FIELDS)
                | ({"local"} if self.schema_version == 2 else set())
                | ({"embedding"} if self.schema_version == 3 else set())
                | ({"ocr"} if self.schema_version == 4 else set()),
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(value.encode()).hexdigest()

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider, self.model)


class Rates(_Strict):
    input: NonNegativeUsd
    cached_input: NonNegativeUsd
    cache_write: NonNegativeUsd
    output: NonNegativeUsd


class LongContext(_Strict):
    threshold_input_tokens: int = Field(ge=1)
    input_multiplier: Decimal = Field(ge=1)
    output_multiplier: Decimal = Field(ge=1)


class PriceEntry(_Strict):
    provider: str
    model: str
    service_tier: Tier
    effective_from: date
    usd_per_million: Rates
    long_context: LongContext | None
    source_url: str = Field(min_length=1)


class _PricesFile(_Strict):
    prices: tuple[PriceEntry, ...]


class Catalogue:
    def __init__(self, models: tuple[ModelEntry, ...], prices: tuple[PriceEntry, ...]) -> None:
        self.models = {entry.alias: entry for entry in models}
        if len(self.models) != len(models):
            raise CatalogueError("Two catalogue entries share an alias.")
        self._prices = sorted(prices, key=lambda entry: entry.effective_from)
        self._check_prices()

    def _check_prices(self) -> None:
        seen: set[tuple[str, str, str, date]] = set()
        for entry in self._prices:
            key = (entry.provider, entry.model, entry.service_tier, entry.effective_from)
            if key in seen:
                raise CatalogueError(
                    f"Two prices for {entry.provider}/{entry.model} {entry.service_tier} "
                    f"start on {entry.effective_from}."
                )
            seen.add(key)
        for entry in self.models.values():
            if (
                entry.billing == "token"
                and self.price(entry.provider, entry.model, "standard", date.today()) is None
            ):
                raise CatalogueError(
                    f"Model {entry.provider}/{entry.model} has no standard price in effect."
                )

    def model(self, alias: str) -> ModelEntry | None:
        return self.models.get(alias)

    def aliases_for(self, provider: str, model: str) -> list[str]:
        return [entry.alias for entry in self.models.values() if entry.key == (provider, model)]

    def price(self, provider: str, model: str, tier: str, on: date) -> PriceEntry | None:
        """The latest entry that started on or before `on`, or None when unpriced."""
        applicable = None
        for entry in self._prices:
            if (
                entry.provider == provider
                and entry.model == model
                and entry.service_tier == tier
                and entry.effective_from <= on
            ):
                applicable = entry
        return applicable


# How each provider names the tiers it reports. OpenAI reports its standard tier
# as "default"; any name not listed here is kept as is and so stays unpriced.
_PROVIDER_TIER_NAMES: dict[str, dict[str, str]] = {
    "openai": {"default": "standard", "auto": "standard", "flex": "flex"},
}


def catalogue_tier(provider: str, reported: str) -> str:
    """The catalogue tier name for a tier as the provider reported it."""
    return _PROVIDER_TIER_NAMES.get(provider, {}).get(reported, reported)


def load_catalogue(models_path: Path, prices_path: Path) -> Catalogue:
    try:
        models = tuple(
            ModelEntry.model_validate(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(models_path.glob("*.json"))
        )
        if not models:
            raise CatalogueError("No model definitions found.")
        prices = _PricesFile.model_validate(json.loads(prices_path.read_text(encoding="utf-8")))
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(part) for part in error["loc"]) or "definition" for error in exc.errors()}
        )
        raise CatalogueError(f"Catalogue fields are invalid: {', '.join(fields)}") from None
    except (OSError, ValueError):
        raise CatalogueError("Catalogue data is invalid.") from None
    return Catalogue(models, prices.prices)


@lru_cache(maxsize=1)
def get_catalogue() -> Catalogue:
    """The packaged catalogue, loaded once per process."""
    return load_catalogue(DATA_DIR / "models", DATA_DIR / "prices.json")
