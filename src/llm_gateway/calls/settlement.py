"""Settling calls: record the outcome, price the usage, decide what counts as spend."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, LiteralString

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from llm_gateway.calls.budget import check_budget_thresholds
from llm_gateway.calls.records import CALL_COLUMNS, CallRecord, CallState, check_transition
from llm_gateway.catalogue.model import Catalogue, catalogue_tier
from llm_gateway.catalogue.pricing import TokenUsage, cost_micro_usd
from llm_gateway.money import format_usd
from llm_gateway.notifier.events import dedupe_key, raise_alert
from llm_gateway.settings import Settings

if TYPE_CHECKING:
    from llm_gateway.llm.results import ResultStore

logger = logging.getLogger(__name__)
_SELECT_CALL: LiteralString = "SELECT " + CALL_COLUMNS + " FROM calls WHERE call_key = %s"  # noqa: S608  # LiteralString fragments only
_SELECT_CALL_FOR_UPDATE: LiteralString = _SELECT_CALL + " FOR UPDATE"


@dataclass(frozen=True, slots=True)
class Outcome:
    state: CallState
    usage: TokenUsage | None = None
    reasoning_tokens: int | None = None
    service_tier_used: str | None = None
    failure_code: str | None = None


class CallNotFoundError(LookupError):
    pass


def _charged(outcome: Outcome, cost: int | None, reserved: int) -> int:
    match outcome.state:
        case "completed":
            # Usage without a price counts the reservation, so the cap stays safe.
            return cost if cost is not None else reserved
        case "failed":
            # A provider-side failure can still bill the tokens it processed.
            return cost or 0
        case "retryable_failure":
            return 0
        case "execution_unknown":
            return reserved
        case _:
            raise ValueError(f"cannot settle into {outcome.state}")


async def settle(
    pool: AsyncConnectionPool,
    settings: Settings,
    catalogue: Catalogue,
    call_key: int,
    outcome: Outcome,
    *,
    actor: str,
    completed_result: tuple[ResultStore, dict[str, Any]] | None = None,
) -> CallRecord:
    """Finish an admitted call; alerts for unknown prices and budget thresholds follow commit."""
    unpriced = False
    async with pool.connection() as connection, connection.transaction():
        call = await _locked_call(connection, call_key)
        check_transition(call.state, outcome.state)
        tier = (
            catalogue_tier(call.provider, outcome.service_tier_used)
            if outcome.service_tier_used
            else call.service_tier_requested
        )
        price = (
            None
            if call.billing_kind == "zero"
            else catalogue.price(call.provider, call.model, tier, call.admitted_at.date())
        )
        cost = cost_micro_usd(outcome.usage, price) if outcome.usage is not None else None
        if call.billing_kind == "zero":
            cost = 0
        unpriced = outcome.usage is not None and cost is None
        charged = _charged(outcome, cost, call.reserved_micro_usd)
        usage = outcome.usage
        await connection.execute(
            """
            UPDATE calls SET state = %s, cost_micro_usd = %s, charged_micro_usd = %s,
                service_tier_used = %s, failure_code = %s, finished_at = now(),
                lease_expires_at = NULL, input_tokens = %s, cached_input_tokens = %s,
                cache_write_tokens = %s, output_tokens = %s, reasoning_tokens = %s,
                price_tier = %s, price_effective_from = %s
            WHERE call_key = %s
            """,
            (
                outcome.state,
                cost,
                charged,
                outcome.service_tier_used,
                outcome.failure_code,
                usage.input_tokens if usage else None,
                usage.cached_input_tokens if usage else None,
                usage.cache_write_tokens if usage else None,
                usage.output_tokens if usage else None,
                outcome.reasoning_tokens,
                price.service_tier if price and cost is not None else None,
                price.effective_from if price and cost is not None else None,
                call_key,
            ),
        )
        await _event(connection, call_key, call.state, outcome.state, charged, actor)
        settled = await _locked_call(connection, call_key)
        if completed_result is not None:
            if outcome.state != "completed":
                raise ValueError("result requires a completed outcome")
            store, body = completed_result
            body["cost_usd"] = format_usd(cost) if cost is not None else None
            await store.save(call_key, body, connection=connection)
    if unpriced:
        await raise_alert(
            pool,
            event_type="call_unpriced",
            severity="error",
            summary=(
                f"Usage of {call.provider}/{call.model} ({tier}) has no price; its reservation "
                "is counted as spend until a price is added."
            ),
            key=dedupe_key("call_unpriced", call.provider, call.model, tier),
            dedupe_window_seconds=settings.notifier_dedupe_window_seconds,
        )
    if call.billing_kind != "zero":
        await check_budget_thresholds(pool, settings, only_pair=(call.project_id, call.environment))
    return settled


async def recover_abandoned(pool: AsyncConnectionPool, *, actor: str = "worker") -> int:
    """Mark calls still admitted after their lease as unknown; they are never replayed."""
    async with pool.connection() as connection, connection.transaction():
        cursor = await connection.execute(
            """
            UPDATE calls SET state = 'execution_unknown', failure_code = 'execution_abandoned',
                charged_micro_usd = reserved_micro_usd, finished_at = now(),
                lease_expires_at = NULL
            WHERE state = 'admitted' AND lease_expires_at < now()
            RETURNING call_key, charged_micro_usd
            """
        )
        rows = await cursor.fetchall()
        for call_key, charged in rows:
            await _event(
                connection, int(call_key), "admitted", "execution_unknown", int(charged), actor
            )
    if rows:
        logger.warning("Abandoned calls marked execution_unknown: %d", len(rows))
    return len(rows)


async def get_call(pool: AsyncConnectionPool, call_key: int) -> CallRecord:
    async with pool.connection() as connection:
        cursor = await connection.execute(_SELECT_CALL, (call_key,))
        row = await cursor.fetchone()
    if row is None:
        raise CallNotFoundError(call_key)
    return CallRecord(*row)


async def _locked_call(connection: AsyncConnection, call_key: int) -> CallRecord:
    cursor = await connection.execute(_SELECT_CALL_FOR_UPDATE, (call_key,))
    row = await cursor.fetchone()
    if row is None:
        raise CallNotFoundError(call_key)
    return CallRecord(*row)


async def _event(
    connection: AsyncConnection,
    call_key: int,
    from_state: str,
    to_state: str,
    charged: int,
    actor: str,
) -> None:
    await connection.execute(
        "INSERT INTO call_events (call_key, from_state, to_state, charged_micro_usd, actor) "
        "VALUES (%s, %s, %s, %s, %s)",
        (call_key, from_state, to_state, charged, actor),
    )
