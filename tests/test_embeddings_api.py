"""Embedding endpoint/client, actual ledger transactions and cross-task recovery."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from llm_gateway_client import GatewayClient
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from llm_gateway.allocation.store import recompute
from llm_gateway.api.routes_allocation import GrantDirectory
from llm_gateway.calls.admission import find_call
from llm_gateway.catalogue.model import Catalogue, get_catalogue
from llm_gateway.catalogue.store import synchronize
from llm_gateway.embeddings.adapter import EmbeddingAdapter
from llm_gateway.embeddings.models import EmbeddingRequest
from llm_gateway.embeddings.service import run_embedding
from llm_gateway.llm.results import ResultStore
from llm_gateway.notifier.events import wait_for_background_alerts
from llm_gateway.projects import service as registry
from llm_gateway.projects.credentials import CredentialStore
from tests.embedding_fakes import EmbeddingService, local_model
from tests.test_auth_api import Gateway
from tests.test_contract import contract_data

pytestmark = [pytest.mark.anyio, pytest.mark.db]
PAID = "openai-text-embedding-3-small"


class API:
    def __init__(self, gateway: Gateway, key: str, local: EmbeddingService, paid: EmbeddingService):
        self.gateway, self.key, self.local, self.paid = gateway, key, local, paid

    async def post(self, **options: Any):
        return await self.gateway.client.post(
            "/v1/embeddings",
            headers={"Authorization": "Bearer " + self.key},
            json={
                "provider_call_id": "embedding-1",
                "operation": "demo",
                "model": "embedding-test",
                "input": ["Synthetic first", "Synthetic second"],
                **options,
            },
        )

    async def ledger(self):
        async with self.gateway.pool.connection() as connection:
            return await (
                await connection.execute(
                    "SELECT task,state,reserved_micro_usd,cost_micro_usd,charged_micro_usd FROM calls ORDER BY call_key"
                )
            ).fetchall()

    async def enable_paid(self):
        deps = self.gateway.app.state.call_deps
        settings = deps.settings.model_copy(update={"global_monthly_budget_usd": Decimal("5")})
        self.gateway.app.state.call_deps = dataclasses.replace(deps, settings=settings)
        await recompute(self.gateway.pool, settings)
        store = CredentialStore(self.gateway.pool, settings.provider_credential_encryption_key)
        await store.set("alpha", "openai", SecretStr("synthetic-alpha"), actor="test")
        return store


@pytest.fixture
async def api(migrated_pool: AsyncConnectionPool) -> AsyncIterator[API]:
    gateway = Gateway(
        migrated_pool,
        embeddings_base_url="http://local.test",
        global_monthly_budget_usd="0",
        allocation_min_monthly_budget_usd="0",
    )
    keys = await gateway.register("alpha")
    await recompute(migrated_pool, gateway.settings)
    entry, metadata = local_model()
    local, paid = EmbeddingService(metadata), EmbeddingService()
    catalogue = Catalogue((*get_catalogue().models.values(), entry), tuple(get_catalogue()._prices))
    await synchronize(migrated_pool, catalogue)
    deps = gateway.app.state.call_deps

    def paid_factory(credential):
        assert credential is not None
        return EmbeddingAdapter(
            "http://paid.test",
            10,
            local=False,
            secret=credential.secret,
            transport=httpx2.MockTransport(paid),
        )

    gateway.app.state.call_deps = dataclasses.replace(
        deps,
        catalogue=catalogue,
        adapters={
            **deps.adapters,
            "local-embeddings": lambda _: EmbeddingAdapter(
                "http://local.test", 10, local=True, transport=httpx2.MockTransport(local)
            ),
            "openai-embeddings": paid_factory,
        },
    )
    gateway.app.state.grant_directory = GrantDirectory(migrated_pool, ttl_seconds=0)
    async with gateway.client:
        yield API(gateway, keys["development"].raw, local, paid)
    await wait_for_background_alerts()


async def test_local_client_zero_budget_and_replay_without_runtime(api: API) -> None:
    async with GatewayClient(
        "http://gateway",
        api.key,
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=api.gateway.app)),
    ) as client:
        result = await client.embeddings.create(
            provider_call_id="client-1",
            operation="demo",
            model="embedding-test",
            input=["Synthetic first", "Synthetic second"],
        )
        assert result.dimensions == 384 and result.cost_usd == "0.00"
        assert [v.index for v in result.data] == [0, 1]
        before = len(api.local.requests)
        api.gateway.app.state.call_deps = dataclasses.replace(
            api.gateway.app.state.call_deps, adapters={}
        )
        replay = await client.embeddings.create(
            provider_call_id="client-1",
            operation="demo",
            model="embedding-test",
            input=["Synthetic first", "Synthetic second"],
        )
        assert replay.replayed and replay.data == result.data
    assert len(api.local.requests) == before
    assert await api.ledger() == [("embedding", "completed", 0, 0, 0)]


async def test_invalid_input_and_task_are_refused_before_admission(api: API) -> None:
    for options in [
        {"stream": False},
        {"dimensions": 384},
        {"input": [""]},
        {"model": "stub-echo"},
    ]:
        response = await api.post(**options)
        assert response.status_code == 422
    assert not await api.ledger() and not api.local.requests
    assert (await api.gateway.client.post("/v1/embeddings", json={})).status_code == 401


async def test_discovery_schema_and_typed_openapi(api: API) -> None:
    headers = {"Authorization": "Bearer " + api.key}
    local = await api.gateway.client.get("/v1/models/embedding-test", headers=headers)
    assert local.json()["task"] == "embedding" and local.json()["capabilities"]["dimensions"] == 384
    assert local.json()["limits"]["max_input_tokens"] == 256
    assert "local.test" not in local.text
    assert "dimensions" not in local.json()["request_schema"]["properties"]
    spec = (await api.gateway.client.get("/openapi.json")).json()
    schema = spec["paths"]["/v1/embeddings"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert schema["$ref"].endswith("/EmbeddingResult")


async def test_confirmed_refusal_retry_and_unknown_never_reexecutes(api: API) -> None:
    api.local.mode = "gpu_unavailable"
    first = await api.post()
    assert first.status_code == 503 and first.headers["Retry-After"] == "5"
    api.local.mode = "success"
    assert (await api.post()).status_code == 200
    api.local.mode = "missing_usage"
    assert (await api.post(provider_call_id="unknown")).json()["code"] == "execution_unknown"
    before = len(api.local.requests)
    api.local.mode = "success"
    assert (await api.post(provider_call_id="unknown")).json()["code"] == "execution_unknown"
    assert len(api.local.requests) == before
    assert await api.ledger() == [
        ("embedding", "completed", 0, 0, 0),
        ("embedding", "execution_unknown", 0, 0, 0),
    ]


@pytest.mark.parametrize("embedding_first", [False, True])
async def test_call_ids_conflict_across_endpoints(api: API, embedding_first: bool) -> None:
    async def response():
        return await api.gateway.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer " + api.key},
            json={
                "provider_call_id": "embedding-1",
                "operation": "demo",
                "model": "stub-echo",
                "input": "Synthetic",
            },
        )

    if embedding_first:
        assert (await api.post()).status_code == 200
        conflict = await response()
    else:
        assert (await response()).status_code == 200
        conflict = await api.post()
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    assert len(await api.ledger()) == 1


async def test_paid_credentials_usage_and_exact_micro_cost(api: API) -> None:
    missing = await api.post(model=PAID, dimensions=3)
    assert missing.json()["code"] == "provider_credentials_missing" and not await api.ledger()
    store = await api.enable_paid()
    result = await api.post(model=PAID, dimensions=3)
    assert result.status_code == 200 and result.json()["dimensions"] == 3
    assert result.json()["usage"] == {"input_tokens": 4, "total_tokens": 4}
    # 31 UTF-8 bytes reserve ceil(31*.02)=1 micro; 4*.02 also rounds to 1.
    assert await api.ledger() == [("embedding", "completed", 1, 1, 1)]
    assert api.paid.requests[0].headers["authorization"] == "Bearer synthetic-alpha"
    await store.remove("alpha", "openai", actor="test")
    before = len(api.paid.requests)
    assert (await api.post(model=PAID, dimensions=3)).json()["replayed"]
    assert len(api.paid.requests) == before


async def test_paid_unknown_charges_the_safe_reservation(api: API) -> None:
    await api.enable_paid()
    api.paid.mode = "missing_usage"
    response = await api.post(model=PAID, input="x" * 100, dimensions=3)
    assert response.json()["code"] == "execution_unknown"
    assert await api.ledger() == [("embedding", "execution_unknown", 2, None, 2)]
    assert (await api.post(model=PAID, input="x" * 100, dimensions=3)).json()[
        "code"
    ] == "execution_unknown"
    assert api.paid.generations == 1


async def test_paid_cap_rejects_before_submission_while_local_is_free(api: API) -> None:
    settings = api.gateway.app.state.call_deps.settings
    store = CredentialStore(api.gateway.pool, settings.provider_credential_encryption_key)
    await store.set("alpha", "openai", SecretStr("synthetic-alpha"), actor="test")
    response = await api.post(model=PAID, input="x" * 100, dimensions=3)
    assert response.status_code in {422, 429}
    assert response.json()["code"] in {"cost_ceiling_exceeded", "budget_exhausted"}
    assert not api.paid.requests and not await api.ledger()
    assert (await api.post()).status_code == 200


async def test_disconnected_embedding_caller_keeps_execution_ownership(api: API) -> None:
    api.local.hold = asyncio.Event()
    body = {
        "provider_call_id": "embedding-1",
        "operation": "demo",
        "model": "embedding-test",
        "input": ["Synthetic first", "Synthetic second"],
    }
    request = EmbeddingRequest.model_validate(body)
    active = asyncio.create_task(
        run_embedding(
            api.gateway.app.state.call_deps,
            project_id="alpha",
            environment="development",
            request=request,
            request_bytes=200,
        )
    )
    for _ in range(100):
        if api.local.generations == 1:
            break
        await asyncio.sleep(0.01)
    assert api.local.generations == 1
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    api.local.hold.set()
    for _ in range(100):
        call = await find_call(api.gateway.pool, "alpha", "development", "embedding-1")
        if call is not None and call.state == "completed":
            break
        await asyncio.sleep(0.01)
    assert (await api.post()).json()["replayed"]
    assert api.local.generations == 1


async def test_embedding_profile_defaults_are_registry_versioned(api: API) -> None:
    await api.enable_paid()
    data = contract_data(project_id="alpha")
    data["profiles"]["vectors"] = {"model": PAID, "parameters": {"dimensions": 3}}
    await registry.update_contract(
        api.gateway.pool, "alpha", lambda _: data, actor="test", note="Embedding profile"
    )
    response = await api.post(model=None, profile="vectors")
    assert response.status_code == 200 and response.json()["dimensions"] == 3


async def test_paid_retry_cannot_switch_registered_credentials(api: API) -> None:
    store = await api.enable_paid()
    api.paid.mode = "rate_limit"
    assert (await api.post(model=PAID, dimensions=3)).status_code == 429
    await store.set("alpha", "openai", SecretStr("synthetic-rotated"), actor="test")
    response = await api.post(model=PAID, dimensions=3)
    assert response.status_code == 409 and response.json()["code"] == "credential_changed"
    assert api.paid.generations == 1


async def test_paid_projects_capture_independent_accounts(api: API) -> None:
    store = await api.enable_paid()
    keys = await api.gateway.register("beta")
    await store.set("beta", "openai", SecretStr("synthetic-beta"), actor="test")
    await recompute(api.gateway.pool, api.gateway.app.state.call_deps.settings)
    assert (await api.post(model=PAID, dimensions=3)).status_code == 200
    response = await api.gateway.client.post(
        "/v1/embeddings",
        headers={"Authorization": "Bearer " + keys["development"].raw},
        json={
            "provider_call_id": "embedding-1",
            "operation": "demo",
            "model": PAID,
            "dimensions": 3,
            "input": "Synthetic text",
        },
    )
    assert response.status_code == 200
    assert [request.headers["authorization"] for request in api.paid.requests] == [
        "Bearer synthetic-alpha",
        "Bearer synthetic-beta",
    ]
    async with api.gateway.pool.connection() as connection:
        revisions = await (
            await connection.execute("SELECT credential_revision FROM calls ORDER BY call_key")
        ).fetchall()
    assert (
        revisions[0][0] is not None and revisions[1][0] is not None and revisions[0] != revisions[1]
    )


@pytest.mark.parametrize("endpoint", ["embeddings", "responses"])
async def test_result_failure_rolls_back_state_and_event(
    api: API, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic persistence failure")

    monkeypatch.setattr(ResultStore, "_save", fail)
    if endpoint == "embeddings":
        response = await api.post()
    else:
        response = await api.gateway.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer " + api.key},
            json={
                "provider_call_id": "embedding-1",
                "operation": "demo",
                "model": "stub-echo",
                "input": "Synthetic",
            },
        )
    assert response.status_code == 500
    call = await find_call(api.gateway.pool, "alpha", "development", "embedding-1")
    assert call is not None and call.state == "admitted"
    async with api.gateway.pool.connection() as connection:
        events = await (await connection.execute("SELECT count(*) FROM call_events")).fetchone()
        results = await (await connection.execute("SELECT count(*) FROM call_results")).fetchone()
    assert events == (1,) and results == (0,)
    assert (await api.post()).status_code == 409


async def test_concurrency_is_shared_across_tasks(api: API) -> None:
    api.local.hold = asyncio.Event()
    first = asyncio.create_task(api.post(provider_call_id="hold-1"))
    second = asyncio.create_task(api.post(provider_call_id="hold-2"))
    try:
        for _ in range(100):
            if api.local.generations == 2:
                break
            await asyncio.sleep(0.01)
        third = await api.gateway.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer " + api.key},
            json={
                "provider_call_id": "third",
                "operation": "demo",
                "model": "stub-echo",
                "input": "Synthetic",
            },
        )
        assert third.status_code == 429 and third.json()["code"] == "concurrency_exhausted"
    finally:
        api.local.hold.set()
        assert all(r.status_code == 200 for r in await asyncio.gather(first, second))
