from __future__ import annotations

import asyncio
import logging

import anyio
import httpx2
import pytest
from psycopg_pool import AsyncConnectionPool

from llm_gateway.notifier.events import raise_alert
from llm_gateway.worker import build_channel, run_worker
from tests.conftest import make_settings
from tests.notifier_fakes import FakeChannel

pytestmark = pytest.mark.anyio


def test_channel_is_built_only_when_telegram_is_configured() -> None:
    client = httpx2.AsyncClient()
    assert build_channel(make_settings(), client) is None
    configured = make_settings(
        notifier_telegram_bot_token="1:abc", notifier_telegram_chat_id="-100"
    )
    channel = build_channel(configured, client)
    assert channel is not None
    assert channel.name == "telegram"


@pytest.mark.db
async def test_worker_delivers_and_stops_when_asked(
    migrated_pool: AsyncConnectionPool,
) -> None:
    channel = FakeChannel()
    stop = asyncio.Event()
    await raise_alert(
        migrated_pool,
        event_type="test_event",
        severity="info",
        summary="Hello.",
        key="k",
        dedupe_window_seconds=900,
    )

    async def stop_after_delivery() -> None:
        while not channel.sent:
            await anyio.sleep(0.01)
        stop.set()

    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(stop_after_delivery)
            group.start_soon(
                lambda: run_worker(
                    make_settings(), migrated_pool, channel, stop, poll_interval_seconds=0.05
                )
            )

    assert len(channel.sent) == 1


async def test_disabled_delivery_is_logged_once_and_the_loop_survives_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A pool that never opened makes every tick fail; the worker must keep running.
    pool: AsyncConnectionPool = AsyncConnectionPool(
        "postgresql://unused@127.0.0.1:1/unused", open=False
    )
    stop = asyncio.Event()

    async def stop_soon() -> None:
        await anyio.sleep(0.2)
        stop.set()

    with caplog.at_level(logging.INFO), anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(stop_soon)
            group.start_soon(
                lambda: run_worker(make_settings(), pool, None, stop, poll_interval_seconds=0.05)
            )

    assert caplog.text.count("Alert delivery is disabled") == 1
    assert "Alert delivery tick failed: PoolClosed" in caplog.text
