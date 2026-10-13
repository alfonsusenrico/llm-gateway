"""Recording owner alerts as durable, deduplicated events.

Callers pass identifiers and fixed sentences only. Prompts, model output, keys,
and provider error text must never reach an alert.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

Severity = Literal["info", "warning", "error", "critical"]
EventStatus = Literal["pending", "delivered", "failed", "expired"]

SUMMARY_MAX_CHARS = 300
DEDUPE_KEY_MAX_CHARS = 200
# Bounds how long raising an alert may wait for a pooled connection.
_RAISE_TIMEOUT_SECONDS = 2.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AlertEvent:
    event_id: int
    event_type: str
    severity: Severity
    summary: str
    occurrences: int
    first_seen_at: datetime
    last_seen_at: datetime
    status: EventStatus
    attempts: int


def dedupe_key(event_type: str, *parts: str) -> str:
    return ":".join((event_type, *parts))[:DEDUPE_KEY_MAX_CHARS]


async def record_alert(
    connection: AsyncConnection,
    *,
    event_type: str,
    severity: Severity,
    summary: str,
    key: str,
    dedupe_window_seconds: int,
) -> int:
    """Store an alert or count it on the open event with the same key; return the event ID.

    Raises on database errors. Request paths use `raise_alert`, which never does.
    """
    async with connection.transaction():
        # Close the key's window first so a late repeat opens a new event.
        await connection.execute(
            "UPDATE notifier_events SET is_open = false "
            "WHERE dedupe_key = %s AND is_open AND window_ends_at <= now()",
            (key,),
        )
        cursor = await connection.execute(
            """
            INSERT INTO notifier_events (event_type, severity, dedupe_key, summary, window_ends_at)
            VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))
            ON CONFLICT (dedupe_key) WHERE is_open
            DO UPDATE SET occurrences = notifier_events.occurrences + 1, last_seen_at = now()
            RETURNING event_id
            """,
            (event_type, severity, key, summary[:SUMMARY_MAX_CHARS], dedupe_window_seconds),
        )
        row = await cursor.fetchone()
    if row is None:  # INSERT ... RETURNING always yields a row
        raise RuntimeError("Alert insert returned no row.")
    return int(row[0])


async def raise_alert(
    pool: AsyncConnectionPool,
    *,
    event_type: str,
    severity: Severity,
    summary: str,
    key: str,
    dedupe_window_seconds: int,
) -> None:
    """Record an alert on its own connection and transaction; never raise to the caller."""
    try:
        async with pool.connection(timeout=_RAISE_TIMEOUT_SECONDS) as connection:
            await record_alert(
                connection,
                event_type=event_type,
                severity=severity,
                summary=summary,
                key=key,
                dedupe_window_seconds=dedupe_window_seconds,
            )
    except Exception as exc:
        logger.warning("Alert %s could not be recorded: %s", event_type, type(exc).__name__)


async def get_event(connection: AsyncConnection, event_id: int) -> AlertEvent | None:
    cursor = await connection.execute(
        "SELECT event_id, event_type, severity, summary, occurrences, first_seen_at, "
        "last_seen_at, status, attempts FROM notifier_events WHERE event_id = %s",
        (event_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return AlertEvent(*row)
