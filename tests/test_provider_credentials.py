"""Registry credentials remain write-only, authenticated and isolated by owner."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from dataclasses import asdict
from typing import Any

import psycopg
import pytest
from cryptography.fernet import Fernet
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from llm_gateway.admin import cli, credential_commands
from llm_gateway.projects import service
from llm_gateway.projects.credentials import CredentialError, CredentialStore
from llm_gateway.projects.service import RegistryError
from tests.conftest import TEST_CREDENTIAL_KEY, make_settings
from tests.test_projects_service import _register

pytestmark = pytest.mark.anyio


def _store(pool: AsyncConnectionPool, key: str = TEST_CREDENTIAL_KEY) -> CredentialStore:
    return CredentialStore(pool, SecretStr(key))


async def _capture(store: CredentialStore, pool: AsyncConnectionPool, project: str = "alpha"):
    async with pool.connection() as connection, connection.transaction():
        await store._lock_project(connection, project)
        return await store.capture(connection, project, "openai")


@pytest.mark.db
async def test_slot_contains_only_encrypted_secret_and_history_contains_no_secret(
    migrated_pool: AsyncConnectionPool, caplog: pytest.LogCaptureFixture
) -> None:
    pool = migrated_pool
    await _register(pool, "alpha")
    before = await service.history(pool, "alpha")
    secret = "synthetic-private-provider-secret"
    store = _store(pool)
    first = await store.set("alpha", "openai", SecretStr(secret), actor="test")
    captured = await _capture(store, pool)
    second = await store.set("alpha", "openai", SecretStr(secret), actor="test")
    assert first != second
    assert str(captured.revision) == first
    assert secret not in repr(captured)
    assert secret not in repr(asdict(captured))
    async with pool.connection() as connection:
        slot = await (
            await connection.execute("SELECT ciphertext FROM project_provider_credentials")
        ).fetchone()
        events = await (
            await connection.execute(
                "SELECT action, provider_credential_events::text FROM provider_credential_events ORDER BY at, revision"
            )
        ).fetchall()
    assert slot is not None and secret.encode() not in bytes(slot[0])
    assert sorted(event[0] for event in events) == ["replace", "set"]
    assert secret not in repr(events) + json.dumps(await store.metadata("alpha")) + caplog.text
    assert await service.history(pool, "alpha") == before
    await store.remove("alpha", "openai", actor="test")
    assert (await store.metadata("alpha"))[0]["configured"] is False
    with pytest.raises(CredentialError, match="provider_credentials_missing"):
        await _capture(store, pool)
    async with pool.connection() as connection:
        assert await (
            await connection.execute("SELECT ciphertext FROM project_provider_credentials")
        ).fetchone() == (None,)
    # Removal does not change an execution's already captured credentials.
    assert captured.secret.get_secret_value() == secret


@pytest.mark.db
@pytest.mark.parametrize("tamper", ["key", "project", "provider", "revision", "corrupt"])
async def test_envelope_substitution_fails_closed(
    migrated_pool: AsyncConnectionPool, tamper: str
) -> None:
    pool = migrated_pool
    await _register(pool, "alpha")
    await _register(pool, "beta")
    store = _store(pool)
    await store.set("alpha", "openai", SecretStr("synthetic-secret"), actor="test")
    if tamper == "key":
        store = _store(pool, Fernet.generate_key().decode())
    else:
        async with pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT ciphertext FROM project_provider_credentials WHERE project_id='alpha'"
                )
            ).fetchone()
            assert row is not None
            cipher = Fernet(TEST_CREDENTIAL_KEY.encode())
            envelope = json.loads(cipher.decrypt(bytes(row[0])))
            if tamper == "corrupt":
                encrypted = b"invalid-ciphertext"
            else:
                envelope[tamper] = "another-owner-or-revision"
                encrypted = cipher.encrypt(json.dumps(envelope).encode())
            await connection.execute(
                "UPDATE project_provider_credentials SET ciphertext=%s WHERE project_id='alpha'",
                (encrypted,),
            )
    with pytest.raises(CredentialError, match="provider_credentials_unreadable"):
        await _capture(store, pool)
    with pytest.raises(CredentialError, match="provider_credentials_missing"):
        await _capture(store, pool, "beta")


@pytest.mark.db
async def test_audit_failure_rolls_back_slot_and_events_are_append_only(
    migrated_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = migrated_pool
    await _register(pool, "alpha")
    store = _store(pool)
    revision = await store.set("alpha", "openai", SecretStr("synthetic-first"), actor="test")

    async def fail(*args: Any) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError):
        await store.set("alpha", "openai", SecretStr("synthetic-second"), actor="test")
    assert str((await _capture(store, pool)).revision) == revision
    async with pool.connection() as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            async with connection.transaction():
                await connection.execute("DELETE FROM provider_credential_events")


@pytest.mark.db
async def test_capture_and_rotation_serialize_under_the_project_lock(
    migrated_pool: AsyncConnectionPool,
) -> None:
    pool = migrated_pool
    await _register(pool, "alpha")
    store = _store(pool)
    old = await store.set("alpha", "openai", SecretStr("synthetic-first"), actor="test")
    started = asyncio.Event()

    async def rotate() -> str:
        started.set()
        return await store.set("alpha", "openai", SecretStr("synthetic-second"), actor="test")

    async with pool.connection() as connection, connection.transaction():
        await store._lock_project(connection, "alpha")
        rotating = asyncio.create_task(rotate())
        await started.wait()
        captured = await store.capture(connection, "alpha", "openai")
        assert str(captured.revision) == old
        assert not rotating.done()
    assert await rotating != old
    assert (await _capture(store, pool)).secret.get_secret_value() == "synthetic-second"


def test_protected_input_requires_explicit_stdin_and_never_accepts_value_arguments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "synthetic-private-argument"
    monkeypatch.setattr(sys, "stdin", io.StringIO(secret + "\n"))
    with pytest.raises(RegistryError, match="terminal"):
        credential_commands.read_credential()
    assert credential_commands.read_credential(stdin=True).get_secret_value() == secret
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            ["project", "credential", "set", "alpha", "--provider", "openai", secret]
        )
    assert secret not in capsys.readouterr().err


def test_hidden_terminal_input_does_not_fall_back_to_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Terminal())

    def unavailable(_prompt: str) -> str:
        import warnings

        warnings.warn(
            "synthetic terminal failure", credential_commands.getpass.GetPassWarning, stacklevel=2
        )
        return "unsafe"

    monkeypatch.setattr(credential_commands.getpass, "getpass", unavailable)
    with pytest.raises(RegistryError, match="unavailable"):
        credential_commands.read_credential()


@pytest.mark.db
async def test_management_commands_return_metadata_only(migrated_pool: AsyncConnectionPool) -> None:
    pool = migrated_pool
    await _register(pool, "alpha")
    settings = make_settings()
    secret = "synthetic-management-secret"
    outputs = [
        await credential_commands.set_credential(
            pool, settings, "alpha", "openai", SecretStr(secret)
        ),
        await credential_commands.list_credentials(pool, settings, "alpha"),
    ]
    assert json.loads(outputs[1])[0]["configured"] is True
    outputs.append(await credential_commands.remove_credential(pool, settings, "alpha", "openai"))
    assert secret not in "".join(outputs)
