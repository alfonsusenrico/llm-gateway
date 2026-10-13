from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from psycopg_pool import AsyncConnectionPool

from llm_gateway.allocation.store import recompute
from llm_gateway.api.routes_allocation import GrantDirectory
from tests.conftest import make_settings
from tests.test_auth_api import Gateway

pytestmark = [pytest.mark.anyio, pytest.mark.db]


@pytest.fixture
async def gateway(migrated_pool: AsyncConnectionPool) -> AsyncIterator[Gateway]:
    gateway = Gateway(migrated_pool)
    gateway.app.state.grant_directory = GrantDirectory(migrated_pool, ttl_seconds=0)
    async with gateway.client:
        yield gateway


async def _allocation(gateway: Gateway, key: str):
    return await gateway.client.get("/v1/allocation", headers={"Authorization": f"Bearer {key}"})


async def test_approved_grant_shows_requested_and_granted(gateway: Gateway) -> None:
    keys = await gateway.register("alpha")
    await recompute(gateway.pool, gateway.settings)

    response = await _allocation(gateway, keys["production"].raw)

    body = response.json()
    assert response.status_code == 200
    assert body["state"] == "approved"
    assert (
        body["requested"]
        == body["granted"]
        == {
            "requests_per_minute": 30,
            "concurrency": 4,
            "monthly_budget_usd": "3.00",
            "per_call_cost_ceiling_usd": "0.05",
        }
    )
    assert body["reasons"] == []
    assert body["profiles"] == ["chat"]
    assert (body["project_id"], body["environment"]) == ("alpha", "production")


async def test_reduced_grant_lists_reasons_side_by_side(gateway: Gateway) -> None:
    keys = await gateway.register("alpha")
    await gateway.register("beta")
    await recompute(gateway.pool, make_settings(allocation_capacity_concurrency=6))

    body = (await _allocation(gateway, keys["production"].raw)).json()

    assert body["state"] == "reduced"
    assert body["requested"]["concurrency"] == 4
    assert body["granted"]["concurrency"] < 4
    assert {"dimension": "concurrency", "code": "capacity_shared"} in body["reasons"]


async def test_missing_grant_is_pending_with_retry_after(gateway: Gateway) -> None:
    keys = await gateway.register("alpha")  # no recompute yet

    response = await _allocation(gateway, keys["development"].raw)

    assert response.status_code == 503
    assert response.json()["code"] == "allocation_pending"
    assert response.headers["Retry-After"] == "5"


async def test_a_project_sees_only_its_own_grant(gateway: Gateway) -> None:
    alpha = await gateway.register("alpha")
    await gateway.register("beta")
    await recompute(gateway.pool, gateway.settings)

    response = await _allocation(gateway, alpha["development"].raw)

    assert response.json()["project_id"] == "alpha"
    assert response.json()["environment"] == "development"
    assert "beta" not in response.text


async def test_allocation_requires_a_key(gateway: Gateway) -> None:
    response = await gateway.client.get("/v1/allocation")

    assert response.json()["code"] == "key_missing"
