from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2
import pytest

from llm_gateway.catalogue.model import Catalogue
from llm_gateway.llm.ollama_adapter import OllamaAdapter
from llm_gateway.llm.providers import ProviderFailure, ProviderSuccess, StreamStarted, StreamText
from llm_gateway.llm.request import ResponseRequest
from llm_gateway.llm.resolve import resolve
from llm_gateway.projects.contract import parse_contract
from tests.ollama_fakes import LocalService, local_entry
from tests.test_contract import contract_data

pytestmark = pytest.mark.anyio


def prepared(**overrides: Any):
    entry = local_entry()
    request = ResponseRequest.model_validate(
        {
            "provider_call_id": "local-1",
            "operation": "demo",
            "model": entry.alias,
            "input": "A synthetic question",
            **overrides,
        }
    )
    resolved = resolve(
        request,
        project_id="alpha",
        environment="development",
        contract=parse_contract(contract_data()),
        catalogue=Catalogue((entry,), ()),
        default_max_output_tokens=4096,
    )
    service = LocalService(entry)
    adapter = OllamaAdapter("http://local.test", 10, httpx2.MockTransport(service))
    return request, resolved, service, adapter


async def test_native_translation_and_no_credentials() -> None:
    request, resolved, service, adapter = prepared(
        instructions="Synthetic instruction", temperature=0.2, max_output_tokens=32
    )
    result = await adapter.execute(resolved, request)
    assert isinstance(result, ProviderSuccess)
    assert result.output_text == "Local answer."
    assert (result.usage.input_tokens, result.usage.output_tokens, result.service_tier) == (
        7,
        3,
        "standard",
    )
    generation = service.requests[-1]
    assert "authorization" not in generation.headers
    assert json.loads(generation.content) == {
        "model": resolved.model,
        "messages": [
            {"role": "system", "content": "Synthetic instruction"},
            {"role": "user", "content": "A synthetic question"},
        ],
        "stream": False,
        "options": {"num_ctx": 4096, "num_predict": 32, "temperature": 0.2},
    }


@pytest.mark.parametrize(
    "mode,category",
    [
        ("service_busy", "temporary_overload"),
        ("gpu_unavailable", "temporary_overload"),
        ("gpu_model_load_failed", "temporary_overload"),
        ("connect", "connection_not_established"),
        ("gpu_execution_unverified", "execution_unknown"),
        ("raw_503", "execution_unknown"),
        ("read_timeout", "execution_unknown"),
        ("malformed", "execution_unknown"),
        ("oversize", "execution_unknown"),
        ("missing_usage", "execution_unknown"),
        ("redirect", "execution_unknown"),
    ],
)
async def test_failure_certainty_is_not_inferred_from_server_status(
    mode: str, category: str
) -> None:
    request, resolved, service, adapter = prepared()
    service.mode = mode
    result = await adapter.execute(resolved, request)
    assert isinstance(result, ProviderFailure) and result.category == category
    assert "PRIVATE_PROVIDER_TEXT" not in repr(result)
    assert service.generations == 1


@pytest.mark.parametrize("mismatch", ["gpu", "version", "model", "template"])
async def test_metadata_mismatch_sends_no_inference_content(mismatch: str) -> None:
    request, resolved, service, adapter = prepared()
    service.mismatch = mismatch
    result = await adapter.execute(resolved, request)
    assert isinstance(result, ProviderFailure) and result.category == "configuration_mismatch"
    assert service.generations == 0
    assert isinstance(request.input, str)
    assert all(request.input.encode() not in sent.content for sent in service.requests)


async def test_stream_translates_chunks_and_closes_native_response() -> None:
    request, resolved, service, adapter = prepared()
    events = [event async for event in adapter.execute_stream(resolved, request)]
    assert isinstance(events[0], StreamStarted)
    assert [event.delta for event in events if isinstance(event, StreamText)] == [
        "Local ",
        "answer.",
    ]
    assert isinstance(events[-1], ProviderSuccess) and events[-1].output_text == "Local answer."
    assert service.streams[0].closed


@pytest.mark.parametrize("mode", ["no_terminal", "stream_break", "malformed_stream"])
async def test_incomplete_stream_is_unknown_and_closed(mode: str) -> None:
    request, resolved, service, adapter = prepared()
    service.mode = mode
    events = [event async for event in adapter.execute_stream(resolved, request)]
    assert isinstance(events[-1], ProviderFailure) and events[-1].category == "execution_unknown"
    assert service.streams[0].closed


async def test_direct_and_stream_have_complete_attempt_deadlines() -> None:
    request, resolved, service, _adapter = prepared()
    service.delay = 0.2
    adapter = OllamaAdapter("http://local.test", 0.05, httpx2.MockTransport(service))
    result = await adapter.execute(resolved, request)
    assert isinstance(result, ProviderFailure) and result.category == "execution_unknown"
    events = [event async for event in adapter.execute_stream(resolved, request)]
    assert isinstance(events[-1], ProviderFailure) and events[-1].category == "execution_unknown"
    assert service.streams[-1].closed


async def test_stream_deadline_never_cancels_the_consumer_outside_network_read() -> None:
    request, resolved, service, _adapter = prepared()
    adapter = OllamaAdapter("http://local.test", 0.05, httpx2.MockTransport(service))
    stream = adapter.execute_stream(resolved, request)
    assert isinstance(await anext(stream), StreamStarted)
    # A deadline context spanning yield would cancel this consumer task here.
    await asyncio.sleep(0.08)
    assert isinstance(await anext(stream), ProviderFailure)
    await stream.aclose()


async def test_early_producer_close_releases_the_native_client() -> None:
    request, resolved, service, adapter = prepared()
    stream = adapter.execute_stream(resolved, request)
    assert isinstance(await anext(stream), StreamStarted)
    assert isinstance(await anext(stream), StreamText)
    await stream.aclose()
    assert service.streams[0].closed


async def test_output_limit_reports_incomplete_without_retry() -> None:
    request, resolved, service, adapter = prepared()
    service.mode = "incomplete"
    result = await adapter.execute(resolved, request)
    assert isinstance(result, ProviderSuccess)
    assert (result.status, result.incomplete_reason) == ("incomplete", "max_output_tokens")


@pytest.mark.parametrize("stream", [False, True])
async def test_invalid_utf8_native_output_is_unknown(stream: bool) -> None:
    request, resolved, service, _adapter = prepared()
    service.output = "\ud800"

    async def handler(outgoing: httpx2.Request) -> httpx2.Response:
        if outgoing.url.path == "/api/chat":
            return httpx2.Response(200, content=json.dumps(service.success()).encode() + b"\n")
        return await service(outgoing)

    adapter = OllamaAdapter("http://local.test", 10, httpx2.MockTransport(handler))
    if stream:
        events = [event async for event in adapter.execute_stream(resolved, request)]
        assert not any(isinstance(event, StreamText) for event in events)
        result = events[-1]
    else:
        result = await adapter.execute(resolved, request)
    assert isinstance(result, ProviderFailure) and result.category == "execution_unknown"
