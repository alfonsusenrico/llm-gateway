"""Interactive admin menu: the same actions as the subcommands, in a loop until 0 or Ctrl+C."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg_pool import AsyncConnectionPool

from llm_gateway.admin import project_commands as projects
from llm_gateway.admin.cli import notifier_test
from llm_gateway.db.pool import create_pool
from llm_gateway.projects.contract import ContractError, Environment
from llm_gateway.projects.service import RegistryError
from llm_gateway.settings import Settings

Read = Callable[[str], str]
Write = Callable[[str], None]

MENU = """
LLM Gateway admin
  1  List projects             7  Enable or disable a project
  2  Show a project            8  Rotate a key
  3  Register a project        9  Revoke a key
  4  Change a request         10  Contract history
  5  Set a profile            11  Export a contract
  6  Remove a profile         12  Send a test alert
                              13  Show grants
                              14  Set a project weight
  0  Exit"""


# Choices that can change a contract or status; grants are recomputed after them.
_GRANT_INPUT_CHOICES = frozenset({"3", "4", "5", "6", "7"})


class _Cancelled(Exception):
    """The owner left a prompt empty where a value was required, or pressed Ctrl+C."""


class Menu:
    def __init__(
        self, pool: AsyncConnectionPool, settings: Settings, *, read: Read, write: Write
    ) -> None:
        self._pool = pool
        self._settings = settings
        self._read = read
        self._write = write

    async def _ask(self, prompt: str, *, required: bool = True) -> str:
        try:
            answer = (await asyncio.to_thread(self._read, prompt)).strip()
        except (EOFError, KeyboardInterrupt):
            raise _Cancelled from None
        if required and not answer:
            raise _Cancelled
        return answer

    async def _ask_parsed[T](self, prompt: str, parse: Callable[[str], T]) -> T:
        """Ask until the answer parses; an empty answer cancels the action."""
        while True:
            try:
                return parse(await self._ask(prompt))
            except ValueError as exc:
                self._write(f"  {exc}")

    async def _ask_optional[T](self, prompt: str, parse: Callable[[str], T]) -> T | None:
        """Ask until the answer parses or is empty."""
        while True:
            answer = await self._ask(prompt, required=False)
            if not answer:
                return None
            try:
                return parse(answer)
            except ValueError as exc:
                self._write(f"  {exc}")

    async def _ask_environment(self) -> Environment:
        def parse(value: str) -> Environment:
            mapping: dict[str, Environment] = {
                "p": "production",
                "production": "production",
                "d": "development",
                "development": "development",
            }
            if value.lower() not in mapping:
                raise ValueError("Enter production (p) or development (d).")
            return mapping[value.lower()]

        return await self._ask_parsed("Environment [p/d]: ", parse)

    async def _ask_request_values(self) -> projects.RequestValues:
        self._write("  Leave a value empty to keep it.")
        return projects.RequestValues(
            await self._ask_optional("  Requests per minute: ", _positive_int),
            await self._ask_optional("  Concurrency: ", _positive_int),
            await self._ask_optional("  Monthly budget USD: ", _usd),
            await self._ask_optional("  Per-call cost ceiling USD: ", _usd),
        )

    async def run(self) -> None:
        while True:
            self._write(MENU)
            try:
                choice = await self._ask("Choose: ", required=False)
            except _Cancelled:
                self._write("")
                return
            if choice == "0":
                return
            try:
                result = await self._dispatch(choice)
            except _Cancelled:
                self._write("Cancelled.")
                continue
            except (RegistryError, ContractError) as exc:
                self._write(f"error: {exc}")
                continue
            if result is None:
                self._write("Choose a number from the menu.")
            else:
                self._write(result)

    async def _dispatch(self, choice: str) -> str | None:
        result = await self._action(choice)
        if result is not None and choice in _GRANT_INPUT_CHOICES:
            result = f"{result}\n{await projects.recompute_grants(self._pool, self._settings)}"
        return result

    async def _action(self, choice: str) -> str | None:
        pool, pepper = self._pool, self._settings.project_key_pepper_bytes
        match choice:
            case "1":
                return await projects.list_projects(pool)
            case "2":
                return await projects.show(pool, await self._ask("Project ID: "))
            case "3":
                project_id = await self._ask("Project ID: ")
                name = await self._ask("Display name: ")
                return await projects.register(
                    pool, pepper, project_id=project_id, display_name=name
                )
            case "4":
                project_id = await self._ask("Project ID: ")
                environment = await self._ask_environment()
                values = await self._ask_request_values()
                return await projects.set_request(pool, project_id, environment, values)
            case "5":
                return await self._set_profile()
            case "6":
                project_id = await self._ask("Project ID: ")
                return await projects.remove_profile(pool, project_id, await self._ask("Profile: "))
            case "7":
                project_id = await self._ask("Project ID: ")
                enable = await self._ask_parsed("Enable or disable [e/d]: ", _enable_choice)
                return await projects.set_status(
                    pool, project_id, "active" if enable else "disabled"
                )
            case "8":
                project_id = await self._ask("Project ID: ")
                environment = await self._ask_environment()
                grace = await self._ask_optional(
                    "Grace minutes for the old key [0]: ", _non_negative_int
                )
                return await projects.rotate_key(pool, pepper, project_id, environment, grace or 0)
            case "9":
                return await projects.revoke_key(pool, await self._ask("Key ID: "))
            case "10":
                return await projects.history(pool, await self._ask("Project ID: "))
            case "11":
                return await projects.export_contract(pool, await self._ask("Project ID: "))
            case "12":
                self._write("Waiting up to 30 seconds for delivery...")
                return await notifier_test(pool, self._settings)
            case "13":
                return await projects.show_grants(pool, self._settings)
            case "14":
                project_id = await self._ask("Project ID: ")
                weight = await self._ask_parsed("Weight [1-100]: ", _positive_int)
                return await projects.set_weight(pool, self._settings, project_id, weight)
        return None

    async def _set_profile(self) -> str:
        project_id = await self._ask("Project ID: ")
        name = await self._ask("Profile name: ")
        profile: dict[str, Any] = {
            "provider": await self._ask_parsed("Provider [openai/stub]: ", _provider),
            "model": await self._ask("Model: "),
            "reasoning_effort": await self._ask("Reasoning effort (optional): ", required=False)
            or None,
            "max_output_tokens": await self._ask_optional(
                "Max output tokens (optional): ", _positive_int
            ),
            "temperature": await self._ask_optional("Temperature (optional): ", _temperature),
        }
        return await projects.set_profile(self._pool, project_id, name, profile)


def _whole_number(value: str, minimum: int) -> int:
    try:
        number = int(value)
    except ValueError:
        number = minimum - 1
    if number < minimum:
        raise ValueError(f"Enter a whole number of at least {minimum}.")
    return number


def _positive_int(value: str) -> int:
    return _whole_number(value, 1)


def _non_negative_int(value: str) -> int:
    return _whole_number(value, 0)


def _temperature(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        raise ValueError("Enter a number such as 0.7.") from None


def _usd(value: str) -> str:
    try:
        Decimal(value)
    except InvalidOperation:
        raise ValueError('Enter a decimal amount such as "1.50".') from None
    return value


def _provider(value: str) -> str:
    if value not in {"openai", "stub"}:
        raise ValueError("Enter openai or stub.")
    return value


def _enable_choice(value: str) -> bool:
    if value.lower() not in {"e", "d"}:
        raise ValueError("Enter e to enable or d to disable.")
    return value.lower() == "e"


async def run_menu(settings: Settings, *, read: Read = input, write: Write = print) -> int:
    pool = create_pool(settings)
    try:
        await pool.open(wait=True, timeout=10)
        await Menu(pool, settings, read=read, write=write).run()
    except Exception as exc:
        write(f"error: the menu stopped ({type(exc).__name__}).")
        return 1
    finally:
        await pool.close()
    return 0
