"""The effective direct-call schema is both published and executed."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator

from llm_gateway.catalogue.model import ModelEntry


def request_schema(entry: ModelEntry) -> dict[str, Any]:
    return deepcopy(_schema(entry.model_dump_json()))


@lru_cache(maxsize=256)
def _schema(definition: str) -> dict[str, Any]:
    from llm_gateway.llm.request import ResponseRequest

    entry = ModelEntry.model_validate_json(definition)
    if entry.task == "embedding":
        from llm_gateway.embeddings.models import embedding_schema

        schema = embedding_schema(entry)
        Draft202012Validator.check_schema(schema)
        return schema
    schema = deepcopy(ResponseRequest.model_json_schema(by_alias=True))
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    properties = schema["properties"]
    properties.pop("profile")
    properties["model"] = {"type": "string", "const": entry.alias}
    schema["required"] = [*schema.get("required", []), "model"]
    properties["max_output_tokens"] = {
        "type": "integer",
        "minimum": 1,
        "maximum": entry.max_output_tokens,
    }
    if not entry.reasoning_efforts:
        properties.pop("reasoning_effort")
    else:
        properties["reasoning_effort"] = {"type": "string", "enum": list(entry.reasoning_efforts)}
        if entry.default_reasoning_effort is not None:
            properties["reasoning_effort"]["default"] = entry.default_reasoning_effort
    if entry.temperature == "never":
        properties.pop("temperature")
    elif entry.temperature == "only_with_effort_none":
        schema["allOf"] = [
            {
                "if": {"required": ["temperature"]},
                "then": {
                    "properties": {"reasoning_effort": {"const": "none"}},
                    "required": ["reasoning_effort"],
                },
            }
        ]
    if not entry.function_tools:
        for name in ("tools", "tool_choice", "parallel_tool_calls"):
            properties.pop(name)
    if not entry.streaming:
        properties.pop("stream")
    if not entry.prompt_cache:
        properties.pop("prompt_cache_key")
    if not entry.structured_output:
        properties["text_format"] = {
            "type": "object",
            "properties": {"type": {"const": "text"}},
            "additionalProperties": False,
        }
    if entry.execution != "stub":
        properties.pop("stub")
    schema["description"] = (
        "Direct alias request after documented defaults are applied. Tool names must be "
        "unique; tool choice must name a declared function; continuation outputs must "
        "follow their corresponding call IDs. JSON Schema is followed by these semantic checks."
    )
    if entry.local is not None:
        schema["x-semantic-constraints"] = {
            "messages": "Developer messages must lead; final message must be user. Text parts join with newlines.",
            "context": "UTF-8 content bytes + fixed allowance + per-message allowance + maximum output tokens must fit context.",
            "prompt_fixed_bytes": entry.local.prompt_fixed_bytes,
            "prompt_message_bytes": entry.local.prompt_message_bytes,
            "context_window_tokens": entry.context_window_tokens,
            "output_schema": "Object schema, at most 16 KiB and depth 32; no references or regex keywords.",
        }
        properties["temperature"]["default"] = entry.local.default_temperature
    content = schema["$defs"]["Message"]["properties"]["content"]["anyOf"]
    parts = content[1]["items"]
    allowed_parts = {"input_text": "text", "input_image": "image", "input_file": "file"}
    allowed_refs = {
        ref
        for kind, ref in parts["discriminator"]["mapping"].items()
        if allowed_parts[kind] in entry.inputs
    }
    parts["oneOf"] = [variant for variant in parts["oneOf"] if variant["$ref"] in allowed_refs]
    parts["discriminator"]["mapping"] = {
        kind: ref for kind, ref in parts["discriminator"]["mapping"].items() if ref in allowed_refs
    }
    if "text" not in entry.inputs:
        content.pop(0)
        # No supported part variants means an invalid definition, rather than an open schema.
    if not entry.function_tools:
        variants = properties["input"]["anyOf"][1]["items"]["oneOf"]
        variants[:] = [variant for variant in variants if variant["$ref"].endswith("/Message")]
    Draft202012Validator.check_schema(schema)
    return schema


@lru_cache(maxsize=256)
def _validator(definition: str) -> Draft202012Validator:
    return Draft202012Validator(_schema(definition))


def setting_errors(entry: ModelEntry, payload: dict[str, Any]) -> list[str]:
    """Never return validator prose: it can quote credentials or prompt content."""
    return sorted(
        {
            (".".join(str(part) for part in error.absolute_path) or "settings")
            + ": "
            + str(error.validator)
            for error in _validator(entry.model_dump_json()).iter_errors(payload)
        }
    )
