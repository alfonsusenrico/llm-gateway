from __future__ import annotations

import logging

import anyio
import pytest
from psycopg_pool import AsyncConnectionPool

from llm_gateway.notifier.events import dedupe_key, raise_alert, record_alert
from tests.conftest import UNREACHABLE_DATABASE_URL

pytestmark = pytest.mark.anyio
WINDOW = 900


async def _raise(pool: AsyncConnectionPool, key: str = "k", summary: str = "Summary.") -> None:
    await raise_alert(
        pool,
        event_type="test_event",
        severity="warning",
        summary=summary,
        key=key,
        dedupe_window_seconds=WINDOW,
    )


async def _rows(pool: AsyncConnectionPool) -> list[tuple[object, ...]]:
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT dedupe_key, occurrences, status, is_open, summary "
            "FROM notifier_events ORDER BY event_id"
        )
        return await cursor.fetchall()


def test_dedupe_key_joins_parts_and_is_bounded() -> None:
    assert dedupe_key("project_key_rejected", "personal_agent", "prod") == (
        "project_key_rejected:personal_agent:prod"
    )
    assert len(dedupe_key("x", "y" * 500)) == 200


@pytest.mark.db
async def test_alert_is_recorded_as_pending(migrated_pool: AsyncConnectionPool) -> None:
    await _raise(migrated_pool)

    assert await _rows(migrated_pool) == [("k", 1, "pending", True, "Summary.")]


@pytest.mark.db
async def test_burst_is_counted_on_one_event(migrated_pool: AsyncConnectionPool) -> None:
    for _ in range(20):
        await _raise(migrated_pool)

    assert await _rows(migrated_pool) == [("k", 20, "pending", True, "Summary.")]


@pytest.mark.db
async def test_alert_after_the_window_opens_a_new_event(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _raise(migrated_pool)
    async with migrated_pool.connection() as connection:
        await connection.execute(
            "UPDATE notifier_events SET window_ends_at = now() - interval '1 second'"
        )

    await _raise(migrated_pool)

    rows = await _rows(migrated_pool)
    assert [(row[1], row[3]) for row in rows] == [(1, False), (1, True)]


@pytest.mark.db
async def test_concurrent_identical_alerts_share_one_event(
    migrated_pool: AsyncConnectionPool,
) -> None:
    async with anyio.create_task_group() as group:
        for _ in range(10):
            group.start_soon(_raise, migrated_pool)

    assert await _rows(migrated_pool) == [("k", 10, "pending", True, "Summary.")]


@pytest.mark.db
async def test_concurrent_alerts_after_the_window_open_exactly_one_new_event(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _raise(migrated_pool)
    async with migrated_pool.connection() as connection:
        await connection.execute(
            "UPDATE notifier_events SET window_ends_at = now() - interval '1 second'"
        )

    async with anyio.create_task_group() as group:
        for _ in range(10):
            group.start_soon(_raise, migrated_pool)

    rows = await _rows(migrated_pool)
    assert [(row[1], row[3]) for row in rows] == [(1, False), (10, True)]


@pytest.mark.db
async def test_long_summary_is_truncated(migrated_pool: AsyncConnectionPool) -> None:
    await _raise(migrated_pool, summary="x" * 400)

    rows = await _rows(migrated_pool)
    assert len(str(rows[0][4])) == 300


@pytest.mark.db
async def test_alert_survives_the_callers_rollback(
    migrated_pool: AsyncConnectionPool,
) -> None:
    with pytest.raises(RuntimeError, match="request failed"):
        async with migrated_pool.connection() as caller, caller.transaction():
            await caller.execute("SELECT 1")
            await _raise(migrated_pool)
            raise RuntimeError("request failed")

    assert len(await _rows(migrated_pool)) == 1


async def test_database_down_never_raises_to_the_caller(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool: AsyncConnectionPool = AsyncConnectionPool(
        UNREACHABLE_DATABASE_URL, min_size=1, open=False
    )
    await pool.open(wait=False)
    try:
        with caplog.at_level(logging.WARNING):
            await _raise(pool, summary="secret-looking summary")
    finally:
        await pool.close()

    assert "test_event could not be recorded: PoolTimeout" in caplog.text
    assert "secret-looking" not in caplog.text


@pytest.mark.db
async def test_record_alert_raises_for_admin_use(migrated_pool: AsyncConnectionPool) -> None:
    async with migrated_pool.connection() as connection:
        with pytest.raises(Exception, match="check"):
            await record_alert(
                connection,
                event_type="Not Valid",
                severity="info",
                summary="Summary.",
                key="k",
                dedupe_window_seconds=WINDOW,
            )
