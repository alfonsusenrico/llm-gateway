"""Worker-side delivery of pending alerts: claim, send outside any lock, record."""

from __future__ import annotations

import logging

from psycopg_pool import AsyncConnectionPool

from llm_gateway.notifier.channels import Channel, DeliveryResult, render_message
from llm_gateway.notifier.events import AlertEvent

MAX_ATTEMPTS = 8
BATCH_SIZE = 10
# Long enough for one channel call with its timeout; an expired claim is retried.
CLAIM_SECONDS = 60
# Delay before attempt n+1, indexed by attempts already made; hourly afterwards.
_BACKOFF_SECONDS = (30, 120, 600, 1800)
_HOURLY_SECONDS = 3600
logger = logging.getLogger(__name__)


def backoff_seconds(attempts_made: int) -> int:
    if 1 <= attempts_made <= len(_BACKOFF_SECONDS):
        return _BACKOFF_SECONDS[attempts_made - 1]
    return _HOURLY_SECONDS


async def expire_stale(pool: AsyncConnectionPool, *, max_age_seconds: int) -> int:
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "UPDATE notifier_events SET status = 'expired', claimed_until = NULL "
            "WHERE status = 'pending' AND first_seen_at < now() - make_interval(secs => %s)",
            (max_age_seconds,),
        )
        return cursor.rowcount


async def claim_due(pool: AsyncConnectionPool, *, limit: int = BATCH_SIZE) -> list[AlertEvent]:
    """Claim due events and count the attempt; the row locks end with this transaction."""
    async with pool.connection() as connection:
        cursor = await connection.execute(
            """
            UPDATE notifier_events
            SET claimed_until = now() + make_interval(secs => %s), attempts = attempts + 1
            WHERE event_id IN (
                SELECT event_id FROM notifier_events
                WHERE status = 'pending' AND next_attempt_at <= now()
                  AND (claimed_until IS NULL OR claimed_until < now())
                ORDER BY next_attempt_at, event_id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            RETURNING event_id, event_type, severity, summary, occurrences, first_seen_at,
                      last_seen_at, status, attempts
            """,
            (CLAIM_SECONDS, limit),
        )
        rows = await cursor.fetchall()
    return sorted((AlertEvent(*row) for row in rows), key=lambda event: event.event_id)


async def record_result(
    pool: AsyncConnectionPool, event: AlertEvent, result: DeliveryResult
) -> None:
    async with pool.connection() as connection:
        if result.outcome == "delivered":
            await connection.execute(
                "UPDATE notifier_events SET status = 'delivered', delivered_at = now(), "
                "claimed_until = NULL, last_error = NULL WHERE event_id = %s",
                (event.event_id,),
            )
            return
        if result.outcome == "rejected" or event.attempts >= MAX_ATTEMPTS:
            await connection.execute(
                "UPDATE notifier_events SET status = 'failed', claimed_until = NULL, "
                "last_error = %s WHERE event_id = %s",
                (result.code, event.event_id),
            )
            logger.warning(
                "Alert %s failed after %d attempts: %s",
                event.event_id,
                event.attempts,
                result.code,
            )
            return
        delay = max(backoff_seconds(event.attempts), result.retry_after_seconds or 0)
        await connection.execute(
            "UPDATE notifier_events SET next_attempt_at = now() + make_interval(secs => %s), "
            "claimed_until = NULL, last_error = %s WHERE event_id = %s",
            (delay, result.code, event.event_id),
        )


async def deliver_due(
    pool: AsyncConnectionPool, channel: Channel | None, *, max_age_seconds: int
) -> int:
    """Expire stale events, then send one batch of due events; return how many were sent."""
    await expire_stale(pool, max_age_seconds=max_age_seconds)
    if channel is None:
        return 0
    events = await claim_due(pool)
    for event in events:
        result = await channel.send(render_message(event))
        await record_result(pool, event, result)
    return len(events)
