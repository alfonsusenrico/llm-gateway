"""Background worker process: delivers owner alerts and keeps grants current."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
import time
from collections.abc import Callable

import httpx2
from psycopg_pool import AsyncConnectionPool

from llm_gateway.allocation.store import recompute
from llm_gateway.db.pool import create_pool
from llm_gateway.logging_setup import configure_logging
from llm_gateway.notifier.channels import Channel, TelegramChannel
from llm_gateway.notifier.delivery import BATCH_SIZE, deliver_due
from llm_gateway.settings import Settings, SettingsError, load_settings

POLL_INTERVAL_SECONDS = 2.0
# Picks up capacity settings changed by a restart; edits recompute immediately anyway.
RECOMPUTE_INTERVAL_SECONDS = 60.0
logger = logging.getLogger(__name__)


def build_channel(settings: Settings, client: httpx2.AsyncClient) -> Channel | None:
    if settings.notifier_telegram_bot_token is None or not settings.notifier_telegram_chat_id:
        return None
    return TelegramChannel(
        bot_token=settings.notifier_telegram_bot_token.get_secret_value(),
        chat_id=settings.notifier_telegram_chat_id,
        client=client,
    )


async def run_worker(
    settings: Settings,
    pool: AsyncConnectionPool,
    channel: Channel | None,
    stop: asyncio.Event,
    *,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    recompute_interval_seconds: float = RECOMPUTE_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Run until `stop` is set; the batch in progress always finishes first."""
    if channel is None:
        logger.warning("Alert delivery is disabled: no notifier channel is configured.")
    else:
        logger.info("Alert delivery uses the %s channel.", channel.name)
    next_recompute = clock()
    while not stop.is_set():
        if clock() >= next_recompute:
            next_recompute = clock() + recompute_interval_seconds
            try:
                await recompute(pool, settings)
            except Exception as exc:
                logger.error("Grant recompute failed: %s", type(exc).__name__)
        sent = 0
        try:
            sent = await deliver_due(
                pool, channel, max_age_seconds=settings.notifier_max_event_age_seconds
            )
        except Exception as exc:
            # The database may be restarting; the next tick tries again.
            logger.error("Alert delivery tick failed: %s", type(exc).__name__)
        if sent == BATCH_SIZE:
            continue  # More may be due; skip the idle wait.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=poll_interval_seconds)


async def _main(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    pool = create_pool(settings)
    await pool.open(wait=False)
    try:
        async with httpx2.AsyncClient() as client:
            await run_worker(settings, pool, build_channel(settings, client), stop)
    finally:
        await pool.close()
    logger.info("Worker stopped.")


def main() -> int:
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 2
    configure_logging(settings)
    asyncio.run(_main(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
