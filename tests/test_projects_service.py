from __future__ import annotations

from typing import Any

import anyio
import pytest
from psycopg_pool import AsyncConnectionPool

from llm_gateway.projects import service
from llm_gateway.projects.contract import ContractError, parse_contract
from llm_gateway.projects.keys import decode_pepper
from tests.conftest import TEST_PEPPER
from tests.test_contract import contract_data

pytestmark = [pytest.mark.anyio, pytest.mark.db]
PEPPER = decode_pepper(TEST_PEPPER)


async def _register(pool: AsyncConnectionPool, project_id: str = "personal_agent") -> list[Any]:
    contract = parse_contract(contract_data(project_id=project_id))
    return await service.register_project(pool, contract, actor="test", pepper=PEPPER)


def _set_concurrency(value: int):
    def change(contract: dict[str, Any]) -> dict[str, Any]:
        contract["request"]["production"]["concurrency"] = value
        return contract

    return change


async def test_registration_stores_version_one_and_two_keys(
    migrated_pool: AsyncConnectionPool,
) -> None:
    keys = await _register(migrated_pool)

    contract, version = await service.get_contract(migrated_pool, "personal_agent")
    stored = await service.list_keys(migrated_pool, "personal_agent")
    assert (version, contract.status) == (1, "active")
    assert sorted(key.environment for key in stored) == ["development", "production"]
    assert {key.key_id for key in stored} == {key.key_id for key in keys}


async def test_raw_keys_are_never_stored(migrated_pool: AsyncConnectionPool) -> None:
    keys = await _register(migrated_pool)

    async with migrated_pool.connection() as connection:
        cursor = await connection.execute("SELECT project_keys::text FROM project_keys")
        rows = " ".join(row[0] for row in await cursor.fetchall())
    for key in keys:
        assert key.raw not in rows
        assert key.raw.rsplit(".", 1)[1] not in rows


async def test_duplicate_registration_is_refused(migrated_pool: AsyncConnectionPool) -> None:
    await _register(migrated_pool)

    with pytest.raises(service.RegistryError, match="already registered"):
        await _register(migrated_pool)


async def test_every_edit_is_a_version_and_old_versions_stay_readable(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _register(migrated_pool)
    await service.update_contract(
        migrated_pool, "personal_agent", _set_concurrency(5), actor="cli:a", note="first"
    )
    await service.update_contract(
        migrated_pool, "personal_agent", _set_concurrency(6), actor="cli:b", note="second"
    )

    versions = await service.history(migrated_pool, "personal_agent")
    middle, _ = await service.get_contract(migrated_pool, "personal_agent", version=2)
    current, current_version = await service.get_contract(migrated_pool, "personal_agent")
    assert [(v.version, v.actor, v.change_note) for v in versions] == [
        (1, "test", "register"),
        (2, "cli:a", "first"),
        (3, "cli:b", "second"),
    ]
    assert middle.request.production.concurrency == 5
    assert (current_version, current.request.production.concurrency) == (3, 6)


async def test_invalid_edit_keeps_the_current_version(migrated_pool: AsyncConnectionPool) -> None:
    await _register(migrated_pool)

    with pytest.raises(ContractError):
        await service.update_contract(
            migrated_pool, "personal_agent", _set_concurrency(0), actor="t", note="bad"
        )

    _, version = await service.get_contract(migrated_pool, "personal_agent")
    assert version == 1


async def test_concurrent_edits_become_consecutive_versions(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _register(migrated_pool)

    async with anyio.create_task_group() as group:
        for value in (5, 6, 7, 8):
            group.start_soon(
                lambda v=value: service.update_contract(
                    migrated_pool, "personal_agent", _set_concurrency(v), actor="t", note=str(v)
                )
            )

    versions = await service.history(migrated_pool, "personal_agent")
    assert [v.version for v in versions] == [1, 2, 3, 4, 5]
    assert sorted(v.change_note for v in versions[1:]) == ["5", "6", "7", "8"]


async def test_import_saves_a_new_version_and_keeps_keys(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _register(migrated_pool)
    keys_before = await service.list_keys(migrated_pool, "personal_agent")
    exported, _ = await service.get_contract(migrated_pool, "personal_agent")

    imported = await service.import_contract(
        migrated_pool, exported.to_json(), actor="t", note="import"
    )

    _, version = await service.get_contract(migrated_pool, "personal_agent")
    assert imported == exported
    assert version == 2
    assert await service.list_keys(migrated_pool, "personal_agent") == keys_before


async def test_import_of_an_unknown_project_is_refused(
    migrated_pool: AsyncConnectionPool,
) -> None:
    with pytest.raises(service.ProjectNotFoundError, match="not registered"):
        await service.import_contract(
            migrated_pool, contract_data(project_id="ghost_project"), actor="t", note="import"
        )


async def test_rotation_sets_grace_on_old_keys_and_revoke_is_immediate(
    migrated_pool: AsyncConnectionPool,
) -> None:
    await _register(migrated_pool)

    new_key = await service.rotate_key(
        migrated_pool, "personal_agent", "production", grace_minutes=30, pepper=PEPPER
    )
    await service.revoke_key(migrated_pool, new_key.key_id)

    keys = {key.key_id: key for key in await service.list_keys(migrated_pool, "personal_agent")}
    production = [key for key in keys.values() if key.environment == "production"]
    old = next(key for key in production if key.key_id != new_key.key_id)
    assert old.expires_at is not None
    assert keys[new_key.key_id].revoked_at is not None
    with pytest.raises(service.RegistryError, match="No active key"):
        await service.revoke_key(migrated_pool, new_key.key_id)


async def test_rotation_of_an_unknown_project_is_refused(
    migrated_pool: AsyncConnectionPool,
) -> None:
    with pytest.raises(service.ProjectNotFoundError):
        await service.rotate_key(
            migrated_pool, "ghost_project", "production", grace_minutes=0, pepper=PEPPER
        )
