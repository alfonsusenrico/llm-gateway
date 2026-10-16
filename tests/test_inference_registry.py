"""Model immutability, executable discovery schemas, and versioned contract upgrades."""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from llm_gateway.allocation.store import recompute
from llm_gateway.catalogue.model import Catalogue, CatalogueError, ModelEntry, get_catalogue
from llm_gateway.catalogue.schemas import request_schema, setting_errors
from llm_gateway.catalogue.store import registered, synchronize
from llm_gateway.projects import service
from llm_gateway.projects.contract import ContractV1, ContractV2, parse_historical_contract
from tests.conftest import make_settings
from tests.test_auth_api import Gateway
from tests.test_projects_service import _register

pytestmark = pytest.mark.anyio


def _catalogue(*entries: ModelEntry) -> Catalogue:
    return Catalogue(entries, ())


@pytest.mark.db
async def test_alias_changes_and_retired_reuse_are_refused_atomically(
    migrated_pool: AsyncConnectionPool,
) -> None:
    pool = migrated_pool
    catalogue = get_catalogue()
    assert await synchronize(pool, catalogue) == 0
    stub = catalogue.models["stub-echo"]
    assert await registered(pool, stub)
    changed = stub.model_copy(update={"max_output_tokens": stub.max_output_tokens + 1})
    added = stub.model_copy(update={"alias": "another-stub"})
    with pytest.raises(CatalogueError, match="cannot be reassigned"):
        await synchronize(pool, _catalogue(added, changed))
    assert not await registered(pool, added)
    # Removing an entry from deployment does not delete its immutable reservation.
    await synchronize(pool, _catalogue(added))
    with pytest.raises(CatalogueError):
        await synchronize(pool, _catalogue(changed))
    documented = stub.model_copy(update={"documentation": ("https://example.org/docs",)})
    assert documented.definition_digest == stub.definition_digest
    assert await synchronize(pool, _catalogue(documented)) == 0
    async with pool.connection() as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            async with connection.transaction():
                await connection.execute("DELETE FROM model_alias_bindings")


@pytest.mark.db
async def test_concurrent_identical_registration_adds_one_binding(
    migrated_pool: AsyncConnectionPool,
) -> None:
    stub = get_catalogue().models["stub-echo"].model_copy(update={"alias": "concurrent-stub"})
    results = await asyncio.gather(
        *(synchronize(migrated_pool, _catalogue(stub)) for _ in range(4))
    )
    assert sorted(results) == [0, 0, 0, 1]


@pytest.mark.parametrize(
    "changes",
    [
        {"adapter": "unknown"},
        {"provider": "unknown"},
        {"task": "ocr"},
        {"execution": "local"},
        {"billing": "zero"},
    ],
)
def test_unsupported_execution_definitions_fail_before_registration(
    changes: dict[str, Any],
) -> None:
    definition = get_catalogue().models["openai-gpt-6-luna"].model_dump() | changes
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelEntry.model_validate(definition)


def test_supported_schema_rejects_explicit_unsupported_presence_and_conditional_settings() -> None:
    entry = (
        get_catalogue()
        .models["stub-echo"]
        .model_copy(
            update={
                "reasoning_efforts": (),
                "default_reasoning_effort": None,
                "temperature": "never",
                "function_tools": False,
                "prompt_cache": False,
                "streaming": False,
                "structured_output": False,
            }
        )
    )
    payload = {
        "provider_call_id": "schema",
        "operation": "demo",
        "model": entry.alias,
        "input": "hi",
    }
    assert setting_errors(entry, payload) == []
    for field, value in [
        ("reasoning_effort", None),
        ("temperature", None),
        ("tools", []),
        ("stream", False),
        ("prompt_cache_key", None),
        ("text_format", {"type": "json_schema"}),
        ("secret", "synthetic-secret"),
    ]:
        errors = setting_errors(entry, payload | {field: value})
        assert errors and "synthetic-secret" not in repr(errors)
    schema = request_schema(entry)
    schema["properties"]["model"]["const"] = "mutated"
    assert request_schema(entry)["properties"]["model"]["const"] == entry.alias
    paid = get_catalogue().models["openai-gpt-6-luna"]
    assert setting_errors(paid, payload | {"model": paid.alias, "temperature": 0.5})
    assert not setting_errors(
        paid, payload | {"model": paid.alias, "temperature": 0.5, "reasoning_effort": "none"}
    )


async def _legacy(
    pool: AsyncConnectionPool, project_id: str = "alpha", model: str = "gpt-6-luna"
) -> dict[str, Any]:
    await _register(pool, project_id)
    current, _ = await service.get_contract(pool, project_id)
    legacy = current.to_json()
    legacy.update(
        contract_version=1,
        profiles={
            "chat": {
                "provider": "openai",
                "model": model,
                "reasoning_effort": "medium",
                "max_output_tokens": 1200,
            }
        },
    )
    # Fixture creates the state an old binary persisted; production changes use the registry.
    async with pool.connection() as connection:
        await connection.execute(
            "UPDATE contract_versions SET contract=%s WHERE project_id=%s",
            (Jsonb(legacy), project_id),
        )
    return legacy


@pytest.mark.db
async def test_upgrade_preserves_keys_requests_weights_grants_and_history(
    migrated_pool: AsyncConnectionPool,
) -> None:
    pool = migrated_pool
    legacy = await _legacy(pool)
    await recompute(pool, make_settings())
    keys = await service.list_keys(pool, "alpha")
    async with pool.connection() as connection:
        before = await (
            await connection.execute("SELECT grants::text FROM grants ORDER BY environment")
        ).fetchall()
        weight = await (
            await connection.execute("SELECT weight FROM projects WHERE project_id='alpha'")
        ).fetchone()
    results = await asyncio.gather(
        *(service.upgrade_contract(pool, "alpha", actor="test") for _ in range(4))
    )
    assert sorted(results) == [False, False, False, True]
    current, version = await service.get_contract(pool, "alpha")
    old, _ = await service.get_contract(pool, "alpha", version=1)
    assert isinstance(old, ContractV1) and old.to_json() == legacy
    assert isinstance(current, ContractV2) and version == 2
    assert current.request == old.request and current.status == old.status
    assert current.profiles["chat"].model == "openai-gpt-6-luna"
    assert current.profiles["chat"].parameters.max_output_tokens == 1200
    assert await service.list_keys(pool, "alpha") == keys
    async with pool.connection() as connection:
        assert (
            await (
                await connection.execute("SELECT grants::text FROM grants ORDER BY environment")
            ).fetchall()
            == before
        )
        assert (
            await (
                await connection.execute("SELECT weight FROM projects WHERE project_id='alpha'")
            ).fetchone()
            == weight
        )


@pytest.mark.db
async def test_unmapped_upgrade_refuses_without_mutation(
    migrated_pool: AsyncConnectionPool,
) -> None:
    legacy = await _legacy(migrated_pool, model="retired")
    with pytest.raises(service.RegistryError, match="unique model alias"):
        await service.upgrade_contract(migrated_pool, "alpha", actor="test")
    current, version = await service.get_contract(migrated_pool, "alpha")
    assert current.to_json() == legacy and version == 1


@pytest.mark.db
async def test_version_one_authentication_requires_upgrade_without_revocation_or_alert(
    migrated_pool: AsyncConnectionPool,
) -> None:
    gateway = Gateway(migrated_pool)
    keys = await gateway.register("alpha")
    current, _ = await service.get_contract(migrated_pool, "alpha")
    data = current.to_json() | {"contract_version": 1, "profiles": {}}
    async with migrated_pool.connection() as connection:
        await connection.execute(
            "UPDATE contract_versions SET contract=%s WHERE project_id='alpha'", (Jsonb(data),)
        )
    async with gateway.client:
        refused = await gateway.get(keys["production"].raw)
        assert (refused.status_code, refused.json()["code"]) == (503, "contract_upgrade_required")
        assert await gateway.alerts() == []
        await service.upgrade_contract(migrated_pool, "alpha", actor="test")
        assert (await gateway.get(keys["production"].raw)).status_code == 200
    assert isinstance(parse_historical_contract(data), ContractV1)


@pytest.mark.db
async def test_all_project_upgrades_continue_after_an_unmapped_project(
    migrated_pool: AsyncConnectionPool,
) -> None:
    from llm_gateway.admin.project_commands import upgrade_contracts

    await _legacy(migrated_pool, "alpha", model="retired")
    await _legacy(migrated_pool, "beta")
    with pytest.raises(service.RegistryError, match="failed projects: alpha"):
        await upgrade_contracts(migrated_pool, None)
    assert isinstance((await service.get_contract(migrated_pool, "alpha"))[0], ContractV1)
    assert isinstance((await service.get_contract(migrated_pool, "beta"))[0], ContractV2)
