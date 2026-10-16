"""Execution snapshots, project account isolation, and replay survive registry changes."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from llm_gateway.allocation.store import recompute
from llm_gateway.catalogue.model import Catalogue
from llm_gateway.llm.providers import ProviderFailure
from llm_gateway.llm.response import CallResultOut
from llm_gateway.projects import service
from llm_gateway.projects.credentials import Credential, CredentialStore
from tests.test_responses_api import Calls, calls_context

pytestmark = [pytest.mark.anyio, pytest.mark.db]


@pytest.fixture
async def calls(migrated_pool: AsyncConnectionPool) -> AsyncIterator[Calls]:
    async with calls_context(migrated_pool) as context:
        yield context


def _store(calls: Calls) -> CredentialStore:
    return CredentialStore(
        calls.gateway.pool, calls.gateway.settings.provider_credential_encryption_key
    )


async def _details(calls: Calls, path: str = "/v1/models", environment: str = "development"):
    return await calls.gateway.client.get(
        path, headers={"Authorization": f"Bearer {calls.keys[environment].raw}"}
    )


async def test_discovery_fields_schemas_and_task_filtering(calls: Calls) -> None:
    assert (await calls.gateway.client.get("/v1/models")).status_code == 401
    assert (await _details(calls, "/v1/models?task=ocr")).json() == {"models": []}
    embeddings = (await _details(calls, "/v1/models?task=embedding")).json()["models"]
    assert len(embeddings) == 4 and all(m["task"] == "embedding" for m in embeddings)
    assert (await _details(calls, "/v1/catalogue")).status_code == 404
    assert (await _details(calls, "/v1/models/stub-echo", "production")).status_code == 404
    assert (await _details(calls, "/v1/models/unknown")).status_code == 404
    model = (await _details(calls, "/v1/models/openai-gpt-6-luna")).json()
    assert model["availability"] == {"configured": True, "reason": None}
    assert not {"deployment", "adapter", "credential", "base_url"} & model.keys()
    assert "synthetic-provider-key" not in json.dumps(model)
    payload = {
        "provider_call_id": "schema-check",
        "operation": "demo",
        "model": model["alias"],
        "input": "hi",
        "reasoning_effort": "none",
        "temperature": 0.5,
    }
    assert Draft202012Validator(model["request_schema"]).is_valid(payload)
    response = await calls.post("production", **payload)
    assert response.status_code == 200
    CallResultOut.model_validate(response.json())
    await _store(calls).remove("alpha", "openai", actor="test")
    model = (await _details(calls, "/v1/models/openai-gpt-6-luna")).json()
    assert model["availability"] == {"configured": False, "reason": "provider_credentials_missing"}
    missing = await calls.post("production", model=model["alias"], provider_call_id="missing")
    assert (missing.status_code, missing.json()["code"]) == (503, "provider_credentials_missing")
    assert calls.openai.calls == 1
    assert len(await calls.ledger()) == 1


async def test_completed_replay_precedes_retired_model_profile_and_credentials(
    calls: Calls,
) -> None:
    first = await calls.post("production", model=None, profile="chat")
    assert first.status_code == 200
    await _store(calls).remove("alpha", "openai", actor="test")
    await service.update_contract(
        calls.gateway.pool,
        "alpha",
        lambda data: data | {"profiles": {}},
        actor="test",
        note="remove profile",
    )
    deps = calls.gateway.app.state.call_deps
    calls.gateway.app.state.call_deps = dataclasses.replace(
        deps, catalogue=Catalogue((), ()), adapters={}
    )
    replay = await calls.post("production", model=None, profile="chat")
    assert replay.status_code == 200
    assert replay.json() == first.json() | {"replayed": True}
    assert calls.openai.calls == 1 and len(await calls.ledger()) == 1


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("replace", "credential_changed"),
        ("remove", "credential_changed"),
        ("profile", "call_configuration_changed"),
    ],
)
async def test_confirmed_refusal_cannot_retry_against_changed_binding(
    calls: Calls, mutation: str, code: str
) -> None:
    calls.openai.result = ProviderFailure("temporary_overload", "server_error")
    first = await calls.post("production", model=None, profile="chat")
    assert first.json()["code"] == "provider_unavailable"
    if mutation == "replace":
        await _store(calls).set("alpha", "openai", SecretStr("synthetic-replacement"), actor="test")
    elif mutation == "remove":
        await _store(calls).remove("alpha", "openai", actor="test")
    else:

        def edit(data: dict[str, Any]) -> dict[str, Any]:
            data["profiles"]["chat"]["parameters"]["max_output_tokens"] = 1100
            return data

        await service.update_contract(
            calls.gateway.pool, "alpha", edit, actor="test", note="parameters"
        )
    retry = await calls.post("production", model=None, profile="chat")
    assert (retry.status_code, retry.json()["code"]) == (409, code)
    assert calls.openai.calls == 1
    assert (await calls.ledger())[0][1] == "retryable_failure"


async def test_concurrent_projects_capture_their_own_account(calls: Calls) -> None:
    other_keys = await calls.gateway.register("beta")
    await recompute(calls.gateway.pool, calls.gateway.settings)
    await _store(calls).set("beta", "openai", SecretStr("synthetic-beta-key"), actor="test")
    revisions: set[str] = set()
    observed: set[str] = set()

    def factory(credential: Credential | None):
        assert credential is not None
        revisions.add(str(credential.revision))
        observed.add(credential.secret.get_secret_value())
        return calls.openai

    calls.gateway.app.state.call_deps.adapters["openai-responses"] = factory

    async def beta():
        return await calls.gateway.client.post(
            "/v1/responses",
            json={
                "provider_call_id": "beta-call",
                "operation": "demo",
                "model": "openai-gpt-6-luna",
                "input": "hi",
            },
            headers={"Authorization": f"Bearer {other_keys['production'].raw}"},
        )

    alpha, other = await asyncio.gather(calls.post("production", model="openai-gpt-6-luna"), beta())
    assert alpha.status_code == other.status_code == 200
    assert observed == {"synthetic-provider-key", "synthetic-beta-key"}
    async with calls.gateway.pool.connection() as connection:
        rows = await (
            await connection.execute(
                "SELECT credential_revision, model_alias, definition_digest, settings_digest, request_schema_version FROM calls"
            )
        ).fetchall()
    assert {str(row[0]) for row in rows} == revisions and len(revisions) == 2
    assert all(
        row[1] == "openai-gpt-6-luna" and len(row[2]) == len(row[3]) == 64 and row[4] == 2
        for row in rows
    )


async def test_zero_fee_bypasses_paid_budget_but_obeys_rate_and_concurrency(calls: Calls) -> None:
    await _store(calls).remove("alpha", "openai", actor="test")
    deps = calls.gateway.app.state.call_deps
    calls.gateway.app.state.call_deps = dataclasses.replace(
        deps, settings=deps.settings.model_copy(update={"global_monthly_budget_usd": 0})
    )
    async with calls.gateway.pool.connection() as connection:
        await connection.execute(
            "UPDATE grants SET monthly_budget_micro_usd=0, per_call_cost_ceiling_micro_usd=0, requests_per_minute=1 WHERE project_id='alpha' AND environment='development'"
        )
    first = await calls.post()
    assert first.status_code == 200 and first.json()["cost_usd"] == "0.00"
    second = await calls.post(provider_call_id="rate-limit")
    assert (second.status_code, second.json()["code"]) == (429, "rate_limited")
    async with calls.gateway.pool.connection() as connection:
        await connection.execute(
            "UPDATE grants SET concurrency=0 WHERE project_id='alpha' AND environment='development'"
        )
        row = await (
            await connection.execute(
                "SELECT reserved_micro_usd, charged_micro_usd, price_tier, price_effective_from FROM calls"
            )
        ).fetchone()
    assert row == (0, 0, None, None)
    third = await calls.post(provider_call_id="concurrency-limit")
    assert (third.status_code, third.json()["code"]) == (429, "concurrency_exhausted")
    assert await calls.gateway.alerts() == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("temperature", 0.5),
        ("unsupported", "synthetic-secret"),
        ("model", {"provider": "openai", "model": "gpt-6-luna"}),
    ],
)
async def test_invalid_configuration_never_reaches_admission(
    calls: Calls, field: str, value: Any
) -> None:
    response = await calls.post("production", **({"model": "openai-gpt-6-luna"} | {field: value}))
    assert response.status_code == 422
    assert "synthetic-secret" not in response.text
    assert await calls.ledger() == [] and calls.openai.calls == 0


def test_openapi_exposes_alias_requests_and_result_metadata() -> None:
    from llm_gateway.api.app import create_app
    from tests.conftest import make_settings

    schema = create_app(make_settings()).openapi()
    request = schema["components"]["schemas"]["ResponseRequest"]
    assert request["properties"]["model"]["anyOf"][0]["type"] == "string"
    assert "model_alias" in schema["components"]["schemas"]["CallResultOut"]["properties"]
    assert (
        "text/event-stream"
        in schema["paths"]["/v1/responses"]["post"]["responses"]["200"]["content"]
    )
    assert "/v1/catalogue" not in schema["paths"]
    problem = schema["paths"]["/v1/responses"]["post"]["responses"]["422"]["content"]
    assert "application/problem+json" in problem
    assert "code" in problem["application/problem+json"]["schema"]["required"]


@pytest.mark.parametrize("stream", [False, True])
async def test_call_scoped_openai_clients_close_after_each_execution(
    calls: Calls, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    import httpx2

    from llm_gateway.llm import openai_adapter
    from llm_gateway.llm.openai_adapter import CredentialOpenAIAdapter
    from tests.test_openai_adapter import _success_body
    from tests.test_streaming import _created, _sse, _terminal

    original = openai_adapter.create_openai_client
    clients = []
    observed = []

    def create(key: str, timeout: int):
        def handler(request: httpx2.Request):
            observed.append(request.headers["authorization"])
            assert json.loads(request.content)["store"] is False
            if stream:
                return httpx2.Response(
                    200,
                    content=_sse(_created(), _terminal()),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx2.Response(200, json=_success_body())

        client = original(
            key, timeout, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        )
        assert client.max_retries == 0
        clients.append(client)
        return client

    monkeypatch.setattr(openai_adapter, "create_openai_client", create)

    def adapter(credential: Credential | None):
        assert credential is not None
        return CredentialOpenAIAdapter(credential.secret, 30)

    calls.gateway.app.state.call_deps.adapters["openai-responses"] = adapter
    response = await calls.post("production", model="openai-gpt-6-luna", stream=stream)
    assert response.status_code == 200
    assert observed == ["Bearer synthetic-provider-key"]
    assert len(clients) == 1 and clients[0].is_closed()


async def test_profile_and_credential_edits_cannot_split_an_admission_snapshot(
    calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured, release = asyncio.Event(), asyncio.Event()
    edit_started, rotate_started = asyncio.Event(), asyncio.Event()
    original_capture = CredentialStore.capture
    captured_revisions = []
    executed = []

    async def pause_capture(store, connection, project_id, provider):
        credential = await original_capture(store, connection, project_id, provider)
        captured_revisions.append(credential.revision)
        captured.set()
        await release.wait()
        return credential

    async def execute(resolved, request):
        executed.append(resolved)
        from tests.test_responses_api import _luna_success

        return _luna_success()

    monkeypatch.setattr(CredentialStore, "capture", pause_capture)
    monkeypatch.setattr(calls.openai, "execute", execute)

    async def edit():
        edit_started.set()

        def change(data):
            data["profiles"]["chat"]["parameters"]["max_output_tokens"] = 1100
            return data

        return await service.update_contract(
            calls.gateway.pool, "alpha", change, actor="test", note="edit during admission"
        )

    async def rotate():
        rotate_started.set()
        return await _store(calls).set(
            "alpha", "openai", SecretStr("synthetic-rotated"), actor="test"
        )

    first = asyncio.create_task(calls.post("production", model=None, profile="chat"))
    await asyncio.wait_for(captured.wait(), 5)
    editing, rotating = asyncio.create_task(edit()), asyncio.create_task(rotate())
    await asyncio.gather(edit_started.wait(), rotate_started.wait())
    assert not editing.done() and not rotating.done()
    release.set()
    response, _, new_revision = await asyncio.gather(first, editing, rotating)
    assert response.status_code == 200 and executed[0].max_output_tokens == 1200
    assert str(captured_revisions[0]) != new_revision
    async with calls.gateway.pool.connection() as connection:
        row = await (
            await connection.execute("SELECT credential_revision, settings_digest FROM calls")
        ).fetchone()
    assert row == (captured_revisions[0], executed[0].settings_digest)


async def test_admission_waiting_on_profile_edit_reads_the_committed_version(
    calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anyio

    inserted, release = asyncio.Event(), asyncio.Event()
    original = service._insert_version
    writer_pid = []
    executed = []

    async def pause(connection, contract, version, actor, note):
        await original(connection, contract, version, actor, note)
        writer_pid.append(connection.info.backend_pid)
        inserted.set()
        await release.wait()

    async def execute(resolved, request):
        executed.append(resolved)
        from tests.test_responses_api import _luna_success

        return _luna_success()

    def change(data):
        data["profiles"]["chat"]["parameters"]["max_output_tokens"] = 1100
        return data

    monkeypatch.setattr(service, "_insert_version", pause)
    monkeypatch.setattr(calls.openai, "execute", execute)
    editing = asyncio.create_task(
        service.update_contract(
            calls.gateway.pool, "alpha", change, actor="test", note="writer before admission"
        )
    )
    await asyncio.wait_for(inserted.wait(), 5)
    calling = asyncio.create_task(calls.post("production", model=None, profile="chat"))
    try:
        with anyio.fail_after(5):
            while True:
                async with calls.gateway.pool.connection() as connection:
                    row = await (
                        await connection.execute(
                            "SELECT count(*) FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                            (writer_pid[0],),
                        )
                    ).fetchone()
                if row is not None and row[0] > 0:
                    break
                await anyio.sleep(0.01)
    finally:
        release.set()
    _, response = await asyncio.gather(editing, calling)
    assert response.status_code == 200
    assert executed[0].max_output_tokens == 1100
