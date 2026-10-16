"""Reviewed local prompt semantics and conservative bounds, before admission."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

from llm_gateway.catalogue.model import ModelEntry
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.llm.request import Message, ResponseRequest, TextPart


def native_messages(request: ResponseRequest) -> list[dict[str, str]]:
    messages = []
    if request.instructions is not None:
        messages.append({"role": "system", "content": request.instructions})
    if isinstance(request.input, str):
        return [*messages, {"role": "user", "content": request.input}]
    seen_conversation = False
    for item in request.input:
        if not isinstance(item, Message):
            raise CallRejectedError("input_not_supported")
        if item.role == "developer" and seen_conversation:
            raise CallRejectedError(
                "invalid_model_settings",
                detail="Local developer messages must precede conversation messages.",
            )
        seen_conversation |= item.role != "developer"
        if isinstance(item.content, str):
            content = item.content
        else:
            if any(not isinstance(part, TextPart) for part in item.content):
                raise CallRejectedError("input_not_supported")
            content = "\n".join(part.text for part in item.content if isinstance(part, TextPart))
        messages.append(
            {"role": "system" if item.role == "developer" else item.role, "content": content}
        )
    if not messages or messages[-1]["role"] != "user":
        raise CallRejectedError(
            "invalid_model_settings", detail="Local input must end with a user message."
        )
    return messages


def _schema_is_bounded(value: Any, depth: int = 0) -> bool:
    if depth > 32:
        return False
    if isinstance(value, dict):
        # External resolution, recursive references, and regex evaluation are not
        # part of this initial local schema contract.
        if {"$ref", "$dynamicRef", "$recursiveRef", "pattern", "patternProperties"} & value.keys():
            return False
        for keyword, item in value.items():
            if keyword in {"properties", "$defs", "definitions", "dependentSchemas"}:
                if isinstance(item, dict) and not all(
                    _schema_is_bounded(child, depth + 1) for child in item.values()
                ):
                    return False
            elif keyword in {"enum", "const", "default", "examples"}:
                continue  # Instance data can have keys resembling schema keywords.
            elif not _schema_is_bounded(item, depth + 1):
                return False
        return True
    if isinstance(value, list):
        return all(_schema_is_bounded(item, depth + 1) for item in value)
    return True


def validate_local_request(
    entry: ModelEntry, request: ResponseRequest, max_output_tokens: int
) -> None:
    if entry.local is None:
        return
    messages = native_messages(request)
    # Each token in the reviewed byte-level tokenizers occupies at least one
    # byte of rendered input. Template/system/join allowances are deliberately
    # larger than the pinned, text-only templates; output space is reserved too.
    try:
        upper = entry.local.prompt_fixed_bytes + sum(
            len(message["content"].encode("utf-8")) + entry.local.prompt_message_bytes
            for message in messages
        )
    except UnicodeError:
        raise CallRejectedError(
            "invalid_model_settings", detail="Local input must be valid UTF-8."
        ) from None
    if upper + max_output_tokens > entry.context_window_tokens:
        raise CallRejectedError(
            "invalid_model_settings", detail="Input exceeds the model's safe context bound."
        )
    if request.text_format.type == "json_schema":
        schema = request.text_format.json_schema
        try:
            encoded = json.dumps(schema, allow_nan=False, ensure_ascii=False).encode()
            if (
                not isinstance(schema, dict)
                or schema.get("type") != "object"
                or len(encoded) > 16_384
                or not _schema_is_bounded(schema)
            ):
                raise ValueError
            Draft202012Validator.check_schema(schema)
        except (ValueError, SchemaError, RecursionError):
            raise CallRejectedError(
                "invalid_model_settings", detail="Local output schema is unsupported or invalid."
            ) from None
