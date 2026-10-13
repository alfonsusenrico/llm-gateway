"""Admin commands for the owner notifier."""

from __future__ import annotations

import asyncio
import time
import uuid

from psycopg_pool import AsyncConnectionPool

from llm_gateway.notifier.events import EventStatus, dedupe_key, get_event, record_alert

TEST_WAIT_SECONDS = 30.0
_POLL_SECONDS = 1.0


async def send_test_alert(
    pool: AsyncConnectionPool,
    *,
    dedupe_window_seconds: int,
    wait_seconds: float = TEST_WAIT_SECONDS,
) -> EventStatus:
    """Raise a test alert and wait for the worker to finish with it; return its status."""
    # A unique key bypasses deduplication, so every test produces a message.
    key = dedupe_key("notifier_test", uuid.uuid4().hex)
    async with pool.connection() as connection:
        event_id = await record_alert(
            connection,
            event_type="notifier_test",
            severity="info",
            summary="Test alert from llm-gateway-admin. Delivery works.",
            key=key,
            dedupe_window_seconds=dedupe_window_seconds,
        )
    deadline = time.monotonic() + wait_seconds
    while True:
        async with pool.connection() as connection:
            event = await get_event(connection, event_id)
        if event is None:
            raise RuntimeError("Test alert disappeared.")
        if event.status != "pending" or time.monotonic() >= deadline:
            return event.status
        await asyncio.sleep(_POLL_SECONDS)
