"""Admitting a call against its grant and the global cap, before any provider call.

One short transaction under a global advisory lock reads the grant and the
ledger, decides, and records the call. Nothing in it waits on the network.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import LiteralString

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from llm_gateway.calls.records import (
    CALL_COLUMNS,
    AdmissionRefusedError,
    Admitted,
    Allowance,
    AlreadyFinished,
    CallRecord,
    CallRequest,
    check_transition,
)
from llm_gateway.calls.spend import month_start, next_month_start, pair_spend, total_spend
from llm_gateway.catalogue.model import Catalogue
from llm_gateway.catalogue.pricing import TokenUsage, cost_micro_usd
from llm_gateway.money import to_micro_usd
from llm_gateway.notifier.events import dedupe_key, raise_alert_soon
from llm_gateway.settings import Settings

# Serializes admissions across processes; an arbitrary constant owned by this module.
_ADMISSION_LOCK_KEY = 7_402_110_003
RATE_WINDOW_SECONDS = 60
# Text averages about four bytes per token; two per token over-reserves on purpose.
_RESERVATION_BYTES_PER_TOKEN = 2
_LEASE_MARGIN_SECONDS = 60
_PENDING_RETRY_SECONDS = 5
_IN_PROGRESS_RETRY_SECONDS = 2


def reservation_micro_usd(catalogue: Catalogue, request: CallRequest, on: date) -> int:
    """The worst-case cost used to gate admission; settlement records the real cost."""
    if request.billing_kind == "zero":
        return 0
    price = catalogue.price(request.provider, request.model, request.service_tier, on)
    usage = TokenUsage(
        input_tokens=(
            request.input_token_upper_bound
            if request.input_token_upper_bound is not None
            else math.ceil(request.request_bytes / _RESERVATION_BYTES_PER_TOKEN)
        ),
        output_tokens=request.max_output_tokens,
    )
    cost = cost_micro_usd(usage, price)
    if cost is None:
        raise AdmissionRefusedError("price_unavailable", status=503)
    return cost


async def admit(
    pool: AsyncConnectionPool,
    settings: Settings,
    catalogue: Catalogue,
    request: CallRequest,
    *,
    actor: str,
    connection: AsyncConnection | None = None,
) -> Admitted | AlreadyFinished:
    """Admit the call or raise `AdmissionRefusedError`; an ended call ID returns its record."""
    now = datetime.now(UTC)
    reserved = reservation_micro_usd(catalogue, request, now.date())
    global_cap = to_micro_usd(settings.global_monthly_budget_usd)
    async with _admission_connection(pool, connection) as connection:
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ADMISSION_LOCK_KEY,))
        existing = await _existing_call(connection, request)
        if existing is not None:
            if (
                existing.request_fingerprint != request.request_fingerprint
                or existing.task != request.task
            ):
                raise AdmissionRefusedError("idempotency_conflict", status=409)
            if existing.state == "admitted":
                raise AdmissionRefusedError(
                    "call_in_progress",
                    status=409,
                    retry_after_seconds=_IN_PROGRESS_RETRY_SECONDS,
                )
            if existing.state != "retryable_failure":
                return AlreadyFinished(existing)
            if existing.credential_revision != request.credential_revision:
                raise AdmissionRefusedError("credential_changed", status=409)
            if (existing.definition_digest, existing.settings_digest) != (
                request.definition_digest,
                request.settings_digest,
            ):
                raise AdmissionRefusedError("call_configuration_changed", status=409)
        grant = await _grant(connection, request)
        if grant is None:
            raise AdmissionRefusedError(
                "allocation_pending", status=503, retry_after_seconds=_PENDING_RETRY_SECONDS
            )
        rpm_limit, concurrency_limit, budget, ceiling = grant
        if request.billing_kind != "zero" and reserved > ceiling:
            raise AdmissionRefusedError("cost_ceiling_exceeded", status=422)
        in_flight = await _count(
            connection,
            "SELECT count(*) FROM calls WHERE project_id = %s AND environment = %s "
            "AND state = 'admitted'",
            request,
        )
        if in_flight >= concurrency_limit:
            raise AdmissionRefusedError("concurrency_exhausted", status=429, retry_after_seconds=1)
        recent, oldest = await _recent_window(connection, request)
        if recent >= rpm_limit:
            raise AdmissionRefusedError(
                "rate_limited", status=429, retry_after_seconds=_window_reset(oldest, now)
            )
        since = month_start(now)
        spent = await pair_spend(connection, request.project_id, request.environment, since)
        until_next_month = max(1, math.ceil((next_month_start(now) - now).total_seconds()))
        if request.billing_kind != "zero" and spent + reserved > budget:
            raise AdmissionRefusedError(
                "budget_exhausted", status=429, retry_after_seconds=until_next_month
            )
        everyone = await total_spend(connection, since)
        if request.billing_kind != "zero" and everyone + reserved > global_cap:
            raise_alert_soon(
                pool,
                event_type="global_budget_exhausted",
                severity="critical",
                summary="The global monthly budget cap is reached; new calls are refused.",
                key=dedupe_key("global_budget_exhausted", since.date().isoformat()),
                dedupe_window_seconds=settings.notifier_dedupe_window_seconds,
            )
            raise AdmissionRefusedError(
                "budget_exhausted", status=429, retry_after_seconds=until_next_month
            )
        call_key = await _record_admission(
            connection, request, existing, reserved, settings.llm_call_timeout_seconds, actor
        )
    allowance = Allowance(
        requests_per_minute_limit=rpm_limit,
        requests_per_minute_remaining=max(0, rpm_limit - recent - 1),
        requests_per_minute_reset_seconds=(
            _window_reset(oldest, now) if oldest is not None else RATE_WINDOW_SECONDS
        ),
        concurrency_limit=concurrency_limit,
        concurrency_remaining=max(0, concurrency_limit - in_flight - 1),
        budget_remaining_micro_usd=max(
            0, min(budget - spent - reserved, global_cap - everyone - reserved)
        ),
    )
    return Admitted(call_key, reserved, allowance)


@asynccontextmanager
async def _admission_connection(
    pool: AsyncConnectionPool, existing: AsyncConnection | None
) -> AsyncIterator[AsyncConnection]:
    if existing is not None:
        yield existing
    else:
        async with pool.connection() as connection, connection.transaction():
            yield connection


async def find_call(
    pool: AsyncConnectionPool, project_id: str, environment: str, call_id: str
) -> CallRecord | None:
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT " + CALL_COLUMNS + " FROM calls "  # noqa: S608  # LiteralString fragments only
            "WHERE project_id = %s AND environment = %s AND provider_call_id = %s",
            (project_id, environment, call_id),
        )
        row = await cursor.fetchone()
        return CallRecord(*row) if row is not None else None


def _window_reset(oldest: datetime | None, now: datetime) -> int:
    if oldest is None:
        return RATE_WINDOW_SECONDS
    remaining = RATE_WINDOW_SECONDS - (now - oldest).total_seconds()
    return max(1, math.ceil(remaining))


async def _existing_call(connection: AsyncConnection, request: CallRequest) -> CallRecord | None:
    cursor = await connection.execute(
        "SELECT " + CALL_COLUMNS + " FROM calls "  # noqa: S608  # LiteralString fragments only
        "WHERE project_id = %s AND environment = %s AND provider_call_id = %s FOR UPDATE",
        (request.project_id, request.environment, request.provider_call_id),
    )
    row = await cursor.fetchone()
    return CallRecord(*row) if row else None


async def _grant(
    connection: AsyncConnection, request: CallRequest
) -> tuple[int, int, int, int] | None:
    cursor = await connection.execute(
        "SELECT requests_per_minute, concurrency, monthly_budget_micro_usd, "
        "per_call_cost_ceiling_micro_usd FROM grants WHERE project_id = %s AND environment = %s",
        (request.project_id, request.environment),
    )
    row = await cursor.fetchone()
    return (int(row[0]), int(row[1]), int(row[2]), int(row[3])) if row else None


async def _count(connection: AsyncConnection, query: LiteralString, request: CallRequest) -> int:
    cursor = await connection.execute(query, (request.project_id, request.environment))
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def _recent_window(
    connection: AsyncConnection, request: CallRequest
) -> tuple[int, datetime | None]:
    cursor = await connection.execute(
        "SELECT count(*), min(admitted_at) FROM calls WHERE project_id = %s "
        "AND environment = %s AND admitted_at > now() - make_interval(secs => %s)",
        (request.project_id, request.environment, RATE_WINDOW_SECONDS),
    )
    row = await cursor.fetchone()
    if row is None:
        return 0, None
    return int(row[0]), row[1]


async def _record_admission(
    connection: AsyncConnection,
    request: CallRequest,
    existing: CallRecord | None,
    reserved: int,
    timeout_seconds: int,
    actor: str,
) -> int:
    lease_seconds = timeout_seconds + _LEASE_MARGIN_SECONDS
    if existing is not None:
        check_transition(existing.state, "admitted")
        await connection.execute(
            """
            UPDATE calls SET state = 'admitted', reserved_micro_usd = %s, charged_micro_usd = 0,
                cost_micro_usd = NULL, failure_code = NULL, finished_at = NULL,
                admitted_at = now(), lease_expires_at = now() + make_interval(secs => %s)
            WHERE call_key = %s
            """,
            (reserved, lease_seconds, existing.call_key),
        )
        call_key, from_state = existing.call_key, existing.state
    else:
        cursor = await connection.execute(
            """
            INSERT INTO calls (project_id, environment, provider_call_id, request_fingerprint,
                operation, provider, model, service_tier_requested, state, reserved_micro_usd,
                lease_expires_at, request_schema_version, task, model_alias, definition_digest,
                settings_digest, credential_revision, billing_kind)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'admitted', %s,
                now() + make_interval(secs => %s), %s, %s, %s, %s, %s, %s, %s)
            RETURNING call_key
            """,
            (
                request.project_id,
                request.environment,
                request.provider_call_id,
                request.request_fingerprint,
                request.operation,
                request.provider,
                request.model,
                request.service_tier,
                reserved,
                lease_seconds,
                2 if request.model_alias is not None else 1,
                request.task,
                request.model_alias,
                request.definition_digest,
                request.settings_digest,
                request.credential_revision,
                request.billing_kind,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Call insert returned no row.")
        call_key, from_state = int(row[0]), None
    await connection.execute(
        "INSERT INTO call_events (call_key, from_state, to_state, charged_micro_usd, actor) "
        "VALUES (%s, %s, 'admitted', NULL, %s)",
        (call_key, from_state, actor),
    )
    return call_key
