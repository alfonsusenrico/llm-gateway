"""The `POST /v1/responses` request: strict, provider-neutral, never passed through raw."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
    model_validator,
)

CALL_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,160}$"
TOOL_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
MAX_TOOLS = 32
_MAX_TOOL_SCHEMA_BYTES = 16_384
_MAX_ITEM_BYTES = 1_000_000
# Output item types a continuation may carry back; extend when the provider documents more.
CONTINUATION_ITEM_TYPES = frozenset({"reasoning", "message", "function_call"})
OPERATION_PATTERN = r"^[a-z][a-z0-9_]{1,63}$"
StubScenario = Literal[
    "rate_limited",
    "overloaded",
    "connection_failed",
    "timeout_after_send",
    "invalid_request",
    "incomplete",
    "invalid_json",
    "fail_mid_stream",
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


class FunctionCallOutput(_Strict):
    """The project's result for one tool call, sent back on the next turn."""

    type: Literal["function_call_output"]
    call_id: str = Field(min_length=1, max_length=256)
    output: str = Field(max_length=_MAX_ITEM_BYTES)


class Continuation(_Strict):
    """Provider output items from an earlier turn, returned exactly as received."""

    type: Literal["continuation"]
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=160)
    items: list[dict[str, Any]] = Field(min_length=1, max_length=1_000)

    @field_validator("items")
    @classmethod
    def _known_items(cls, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for item in items:
            if item.get("type") not in CONTINUATION_ITEM_TYPES:
                raise ValueError(f"items may only hold {sorted(CONTINUATION_ITEM_TYPES)}")
            if len(json.dumps(item, separators=(",", ":"))) > _MAX_ITEM_BYTES:
                raise ValueError("each item may be at most 1 MB")
        return items

    def function_call_ids(self) -> set[str]:
        return {
            str(item.get("call_id")) for item in self.items if item.get("type") == "function_call"
        }


def _input_item_kind(value: Any) -> str:
    # Messages carry no type, so anything without one is a message.
    if isinstance(value, dict):
        return str(value.get("type", "message"))
    return str(getattr(value, "type", "message"))


InputItem = Annotated[
    Annotated[Message, Tag("message")]
    | Annotated[FunctionCallOutput, Tag("function_call_output")]
    | Annotated[Continuation, Tag("continuation")],
    Discriminator(_input_item_kind),
]


class FunctionTool(_Strict):
    type: Literal["function"] = "function"
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str | None = Field(default=None, max_length=_MAX_TOOL_SCHEMA_BYTES)
    parameters: dict[str, Any]
    strict: bool = True

    @field_validator("parameters")
    @classmethod
    def _bounded_schema(cls, parameters: dict[str, Any]) -> dict[str, Any]:
        # The provider enforces strict-schema rules; the gateway bounds the size only.
        if len(json.dumps(parameters, separators=(",", ":"))) > _MAX_TOOL_SCHEMA_BYTES:
            raise ValueError("parameters may be at most 16 KB")
        return parameters


class NamedToolChoice(_Strict):
    type: Literal["function"]
    name: str = Field(pattern=TOOL_NAME_PATTERN)


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


class StubToolCall(_Strict):
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    # A string is sent as is, so invalid argument JSON can be tested.
    arguments: dict[str, Any] | str = Field(default_factory=dict)


class StubScript(_Strict):
    reply: str | dict[str, Any] | None = None
    scenario: StubScenario | None = None
    tool_calls: list[StubToolCall] | None = Field(default=None, min_length=1, max_length=MAX_TOOLS)


class ResponseRequest(_Strict):
    provider_call_id: str = Field(pattern=CALL_ID_PATTERN)
    operation: str = Field(pattern=OPERATION_PATTERN)
    profile: str | None = Field(default=None, min_length=1, max_length=40)
    model: ModelRef | None = None
    input: str | list[InputItem] = Field(min_length=1)
    instructions: str | None = Field(default=None, max_length=100_000)
    text_format: TextFormat = TextFormat()
    max_output_tokens: int | None = Field(default=None, ge=1, le=128_000)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=32)
    temperature: float | None = Field(default=None, ge=0, le=2)
    prompt_cache_key: str | None = Field(default=None, min_length=1, max_length=64)
    tools: list[FunctionTool] | None = Field(default=None, min_length=1, max_length=MAX_TOOLS)
    tool_choice: Literal["auto", "none", "required"] | NamedToolChoice | None = None
    parallel_tool_calls: bool | None = None
    stub: StubScript | None = None
    stream: bool = False

    @model_validator(mode="after")
    def _one_model_source(self) -> ResponseRequest:
        if (self.profile is None) == (self.model is None):
            raise ValueError("give exactly one of profile or model")
        if isinstance(self.input, list) and len(self.input) > 200:
            raise ValueError("input may hold at most 200 items")
        return self

    @model_validator(mode="after")
    def _consistent_tools(self) -> ResponseRequest:
        names = [tool.name for tool in self.tools or []]
        if len(set(names)) != len(names):
            raise ValueError("tool names must be unique")
        if self.tool_choice is not None and not names:
            raise ValueError("tool_choice needs tools")
        if isinstance(self.tool_choice, NamedToolChoice) and self.tool_choice.name not in names:
            raise ValueError(f"tool_choice names {self.tool_choice.name}, which is not in tools")
        if self.parallel_tool_calls is not None and not names:
            raise ValueError("parallel_tool_calls needs tools")
        return self

    @model_validator(mode="after")
    def _outputs_answer_known_calls(self) -> ResponseRequest:
        # With no stored conversation, every output's call must travel in a continuation.
        if isinstance(self.input, str):
            return self
        known: set[str] = set()
        for item in self.input:
            if isinstance(item, Continuation):
                known |= item.function_call_ids()
            elif isinstance(item, FunctionCallOutput) and item.call_id not in known:
                raise ValueError(
                    f"function_call_output {item.call_id} must follow the continuation "
                    "holding its call"
                )
        return self

    def messages(self) -> list[Message]:
        if isinstance(self.input, str):
            return []
        return [item for item in self.input if isinstance(item, Message)]

    def continuations(self) -> list[Continuation]:
        if isinstance(self.input, str):
            return []
        return [item for item in self.input if isinstance(item, Continuation)]

    def input_types(self) -> set[str]:
        types = {"text"}
        for message in self.messages():
            if isinstance(message.content, list):
                for part in message.content:
                    if isinstance(part, ImagePart):
                        types.add("image")
                    elif isinstance(part, FilePart):
                        types.add("file")
        return types

    def fingerprint(self) -> str:
        """SHA-256 of the canonical request, to tell a resubmission from a different call.

        `stream` is left out: a call ID may be replayed streamed or not.
        """
        canonical = json.dumps(
            self.model_dump(mode="json", exclude_none=True, by_alias=True, exclude={"stream"}),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
