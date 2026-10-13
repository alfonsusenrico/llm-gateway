"""Budget threshold alerts: 50%, 80%, and 100% of a grant or the global cap, once a month."""

from __future__ import annotations

from datetime import UTC, datetime

from psycopg_pool import AsyncConnectionPool

from llm_gateway.calls.spend import month_start, spend_by_pair
from llm_gateway.money import format_usd, to_micro_usd
from llm_gateway.notifier.events import Severity, dedupe_key, raise_alert
from llm_gateway.settings import Settings

THRESHOLDS: tuple[tuple[int, Severity], ...] = ((50, "info"), (80, "warning"), (100, "critical"))


async def check_budget_thresholds(
    pool: AsyncConnectionPool,
    settings: Settings,
    *,
    only_pair: tuple[str, str] | None = None,
) -> int:
    """Alert on thresholds crossed this month that were not reported yet; return the count."""
    now = datetime.now(UTC)
    since = month_start(now)
    month = since.date()
    async with pool.connection() as connection:
        spend = await spend_by_pair(connection, since)
        cursor = await connection.execute(
            "SELECT project_id, environment, monthly_budget_micro_usd FROM grants"
        )
        budgets = {(str(r[0]), str(r[1])): int(r[2]) for r in await cursor.fetchall()}
    scopes: list[tuple[str, str, int, int]] = []
    for pair, budget in budgets.items():
        if only_pair is not None and pair != only_pair:
            continue
        label = f"{pair[0]}/{pair[1]}"
        scopes.append((f"pair:{label}", f"Grant {label}", spend.get(pair, 0), budget))
    cap = to_micro_usd(settings.global_monthly_budget_usd)
    scopes.append(("global", "The global cap", sum(spend.values()), cap))
    sent = 0
    for scope, label, spent, budget in scopes:
        if budget <= 0:
            continue
        for percent, severity in THRESHOLDS:
            if spent * 100 < budget * percent:
                break
            if not await _claim(pool, scope, month, percent):
                continue
            await raise_alert(
                pool,
                event_type="budget_threshold",
                severity=severity,
                summary=(
                    f"{label} has used {percent}% of its monthly budget: "
                    f"{format_usd(spent)} of {format_usd(budget)} USD."
                ),
                key=dedupe_key("budget_threshold", scope, month.isoformat(), str(percent)),
                dedupe_window_seconds=settings.notifier_dedupe_window_seconds,
            )
            sent += 1
    return sent


async def _claim(pool: AsyncConnectionPool, scope: str, month: object, percent: int) -> bool:
    """Record a threshold as reported; False when it already was this month."""
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "INSERT INTO alerts_sent (scope, month, threshold_percent) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (scope, month, percent),
        )
        return cursor.rowcount == 1
