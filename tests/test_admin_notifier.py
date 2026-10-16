from __future__ import annotations

import anyio
import pytest
from psycopg_pool import AsyncConnectionPool

from llm_gateway.admin import cli
from llm_gateway.admin.notifier_commands import send_test_alert
from llm_gateway.notifier.channels import DeliveryResult
from llm_gateway.notifier.delivery import deliver_due
from tests.conftest import TEST_CREDENTIAL_KEY, TEST_PEPPER, TEST_RESULT_KEY
from tests.notifier_fakes import FakeChannel

pytestmark = pytest.mark.anyio


async def _deliver_until(
    pool: AsyncConnectionPool, channel: FakeChannel, done: anyio.Event
) -> None:
    while not done.is_set():
        await deliver_due(pool, channel, max_age_seconds=86_400)
        await anyio.sleep(0.05)


async def _run_test(pool: AsyncConnectionPool, channel: FakeChannel | None) -> str:
    done = anyio.Event()
    result: list[str] = []

    async def run() -> None:
        result.append(await send_test_alert(pool, dedupe_window_seconds=900, wait_seconds=3))
        done.set()

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        if channel is not None:
            group.start_soon(_deliver_until, pool, channel, done)
    return result[0]


@pytest.mark.db
async def test_test_alert_is_delivered(migrated_pool: AsyncConnectionPool) -> None:
    channel = FakeChannel()

    assert await _run_test(migrated_pool, channel) == "delivered"
    assert channel.sent[0].startswith("[INFO] notifier_test\n")


@pytest.mark.db
async def test_test_alerts_bypass_deduplication(migrated_pool: AsyncConnectionPool) -> None:
    channel = FakeChannel()

    await _run_test(migrated_pool, channel)
    await _run_test(migrated_pool, channel)

    assert len(channel.sent) == 2


@pytest.mark.db
async def test_without_a_worker_the_test_reports_pending(
    migrated_pool: AsyncConnectionPool,
) -> None:
    assert await _run_test(migrated_pool, None) == "pending"


@pytest.mark.db
async def test_rejected_test_reports_failed(migrated_pool: AsyncConnectionPool) -> None:
    channel = FakeChannel(DeliveryResult("rejected", code="http_401"))

    assert await _run_test(migrated_pool, channel) == "failed"


def test_cli_reports_unrecordable_alert_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/unused")
    monkeypatch.setenv("PROJECT_KEY_PEPPER", TEST_PEPPER)
    monkeypatch.setenv("RESULT_ENCRYPTION_KEY", TEST_RESULT_KEY)
    monkeypatch.setenv("PROVIDER_CREDENTIAL_ENCRYPTION_KEY", TEST_CREDENTIAL_KEY)
    monkeypatch.setattr(cli, "_POOL_OPEN_TIMEOUT_SECONDS", 0.5)

    status = cli.main(["notifier", "test"])

    assert status == 1
    assert "the operation failed (PoolTimeout)" in capsys.readouterr().out


def test_cli_stops_on_invalid_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert cli.main(["notifier", "test"]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err
