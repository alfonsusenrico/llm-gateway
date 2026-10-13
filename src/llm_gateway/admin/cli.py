"""`llm-gateway-admin`: the owner's administration command.

Run without arguments for the interactive menu.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from llm_gateway.admin import project_commands as projects
from llm_gateway.admin.notifier_commands import TEST_WAIT_SECONDS, send_test_alert
from llm_gateway.db.pool import create_pool
from llm_gateway.projects.contract import ContractError
from llm_gateway.projects.service import RegistryError
from llm_gateway.settings import Settings, SettingsError, load_settings

_POOL_OPEN_TIMEOUT_SECONDS = 10.0
TEST_MESSAGES = {
    "delivered": "delivered: the test alert reached the notifier channel.",
    "pending": (
        f"pending: not delivered within {TEST_WAIT_SECONDS:.0f} s. Check that the worker "
        "is running and the notifier channel is configured."
    ),
    "failed": "failed: the channel rejected the alert. Check the bot token and chat ID.",
    "expired": "expired: the alert aged out before delivery.",
}
Action = Callable[[AsyncConnectionPool, Settings], Awaitable[str]]


def _request_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rpm", type=int, help="requests per minute")
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--budget-usd", help='monthly budget, a decimal string such as "1.50"')
    parser.add_argument("--cost-ceiling-usd", help="per-call cost ceiling, a decimal string")


def _request_values(args: argparse.Namespace) -> projects.RequestValues:
    return projects.RequestValues(
        args.rpm, args.concurrency, args.budget_usd, args.cost_ceiling_usd
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-gateway-admin", description=__doc__)
    groups = parser.add_subparsers(dest="group")

    notifier = groups.add_parser("notifier", help="owner notifier")
    notifier_actions = notifier.add_subparsers(dest="action", required=True)
    notifier_actions.add_parser("test", help="send a test alert and wait for delivery")

    project = groups.add_parser("project", help="project registry")
    actions = project.add_subparsers(dest="action", required=True)
    register = actions.add_parser("register", help="register a project and issue its keys")
    register.add_argument("project_id")
    register.add_argument("--display-name", required=True)
    actions.add_parser("list", help="list projects")
    show = actions.add_parser("show", help="show a contract and its keys")
    show.add_argument("project_id")
    show.add_argument("--version", type=int)
    actions.add_parser("history", help="list contract versions").add_argument("project_id")
    set_request = actions.add_parser("set-request", help="change an environment request")
    set_request.add_argument("project_id")
    set_request.add_argument("--env", required=True, choices=["production", "development"])
    _request_options(set_request)
    set_request.add_argument("--note")
    set_profile = actions.add_parser("set-profile", help="add or replace a model profile")
    set_profile.add_argument("project_id")
    set_profile.add_argument("name")
    set_profile.add_argument("--provider", required=True, choices=["openai", "stub"])
    set_profile.add_argument("--model", required=True)
    set_profile.add_argument("--reasoning-effort")
    set_profile.add_argument("--max-output-tokens", type=int)
    set_profile.add_argument("--temperature", type=float)
    remove_profile = actions.add_parser("remove-profile", help="remove a model profile")
    remove_profile.add_argument("project_id")
    remove_profile.add_argument("name")
    actions.add_parser("enable", help="enable a project").add_argument("project_id")
    actions.add_parser("disable", help="disable a project").add_argument("project_id")
    rotate = actions.add_parser("rotate-key", help="issue a new key for an environment")
    rotate.add_argument("project_id")
    rotate.add_argument("--env", required=True, choices=["production", "development"])
    rotate.add_argument("--grace-minutes", type=int, default=0)
    actions.add_parser("revoke-key", help="revoke a key immediately").add_argument("key_id")
    actions.add_parser("export", help="print the current contract as JSON").add_argument(
        "project_id"
    )
    import_ = actions.add_parser("import", help="save a JSON contract as a new version")
    import_.add_argument("file", type=Path)
    import_.add_argument("--note")
    weight = actions.add_parser("set-weight", help="set the backend weight used to share capacity")
    weight.add_argument("project_id")
    weight.add_argument("weight", type=int)

    grants = groups.add_parser("grants", help="allocation grants")
    grant_actions = grants.add_subparsers(dest="action", required=True)
    grant_actions.add_parser("show", help="every grant with requested values and pool use")
    grant_actions.add_parser("recompute", help="recompute grants now")
    return parser


# Actions that can change a contract, status, or weight; grants are recomputed after them.
_GRANT_INPUT_ACTIONS = frozenset(
    {"register", "set-request", "set-profile", "remove-profile", "enable", "disable", "import"}
)


def _with_recompute(action: Action) -> Action:
    async def run(pool: AsyncConnectionPool, settings: Settings) -> str:
        result = await action(pool, settings)
        return f"{result}\n{await projects.recompute_grants(pool, settings)}"

    return run


def _project_action(args: argparse.Namespace) -> Action:
    action = _project_action_only(args)
    return _with_recompute(action) if args.action in _GRANT_INPUT_ACTIONS else action


def _project_action_only(args: argparse.Namespace) -> Action:
    match args.action:
        case "register":
            return lambda pool, settings: projects.register(
                pool,
                settings.project_key_pepper_bytes,
                project_id=args.project_id,
                display_name=args.display_name,
            )
        case "list":
            return lambda pool, _settings: projects.list_projects(pool)
        case "show":
            return lambda pool, _settings: projects.show(pool, args.project_id, args.version)
        case "history":
            return lambda pool, _settings: projects.history(pool, args.project_id)
        case "set-request":
            return lambda pool, _settings: projects.set_request(
                pool, args.project_id, args.env, _request_values(args), note=args.note
            )
        case "set-profile":
            profile = {
                "provider": args.provider,
                "model": args.model,
                "reasoning_effort": args.reasoning_effort,
                "max_output_tokens": args.max_output_tokens,
                "temperature": args.temperature,
            }
            return lambda pool, _settings: projects.set_profile(
                pool, args.project_id, args.name, profile
            )
        case "remove-profile":
            return lambda pool, _settings: projects.remove_profile(pool, args.project_id, args.name)
        case "enable" | "disable":
            status = "active" if args.action == "enable" else "disabled"
            return lambda pool, _settings: projects.set_status(pool, args.project_id, status)
        case "rotate-key":
            return lambda pool, settings: projects.rotate_key(
                pool,
                settings.project_key_pepper_bytes,
                args.project_id,
                args.env,
                args.grace_minutes,
            )
        case "revoke-key":
            return lambda pool, _settings: projects.revoke_key(pool, args.key_id)
        case "export":
            return lambda pool, _settings: projects.export_contract(pool, args.project_id)
        case "import":
            text = args.file.read_text(encoding="utf-8")
            return lambda pool, _settings: projects.import_contract(pool, text, note=args.note)
        case "set-weight":
            return lambda pool, settings: projects.set_weight(
                pool, settings, args.project_id, args.weight
            )
    raise ValueError(f"unknown project action {args.action}")


async def notifier_test(pool: AsyncConnectionPool, settings: Settings) -> str:
    status = await send_test_alert(
        pool, dedupe_window_seconds=settings.notifier_dedupe_window_seconds
    )
    if status != "delivered":
        raise RegistryError(TEST_MESSAGES[status])
    return TEST_MESSAGES[status]


async def run_with_pool(settings: Settings, action: Action) -> int:
    """Run one action and print its result; owner-facing failures exit 1 without a traceback."""
    pool = create_pool(settings)
    try:
        await pool.open(wait=True, timeout=_POOL_OPEN_TIMEOUT_SECONDS)
        print(await action(pool, settings))
        return 0
    except (RegistryError, ContractError) as exc:
        print(f"error: {exc}")
        return 1
    except Exception as exc:
        print(f"error: the operation failed ({type(exc).__name__}).")
        return 1
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.group is None:
        from llm_gateway.admin.menu import run_menu

        return asyncio.run(run_menu(settings))
    if args.group == "notifier":
        return asyncio.run(run_with_pool(settings, notifier_test))
    if args.group == "grants":
        action = projects.show_grants if args.action == "show" else projects.recompute_grants
        return asyncio.run(run_with_pool(settings, action))
    try:
        action = _project_action(args)
    except OSError as exc:
        print(f"error: cannot read the file ({type(exc).__name__}).")
        return 1
    return asyncio.run(run_with_pool(settings, action))


if __name__ == "__main__":
    raise SystemExit(main())
