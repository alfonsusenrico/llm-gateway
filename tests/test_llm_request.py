from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from llm_gateway.catalogue.model import DATA_DIR, get_catalogue, load_catalogue
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.llm.request import ResponseRequest
from llm_gateway.llm.resolve import default_cache_key, resolve
from llm_gateway.projects.contract import parse_contract
from tests.test_contract import contract_data

CONTRACT = parse_contract(contract_data())


def _request(**overrides: Any) -> ResponseRequest:
    data: dict[str, Any] = {
        "provider_call_id": "call-1",
        "operation": "summarise",
        "profile": "chat",
        "input": "Hello",
    }
    data.update(overrides)
    return ResponseRequest.model_validate(data)


def _resolve(request: ResponseRequest, environment: str = "production", catalogue: Any = None):
    return resolve(
        request,
        project_id="personal_agent",
        environment=environment,
        contract=CONTRACT,
        catalogue=catalogue or get_catalogue(),
        default_max_output_tokens=4096,
    )


def _code(raised: pytest.ExceptionInfo[CallRejectedError]) -> str:
    return raised.value.code


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": {"provider": "openai", "model": "gpt-6-luna"}},  # both profile and model
        {"profile": None},  # neither
        {"text_format": {"type": "json_schema", "name": "x"}},  # schema missing
        {"text_format": {"type": "text", "name": "x"}},  # schema fields on text
        {"surprise": 1},
        {"provider_call_id": "has spaces"},
        {"operation": "Summarise"},
        {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "media_type": "image/png",
                            "data_base64": "not base64!!",
                        }
                    ],
                }
            ]
        },
    ],
)
def test_invalid_requests_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _request(**overrides)


def test_fingerprint_is_stable_and_sensitive() -> None:
    assert _request().fingerprint() == _request().fingerprint()
    assert _request().fingerprint() != _request(input="Hello!").fingerprint()


def test_profile_settings_resolve_with_request_overrides() -> None:
    resolved = _resolve(_request(max_output_tokens=300))

    assert (resolved.provider, resolved.model) == ("openai", "gpt-6-luna")
    assert (resolved.reasoning_effort, resolved.max_output_tokens) == ("low", 300)
    assert resolved.prompt_cache_key == "personal_agent:summarise"


def test_model_reference_uses_the_default_output_limit() -> None:
    resolved = _resolve(_request(profile=None, model={"provider": "openai", "model": "gpt-6-luna"}))

    assert resolved.max_output_tokens == 4096


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"profile": "missing"}, "profile_not_found"),
        (
            {"profile": None, "model": {"provider": "openai", "model": "gpt-9"}},
            "model_not_in_catalogue",
        ),
        (
            {"profile": None, "model": {"provider": "stub", "model": "stub-echo"}},
            "stub_not_allowed",
        ),
        ({"temperature": 0.5}, "invalid_model_settings"),  # luna with effort low
        ({"reasoning_effort": "turbo"}, "invalid_model_settings"),
        ({"stub": {"reply": "hi"}}, "request_invalid"),
    ],
)
def test_resolution_refusals(overrides: dict[str, Any], code: str) -> None:
    with pytest.raises(CallRejectedError) as raised:
        _resolve(_request(**overrides))

    assert _code(raised) == code


def test_temperature_with_effort_none_is_accepted() -> None:
    resolved = _resolve(_request(reasoning_effort="none", temperature=0.5))

    assert (resolved.reasoning_effort, resolved.temperature) == ("none", 0.5)


def test_stub_is_allowed_for_development_keys() -> None:
    resolved = _resolve(
        _request(profile=None, model={"provider": "stub", "model": "stub-echo"}),
        environment="development",
    )

    assert resolved.provider == "stub"


def test_input_type_the_model_does_not_accept(tmp_path: Path) -> None:
    models = json.loads((DATA_DIR / "models.json").read_text())
    models["models"][0]["inputs"] = ["text"]
    (tmp_path / "models.json").write_text(json.dumps(models))
    catalogue = load_catalogue(tmp_path / "models.json", DATA_DIR / "prices.json")
    image = {"type": "input_image", "media_type": "image/png", "data_base64": "aGVsbG8="}

    with pytest.raises(CallRejectedError) as raised:
        _resolve(_request(input=[{"role": "user", "content": [image]}]), catalogue=catalogue)

    assert _code(raised) == "input_not_supported"


def test_long_cache_keys_are_hashed_to_64_characters() -> None:
    key = default_cache_key("p" * 40, "o" * 40)

    assert len(key) == 64 and key.startswith("h:")
