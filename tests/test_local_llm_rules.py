from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from llm_gateway.catalogue.model import Catalogue, ModelEntry, get_catalogue
from llm_gateway.catalogue.schemas import request_schema
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.llm.local_rules import native_messages
from llm_gateway.llm.request import ResponseRequest
from llm_gateway.llm.resolve import resolve
from llm_gateway.projects.contract import parse_contract
from tests.conftest import make_settings
from tests.ollama_fakes import local_entry
from tests.test_contract import contract_data


def resolved(**overrides: Any):
    entry = local_entry()
    request = ResponseRequest.model_validate(
        {
            "provider_call_id": "local-1",
            "operation": "demo",
            "model": entry.alias,
            "input": "synthetic",
            **overrides,
        }
    )
    return resolve(
        request,
        project_id="alpha",
        environment="development",
        contract=parse_contract(contract_data()),
        catalogue=Catalogue((entry,), ()),
        default_max_output_tokens=4096,
    )


def test_existing_definition_digests_are_preserved_exactly() -> None:
    catalogue = get_catalogue()
    assert (
        catalogue.models["openai-gpt-6-luna"].definition_digest
        == "51028abb0fc571315c17f742e70aef457b0d773ffc3bf610821efb789a2f379b"
    )
    assert (
        catalogue.models["stub-echo"].definition_digest
        == "8852105cc8af375c6c7a75373bb42100698fea8a61b231f49616429b61925754"
    )
    entry = local_entry()
    assert entry.local is not None
    changed = entry.model_copy(
        update={"local": entry.local.model_copy(update={"artifact_digest": "b" * 64})}
    )
    assert changed.definition_digest != entry.definition_digest


def test_conservative_context_bound_counts_utf8_bytes_and_output_space() -> None:
    value = "é" * 1216  # 2432 bytes + 512 fixed + 128/message + 1024 output = 4096.
    assert resolved(input=value).max_output_tokens == 1024
    with pytest.raises(CallRejectedError, match="invalid_model_settings"):
        resolved(input=value + "a")


@pytest.mark.parametrize(
    "extra",
    [{"reasoning_effort": "none"}, {"prompt_cache_key": "x"}, {"parallel_tool_calls": False}],
)
def test_explicit_unsupported_fields_are_not_dropped(extra: dict[str, Any]) -> None:
    with pytest.raises((CallRejectedError, ValidationError)):
        resolved(**extra)


def test_local_messages_preserve_leading_instructions_and_text_parts() -> None:
    request = ResponseRequest.model_validate(
        {
            "provider_call_id": "msg-1",
            "operation": "demo",
            "model": "local-test",
            "instructions": "First",
            "input": [
                {"role": "developer", "content": "Second"},
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "A"},
                        {"type": "input_text", "text": "B"},
                    ],
                },
            ],
        }
    )
    assert native_messages(request) == [
        {"role": "system", "content": "First"},
        {"role": "system", "content": "Second"},
        {"role": "user", "content": "A\nB"},
    ]


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "assistant", "content": "prefix"}],
        [
            {"role": "user", "content": "one"},
            {"role": "developer", "content": "late"},
            {"role": "user", "content": "two"},
        ],
    ],
)
def test_unsupported_message_placement_is_explicitly_rejected(
    messages: list[dict[str, str]],
) -> None:
    with pytest.raises(CallRejectedError):
        resolved(input=messages)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "$ref": "https://untrusted.test/schema"},
        {"type": "object", "properties": {"x": {"type": "string", "pattern": "(a+)+$"}}},
        {"type": "array"},
    ],
)
def test_unsupported_output_schema_is_refused(schema: dict[str, Any]) -> None:
    with pytest.raises(CallRejectedError):
        resolved(
            text_format={"type": "json_schema", "name": "answer", "schema": schema, "strict": True}
        )


def test_schema_keyword_like_property_names_are_regular_instance_fields() -> None:
    assert resolved(
        text_format={
            "type": "json_schema",
            "name": "answer",
            "schema": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    )
    schema = request_schema(local_entry())
    assert schema["properties"]["temperature"]["default"] == 0
    assert "reasoning_effort" not in schema["properties"]
    assert schema["x-semantic-constraints"]["context_window_tokens"] == 4096


@pytest.mark.parametrize(
    "url",
    [
        "file:///secret",
        "http://user:secret@host",
        "http://host?token=secret",
        "http://host/#secret",
        "http://bad host",
        "http://host:99999",
    ],
)
def test_local_destination_rejects_secret_or_invalid_url_components(url: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(ollama_base_url=url)


def test_schema_one_cannot_acquire_a_local_contract() -> None:
    entry = local_entry().model_dump()
    entry["schema_version"] = 1
    with pytest.raises(ValidationError):
        ModelEntry.model_validate(entry)


def test_invalid_unicode_is_refused_before_provider_execution() -> None:
    with pytest.raises(CallRejectedError):
        resolved(input="\ud800")
