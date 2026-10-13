from __future__ import annotations

import anyio
import pytest
from psycopg_pool import AsyncConnectionPool

from llm_gateway.notifier.channels import DeliveryResult
from llm_gateway.notifier.delivery import MAX_ATTEMPTS, backoff_seconds, deliver_due
from llm_gateway.notifier.events import raise_alert
from tests.notifier_fakes import FakeChannel

pytestmark = [pytest.mark.anyio, pytest.mark.db]
MAX_AGE = 86_400


async def _raise(pool: AsyncConnectionPool, key: str = "k") -> None:
    await raise_alert(
        pool,
        event_type="test_event",
        severity="error",
        summary="Something needs attention.",
        key=key,
        dedupe_window_seconds=900,
    )


async def _state(pool: AsyncConnectionPool) -> tuple[object, ...]:
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT status, attempts, last_error, claimed_until IS NULL, "
            "extract(epoch FROM next_attempt_at - now())::int "
            "FROM notifier_events ORDER BY event_id LIMIT 1"
        )
        row = await cursor.fetchone()
    assert row is not None
    return tuple(row)


async def _make_due(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as connection:
        await connection.execute("UPDATE notifier_events SET next_attempt_at = now()")


async def test_pending_event_is_delivered(migrated_pool: AsyncConnectionPool) -> None:
    channel = FakeChannel()
    await _raise(migrated_pool)

    sent = await deliver_due(migrated_pool, channel, max_age_seconds=MAX_AGE)

    assert sent == 1
    assert channel.sent[0].startswith("[ERROR] test_event\nSomething needs attention.")
    assert (await _state(migrated_pool))[:4] == ("delivered", 1, None, True)


async def test_delivered_event_is_not_sent_again(migrated_pool: AsyncConnectionPool) -> None:
    channel = FakeChannel()
    await _raise(migrated_pool)
    await deliver_due(migrated_pool, channel, max_age_seconds=MAX_AGE)

    await _raise(migrated_pool)  # a repeat inside the window is only counted
    await deliver_due(migrated_pool, channel, max_age_seconds=MAX_AGE)

    assert len(channel.sent) == 1


async def test_temporary_failure_is_retried_later(migrated_pool: AsyncConnectionPool) -> None:
    channel = FakeChannel(DeliveryResult("retry", code="http_502"))
    await _raise(migrated_pool)

    await deliver_due(migrated_pool, channel, max_age_seconds=MAX_AGE)

    status, attempts, error, unclaimed, due_in = await _state(migrated_pool)
    assert (status, attempts, error, unclaimed) == ("pending", 1, "http_502", True)
    assert 25 <= int(str(due_in)) <= 30
    assert await deliver_due(migrated_pool, channel, max_age_seconds=MAX_AGE) == 0

    await _make_due(migrated_pool)
    await deliver_due(migrated_pool, channel, max_age_seconds=MAX_AGE)

    assert (await _state(migrated_pool))[:2] == ("delivered", 2)


async def test_channel_retry_delay_is_honoured(migrated_pool: AsyncConnectionPool) -> None:
    channel = FakeChannel(DeliveryResult("retry", code="http_429", retry_after_seconds=90))
    await _raise(migrated_pool)

    await deliver_due(migrated_pool, channel, max_age_seconds=MAX_AGE)

    assert 85 <= int(str((await _state(migrated_pool))[4])) <= 90


async def test_permanent_rejection_fails_without_retry(
    migrated_pool: AsyncConnectionPool,
) -> None:
    channel = FakeChannel(DeliveryResult("rejected", code="http_403"))
    await _raise(migrated_pool)

    await deliver_due(migrated_pool, channel, max_age_seconds=MAX_AGE)
    await _make_due(migrated_pool)
    await deliver_due(migrated_pool, channel, max_age_seconds=MAX_AGE)

    assert (await _state(migrated_pool))[:3] == ("failed", 1, "http_403")
    assert len(channel.sent) == 1


async def test_attempts_are_bounded(migrated_pool: AsyncConnectionPool) -> None:
    channel = FakeChannel(*[DeliveryResult("retry", code="http_500")] * (MAX_ATTEMPTS + 2))
    await _raise(migrated_pool)

    for _ in range(MAX_ATTEMPTS + 2):
        await _make_due(migrated_pool)
        await deliver_due(migrated_pool, channel, max_age_seconds=MAX_AGE)

    assert (await _state(migrated_pool))[:2] == ("failed", MAX_ATTEMPTS)
    assert len(channel.sent) == MAX_ATTEMPTS


async def test_stale_event_expires_unsent(migrated_pool: AsyncConnectionPool) -> None:
    channel = FakeChannel()
    await _raise(migrated_pool)
    async with migrated_pool.connection() as connection:
        await connection.execute(
            "UPDATE notifier_events SET first_seen_at = now() - interval '2 days'"
        )

    await deliver_due(migrated_pool, channel, max_age_seconds=MAX_AGE)

    assert (await _state(migrated_pool))[0] == "expired"
    assert channel.sent == []


async def test_without_a_channel_events_stay_pending(migrated_pool: AsyncConnectionPool) -> None:
    await _raise(migrated_pool)

    assert await deliver_due(migrated_pool, None, max_age_seconds=MAX_AGE) == 0

    assert (await _state(migrated_pool))[:2] == ("pending", 0)


async def test_event_raised_while_no_worker_runs_is_delivered_later(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _raise(migrated_pool)
    await deliver_due(migrated_pool, None, max_age_seconds=MAX_AGE)  # worker without channel

    channel = FakeChannel()
    await deliver_due(migrated_pool, channel, max_age_seconds=MAX_AGE)

    assert len(channel.sent) == 1


async def test_two_workers_never_send_the_same_event_twice(
    migrated_pool: AsyncConnectionPool,
) -> None:
    for index in range(6):
        await _raise(migrated_pool, key=f"k{index}")
    first, second = FakeChannel(delay_seconds=0.05), FakeChannel(delay_seconds=0.05)

    async with anyio.create_task_group() as group:
        group.start_soon(lambda: deliver_due(migrated_pool, first, max_age_seconds=MAX_AGE))
        group.start_soon(lambda: deliver_due(migrated_pool, second, max_age_seconds=MAX_AGE))

    assert len(first.sent) + len(second.sent) == 6


async def test_expired_claim_is_retried(migrated_pool: AsyncConnectionPool) -> None:
    await _raise(migrated_pool)
    async with migrated_pool.connection() as connection:
        # A worker claimed the event and died before recording a result.
        await connection.execute(
            "UPDATE notifier_events SET attempts = 1, claimed_until = now() - interval '1 second'"
        )
    channel = FakeChannel()

    await deliver_due(migrated_pool, channel, max_age_seconds=MAX_AGE)

    assert (await _state(migrated_pool))[:2] == ("delivered", 2)


def test_backoff_schedule() -> None:
    assert [backoff_seconds(n) for n in range(1, 8)] == [30, 120, 600, 1800, 3600, 3600, 3600]
