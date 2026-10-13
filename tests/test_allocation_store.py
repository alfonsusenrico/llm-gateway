from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from llm_gateway.allocation.store import get_grant, list_grants, recompute, set_weight
from llm_gateway.notifier.events import wait_for_background_alerts
from llm_gateway.projects import service
from llm_gateway.projects.contract import parse_contract
from llm_gateway.projects.keys import decode_pepper
from tests.conftest import TEST_PEPPER, make_settings
from tests.test_contract import contract_data

pytestmark = [pytest.mark.anyio, pytest.mark.db]
PEPPER = decode_pepper(TEST_PEPPER)
SETTINGS = make_settings()


async def _register(pool: AsyncConnectionPool, project_id: str, concurrency: int = 4) -> None:
    data = contract_data(project_id=project_id)
    data["request"]["production"]["concurrency"] = concurrency
    await service.register_project(pool, parse_contract(data), actor="t", pepper=PEPPER)


async def _set(pool: AsyncConnectionPool, project_id: str, **fields: Any) -> None:
    def change(contract: dict[str, Any]) -> dict[str, Any]:
        contract.update({k: v for k, v in fields.items() if k == "status"})
        contract["request"]["production"].update({k: v for k, v in fields.items() if k != "status"})
        return contract

    await service.update_contract(pool, project_id, change, actor="t", note="change")


async def _alerts(pool: AsyncConnectionPool) -> list[tuple[str, str]]:
    await wait_for_background_alerts()
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT event_type, summary FROM notifier_events ORDER BY event_id"
        )
        return [(str(row[0]), str(row[1])) for row in await cursor.fetchall()]


async def test_registration_gets_grants_for_both_environments(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _register(migrated_pool, "alpha")

    result = await recompute(migrated_pool, SETTINGS)

    grants = await list_grants(migrated_pool)
    assert result.changed
    assert [(g.project_id, g.environment, g.state) for g in grants] == [
        ("alpha", "development", "approved"),
        ("alpha", "production", "approved"),
    ]
    assert grants[1].monthly_budget_micro_usd == 3_000_000


async def test_unchanged_inputs_write_nothing_and_alert_nothing(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _register(migrated_pool, "alpha")
    await recompute(migrated_pool, SETTINGS)
    before = await list_grants(migrated_pool)

    result = await recompute(migrated_pool, SETTINGS)

    assert result.changed is False
    assert await list_grants(migrated_pool) == before
    assert await _alerts(migrated_pool) == []


async def test_disabling_removes_grants_and_returns_the_share(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _register(migrated_pool, "alpha", concurrency=6)
    await _register(migrated_pool, "beta", concurrency=6)
    await recompute(migrated_pool, SETTINGS)
    await _set(migrated_pool, "alpha", status="disabled")

    await recompute(migrated_pool, SETTINGS)

    assert await get_grant(migrated_pool, "alpha", "production") is None
    beta = await get_grant(migrated_pool, "beta", "production")
    assert beta is not None and (beta.concurrency, beta.state) == (6, "approved")


async def test_reduction_and_restoration_alert_the_owner(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _register(migrated_pool, "alpha", concurrency=6)
    await recompute(migrated_pool, SETTINGS)

    await _register(migrated_pool, "beta", concurrency=6)  # 6 + 6 + 2 + 2 > 8
    await recompute(migrated_pool, SETTINGS)
    await _set(migrated_pool, "beta", status="disabled")
    await recompute(migrated_pool, SETTINGS)

    alerts = await _alerts(migrated_pool)
    reduced = [summary for event, summary in alerts if event == "grant_reduced"]
    restored = [summary for event, summary in alerts if event == "grant_restored"]
    assert any(
        s.startswith("Grant for alpha/production reduced: concurrency 6 to") for s in reduced
    )
    assert "Grant for alpha/production is fully approved again." in restored


async def test_weight_change_shifts_the_shares(migrated_pool: AsyncConnectionPool) -> None:
    await _register(migrated_pool, "alpha", concurrency=8)
    await _register(migrated_pool, "beta", concurrency=8)
    await recompute(migrated_pool, SETTINGS)
    even = await get_grant(migrated_pool, "alpha", "production")

    assert await set_weight(migrated_pool, "alpha", 3)
    await recompute(migrated_pool, SETTINGS)

    heavier = await get_grant(migrated_pool, "alpha", "production")
    assert even is not None and heavier is not None
    assert heavier.concurrency > even.concurrency
    assert await set_weight(migrated_pool, "ghost", 2) is False


async def test_lower_capacity_settings_recompute_on_the_next_run(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _register(migrated_pool, "alpha", concurrency=6)
    await recompute(migrated_pool, SETTINGS)

    smaller = make_settings(allocation_capacity_concurrency=4)
    result = await recompute(migrated_pool, smaller)

    grant = await get_grant(migrated_pool, "alpha", "production")
    assert result.changed
    assert grant is not None and grant.concurrency < 6 and grant.state == "reduced"


async def test_global_budget_cap_holds_across_projects(
    migrated_pool: AsyncConnectionPool,
) -> None:
    for name in ("alpha", "beta", "gamma"):
        await _register(migrated_pool, name)  # 3.00 + 0.50 each = 10.50 requested

    await recompute(migrated_pool, SETTINGS)

    grants = await list_grants(migrated_pool)
    total = sum(g.monthly_budget_micro_usd for g in grants)
    assert total <= int(SETTINGS.global_monthly_budget_usd * Decimal(1_000_000))
