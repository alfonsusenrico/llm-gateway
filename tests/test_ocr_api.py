"""OCR with real admission, shared grants, encrypted replay and durable accounting."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from llm_gateway_client import GatewayClient
from psycopg_pool import AsyncConnectionPool

from llm_gateway.allocation.store import recompute
from llm_gateway.api.routes_allocation import GrantDirectory
from llm_gateway.calls.admission import find_call
from llm_gateway.catalogue.model import Catalogue, get_catalogue
from llm_gateway.catalogue.store import synchronize
from llm_gateway.llm.results import ResultStore
from llm_gateway.notifier.events import wait_for_background_alerts
from llm_gateway.ocr.adapter import OcrAdapter
from llm_gateway.ocr.models import OcrRequest
from llm_gateway.ocr.service import run_ocr
from llm_gateway.projects import service as registry
from tests.ocr_fakes import OcrService, image_data, local_model
from tests.test_auth_api import Gateway
from tests.test_contract import contract_data

pytestmark = [pytest.mark.anyio, pytest.mark.db]


def image_options() -> dict[str, Any]:
    return dict(media_type="image/png", data_base64=base64.b64encode(image_data()).decode())


class API:
    def __init__(self, gateway: Gateway, key: str, local: OcrService):
        self.gateway, self.key, self.local = gateway, key, local

    async def post(self, **options: Any):
        return await self.gateway.client.post(
            "/v1/ocr",
            headers={"Authorization": "Bearer " + self.key},
            json={
                "provider_call_id": "ocr-1",
                "operation": "demo",
                "model": "ocr-test",
                **image_options(),
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


@pytest.fixture
async def api(migrated_pool: AsyncConnectionPool) -> AsyncIterator[API]:
    gateway = Gateway(
        migrated_pool,
        ocr_base_url="http://local.test",
        global_monthly_budget_usd="0",
        allocation_min_monthly_budget_usd="0",
    )
    keys = await gateway.register("alpha")
    await recompute(migrated_pool, gateway.settings)
    entry, metadata = local_model()
    local = OcrService(metadata)
    catalogue = Catalogue((*get_catalogue().models.values(), entry), tuple(get_catalogue()._prices))
    await synchronize(migrated_pool, catalogue)
    deps = gateway.app.state.call_deps
    gateway.app.state.call_deps = dataclasses.replace(
        deps,
        catalogue=catalogue,
        adapters={
            **deps.adapters,
            "local-ocr": lambda _: OcrAdapter(
                "http://local.test", 10, transport=httpx2.MockTransport(local)
            ),
        },
    )
    gateway.app.state.grant_directory = GrantDirectory(migrated_pool, ttl_seconds=0)
    async with gateway.client:
        yield API(gateway, keys["development"].raw, local)
    await wait_for_background_alerts()


async def test_local_client_zero_budget_and_replay_without_runtime(api: API) -> None:
    async with GatewayClient(
        "http://gateway",
        api.key,
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=api.gateway.app)),
    ) as client:
        result = await client.ocr.create(
            provider_call_id="client-1",
            operation="demo",
            model="ocr-test",
            **image_options(),
        )
        assert result.width == 100 and result.cost_usd == "0.00"
        assert len(result.blocks) == 1
        before = len(api.local.requests)
        api.gateway.app.state.call_deps = dataclasses.replace(
            api.gateway.app.state.call_deps, adapters={}
        )
        replay = await client.ocr.create(
            provider_call_id="client-1",
            operation="demo",
            model="ocr-test",
            **image_options(),
        )
        assert replay.replayed and replay.blocks == result.blocks
    assert len(api.local.requests) == before
    assert await api.ledger() == [("ocr", "completed", 0, 0, 0)]


async def test_invalid_input_and_task_are_refused_before_admission(api: API) -> None:
    for options in [
        {"stream": False},
        {"dimensions": 384},
        {"data_base64": "!!!!"},
        {"model": "stub-echo"},
    ]:
        response = await api.post(**options)
        assert response.status_code == 422
    assert not await api.ledger() and not api.local.requests
    assert (await api.gateway.client.post("/v1/ocr", json={})).status_code == 401


async def test_discovery_schema_and_typed_openapi(api: API) -> None:
    headers = {"Authorization": "Bearer " + api.key}
    local = await api.gateway.client.get("/v1/models/ocr-test", headers=headers)
    assert local.json()["task"] == "ocr" and local.json()["capabilities"]["exif_rotation"] is False
    assert local.json()["limits"]["max_side"] == 4096
    assert "local.test" not in local.text
    assert "dimensions" not in local.json()["request_schema"]["properties"]
    spec = (await api.gateway.client.get("/openapi.json")).json()
    schema = spec["paths"]["/v1/ocr"]["post"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert schema["$ref"].endswith("/OcrResult")


async def test_confirmed_refusal_retry_and_unknown_never_reexecutes(api: API) -> None:
    api.local.mode = "gpu_unavailable"
    first = await api.post()
    assert first.status_code == 503 and first.headers["Retry-After"] == "5"
    api.local.mode = "success"
    assert (await api.post()).status_code == 200
    api.local.mode = "wrong_width"
    assert (await api.post(provider_call_id="unknown")).json()["code"] == "execution_unknown"
    before = len(api.local.requests)
    api.local.mode = "success"
    assert (await api.post(provider_call_id="unknown")).json()["code"] == "execution_unknown"
    assert len(api.local.requests) == before
    assert await api.ledger() == [
        ("ocr", "completed", 0, 0, 0),
        ("ocr", "execution_unknown", 0, 0, 0),
    ]


@pytest.mark.parametrize("ocr_first", [False, True])
async def test_call_ids_conflict_across_endpoints(api: API, ocr_first: bool) -> None:
    async def response():
        return await api.gateway.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer " + api.key},
            json={
                "provider_call_id": "ocr-1",
                "operation": "demo",
                "model": "stub-echo",
                "input": "Synthetic",
            },
        )

    if ocr_first:
        assert (await api.post()).status_code == 200
        conflict = await response()
    else:
        assert (await response()).status_code == 200
        conflict = await api.post()
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    assert len(await api.ledger()) == 1


async def test_disconnected_ocr_caller_keeps_execution_ownership(api: API) -> None:
    api.local.hold = asyncio.Event()
    body = {
        "provider_call_id": "ocr-1",
        "operation": "demo",
        "model": "ocr-test",
        **image_options(),
    }
    request = OcrRequest.model_validate(body)
    active = asyncio.create_task(
        run_ocr(
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
        call = await find_call(api.gateway.pool, "alpha", "development", "ocr-1")
        if call is not None and call.state == "completed":
            break
        await asyncio.sleep(0.01)
    assert (await api.post()).json()["replayed"]
    assert api.local.generations == 1


@pytest.mark.parametrize("endpoint", ["ocr", "responses"])
async def test_result_failure_rolls_back_state_and_event(
    api: API, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic persistence failure")

    monkeypatch.setattr(ResultStore, "_save", fail)
    if endpoint == "ocr":
        response = await api.post()
    else:
        response = await api.gateway.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer " + api.key},
            json={
                "provider_call_id": "ocr-1",
                "operation": "demo",
                "model": "stub-echo",
                "input": "Synthetic",
            },
        )
    assert response.status_code == 500
    call = await find_call(api.gateway.pool, "alpha", "development", "ocr-1")
    assert call is not None and call.state == "admitted"
    async with api.gateway.pool.connection() as connection:
        events = await (await connection.execute("SELECT count(*) FROM call_events")).fetchone()
        results = await (await connection.execute("SELECT count(*) FROM call_results")).fetchone()
    assert events == (1,) and results == (0,)
    assert (await api.post()).status_code == 409


async def test_concurrency_is_shared_across_tasks(api: API) -> None:
    api.local.hold = asyncio.Event()
    first = asyncio.create_task(api.post(provider_call_id="hold-1"))
    for _ in range(100):
        if api.local.generations == 1:
            break
        await asyncio.sleep(0.01)
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


async def test_ocr_profile_defaults_are_registry_versioned(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = contract_data(project_id="alpha")
    data["profiles"]["reader"] = {"model": "ocr-test", "parameters": {}}
    monkeypatch.setattr(
        registry, "get_catalogue", lambda: api.gateway.app.state.call_deps.catalogue
    )
    await registry.update_contract(
        api.gateway.pool,
        "alpha",
        lambda _: data,
        actor="test",
        note="OCR profile",
    )
    response = await api.post(model=None, profile="reader")
    assert response.status_code == 200 and response.json()["exif_rotation"] is False


async def test_missing_config_and_replay_ignores_busy_decoder(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    deps = api.gateway.app.state.call_deps
    api.gateway.app.state.call_deps = dataclasses.replace(deps, adapters={})
    assert (await api.post()).json()["code"] == "provider_not_configured"
    assert not await api.ledger() and not api.local.requests
    api.gateway.app.state.call_deps = deps
    assert (await api.post()).status_code == 200

    async def fail(*args, **kwargs):
        pytest.fail("replay must precede image decoding")

    monkeypatch.setattr("llm_gateway.ocr.service.validate_image", fail)
    assert (await api.post()).json()["replayed"]


@pytest.mark.parametrize("ocr_first", [False, True])
async def test_ocr_and_embeddings_share_call_identity(api: API, ocr_first: bool) -> None:
    from llm_gateway.embeddings.adapter import EmbeddingAdapter
    from tests.embedding_fakes import EmbeddingService
    from tests.embedding_fakes import local_model as embedding_model

    deps = api.gateway.app.state.call_deps
    entry, metadata = embedding_model()
    native = EmbeddingService(metadata)
    catalogue = Catalogue((*deps.catalogue.models.values(), entry), tuple(deps.catalogue._prices))
    await synchronize(api.gateway.pool, catalogue)
    api.gateway.app.state.call_deps = dataclasses.replace(
        deps,
        catalogue=catalogue,
        adapters={
            **deps.adapters,
            "local-embeddings": lambda _: EmbeddingAdapter(
                "http://embedding.test", 10, local=True, transport=httpx2.MockTransport(native)
            ),
        },
    )

    async def embedding():
        return await api.gateway.client.post(
            "/v1/embeddings",
            headers={"Authorization": "Bearer " + api.key},
            json={
                "provider_call_id": "ocr-1",
                "operation": "demo",
                "model": "embedding-test",
                "input": "Synthetic",
            },
        )

    first, second = (api.post, embedding) if ocr_first else (embedding, api.post)
    assert (await first()).status_code == 200
    assert (await second()).json()["code"] == "idempotency_conflict"
    assert len(await api.ledger()) == 1
    assert native.generations + api.local.generations == 1


async def test_rate_grants_apply_to_free_ocr(api: API) -> None:
    data = contract_data(project_id="alpha")
    data["request"]["development"]["requests_per_minute"] = 1
    await registry.update_contract(
        api.gateway.pool, "alpha", lambda _: data, actor="test", note="Rate test"
    )
    await recompute(api.gateway.pool, api.gateway.settings)
    assert (await api.post()).status_code == 200
    rejected = await api.post(provider_call_id="ocr-2")
    assert rejected.status_code == 429 and rejected.json()["code"] == "rate_limited"
    assert len(await api.ledger()) == 1 and api.local.generations == 1


async def test_content_never_enters_logs_or_plain_results(
    api: API, caplog: pytest.LogCaptureFixture
) -> None:
    assert (await api.post()).status_code == 200
    api.local.mode = "timeout"
    assert (await api.post(provider_call_id="unknown")).json()["code"] == "execution_unknown"
    assert "Synthetic text" not in caplog.text
    assert "PRIVATE_PROVIDER_TEXT" not in caplog.text
    assert image_options()["data_base64"] not in caplog.text
    async with api.gateway.pool.connection() as connection:
        rows = await (await connection.execute("SELECT encrypted FROM call_results")).fetchall()
    assert len(rows) == 1 and b"Synthetic text" not in bytes(rows[0][0])


async def test_ocr_route_enforces_request_size_before_decoding(api: API) -> None:
    deps = api.gateway.app.state.call_deps
    api.gateway.app.state.call_deps = dataclasses.replace(
        deps, settings=deps.settings.model_copy(update={"request_max_bytes": 10_000})
    )
    response = await api.post(data_base64="A" * 12_000)
    assert response.status_code == 413 and response.json()["code"] == "request_too_large"
    assert not api.local.requests and not await api.ledger()
