from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from llm_gateway.calls.admission import reservation_micro_usd
from llm_gateway.calls.records import CallRequest
from llm_gateway.catalogue.model import get_catalogue
from llm_gateway.catalogue.profiles import profile_errors
from llm_gateway.catalogue.schemas import request_schema
from llm_gateway.embeddings.models import EmbeddingRequest, resolve_embedding
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.projects.contract import ModelProfile, ProfileParameters, parse_contract
from tests.test_contract import contract_data

LOCAL = "local-minilm-l6-v2-v1"
PAID = "openai-text-embedding-3-small"


def request(**overrides):
    return EmbeddingRequest.model_validate(
        {
            "provider_call_id": "embed-1",
            "operation": "demo",
            "model": LOCAL,
            "input": "Synthetic text",
            **overrides,
        }
    )


def resolve(value: EmbeddingRequest):
    return resolve_embedding(value, parse_contract(contract_data()), get_catalogue(), "development")


def test_all_existing_definition_digests_are_preserved() -> None:
    expected = {
        "ollama-llama3.2-3b-instruct-q4km-v1": "91943d60f91443fb60b5972af98e5339421ef4b8cea1f95e01ae7345427e5df8",
        "ollama-qwen2.5-3b-instruct-q4km-v1": "25914016970432fd96c119a65d816395f1d8864da81a8a275bc74e7c4710d59e",
        "openai-gpt-6-luna": "51028abb0fc571315c17f742e70aef457b0d773ffc3bf610821efb789a2f379b",
        "stub-echo": "8852105cc8af375c6c7a75373bb42100698fea8a61b231f49616429b61925754",
    }
    assert {a: get_catalogue().models[a].definition_digest for a in expected} == expected


@pytest.mark.parametrize(
    "input", ["", "  ", [], ["good", ""], ["text"] * 65, [1, 2], "\ud800", "a" * 200001]
)
def test_invalid_input_rejected(input) -> None:
    with pytest.raises(ValidationError):
        request(input=input)


@pytest.mark.parametrize(
    "overrides",
    [
        {"stream": False},
        {"reasoning_effort": "none"},
        {"encoding_format": "base64"},
        {"dimensions": True},
        {"input_type": "other"},
    ],
)
def test_embedding_request_rejects_unsupported_fields(overrides) -> None:
    with pytest.raises(ValidationError):
        request(**overrides)


def test_task_schemas_defaults_and_effective_profile_settings() -> None:
    catalogue = get_catalogue()
    local = resolve(request())
    assert (local.dimensions, local.input_type) == (384, "document")
    assert "dimensions" not in request_schema(catalogue.models[LOCAL])["properties"]
    assert "input_type" not in request_schema(catalogue.models[PAID])["properties"]
    assert (
        profile_errors(
            "embedding",
            ModelProfile(model=LOCAL, parameters=ProfileParameters(input_type="query")),
            catalogue,
        )
        == []
    )
    assert (
        profile_errors(
            "embedding",
            ModelProfile(model=PAID, parameters=ProfileParameters(dimensions=256)),
            catalogue,
        )
        == []
    )
    assert profile_errors(
        "chat",
        ModelProfile(model="openai-gpt-6-luna", parameters=ProfileParameters(dimensions=256)),
        catalogue,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"dimensions": 384},
        {"dimensions": None},
        {"model": "openai-gpt-6-luna"},
        {"input": "\0" * 50000},
    ],
)
def test_local_semantic_refusals(overrides) -> None:
    with pytest.raises(CallRejectedError):
        resolve(request(**overrides))


def test_paid_safe_bounds_and_backend_reservation() -> None:
    value = request(model=PAID, input="é" * 4096, dimensions=256)
    resolved = resolve(value)
    assert resolved.input_token_upper_bound == 8192 and resolved.dimensions == 256
    with pytest.raises(CallRejectedError):
        resolve(request(model=PAID, input="é" * 4096 + "a"))
    backend = CallRequest(
        "alpha",
        "development",
        "bound",
        "hash",
        "demo",
        "openai",
        "text-embedding-3-small",
        "standard",
        2,
        0,
        task="embedding",
        input_token_upper_bound=100,
    )
    assert reservation_micro_usd(get_catalogue(), backend, date(2026, 10, 16)) == 2
