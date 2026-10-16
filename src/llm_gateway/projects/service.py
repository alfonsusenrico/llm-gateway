"""Registry operations: register, versioned edits, history, keys, export and import."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from llm_gateway.catalogue.model import get_catalogue
from llm_gateway.catalogue.profiles import profile_errors
from llm_gateway.projects.contract import (
    ENVIRONMENTS,
    ContractError,
    ContractV1,
    ContractV2,
    Environment,
    parse_contract,
    parse_historical_contract,
)
from llm_gateway.projects.keys import IssuedKey, issue_key


class RegistryError(Exception):
    """A registry operation that cannot be performed; the message is shown to the owner."""


class ProjectNotFoundError(RegistryError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    project_id: str
    display_name: str
    status: str
    version: int
    weight: int


@dataclass(frozen=True, slots=True)
class VersionInfo:
    version: int
    changed_at: datetime
    actor: str
    change_note: str


@dataclass(frozen=True, slots=True)
class KeyInfo:
    key_id: str
    environment: str
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None


async def register_project(
    pool: AsyncConnectionPool, contract: ContractV2, *, actor: str, pepper: bytes
) -> list[IssuedKey]:
    """Store version 1 and issue one key per environment; return the keys to show once."""
    _check_changed_profiles({}, contract)
    keys = [issue_key(environment, pepper) for environment in ENVIRONMENTS]
    async with pool.connection() as connection, connection.transaction():
        cursor = await connection.execute(
            "INSERT INTO projects (project_id, current_version) VALUES (%s, 1) "
            "ON CONFLICT (project_id) DO NOTHING",
            (contract.project_id,),
        )
        if cursor.rowcount == 0:
            raise RegistryError(f"Project {contract.project_id} is already registered.")
        await _insert_version(connection, contract, 1, actor, "register")
        for key in keys:
            await _insert_key(connection, contract.project_id, key)
    return keys


async def update_contract(
    pool: AsyncConnectionPool,
    project_id: str,
    change: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    actor: str,
    note: str,
) -> ContractV2:
    """Apply `change` to the current contract and store the result as a new version.

    The project row is locked, so concurrent edits become consecutive versions.
    """
    async with pool.connection() as connection, connection.transaction():
        cursor = await connection.execute(
            "SELECT current_version FROM projects WHERE project_id = %s FOR UPDATE",
            (project_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ProjectNotFoundError(f"Project {project_id} is not registered.")
        version = int(row[0])
        current = await _contract_json(connection, project_id, version)
        updated = parse_contract(change(copy.deepcopy(current)))
        if updated.project_id != project_id:
            raise ContractError("project_id: cannot be changed")
        _check_changed_profiles(_previous_profiles(current), updated)
        await _insert_version(connection, updated, version + 1, actor, note)
        await connection.execute(
            "UPDATE projects SET current_version = %s, updated_at = now() WHERE project_id = %s",
            (version + 1, project_id),
        )
    return updated


async def import_contract(
    pool: AsyncConnectionPool, data: object, *, actor: str, note: str
) -> ContractV2:
    contract = parse_contract(data)
    return await update_contract(
        pool, contract.project_id, lambda _current: contract.to_json(), actor=actor, note=note
    )


async def get_contract(
    pool: AsyncConnectionPool, project_id: str, version: int | None = None
) -> tuple[ContractV1 | ContractV2, int]:
    async with pool.connection() as connection:
        if version is None:
            cursor = await connection.execute(
                "SELECT current_version FROM projects WHERE project_id = %s", (project_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise ProjectNotFoundError(f"Project {project_id} is not registered.")
            version = int(row[0])
        data = await _contract_json(connection, project_id, version)
    return parse_historical_contract(data), version


async def list_projects(pool: AsyncConnectionPool) -> list[ProjectSummary]:
    async with pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT p.project_id, v.contract ->> 'display_name', v.contract ->> 'status',
                   p.current_version, p.weight
            FROM projects p
            JOIN contract_versions v
              ON v.project_id = p.project_id AND v.version = p.current_version
            ORDER BY p.project_id
            """
        )
        rows = await cursor.fetchall()
    return [ProjectSummary(*row) for row in rows]


async def history(pool: AsyncConnectionPool, project_id: str) -> list[VersionInfo]:
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT version, changed_at, actor, change_note FROM contract_versions "
            "WHERE project_id = %s ORDER BY version",
            (project_id,),
        )
        rows = await cursor.fetchall()
    if not rows:
        raise ProjectNotFoundError(f"Project {project_id} is not registered.")
    return [VersionInfo(*row) for row in rows]


async def list_keys(pool: AsyncConnectionPool, project_id: str) -> list[KeyInfo]:
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT key_id, environment, created_at, expires_at, revoked_at FROM project_keys "
            "WHERE project_id = %s ORDER BY created_at, key_id",
            (project_id,),
        )
        rows = await cursor.fetchall()
    return [KeyInfo(*row) for row in rows]


async def rotate_key(
    pool: AsyncConnectionPool,
    project_id: str,
    environment: Environment,
    *,
    grace_minutes: int,
    pepper: bytes,
) -> IssuedKey:
    """Issue a new key; current keys of that environment stop working after the grace period."""
    if not 0 <= grace_minutes <= 7 * 24 * 60:
        raise RegistryError("Grace period must be between 0 and 10080 minutes.")
    key = issue_key(environment, pepper)
    async with pool.connection() as connection, connection.transaction():
        cursor = await connection.execute(
            "SELECT 1 FROM projects WHERE project_id = %s FOR UPDATE", (project_id,)
        )
        if await cursor.fetchone() is None:
            raise ProjectNotFoundError(f"Project {project_id} is not registered.")
        await connection.execute(
            """
            UPDATE project_keys
            SET expires_at = LEAST(
                coalesce(expires_at, 'infinity'), now() + make_interval(mins => %s)
            )
            WHERE project_id = %s AND environment = %s AND revoked_at IS NULL
            """,
            (grace_minutes, project_id, environment),
        )
        await _insert_key(connection, project_id, key)
    return key


async def revoke_key(pool: AsyncConnectionPool, key_id: str) -> None:
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "UPDATE project_keys SET revoked_at = now() WHERE key_id = %s AND revoked_at IS NULL",
            (key_id,),
        )
        if cursor.rowcount == 0:
            raise RegistryError(f"No active key {key_id}.")


def _previous_profiles(current: dict[str, Any]) -> dict[str, Any]:
    """The stored version's profiles; none when it is invalid, so all are checked."""
    try:
        return dict(parse_contract(current).profiles)
    except ContractError:
        return {}


def _check_changed_profiles(before: dict[str, Any], after: ContractV2) -> None:
    """Check new or changed profiles against the catalogue.

    Unchanged profiles are not rechecked, so a later catalogue change never
    blocks unrelated edits such as disabling a project.
    """
    catalogue = get_catalogue()
    errors = [
        error
        for name, profile in after.profiles.items()
        if before.get(name) != profile
        for error in profile_errors(name, profile, catalogue)
    ]
    if errors:
        raise ContractError("; ".join(errors))


async def _contract_json(
    connection: AsyncConnection, project_id: str, version: int
) -> dict[str, Any]:
    cursor = await connection.execute(
        "SELECT contract FROM contract_versions WHERE project_id = %s AND version = %s",
        (project_id, version),
    )
    row = await cursor.fetchone()
    if row is None:
        raise ProjectNotFoundError(f"Project {project_id} has no version {version}.")
    return dict(row[0])


async def _insert_version(
    connection: AsyncConnection, contract: ContractV2, version: int, actor: str, note: str
) -> None:
    await connection.execute(
        "INSERT INTO contract_versions (project_id, version, contract, actor, change_note) "
        "VALUES (%s, %s, %s, %s, %s)",
        (contract.project_id, version, Jsonb(contract.to_json()), actor[:120], note[:200]),
    )


async def _insert_key(connection: AsyncConnection, project_id: str, key: IssuedKey) -> None:
    await connection.execute(
        "INSERT INTO project_keys (key_id, project_id, environment, secret_hmac) "
        "VALUES (%s, %s, %s, %s)",
        (key.key_id, project_id, key.environment, key.secret_hmac),
    )


async def upgrade_contract(pool: AsyncConnectionPool, project_id: str, *, actor: str) -> bool:
    """Append the alias-backed document; historical versions and access keys stay intact."""
    async with pool.connection() as connection, connection.transaction():
        cursor = await connection.execute(
            "SELECT current_version FROM projects WHERE project_id = %s FOR UPDATE", (project_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise ProjectNotFoundError("Project is not registered.")
        version = int(row[0])
        current = parse_historical_contract(await _contract_json(connection, project_id, version))
        if isinstance(current, ContractV2):
            return False
        catalogue = get_catalogue()
        profiles = {}
        for name, profile in current.profiles.items():
            aliases = catalogue.aliases_for(profile.provider, profile.model)
            if len(aliases) != 1:
                raise RegistryError(f"Profile {name} has no unique model alias mapping.")
            parameters = profile.model_dump(exclude={"provider", "model"}, exclude_none=True)
            profiles[name] = {"model": aliases[0], "parameters": parameters}
        data = current.to_json()
        data.update(contract_version=2, profiles=profiles)
        upgraded = parse_contract(data)
        _check_changed_profiles({}, upgraded)
        await _insert_version(connection, upgraded, version + 1, actor, "upgrade contract schema")
        await connection.execute(
            "UPDATE projects SET current_version = %s, updated_at = now() WHERE project_id = %s",
            (version + 1, project_id),
        )
        return True
