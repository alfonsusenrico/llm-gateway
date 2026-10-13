"""`llm-gateway-admin`: the owner's administration command."""

from __future__ import annotations

import argparse
import asyncio
import sys

from llm_gateway.admin.notifier_commands import TEST_WAIT_SECONDS, send_test_alert
from llm_gateway.db.pool import create_pool
from llm_gateway.settings import Settings, SettingsError, load_settings

_POOL_OPEN_TIMEOUT_SECONDS = 10.0
_TEST_MESSAGES = {
    "delivered": "delivered: the test alert reached the notifier channel.",
    "pending": (
        f"pending: not delivered within {TEST_WAIT_SECONDS:.0f} s. Check that the worker "
        "is running and the notifier channel is configured."
    ),
    "failed": "failed: the channel rejected the alert. Check the bot token and chat ID.",
    "expired": "expired: the alert aged out before delivery.",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-gateway-admin", description=__doc__)
    groups = parser.add_subparsers(dest="group", required=True)
    notifier = groups.add_parser("notifier", help="owner notifier")
    notifier_actions = notifier.add_subparsers(dest="action", required=True)
    notifier_actions.add_parser("test", help="send a test alert and wait for delivery")
    return parser


async def _notifier_test(settings: Settings) -> int:
    pool = create_pool(settings)
    try:
        await pool.open(wait=True, timeout=_POOL_OPEN_TIMEOUT_SECONDS)
        status = await send_test_alert(
            pool, dedupe_window_seconds=settings.notifier_dedupe_window_seconds
        )
    finally:
        await pool.close()
    print(_TEST_MESSAGES[status])
    return 0 if status == "delivered" else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.group == "notifier" and args.action == "test":
        try:
            return asyncio.run(_notifier_test(settings))
        except Exception as exc:
            print(f"error: the test alert could not be recorded ({type(exc).__name__}).")
            return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
