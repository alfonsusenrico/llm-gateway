from __future__ import annotations

import pytest
from psycopg_pool import AsyncConnectionPool

from llm_gateway.admin.menu import Menu
from llm_gateway.projects import service
from tests.conftest import make_settings

pytestmark = [pytest.mark.anyio, pytest.mark.db]


class Script:
    """Feeds scripted answers to the menu and collects everything it writes."""

    def __init__(self, *answers: str | type[BaseException]) -> None:
        self._answers = list(answers)
        self.output: list[str] = []

    def read(self, prompt: str) -> str:
        self.output.append(prompt)
        answer = self._answers.pop(0)
        if isinstance(answer, type):
            raise answer
        return answer

    def write(self, text: str) -> None:
        self.output.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.output)


async def _run(pool: AsyncConnectionPool, script: Script) -> None:
    await Menu(pool, make_settings(), read=script.read, write=script.write).run()


async def test_register_through_the_menu_then_exit(migrated_pool: AsyncConnectionPool) -> None:
    script = Script("3", "personal_agent", "Personal Agent", "0")

    await _run(migrated_pool, script)

    assert "Registered personal_agent (version 1)." in script.text
    assert script.text.count("lgw.prod.") == 1
    assert [p.project_id for p in await service.list_projects(migrated_pool)] == ["personal_agent"]


async def test_invalid_input_re_prompts_without_exiting(migrated_pool: AsyncConnectionPool) -> None:
    script = Script(
        "3", "personal_agent", "Agent",
        "4", "personal_agent", "x", "p", "many", "", "", "", "", "0",
    )  # fmt: skip

    await _run(migrated_pool, script)

    assert "Enter production (p) or development (d)." in script.text
    assert "Enter a whole number of at least 1." in script.text
    assert "error: Give at least one value to change." in script.text


async def test_unknown_choice_and_registry_errors_keep_the_loop(
    migrated_pool: AsyncConnectionPool,
) -> None:
    script = Script("42", "2", "ghost_project", "0")

    await _run(migrated_pool, script)

    assert "Choose a number from the menu." in script.text
    assert "error: Project ghost_project is not registered." in script.text


async def test_ctrl_c_at_the_menu_exits_and_inside_an_action_cancels(
    migrated_pool: AsyncConnectionPool,
) -> None:
    script = Script("3", KeyboardInterrupt, KeyboardInterrupt)

    await _run(migrated_pool, script)

    assert "Cancelled." in script.text
    assert await service.list_projects(migrated_pool) == []


async def test_change_request_through_the_menu(migrated_pool: AsyncConnectionPool) -> None:
    script = Script(
        "3", "personal_agent", "Agent",
        "4", "personal_agent", "d", "", "3", "", "", "0",
    )  # fmt: skip

    await _run(migrated_pool, script)

    contract, version = await service.get_contract(migrated_pool, "personal_agent")
    assert (version, contract.request.development.concurrency) == (2, 3)


async def test_grants_and_weight_through_the_menu(migrated_pool: AsyncConnectionPool) -> None:
    script = Script("3", "alpha", "Alpha", "14", "alpha", "0", "2", "13", "0")

    await _run(migrated_pool, script)

    assert "Grants recomputed: 2 grants, 0 reduced." in script.text
    assert "Enter a whole number of at least 1." in script.text
    assert "Weight of alpha is now 2." in script.text
    assert "alpha/production" in script.text
