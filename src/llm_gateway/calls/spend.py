"""Month-to-date spend: charged amounts of finished calls plus open reservations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import LiteralString

from psycopg import AsyncConnection

# Admitted calls count their reservation; finished calls count what was charged.
_SPEND: LiteralString = "coalesce(sum(CASE WHEN state = 'admitted' THEN reserved_micro_usd ELSE charged_micro_usd END), 0)"


def month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month_start(now: datetime) -> datetime:
    start = month_start(now)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


async def pair_spend(
    connection: AsyncConnection, project_id: str, environment: str, since: datetime
) -> int:
    cursor = await connection.execute(
        "SELECT " + _SPEND + " FROM calls "  # noqa: S608  # LiteralString fragments only
        "WHERE project_id = %s AND environment = %s AND admitted_at >= %s",
        (project_id, environment, since),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def total_spend(connection: AsyncConnection, since: datetime) -> int:
    cursor = await connection.execute(
        "SELECT " + _SPEND + " FROM calls WHERE admitted_at >= %s",  # noqa: S608  # LiteralString fragments only
        (since,),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def spend_by_pair(connection: AsyncConnection, since: datetime) -> dict[tuple[str, str], int]:
    cursor = await connection.execute(
        "SELECT project_id, environment, " + _SPEND + " FROM calls "  # noqa: S608  # LiteralString fragments only
        "WHERE admitted_at >= %s GROUP BY project_id, environment",
        (since,),
    )
    return {(str(row[0]), str(row[1])): int(row[2]) for row in await cursor.fetchall()}
