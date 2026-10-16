"""Responses API/client and real ledger behavior with a synthetic local HTTP service."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from llm_gateway_client import GatewayClient
from llm_gateway_client.errors import GatewayUnavailableError
from psycopg_pool import AsyncConnectionPool

from llm_gateway.allocation.store import recompute
from llm_gateway.api.routes_allocation import GrantDirectory
from llm_gateway.catalogue.model import Catalogue
from llm_gateway.catalogue.store import synchronize
from llm_gateway.llm.ollama_adapter import OllamaAdapter
from llm_gateway.llm.request import ResponseRequest
from llm_gateway.llm.streaming import start_stream
from llm_gateway.notifier.events import wait_for_background_alerts
from llm_gateway.projects.contract import ContractV2
from tests.ollama_fakes import LocalService, local_entry
from tests.test_auth_api import Gateway

pytestmark = [pytest.mark.anyio, pytest.mark.db]


class LocalAPI:
    def __init__(self, gateway: Gateway, key: str, service: LocalService):
        self.gateway, self.key, self.service = gateway, key, service

    async def post(self, **options: Any):
        body = {
            "provider_call_id": "local-1",
            "operation": "demo",
            "model": "local-test",
            "input": "Synthetic input",
            **options,
        }
        return await self.gateway.client.post(
            "/v1/responses", json=body, headers={"Authorization": "Bearer " + self.key}
        )

    async def ledger(self):
        async with self.gateway.pool.connection() as connection:
            return await (
                await connection.execute(
                    "SELECT state, reserved_micro_usd, cost_micro_usd, charged_micro_usd FROM calls ORDER BY call_key"
                )
            ).fetchall()

    def client(self):
        return GatewayClient(
            "http://gateway",
            self.key,
            http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.gateway.app)),
        )


@pytest.fixture
async def local_api(migrated_pool: AsyncConnectionPool) -> AsyncIterator[LocalAPI]:
    gateway = Gateway(
        migrated_pool,
        ollama_base_url="http://local.test",
        global_monthly_budget_usd="0",
        allocation_min_monthly_budget_usd="0",
    )
    keys = await gateway.register("alpha")
    await recompute(migrated_pool, gateway.settings)
    entry = local_entry()
    service = LocalService(entry)
    catalogue = Catalogue((entry,), ())
    await synchronize(migrated_pool, catalogue)
    deps = gateway.app.state.call_deps
    gateway.app.state.call_deps = dataclasses.replace(
        deps,
        catalogue=catalogue,
        adapters={
            "ollama-native": lambda _: OllamaAdapter(
                "http://local.test", 10, httpx2.MockTransport(service)
            )
        },
    )
    gateway.app.state.grant_directory = GrantDirectory(migrated_pool, ttl_seconds=0)
    async with gateway.client:
        yield LocalAPI(gateway, keys["development"].raw, service)
    await wait_for_background_alerts()


async def test_local_discovery_and_validation_precede_admission(local_api: LocalAPI) -> None:
    details = await local_api.gateway.client.get(
        "/v1/models/local-test", headers={"Authorization": "Bearer " + local_api.key}
    )
    assert details.status_code == 200
    assert details.json()["availability"] == {"configured": True, "reason": None}
    assert "local.test" not in details.text
    assert details.json()["billing"] == "zero"
    for options in [{"reasoning_effort": "none"}, {"input": "x" * 4096}]:
        response = await local_api.post(**options)
        assert response.status_code == 422
    assert not await local_api.ledger()
    assert not local_api.service.requests


async def test_client_direct_replay_without_paid_credential_or_budget(local_api: LocalAPI) -> None:
    async with local_api.client() as client:
        result = await client.responses.create(
            provider_call_id="client-1",
            operation="demo",
            model="local-test",
            input="Synthetic input",
        )
        assert result.model_alias == "local-test" and result.cost_usd == "0.00"
        before = len(local_api.service.requests)
        local_api.gateway.app.state.call_deps = dataclasses.replace(
            local_api.gateway.app.state.call_deps, adapters={}
        )
        replay = await client.responses.create(
            provider_call_id="client-1",
            operation="demo",
            model="local-test",
            input="Synthetic input",
        )
        assert replay.replayed and replay.output_text == result.output_text
    assert len(local_api.service.requests) == before
    assert await local_api.ledger() == [("completed", 0, 0, 0)]


async def test_confirmed_refusal_retries_one_ledger_record(local_api: LocalAPI) -> None:
    local_api.service.mode = "gpu_unavailable"
    first = await local_api.post()
    assert (first.status_code, first.json()["code"]) == (503, "provider_unavailable")
    assert first.headers["Retry-After"] == "5"
    local_api.service.mode = "success"
    assert (await local_api.post()).status_code == 200
    assert (await local_api.post()).json()["replayed"]
    assert local_api.service.generations == 2
    assert await local_api.ledger() == [("completed", 0, 0, 0)]


@pytest.mark.parametrize("stream", [False, True])
async def test_unknown_is_never_executed_again(local_api: LocalAPI, stream: bool) -> None:
    local_api.service.mode = "stream_break" if stream else "read_timeout"
    first = await local_api.post(stream=stream)
    assert "execution_unknown" in first.text
    assert local_api.service.generations == 1
    before = len(local_api.service.requests)
    local_api.service.mode = "success"
    replay = await local_api.post(stream=stream)
    assert "execution_unknown" in replay.text
    assert len(local_api.service.requests) == before
    assert await local_api.ledger() == [("execution_unknown", 0, 0, 0)]
    assert "PRIVATE_PROVIDER_TEXT" not in first.text


async def test_configuration_problem_is_terminal_and_client_does_not_retry(
    local_api: LocalAPI,
) -> None:
    local_api.service.mismatch = "model"
    async with local_api.client() as client:
        with pytest.raises(GatewayUnavailableError) as error:
            await client.responses.create(
                provider_call_id="mismatch",
                operation="demo",
                model="local-test",
                input="Synthetic input",
            )
    assert error.value.code == "provider_configuration_mismatch"
    assert len(local_api.service.requests) == 4
    assert local_api.service.generations == 0
    assert await local_api.ledger() == [("failed", 0, 0, 0)]


async def test_schema_mismatch_is_completed_and_replayable(local_api: LocalAPI) -> None:
    local_api.service.output = "{}"
    text_format = {
        "type": "json_schema",
        "name": "answer",
        "schema": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        "strict": True,
    }
    result = await local_api.post(text_format=text_format)
    assert result.status_code == 200
    assert result.json()["output_json"] is None
    assert result.json()["structured_output_error"] == "schema_mismatch"
    assert (await local_api.post(text_format=text_format)).json()["replayed"]
    assert local_api.service.generations == 1
    assert await local_api.ledger() == [("completed", 0, 0, 0)]


@pytest.mark.parametrize(
    "output",
    ['{"ok":NaN}', '{"ok":"\\ud800"}'],
    ids=["nonfinite-number", "invalid-unicode"],
)
async def test_unsafe_structured_json_remains_completed_and_replayable(
    local_api: LocalAPI, output: str
) -> None:
    local_api.service.output = output
    options = {
        "text_format": {
            "type": "json_schema",
            "name": "answer",
            "schema": {"type": "object"},
            "strict": True,
        }
    }
    response = await local_api.post(**options)
    assert response.status_code == 200
    assert response.json()["structured_output_error"] == "invalid_json"
    assert response.json()["output_json"] is None
    assert (await local_api.post(**options)).json()["replayed"]
    assert local_api.service.generations == 1
    assert await local_api.ledger() == [("completed", 0, 0, 0)]


async def test_stream_disconnect_keeps_producer_until_settlement(local_api: LocalAPI) -> None:
    local_api.service.delay = 0.05
    deps = local_api.gateway.app.state.call_deps
    loaded = await local_api.gateway.authenticator.directory.load("alpha")
    assert loaded is not None and isinstance(loaded.contract, ContractV2)
    request = ResponseRequest.model_validate(
        {
            "provider_call_id": "disconnect",
            "operation": "demo",
            "model": "local-test",
            "input": "Synthetic input",
            "stream": True,
        }
    )
    plan = await start_stream(
        deps,
        project_id="alpha",
        environment="development",
        contract=loaded.contract,
        request=request,
        request_bytes=len(request.model_dump_json().encode()),
    )
    assert plan.channel is not None
    plan.channel.detach()
    for _ in range(100):
        rows = await local_api.ledger()
        if rows and rows[0][0] == "completed":
            async with local_api.gateway.pool.connection() as connection:
                stored = await (
                    await connection.execute("SELECT count(*) FROM call_results")
                ).fetchone()
                if stored is not None and stored[0] == 1:
                    break
        await asyncio.sleep(0.01)
    assert await local_api.ledger() == [("completed", 0, 0, 0)]
    replay = await local_api.post(provider_call_id="disconnect", input="Synthetic input")
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert local_api.service.generations == 1


async def test_local_calls_keep_concurrency_grants(local_api: LocalAPI) -> None:
    local_api.service.hold = asyncio.Event()
    first = asyncio.create_task(local_api.post(provider_call_id="parallel-1"))
    second = asyncio.create_task(local_api.post(provider_call_id="parallel-2"))
    for _ in range(100):
        if local_api.service.generations == 2:
            break
        await asyncio.sleep(0.005)
    try:
        third = await local_api.post(provider_call_id="parallel-3")
        assert third.status_code == 429 and third.json()["code"] == "concurrency_exhausted"
    finally:
        local_api.service.hold.set()
        responses = await asyncio.gather(first, second)
    assert all(response.status_code == 200 for response in responses)
    assert len(await local_api.ledger()) == 2
