"""Recomputing, storing, and reading grants; alerts when grants are reduced or restored."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import LiteralString

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from llm_gateway.allocation.policy import Grant, PairRequest, Pool, Reason, allocate
from llm_gateway.money import format_usd, to_micro_usd
from llm_gateway.notifier.events import dedupe_key, raise_alert
from llm_gateway.projects.contract import ENVIRONMENTS, ContractError, parse_historical_contract
from llm_gateway.settings import Settings

# Serializes recomputes across processes; an arbitrary constant owned by this module.
_RECOMPUTE_LOCK_KEY = 7_402_110_002
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StoredGrant:
    project_id: str
    environment: str
    requests_per_minute: int
    concurrency: int
    monthly_budget_micro_usd: int
    per_call_cost_ceiling_micro_usd: int
    state: str
    reasons: tuple[Reason, ...]
    computed_at: datetime


@dataclass(frozen=True, slots=True)
class RecomputeResult:
    changed: bool
    grants: list[Grant]


def capacity_pool(settings: Settings) -> Pool:
    return Pool(
        settings.allocation_capacity_requests_per_minute,
        settings.allocation_capacity_concurrency,
        to_micro_usd(settings.global_monthly_budget_usd),
    )


def minimum_pool(settings: Settings) -> Pool:
    return Pool(
        settings.allocation_min_requests_per_minute,
        settings.allocation_min_concurrency,
        to_micro_usd(settings.allocation_min_monthly_budget_usd),
    )


async def _read_requests(connection: AsyncConnection) -> list[PairRequest]:
    cursor = await connection.execute(
        "SELECT p.project_id, p.weight, v.contract FROM projects p "
        "JOIN contract_versions v ON v.project_id = p.project_id AND v.version = p.current_version"
    )
    pairs = []
    for project_id, weight, data in await cursor.fetchall():
        try:
            contract = parse_historical_contract(data)
        except ContractError:
            continue  # Quarantined projects take no share; the API alerts about them.
        if contract.status != "active":
            continue
        for environment in ENVIRONMENTS:
            request = getattr(contract.request, environment)
            pairs.append(
                PairRequest(
                    project_id=project_id,
                    environment=environment,
                    weight=int(weight),
                    requests_per_minute=request.requests_per_minute,
                    concurrency=request.concurrency,
                    monthly_budget_micro_usd=to_micro_usd(request.monthly_budget_usd),
                    per_call_cost_ceiling_micro_usd=to_micro_usd(request.per_call_cost_ceiling_usd),
                )
            )
    return sorted(pairs, key=lambda pair: pair.sort_key)


async def current_requests(pool: AsyncConnectionPool) -> list[PairRequest]:
    """The requests of every pair that takes part in allocation."""
    async with pool.connection() as connection:
        return await _read_requests(connection)


def _inputs_hash(pairs: list[PairRequest], capacity: Pool, minimum: Pool) -> str:
    inputs = {
        "pairs": [asdict(pair) for pair in pairs],
        "capacity": asdict(capacity),
        "minimum": asdict(minimum),
    }
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


async def recompute(pool: AsyncConnectionPool, settings: Settings) -> RecomputeResult:
    """Recompute every grant when any input changed; alert on reductions and restorations."""
    capacity, minimum = capacity_pool(settings), minimum_pool(settings)
    async with pool.connection() as connection, connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (_RECOMPUTE_LOCK_KEY,))
        pairs = await _read_requests(connection)
        inputs_hash = _inputs_hash(pairs, capacity, minimum)
        cursor = await connection.execute("SELECT inputs_hash FROM allocation_state")
        row = await cursor.fetchone()
        if row is not None and row[0] == inputs_hash:
            return RecomputeResult(changed=False, grants=[])
        previous = {
            (grant.project_id, grant.environment): grant for grant in await _read_grants(connection)
        }
        grants = allocate(pairs, capacity, minimum)
        await connection.execute("DELETE FROM grants")
        for grant in grants:
            await connection.execute(
                "INSERT INTO grants (project_id, environment, requests_per_minute, concurrency, "
                "monthly_budget_micro_usd, per_call_cost_ceiling_micro_usd, state, reasons) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    grant.project_id,
                    grant.environment,
                    grant.requests_per_minute,
                    grant.concurrency,
                    grant.monthly_budget_micro_usd,
                    grant.per_call_cost_ceiling_micro_usd,
                    grant.state,
                    Jsonb([{"dimension": r.dimension, "code": r.code} for r in grant.reasons]),
                ),
            )
        await connection.execute(
            "INSERT INTO allocation_state (singleton, inputs_hash) VALUES (1, %s) "
            "ON CONFLICT (singleton) DO UPDATE SET inputs_hash = excluded.inputs_hash, "
            "computed_at = now()",
            (inputs_hash,),
        )
    # Alerts go out after commit, so a rolled-back recompute never alerts.
    pair_requests = {pair.sort_key: pair for pair in pairs}
    for grant in grants:
        key = (grant.project_id, grant.environment)
        await _alert_on_change(pool, settings, previous.get(key), grant, pair_requests[key])
    logger.info("Grants recomputed: %d pairs", len(grants))
    return RecomputeResult(changed=True, grants=grants)


def _decreased(before: StoredGrant, after: Grant) -> bool:
    return (
        after.requests_per_minute < before.requests_per_minute
        or after.concurrency < before.concurrency
        or after.monthly_budget_micro_usd < before.monthly_budget_micro_usd
    )


def _reduction_text(grant: Grant, request: PairRequest) -> str:
    parts = []
    for reason in grant.reasons:
        if reason.dimension == "monthly_budget":
            asked = format_usd(request.monthly_budget_micro_usd)
            given = format_usd(grant.monthly_budget_micro_usd)
            parts.append(f"monthly budget {asked} to {given} USD ({reason.code})")
        else:
            label = reason.dimension.replace("_", " ")
            asked = request.requested(reason.dimension)
            given = getattr(grant, reason.dimension)
            parts.append(f"{label} {asked} to {given} ({reason.code})")
    return "; ".join(parts)


async def _alert_on_change(
    pool: AsyncConnectionPool,
    settings: Settings,
    before: StoredGrant | None,
    after: Grant,
    request: PairRequest,
) -> None:
    pair = f"{after.project_id}/{after.environment}"
    if after.state == "reduced" and (
        before is None or before.state == "approved" or _decreased(before, after)
    ):
        await raise_alert(
            pool,
            event_type="grant_reduced",
            severity="warning",
            summary=f"Grant for {pair} reduced: {_reduction_text(after, request)}.",
            key=dedupe_key("grant_reduced", after.project_id, after.environment),
            dedupe_window_seconds=settings.notifier_dedupe_window_seconds,
        )
    elif after.state == "approved" and before is not None and before.state == "reduced":
        await raise_alert(
            pool,
            event_type="grant_restored",
            severity="info",
            summary=f"Grant for {pair} is fully approved again.",
            key=dedupe_key("grant_restored", after.project_id, after.environment),
            dedupe_window_seconds=settings.notifier_dedupe_window_seconds,
        )


async def _read_grants(
    connection: AsyncConnection, where: LiteralString = "", params: tuple[str, ...] = ()
) -> list[StoredGrant]:
    query: LiteralString = (
        "SELECT project_id, environment, requests_per_minute, concurrency, "
        "monthly_budget_micro_usd, per_call_cost_ceiling_micro_usd, state, reasons, computed_at "
        "FROM grants " + where + " ORDER BY project_id, environment"
    )
    cursor = await connection.execute(query, params)
    grants = []
    for row in await cursor.fetchall():
        reasons = tuple(Reason(item["dimension"], item["code"]) for item in row[7])
        grants.append(StoredGrant(*row[:7], reasons=reasons, computed_at=row[8]))
    return grants


async def list_grants(pool: AsyncConnectionPool) -> list[StoredGrant]:
    async with pool.connection() as connection:
        return await _read_grants(connection)


async def get_grant(
    pool: AsyncConnectionPool, project_id: str, environment: str
) -> StoredGrant | None:
    async with pool.connection() as connection:
        grants = await _read_grants(
            connection, "WHERE project_id = %s AND environment = %s", (project_id, environment)
        )
    return grants[0] if grants else None


async def set_weight(pool: AsyncConnectionPool, project_id: str, weight: int) -> bool:
    """Set a project's backend weight; return False when the project does not exist."""
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "UPDATE projects SET weight = %s, updated_at = now() WHERE project_id = %s",
            (weight, project_id),
        )
        return cursor.rowcount == 1
