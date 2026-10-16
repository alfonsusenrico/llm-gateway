from __future__ import annotations

import json

import httpx2
import pytest
from pydantic import SecretStr

from llm_gateway.catalogue.model import get_catalogue
from llm_gateway.embeddings.adapter import EmbeddingAdapter, EmbeddingSuccess
from llm_gateway.embeddings.models import EmbeddingRequest, ResolvedEmbedding
from llm_gateway.llm.providers import ProviderFailure
from tests.embedding_fakes import EmbeddingService, local_model

pytestmark = pytest.mark.anyio


def prepared(local: bool = True):
    entry, metadata = local_model()
    if not local:
        entry = get_catalogue().models["openai-text-embedding-3-small"]
    assert entry.embedding is not None
    request = EmbeddingRequest(
        provider_call_id="embed-1",
        operation="demo",
        model=entry.alias,
        input=["first text", "second text"],
    )
    resolved = ResolvedEmbedding(entry, 384 if local else 3, "query" if local else None, 21)
    service = EmbeddingService(metadata if local else None)
    adapter = EmbeddingAdapter(
        "http://synthetic.test",
        10,
        local=local,
        secret=None if local else SecretStr("test-account"),
        transport=httpx2.MockTransport(service),
    )
    return request, resolved, service, adapter


@pytest.mark.parametrize("local", [False, True])
async def test_native_embedding_translation_and_order(local: bool) -> None:
    request, resolved, service, adapter = prepared(local)
    service.mode = "reversed"
    result = await adapter.execute(resolved, request)
    assert isinstance(result, EmbeddingSuccess)
    assert [v.index for v in result.vectors] == [0, 1]
    assert result.usage.input_tokens == 4 and result.usage.output_tokens == 0
    sent = service.requests[-1]
    body = json.loads(sent.content)
    assert body == {
        "model": resolved.entry.model,
        "input": request.input,
        "encoding_format": "float",
        **({"input_type": "query"} if local else {"dimensions": 3}),
    }
    assert (
        ("authorization" not in sent.headers)
        if local
        else sent.headers["authorization"] == "Bearer test-account"
    )
    assert "store" not in body
    assert len(service.requests) == (3 if local else 1)


@pytest.mark.parametrize(
    "mode",
    [
        "missing_usage",
        "negative_usage",
        "usage_over_bound",
        "wrong_model",
        "bad_revision",
        "bad_dimensions",
        "nan",
        "bool",
        "zero",
        "index",
        "duplicate",
        "malformed",
        "raw_503",
        "timeout",
    ],
)
async def test_invalid_or_ambiguous_native_output_is_unknown(mode: str) -> None:
    request, resolved, service, adapter = prepared()
    service.mode = mode
    result = await adapter.execute(resolved, request)
    assert isinstance(result, ProviderFailure) and result.category == "execution_unknown"
    assert "PRIVATE_PROVIDER_TEXT" not in repr(result)
    assert service.generations == 1


@pytest.mark.parametrize(
    "mode,category",
    [
        ("gpu_unavailable", "temporary_overload"),
        ("service_busy", "temporary_overload"),
        ("input_token_limit_exceeded", "invalid_request"),
        ("connect", "connection_not_established"),
        ("metadata_mismatch", "configuration_mismatch"),
    ],
)
async def test_confirmed_refusals_and_configuration(mode: str, category: str) -> None:
    request, resolved, service, adapter = prepared()
    service.mode = mode
    result = await adapter.execute(resolved, request)
    assert isinstance(result, ProviderFailure) and result.category == category
    if mode == "metadata_mismatch":
        assert service.generations == 0
        assert all(b"first text" not in sent.content for sent in service.requests)


@pytest.mark.parametrize(
    "mode,category",
    [
        ("insufficient_quota", "account_blocked"),
        ("rate_limit", "temporary_throttle"),
        ("raw_503", "execution_unknown"),
        ("missing_usage", "execution_unknown"),
    ],
)
async def test_openai_failures_never_invent_usage_or_retry_servers(
    mode: str, category: str
) -> None:
    request, resolved, service, adapter = prepared(False)
    service.mode = mode
    result = await adapter.execute(resolved, request)
    assert isinstance(result, ProviderFailure) and result.category == category
    assert result.usage is None and service.generations == 1


async def test_complete_attempt_timeout() -> None:
    request, resolved, service, _ = prepared()
    service.delay = 0.2
    adapter = EmbeddingAdapter(
        "http://synthetic.test", 0.03, local=True, transport=httpx2.MockTransport(service)
    )
    result = await adapter.execute(resolved, request)
    assert isinstance(result, ProviderFailure) and result.category == "execution_unknown"


@pytest.mark.parametrize("metadata", [False, True])
async def test_bounded_native_response_before_or_after_submission(metadata: bool) -> None:
    request, resolved, service, _ = prepared()

    async def handler(outgoing: httpx2.Request) -> httpx2.Response:
        target = "/v1/models" if metadata else "/v1/embeddings"
        if outgoing.url.path == target:
            return httpx2.Response(200, content=b"x" * (256001 if metadata else 8000001))
        return await service(outgoing)

    adapter = EmbeddingAdapter(
        "http://synthetic.test", 10, local=True, transport=httpx2.MockTransport(handler)
    )
    result = await adapter.execute(resolved, request)
    assert isinstance(result, ProviderFailure)
    assert result.category == ("temporary_overload" if metadata else "execution_unknown")


async def test_changed_artifact_metadata_is_rejected_before_content() -> None:
    request, resolved, service, adapter = prepared()
    assert service.metadata is not None
    service.metadata["artifact_files"]["model"] = "b" * 64
    result = await adapter.execute(resolved, request)
    assert isinstance(result, ProviderFailure) and result.category == "configuration_mismatch"
    assert service.generations == 0
