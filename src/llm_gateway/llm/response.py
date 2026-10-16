"""Public LLM result schema used by the API's generated OpenAPI contract."""

from typing import Any, Literal

from pydantic import BaseModel


class UsageOut(BaseModel):
    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None


class ToolCallOut(BaseModel):
    call_id: str
    name: str
    arguments: str
    arguments_json: dict[str, Any] | None
    arguments_error: str | None


class CallResultOut(BaseModel):
    provider_call_id: str
    status: Literal["completed", "incomplete"]
    provider: str
    model: str
    model_alias: str
    definition_digest: str
    service_tier: str | None
    output_text: str | None
    output_json: dict[str, Any] | None
    refusal: str | None
    incomplete_reason: str | None
    structured_output_error: str | None
    tool_calls: list[ToolCallOut]
    continuation: dict[str, Any] | None
    usage: UsageOut
    cost_usd: str | None
    provider_response_id: str | None
    replayed: bool
