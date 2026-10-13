from __future__ import annotations

import hashlib
from typing import Any

import anyio
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from llm_gateway.allocation.store import recompute
from llm_gateway.calls.admission import admit, reservation_micro_usd
from llm_gateway.calls.budget import check_budget_thresholds
from llm_gateway.calls.records import (
    AdmissionRefusedError,
    Admitted,
    AlreadyFinished,
    CallRequest,
    IllegalTransitionError,
)
from llm_gateway.calls.settlement import Outcome, get_call, recover_abandoned, settle
from llm_gateway.catalogue.model import get_catalogue
from llm_gateway.catalogue.pricing import TokenUsage
from llm_gateway.notifier.events import wait_for_background_alerts
from llm_gateway.projects import service
from llm_gateway.projects.contract import parse_contract
from llm_gateway.projects.keys import decode_pepper
from tests.conftest import TEST_PEPPER, make_settings
from tests.test_contract import contract_data

pytestmark = [pytest.mark.anyio, pytest.mark.db]
CATALOGUE = get_catalogue()
SETTINGS = make_settings()
PEPPER = decode_pepper(TEST_PEPPER)


async def _project(
    pool: AsyncConnectionPool,
    project_id: str = "alpha",
    settings: Any = SETTINGS,
    **production: Any,
) -> None:
    data = contract_data(project_id=project_id)
    data["request"]["production"].update(production)
    await service.register_project(pool, parse_contract(data), actor="t", pepper=PEPPER)
    await recompute(pool, settings)


def _request(call_id: str = "call-1", *, body: str = "x", **overrides: Any) -> CallRequest:
    values: dict[str, Any] = {
        "project_id": "alpha",
        "environment": "production",
        "provider_call_id": call_id,
        "request_fingerprint": hashlib.sha256(body.encode()).hexdigest(),
        "operation": "summarise",
        "provider": "openai",
        "model": "gpt-6-luna",
        "service_tier": "standard",
        "request_bytes": 400,  # reserves 200 input tokens
        "max_output_tokens": 1_000,
    }
    values.update(overrides)
    return CallRequest(**values)


async def _admit(pool: AsyncConnectionPool, request: CallRequest, settings: Any = SETTINGS):
    return await admit(pool, settings, CATALOGUE, request, actor="test")


async def _admitted(pool: AsyncConnectionPool, request: CallRequest) -> Admitted:
    result = await _admit(pool, request)
    assert isinstance(result, Admitted)
    return result


async def _complete(
    pool: AsyncConnectionPool, call_key: int, input_tokens: int = 200, output: int = 1_000
):
    return await settle(
        pool,
        SETTINGS,
        CATALOGUE,
        call_key,
        Outcome("completed", TokenUsage(input_tokens, output), service_tier_used="default"),
        actor="test",
    )


async def _refused(pool: AsyncConnectionPool, request: CallRequest, settings: Any = SETTINGS):
    with pytest.raises(AdmissionRefusedError) as raised:
        await _admit(pool, request, settings)
    return raised.value


async def _events(pool: AsyncConnectionPool, call_key: int) -> list[tuple[Any, ...]]:
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT from_state, to_state, charged_micro_usd, actor FROM call_events "
            "WHERE call_key = %s ORDER BY event_id",
            (call_key,),
        )
        return await cursor.fetchall()


async def _alerts(pool: AsyncConnectionPool) -> list[tuple[str, str, str]]:
    await wait_for_background_alerts()
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT event_type, severity, summary FROM notifier_events ORDER BY event_id"
        )
        return [(str(r[0]), str(r[1]), str(r[2])) for r in await cursor.fetchall()]


def test_reservation_is_the_worst_case_at_list_price() -> None:
    from datetime import date

    # 200 input tokens x 0.10 + 1000 output x 0.50 = 520 micro-USD
    assert reservation_micro_usd(CATALOGUE, _request(), date(2026, 10, 13)) == 520


async def test_admission_records_the_call_and_reports_the_allowance(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _project(migrated_pool, requests_per_minute=30, concurrency=4)

    result = await _admitted(migrated_pool, _request())

    call = await get_call(migrated_pool, result.call_key)
    assert (call.state, call.reserved_micro_usd) == ("admitted", 520)
    assert result.allowance.requests_per_minute_limit == 30
    assert result.allowance.requests_per_minute_remaining == 29
    assert (result.allowance.concurrency_limit, result.allowance.concurrency_remaining) == (4, 3)
    assert await _events(migrated_pool, result.call_key) == [(None, "admitted", None, "test")]


async def test_no_grant_is_allocation_pending(migrated_pool: AsyncConnectionPool) -> None:
    data = contract_data(project_id="alpha")
    await service.register_project(migrated_pool, parse_contract(data), actor="t", pepper=PEPPER)

    refusal = await _refused(migrated_pool, _request())

    assert (refusal.code, refusal.status, refusal.retry_after_seconds) == (
        "allocation_pending",
        503,
        5,
    )


async def test_reservation_over_the_ceiling_is_refused_and_not_retryable(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _project(migrated_pool, per_call_cost_ceiling_usd="0.02")

    refusal = await _refused(migrated_pool, _request(max_output_tokens=128_000))

    assert (refusal.code, refusal.status, refusal.retryable) == (
        "cost_ceiling_exceeded",
        422,
        False,
    )


async def test_full_concurrency_is_refused(migrated_pool: AsyncConnectionPool) -> None:
    await _project(migrated_pool, concurrency=2)
    await _admitted(migrated_pool, _request("a"))
    await _admitted(migrated_pool, _request("b"))

    refusal = await _refused(migrated_pool, _request("c"))

    assert (refusal.code, refusal.status, refusal.retry_after_seconds) == (
        "concurrency_exhausted",
        429,
        1,
    )


async def test_full_rate_window_is_refused_until_the_oldest_ages_out(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _project(migrated_pool, requests_per_minute=10, concurrency=10)
    for index in range(10):
        admitted = await _admitted(migrated_pool, _request(f"call-{index}"))
        await _complete(migrated_pool, admitted.call_key)

    refusal = await _refused(migrated_pool, _request("call-10"))

    assert (refusal.code, refusal.status) == ("rate_limited", 429)
    assert refusal.retry_after_seconds is not None and 1 <= refusal.retry_after_seconds <= 60


async def test_simultaneous_admissions_never_exceed_concurrency(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _project(migrated_pool, concurrency=3, requests_per_minute=100)
    outcomes: list[str] = []

    async def attempt(index: int) -> None:
        try:
            await _admit(migrated_pool, _request(f"race-{index}"))
            outcomes.append("admitted")
        except AdmissionRefusedError as exc:
            outcomes.append(exc.code)

    async with anyio.create_task_group() as group:
        for index in range(10):
            group.start_soon(attempt, index)

    assert outcomes.count("admitted") == 3
    assert outcomes.count("concurrency_exhausted") == 7


async def test_spent_grant_budget_is_refused_until_next_month(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _project(migrated_pool, monthly_budget_usd="0.001", per_call_cost_ceiling_usd="0.001")
    first = await _admitted(migrated_pool, _request("a"))
    await _complete(migrated_pool, first.call_key)  # 520 of 1000 spent

    refusal = await _refused(migrated_pool, _request("b"))

    assert (refusal.code, refusal.status) == ("budget_exhausted", 429)
    assert refusal.retry_after_seconds is not None and refusal.retry_after_seconds > 0


async def test_global_cap_is_refused_and_alerts(migrated_pool: AsyncConnectionPool) -> None:
    await _project(migrated_pool)
    await _project(migrated_pool, "beta")
    async with migrated_pool.connection() as connection:
        # Another project already spent the whole USD 5 cap this month.
        await connection.execute(
            "INSERT INTO calls (project_id, environment, provider_call_id, request_fingerprint, "
            "operation, provider, model, service_tier_requested, state, reserved_micro_usd, "
            "charged_micro_usd, finished_at) VALUES ('beta', 'production', 'spent', %s, "
            "'summarise', 'openai', 'gpt-6-luna', 'standard', 'completed', 0, 5000000, now())",
            ("0" * 64,),
        )

    refusal = await _refused(migrated_pool, _request())

    assert refusal.code == "budget_exhausted"
    assert ("global_budget_exhausted", "critical") in [a[:2] for a in await _alerts(migrated_pool)]


async def test_same_call_id_rules(migrated_pool: AsyncConnectionPool) -> None:
    await _project(migrated_pool)
    first = await _admitted(migrated_pool, _request("same", body="original"))

    conflict = await _refused(migrated_pool, _request("same", body="changed"))
    in_progress = await _refused(migrated_pool, _request("same", body="original"))
    await _complete(migrated_pool, first.call_key)
    finished = await _admit(migrated_pool, _request("same", body="original"))

    assert (conflict.code, conflict.status) == ("idempotency_conflict", 409)
    assert (in_progress.code, in_progress.retry_after_seconds) == ("call_in_progress", 2)
    assert isinstance(finished, AlreadyFinished)
    assert finished.call.state == "completed"


async def test_retryable_failure_is_admitted_again_with_an_audit_trail(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _project(migrated_pool)
    first = await _admitted(migrated_pool, _request("retry-me"))
    await settle(
        migrated_pool, SETTINGS, CATALOGUE, first.call_key,
        Outcome("retryable_failure", failure_code="provider_unavailable"), actor="test",
    )  # fmt: skip

    again = await _admitted(migrated_pool, _request("retry-me"))
    await _complete(migrated_pool, again.call_key)

    assert again.call_key == first.call_key
    assert [(e[0], e[1]) for e in await _events(migrated_pool, first.call_key)] == [
        (None, "admitted"),
        ("admitted", "retryable_failure"),
        ("retryable_failure", "admitted"),
        ("admitted", "completed"),
    ]


async def test_settlement_charges_by_outcome(migrated_pool: AsyncConnectionPool) -> None:
    await _project(migrated_pool, concurrency=8)
    completed = await _admitted(migrated_pool, _request("ok"))
    failed = await _admitted(migrated_pool, _request("bad"))
    unknown = await _admitted(migrated_pool, _request("lost"))

    done = await settle(
        migrated_pool, SETTINGS, CATALOGUE, completed.call_key,
        Outcome("completed", TokenUsage(2000, 500, cached_input_tokens=1024), service_tier_used="default"),
        actor="test",
    )  # fmt: skip
    rejected = await settle(
        migrated_pool, SETTINGS, CATALOGUE, failed.call_key,
        Outcome("failed", failure_code="provider_invalid_request"), actor="test",
    )  # fmt: skip
    lost = await settle(
        migrated_pool, SETTINGS, CATALOGUE, unknown.call_key,
        Outcome("execution_unknown", failure_code="execution_unknown"), actor="test",
    )  # fmt: skip

    assert (done.cost_micro_usd, done.charged_micro_usd) == (358, 358)
    assert (rejected.cost_micro_usd, rejected.charged_micro_usd) == (None, 0)
    assert (lost.cost_micro_usd, lost.charged_micro_usd) == (None, 520)


async def test_price_snapshot_is_kept(migrated_pool: AsyncConnectionPool) -> None:
    await _project(migrated_pool)
    admitted = await _admitted(migrated_pool, _request())
    await _complete(migrated_pool, admitted.call_key)

    async with migrated_pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT price_tier, price_effective_from::text FROM calls WHERE call_key = %s",
            (admitted.call_key,),
        )
        row = await cursor.fetchone()

    assert row == ("standard", "2026-10-13")


async def test_unpriced_usage_counts_the_reservation_and_alerts(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _project(migrated_pool)
    admitted = await _admitted(migrated_pool, _request())

    settled = await settle(
        migrated_pool, SETTINGS, CATALOGUE, admitted.call_key,
        Outcome("completed", TokenUsage(100, 100), service_tier_used="priority"), actor="test",
    )  # fmt: skip

    assert (settled.cost_micro_usd, settled.charged_micro_usd) == (None, 520)
    assert [a[:2] for a in await _alerts(migrated_pool)] == [("call_unpriced", "error")]


async def test_illegal_transition_is_rejected(migrated_pool: AsyncConnectionPool) -> None:
    await _project(migrated_pool)
    admitted = await _admitted(migrated_pool, _request())
    await _complete(migrated_pool, admitted.call_key)

    with pytest.raises(IllegalTransitionError, match="from completed to failed"):
        await settle(
            migrated_pool, SETTINGS, CATALOGUE, admitted.call_key, Outcome("failed"), actor="test"
        )


async def test_audit_events_are_append_only(migrated_pool: AsyncConnectionPool) -> None:
    await _project(migrated_pool)
    admitted = await _admitted(migrated_pool, _request())

    async with migrated_pool.connection() as connection:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            await connection.execute(
                "UPDATE call_events SET actor = 'x' WHERE call_key = %s", (admitted.call_key,)
            )


async def test_last_months_spend_does_not_count(migrated_pool: AsyncConnectionPool) -> None:
    await _project(migrated_pool, monthly_budget_usd="0.001", per_call_cost_ceiling_usd="0.001")
    first = await _admitted(migrated_pool, _request("old"))
    await _complete(migrated_pool, first.call_key)
    async with migrated_pool.connection() as connection:
        await connection.execute(
            "UPDATE calls SET admitted_at = admitted_at - interval '40 days' WHERE call_key = %s",
            (first.call_key,),
        )

    assert isinstance(await _admit(migrated_pool, _request("new")), Admitted)


async def test_abandoned_call_becomes_unknown_and_frees_its_slot(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _project(migrated_pool, concurrency=1)
    stuck = await _admitted(migrated_pool, _request("stuck"))
    async with migrated_pool.connection() as connection:
        await connection.execute(
            "UPDATE calls SET lease_expires_at = now() - interval '1 second' WHERE call_key = %s",
            (stuck.call_key,),
        )

    assert await recover_abandoned(migrated_pool) == 1

    call = await get_call(migrated_pool, stuck.call_key)
    assert (call.state, call.failure_code, call.charged_micro_usd) == (
        "execution_unknown",
        "execution_abandoned",
        520,
    )
    assert isinstance(await _admit(migrated_pool, _request("next")), Admitted)
    replay = await _admit(migrated_pool, _request("stuck"))
    assert isinstance(replay, AlreadyFinished) and replay.call.state == "execution_unknown"


async def test_budget_thresholds_alert_once_per_month(migrated_pool: AsyncConnectionPool) -> None:
    await _project(migrated_pool, monthly_budget_usd="0.001", per_call_cost_ceiling_usd="0.001")
    first = await _admitted(migrated_pool, _request("a"))
    await _complete(migrated_pool, first.call_key, input_tokens=200, output=1_000)  # 520 = 52%
    second = await _admitted(migrated_pool, _request("b", request_bytes=0, max_output_tokens=600))
    await _complete(migrated_pool, second.call_key, input_tokens=0, output=600)  # +300 = 82%

    repeated = await check_budget_thresholds(migrated_pool, SETTINGS)  # as after a restart

    alerts = [a for a in await _alerts(migrated_pool) if a[0] == "budget_threshold"]
    assert [a[1] for a in alerts] == ["info", "warning"]
    assert (
        "Grant alpha/production has used 80% of its monthly budget: 0.00082 of 0.001"
        in alerts[1][2]
    )
    assert repeated == 0


async def test_worker_recovers_abandoned_calls(migrated_pool: AsyncConnectionPool) -> None:
    import asyncio

    from llm_gateway.worker import run_worker

    await _project(migrated_pool)
    stuck = await _admitted(migrated_pool, _request("stuck"))
    async with migrated_pool.connection() as connection:
        await connection.execute(
            "UPDATE calls SET lease_expires_at = now() - interval '1 second' WHERE call_key = %s",
            (stuck.call_key,),
        )
    stop = asyncio.Event()

    async def stop_when_recovered() -> None:
        while (await get_call(migrated_pool, stuck.call_key)).state == "admitted":
            await anyio.sleep(0.02)
        stop.set()

    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(stop_when_recovered)
            group.start_soon(
                lambda: run_worker(SETTINGS, migrated_pool, None, stop, poll_interval_seconds=0.05)
            )

    assert (await get_call(migrated_pool, stuck.call_key)).state == "execution_unknown"
