"""Encrypted, short-lived storage of completed call responses for replay."""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from psycopg_pool import AsyncConnectionPool


class ResultStore:
    def __init__(self, pool: AsyncConnectionPool, key: str, retention_seconds: int) -> None:
        self._pool = pool
        self._fernet = Fernet(key.encode("ascii"))
        self._retention_seconds = retention_seconds

    async def save(self, call_key: int, body: dict[str, Any]) -> None:
        token = self._fernet.encrypt(json.dumps(body, separators=(",", ":")).encode("utf-8"))
        async with self._pool.connection() as connection:
            await connection.execute(
                "INSERT INTO call_results (call_key, encrypted, expires_at) "
                "VALUES (%s, %s, now() + make_interval(secs => %s)) "
                "ON CONFLICT (call_key) DO UPDATE SET encrypted = excluded.encrypted, "
                "expires_at = excluded.expires_at",
                (call_key, token, self._retention_seconds),
            )

    async def load(self, call_key: int) -> dict[str, Any] | None:
        """The stored body, or None when missing, expired, or unreadable with this key."""
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT encrypted FROM call_results WHERE call_key = %s AND expires_at > now()",
                (call_key,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            return json.loads(self._fernet.decrypt(bytes(row[0])))
        except InvalidToken:
            return None


async def delete_expired_results(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as connection:
        cursor = await connection.execute("DELETE FROM call_results WHERE expires_at <= now()")
        return cursor.rowcount
