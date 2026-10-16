"""Function tools and continuations: request rules, provider mapping, and tool loops."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from llm_gateway.catalogue.model import DATA_DIR, load_catalogue
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.llm.openai_adapter import build_request
from llm_gateway.llm.providers import FunctionCall, ProviderSuccess, StreamStarted
from llm_gateway.llm.request import ResponseRequest
from llm_gateway.llm.stub_adapter import StubAdapter
from tests.test_catalogue import _base, _files
from tests.test_llm_request import _resolve
from tests.test_openai_adapter import RESOLVED, _adapter, _success_body
from tests.test_openai_adapter import _request as _luna_request
from tests.test_responses_api import Calls, calls_context
from tests.test_streaming import _adapter as _stream_adapter
from tests.test_streaming import _created, _events, _sse, _terminal

pytestmark = pytest.mark.anyio
EVENTS_TOOL = {
    "type": "function",
    "name": "get_events",
    "description": "Calendar events for one day.",
    "parameters": {
        "type": "object",
        "properties": {"day": {"type": "string"}},
        "required": ["day"],
        "additionalProperties": False,
    },
}
REASONING = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAA-secret"}
CALL_ITEM = {
    "type": "function_call",
    "id": "fc_1",
    "call_id": "call_1",
    "name": "get_events",
    "arguments": '{"day":"today"}',
    "status": "completed",
}
LUNA_CONTINUATION = {
    "type": "continuation",
    "provider": "openai",
    "model": "gpt-6-luna",
    "model_alias": RESOLVED.entry.alias,
    "definition_digest": RESOLVED.entry.definition_digest,
    "items": [REASONING, CALL_ITEM],
}


def _with_tools(**overrides: Any) -> ResponseRequest:
    return _luna_request(tools=[EVENTS_TOOL], **overrides)


# Request rules


@pytest.mark.parametrize(
    "overrides",
    [
        {"tool_choice": {"type": "function", "name": "send_mail"}},  # not declared
        {"tools": [EVENTS_TOOL, EVENTS_TOOL]},  # duplicate names
        {"tools": [{**EVENTS_TOOL, "name": "has spaces"}]},
        {"tools": [{**EVENTS_TOOL, "name": f"t{n}"} for n in range(33)]},
        {"tools": [{**EVENTS_TOOL, "parameters": {"description": "x" * 17_000}}]},
        {"tools": None, "tool_choice": "required"},
        {"tools": None, "parallel_tool_calls": False},
        {
            "input": [
                {
                    "type": "continuation",
                    **{k: v for k, v in LUNA_CONTINUATION.items() if k != "type"},
                    "items": [{"type": "web_search_call"}],
                }
            ]
        },
        {"input": [{"type": "function_call_output", "call_id": "call_1", "output": "[]"}]},
        {
            "input": [
                {"type": "function_call_output", "call_id": "call_1", "output": "[]"},
                LUNA_CONTINUATION,
            ]
        },
        {"input": [{"type": "surprise"}]},
    ],
)
def test_invalid_tool_requests_are_rejected(overrides: dict[str, Any]) -> None:
    base: dict[str, Any] = {"tools": [EVENTS_TOOL]}
    base.update(overrides)
    with pytest.raises(ValidationError):
        _luna_request(**{k: v for k, v in base.items() if v is not None})


def test_a_second_turn_request_is_accepted() -> None:
    request = _with_tools(
        input=[
            {"role": "user", "content": "What is on today?"},
            LUNA_CONTINUATION,
            {"type": "function_call_output", "call_id": "call_1", "output": "[]"},
        ],
        tool_choice="auto",
        parallel_tool_calls=False,
    )

    assert [type(item).__name__ for item in request.input] == [
        "Message",
        "Continuation",
        "FunctionCallOutput",
    ]
    assert request.input_types() == {"text"}


def test_tools_change_the_fingerprint() -> None:
    assert _with_tools().fingerprint() != _luna_request().fingerprint()


# Resolution


def test_model_without_function_tools_refuses_tools(tmp_path: Path) -> None:
    models = _base("models.json")
    next(m for m in models["models"] if m["alias"] == "openai-gpt-6-luna")["function_tools"] = False
    catalogue = load_catalogue(*_files(tmp_path, models=models))

    with pytest.raises(CallRejectedError) as raised:
        _resolve(_with_tools(), catalogue=catalogue)

    assert raised.value.code == "input_not_supported"


def test_continuation_from_another_model_is_refused() -> None:
    stub_continuation = {**LUNA_CONTINUATION, "provider": "stub", "model": "stub-echo"}
    request = _with_tools(
        input=[
            stub_continuation,
            {"type": "function_call_output", "call_id": "call_1", "output": "[]"},
        ]
    )

    with pytest.raises(CallRejectedError) as raised:
        _resolve(request)

    assert raised.value.code == "continuation_mismatch"


def test_packaged_models_declare_function_tool_support() -> None:
    catalogue = load_catalogue(DATA_DIR / "models", DATA_DIR / "prices.json")

    assert {alias: entry.function_tools for alias, entry in catalogue.models.items()} == {
        "openai-gpt-6-luna": True,
        "stub-echo": True,
        "ollama-qwen2.5-3b-instruct-q4km-v1": False,
        "ollama-llama3.2-3b-instruct-q4km-v1": False,
    }


# OpenAI mapping


def test_tools_and_second_turn_input_map_to_the_provider() -> None:
    request = _with_tools(
        input=[
            {"role": "user", "content": "What is on today?"},
            LUNA_CONTINUATION,
            {"type": "function_call_output", "call_id": "call_1", "output": '["standup"]'},
        ],
        tool_choice={"type": "function", "name": "get_events"},
        parallel_tool_calls=False,
    )

    body = build_request(RESOLVED, request)

    assert body["tools"] == [{**EVENTS_TOOL, "strict": True}]
    assert body["tool_choice"] == {"type": "function", "name": "get_events"}
    assert body["parallel_tool_calls"] is False
    assert body["input"] == [
        {"role": "user", "content": "What is on today?"},
        REASONING,  # untouched, encrypted content included
        CALL_ITEM,
        {"type": "function_call_output", "call_id": "call_1", "output": '["standup"]'},
    ]
    assert body["store"] is False


def test_plain_calls_send_no_tool_fields() -> None:
    body = build_request(RESOLVED, _luna_request())

    assert not {"tools", "tool_choice", "parallel_tool_calls"} & body.keys()


async def test_tool_calls_and_output_items_are_extracted() -> None:
    output = [REASONING, CALL_ITEM, {**CALL_ITEM, "call_id": "call_2", "arguments": "{oops"}]

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_success_body(output=output))

    result = await _adapter(handler).execute(RESOLVED, _with_tools())

    assert isinstance(result, ProviderSuccess)
    assert result.output_text == ""
    assert result.function_calls == (
        FunctionCall("call_1", "get_events", '{"day":"today"}'),
        FunctionCall("call_2", "get_events", "{oops"),
    )
    assert list(result.output_items) == output


async def test_stream_announces_calls_and_keeps_completed_reasoning() -> None:
    # Reasoning content may be incomplete outside `output_item.done`.
    done_events = [
        {"type": "response.output_item.done", "sequence_number": 1, "output_index": 0,
         "item": REASONING},
        {"type": "response.output_item.done", "sequence_number": 2, "output_index": 1,
         "item": CALL_ITEM},
    ]  # fmt: skip
    partial_reasoning = {**REASONING, "encrypted_content": "gAAA-partial"}
    events = [
        event
        async for event in _stream_adapter(
            _sse(_created(), *done_events, _terminal(output=[partial_reasoning, CALL_ITEM]))
        ).execute_stream(RESOLVED, _with_tools())
    ]

    assert isinstance(events[0], StreamStarted)
    assert events[1] == FunctionCall("call_1", "get_events", '{"day":"today"}')
    final = events[-1]
    assert isinstance(final, ProviderSuccess)
    assert list(final.output_items) == [REASONING, CALL_ITEM]


# Stub


def _stub_request(**overrides: Any) -> ResponseRequest:
    data: dict[str, Any] = {
        "provider_call_id": "s",
        "operation": "demo",
        "model": "stub-echo",
        "input": "What is on today?",
        "tools": [EVENTS_TOOL],
    }
    data.update(overrides)
    return ResponseRequest.model_validate(data)


async def test_stub_scripts_calls_and_answers_function_outputs() -> None:
    first = await StubAdapter().execute(
        RESOLVED, _stub_request(stub={"tool_calls": [{"name": "get_events", "arguments": {"day": "today"}}]})
    )  # fmt: skip
    assert isinstance(first, ProviderSuccess)
    second = await StubAdapter().execute(
        RESOLVED,
        _stub_request(
            input=[
                {"type": "continuation", "provider": "stub", "model": "stub-echo",
                 "model_alias": "stub-echo", "definition_digest": RESOLVED.entry.definition_digest,
                 "items": list(first.output_items)},
                {"type": "function_call_output", "call_id": "stub_call_1", "output": "standup"},
            ]
        ),
    )  # fmt: skip

    assert first.function_calls == (FunctionCall("stub_call_1", "get_events", '{"day": "today"}'),)
    assert [item["type"] for item in first.output_items] == ["reasoning", "function_call"]
    assert isinstance(second, ProviderSuccess)
    assert (second.output_text, second.function_calls) == ("stub: standup", ())


# Through the API


@pytest.fixture
async def calls(migrated_pool: AsyncConnectionPool) -> AsyncIterator[Calls]:
    async with calls_context(migrated_pool) as context:
        yield context


def _scripted(*arguments: dict[str, Any] | str) -> dict[str, Any]:
    return {"tool_calls": [{"name": "get_events", "arguments": a} for a in arguments]}


@pytest.mark.db
async def test_two_turn_stub_loop(calls: Calls) -> None:
    first = (
        await calls.post(provider_call_id="t1", tools=[EVENTS_TOOL], stub=_scripted({"day": "today"}))
    ).json()  # fmt: skip
    call = first["tool_calls"][0]
    second = (
        await calls.post(
            provider_call_id="t2",
            tools=[EVENTS_TOOL],
            input=[
                {"type": "continuation", **first["continuation"]},
                {"type": "function_call_output", "call_id": call["call_id"], "output": "standup"},
            ],
        )
    ).json()

    assert call == {
        "call_id": "stub_call_1",
        "name": "get_events",
        "arguments": '{"day": "today"}',
        "arguments_json": {"day": "today"},
        "arguments_error": None,
    }
    assert first["output_text"] == ""
    assert first["continuation"]["provider"] == "stub"
    assert second["output_text"] == "stub: standup"
    assert (second["tool_calls"], second["continuation"]) == ([], None)


@pytest.mark.db
async def test_multi_step_chain_keeps_every_continuation(calls: Calls) -> None:
    chain: list[dict[str, Any]] = []
    history = [{"role": "user", "content": "Plan my day."}]
    scripts = [_scripted({"day": "today"}), _scripted({"day": "tomorrow"}), None]
    bodies = []
    for step, script in enumerate(scripts):
        body = (
            await calls.post(
                provider_call_id=f"chain-{step}",
                tools=[EVENTS_TOOL],
                input=[*history, *chain],
                stub=script,
            )
        ).json()
        bodies.append(body)
        if body["tool_calls"]:
            chain.append({"type": "continuation", **body["continuation"]})
            chain += [
                {"type": "function_call_output", "call_id": c["call_id"], "output": f"out-{step}"}
                for c in body["tool_calls"]
            ]

    assert [b["tool_calls"][0]["call_id"] for b in bodies[:2]] == ["stub_call_1", "stub_call_2"]
    assert bodies[2]["output_text"] == "stub: out-1"
    assert bodies[2]["continuation"] is None


@pytest.mark.db
async def test_invalid_argument_json_is_reported_not_fatal(calls: Calls) -> None:
    body = (await calls.post(tools=[EVENTS_TOOL], stub=_scripted("{oops", "[1]"))).json()

    assert [(c["arguments_json"], c["arguments_error"]) for c in body["tool_calls"]] == [
        (None, "invalid_json"),
        (None, "not_json_object"),
    ]


@pytest.mark.db
async def test_replay_returns_the_same_continuation(calls: Calls) -> None:
    first = (await calls.post(tools=[EVENTS_TOOL], stub=_scripted({"day": "today"}))).json()
    again = (await calls.post(tools=[EVENTS_TOOL], stub=_scripted({"day": "today"}))).json()

    assert again["replayed"] is True
    assert (again["tool_calls"], again["continuation"]) == (
        first["tool_calls"],
        first["continuation"],
    )


@pytest.mark.db
async def test_mismatched_continuation_is_refused_before_admission(calls: Calls) -> None:
    response = await calls.post(
        "production",
        model="openai-gpt-6-luna",
        tools=[EVENTS_TOOL],
        input=[
            {**LUNA_CONTINUATION, "provider": "stub", "model": "stub-echo"},
            {"type": "function_call_output", "call_id": "call_1", "output": "[]"},
        ],
    )

    assert response.status_code == 422
    assert response.json()["code"] == "continuation_mismatch"
    assert await calls.ledger() == []


@pytest.mark.db
async def test_streamed_tool_call(calls: Calls) -> None:
    response = await calls.post(stream=True, tools=[EVENTS_TOOL], stub=_scripted({"day": "today"}))
    direct = (
        await calls.post(provider_call_id="t-direct", tools=[EVENTS_TOOL], stub=_scripted({"day": "today"}))
    ).json()  # fmt: skip

    events = _events(response.text)
    assert [name for name, _ in events] == ["response.started", "tool_call", "response.completed"]
    assert events[1][1] == events[2][1]["tool_calls"][0]
    assert events[2][1]["tool_calls"] == direct["tool_calls"]
    assert events[2][1]["continuation"] == direct["continuation"]


@pytest.mark.db
async def test_catalogue_shows_function_tool_support(calls: Calls) -> None:
    response = await calls.gateway.client.get(
        "/v1/models", headers={"Authorization": f"Bearer {calls.keys['production'].raw}"}
    )

    models = {model["alias"]: model for model in response.json()["models"]}
    assert models["openai-gpt-6-luna"]["capabilities"]["function_tools"] is True
    assert models["ollama-qwen2.5-3b-instruct-q4km-v1"]["capabilities"]["function_tools"] is False


def test_continuation_from_another_alias_of_same_upstream_is_refused() -> None:
    request = _with_tools(input=[{**LUNA_CONTINUATION, "model_alias": "another-alias"}])
    with pytest.raises(CallRejectedError, match="continuation_mismatch"):
        _resolve(request)
