"""Write-only provider credentials, authenticated and bound to their registry slot."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from llm_gateway.catalogue.adapters import ADAPTERS
from llm_gateway.projects.service import ProjectNotFoundError, RegistryError


class CredentialError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Credential:
    revision: uuid.UUID
    secret: SecretStr = field(repr=False)


def validate_secret(value: str) -> SecretStr:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16_384
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise RegistryError("Credential must contain 1 to 16384 printable non-space ASCII bytes.")
    return SecretStr(value)


class CredentialStore:
    def __init__(self, pool: AsyncConnectionPool, key: SecretStr) -> None:
        self._pool = pool
        self._fernet = Fernet(key.get_secret_value().encode())

    async def set(self, project_id: str, provider: str, secret: SecretStr, *, actor: str) -> str:
        validate_secret(secret.get_secret_value())
        if not any(a.provider == provider and a.execution == "remote" for a in ADAPTERS.values()):
            raise RegistryError("Provider does not have a registered credential-requiring adapter.")
        revision = uuid.uuid4()
        envelope = json.dumps(
            {
                "project": project_id,
                "provider": provider,
                "revision": str(revision),
                "secret": secret.get_secret_value(),
            }
        )
        ciphertext = self._fernet.encrypt(envelope.encode())
        async with self._pool.connection() as connection, connection.transaction():
            await self._lock_project(connection, project_id)
            cursor = await connection.execute(
                "SELECT 1 FROM project_provider_credentials WHERE project_id = %s AND provider = %s",
                (project_id, provider),
            )
            action = "replace" if await cursor.fetchone() else "set"
            await connection.execute(
                "INSERT INTO project_provider_credentials (project_id, provider, revision, ciphertext) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (project_id, provider) DO UPDATE SET "
                "revision = excluded.revision, ciphertext = excluded.ciphertext, updated_at = now()",
                (project_id, provider, revision, ciphertext),
            )
            await self._event(connection, project_id, provider, revision, action, actor)
        return str(revision)

    async def remove(self, project_id: str, provider: str, *, actor: str) -> None:
        revision = uuid.uuid4()
        async with self._pool.connection() as connection, connection.transaction():
            await self._lock_project(connection, project_id)
            cursor = await connection.execute(
                "UPDATE project_provider_credentials SET ciphertext = NULL, revision = %s, updated_at = now() "
                "WHERE project_id = %s AND provider = %s AND ciphertext IS NOT NULL",
                (revision, project_id, provider),
            )
            if cursor.rowcount == 0:
                raise RegistryError("No active credential for this project/provider.")
            await self._event(connection, project_id, provider, revision, "remove", actor)

    async def metadata(self, project_id: str) -> list[dict[str, object]]:
        async with self._pool.connection() as connection:
            existing = await connection.execute(
                "SELECT 1 FROM projects WHERE project_id = %s", (project_id,)
            )
            if await existing.fetchone() is None:
                raise ProjectNotFoundError("Project is not registered.")
            cursor = await connection.execute(
                "SELECT provider, revision, ciphertext IS NOT NULL, updated_at "
                "FROM project_provider_credentials WHERE project_id = %s ORDER BY provider",
                (project_id,),
            )
            return [
                {
                    "provider": row[0],
                    "revision": str(row[1]),
                    "configured": row[2],
                    "updated_at": row[3].isoformat(),
                }
                for row in await cursor.fetchall()
            ]

    async def capture(
        self, connection: AsyncConnection, project_id: str, provider: str
    ) -> Credential:
        """Caller holds the project row lock until admission has recorded this revision."""
        cursor = await connection.execute(
            "SELECT revision, ciphertext FROM project_provider_credentials WHERE project_id = %s AND provider = %s",
            (project_id, provider),
        )
        row = await cursor.fetchone()
        if row is None or row[1] is None:
            raise CredentialError("provider_credentials_missing")
        revision = row[0]
        try:
            envelope = json.loads(self._fernet.decrypt(bytes(row[1])))
            if (envelope["project"], envelope["provider"], envelope["revision"]) != (
                project_id,
                provider,
                str(revision),
            ):
                raise InvalidToken
            secret = validate_secret(envelope["secret"])
        except (InvalidToken, ValueError, KeyError, TypeError, RegistryError):
            raise CredentialError("provider_credentials_unreadable") from None
        return Credential(revision, secret)

    @staticmethod
    async def _lock_project(connection: AsyncConnection, project_id: str) -> None:
        cursor = await connection.execute(
            "SELECT 1 FROM projects WHERE project_id = %s FOR UPDATE", (project_id,)
        )
        if await cursor.fetchone() is None:
            raise ProjectNotFoundError("Project is not registered.")

    @staticmethod
    async def _event(
        connection: AsyncConnection,
        project_id: str,
        provider: str,
        revision: uuid.UUID,
        action: str,
        actor: str,
    ) -> None:
        await connection.execute(
            "INSERT INTO provider_credential_events (revision, project_id, provider, action, actor) VALUES (%s, %s, %s, %s, %s)",
            (revision, project_id, provider, action, actor[:120]),
        )
