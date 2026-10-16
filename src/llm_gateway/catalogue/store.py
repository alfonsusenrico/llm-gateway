"""Immutable alias registration, separate from schema migrations and API startup."""

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from llm_gateway.catalogue.model import Catalogue, CatalogueError, ModelEntry


async def synchronize(pool: AsyncConnectionPool, catalogue: Catalogue) -> int:
    added = 0
    async with pool.connection() as connection, connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (7_402_110_006,))
        for entry in sorted(catalogue.models.values(), key=lambda item: item.alias):
            cursor = await connection.execute(
                "SELECT task, definition_digest FROM model_alias_bindings WHERE alias = %s",
                (entry.alias,),
            )
            row = await cursor.fetchone()
            if row is not None:
                if tuple(row) != (entry.task, entry.definition_digest):
                    raise CatalogueError(f"Registered alias {entry.alias} cannot be reassigned.")
                continue
            await connection.execute(
                "INSERT INTO model_alias_bindings (alias, task, definition_digest) VALUES (%s, %s, %s)",
                (entry.alias, entry.task, entry.definition_digest),
            )
            added += 1
    return added


async def registered(
    pool: AsyncConnectionPool, entry: ModelEntry, connection: AsyncConnection | None = None
) -> bool:
    if connection is not None:
        return await _registered(connection, entry)
    async with pool.connection() as connection:
        return await _registered(connection, entry)


async def _registered(connection: AsyncConnection, entry: ModelEntry) -> bool:
    cursor = await connection.execute(
        "SELECT 1 FROM model_alias_bindings WHERE alias = %s AND task = %s AND definition_digest = %s",
        (entry.alias, entry.task, entry.definition_digest),
    )
    return await cursor.fetchone() is not None
