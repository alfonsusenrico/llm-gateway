"""Project registry actions shared by the admin subcommands and the interactive menu.

Each action returns the text to show; failures raise `RegistryError` or
`ContractError` with a message meant for the owner.
"""

from __future__ import annotations

import getpass
import json
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool

from llm_gateway.allocation import store as allocation
from llm_gateway.money import format_usd
from llm_gateway.projects import service
from llm_gateway.projects.contract import ContractError, Environment, parse_contract
from llm_gateway.projects.keys import IssuedKey
from llm_gateway.settings import Settings


@dataclass(frozen=True, slots=True)
class RequestValues:
    requests_per_minute: int | None = None
    concurrency: int | None = None
    monthly_budget_usd: str | None = None
    per_call_cost_ceiling_usd: str | None = None

    def as_changes(self) -> dict[str, Any]:
        values = {field.name: getattr(self, field.name) for field in fields(self)}
        return {name: value for name, value in values.items() if value is not None}


# Modest starting requests; the owner raises them with set-request.
DEFAULT_REQUESTS: dict[Environment, RequestValues] = {
    "production": RequestValues(30, 4, "1.00", "0.05"),
    "development": RequestValues(10, 2, "0.25", "0.02"),
}


def actor() -> str:
    return f"cli:{getpass.getuser()}"


def _when(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _keys_text(keys: list[IssuedKey]) -> str:
    lines = ["Store these keys now; they are never shown again:"]
    lines += [f"  {key.environment:<12} {key.raw}" for key in keys]
    return "\n".join(lines)


async def register(
    pool: AsyncConnectionPool,
    pepper: bytes,
    *,
    project_id: str,
    display_name: str,
    requests: dict[Environment, RequestValues] | None = None,
) -> str:
    overrides = requests or {}
    request = {
        environment: {
            **defaults.as_changes(),
            **overrides.get(environment, RequestValues()).as_changes(),
        }
        for environment, defaults in DEFAULT_REQUESTS.items()
    }
    contract = parse_contract(
        {
            "contract_version": 2,
            "project_id": project_id,
            "display_name": display_name,
            "status": "active",
            "request": request,
            "profiles": {},
        }
    )
    keys = await service.register_project(pool, contract, actor=actor(), pepper=pepper)
    return f"Registered {project_id} (version 1).\n{_keys_text(keys)}"


async def list_projects(pool: AsyncConnectionPool) -> str:
    projects = await service.list_projects(pool)
    if not projects:
        return "No projects registered."
    lines = [f"{'PROJECT':<24} {'STATUS':<9} {'VERSION':>7} {'WEIGHT':>6}  NAME"]
    lines += [
        f"{p.project_id:<24} {p.status:<9} {p.version:>7} {p.weight:>6}  {p.display_name}"
        for p in projects
    ]
    return "\n".join(lines)


async def upgrade_contracts(pool: AsyncConnectionPool, project_id: str | None) -> str:
    identifiers = (
        [project_id] if project_id else [p.project_id for p in await service.list_projects(pool)]
    )
    upgraded = 0
    failed = []
    for identifier in identifiers:
        try:
            upgraded += await service.upgrade_contract(pool, identifier, actor=actor())
        except (service.RegistryError, ContractError):
            failed.append(identifier)
    if failed:
        raise service.RegistryError(
            f"Contract upgrades: {upgraded}; failed projects: {', '.join(failed)}"
        )
    return f"Contracts upgraded: {upgraded}."


async def show(pool: AsyncConnectionPool, project_id: str, version: int | None = None) -> str:
    contract, shown_version = await service.get_contract(pool, project_id, version)
    keys = await service.list_keys(pool, project_id)
    now = datetime.now(UTC)
    key_lines = []
    for key in keys:
        if key.revoked_at is not None:
            state = f"revoked {_when(key.revoked_at)}"
        elif key.expires_at is not None and key.expires_at <= now:
            state = f"expired {_when(key.expires_at)}"
        elif key.expires_at is not None:
            state = f"expires {_when(key.expires_at)}"
        else:
            state = "active"
        key_lines.append(f"  {key.key_id}  {key.environment:<12} {state}")
    body = json.dumps(contract.to_json(), indent=2)
    return f"Version {shown_version}\n{body}\nKeys:\n" + "\n".join(key_lines)


async def history(pool: AsyncConnectionPool, project_id: str) -> str:
    versions = await service.history(pool, project_id)
    return "\n".join(
        f"v{item.version:<4} {_when(item.changed_at)}  {item.actor:<20} {item.change_note}"
        for item in versions
    )


async def set_request(
    pool: AsyncConnectionPool,
    project_id: str,
    environment: Environment,
    values: RequestValues,
    *,
    note: str | None = None,
) -> str:
    changes = values.as_changes()
    if not changes:
        raise service.RegistryError("Give at least one value to change.")

    def change(contract: dict[str, Any]) -> dict[str, Any]:
        contract["request"][environment].update(changes)
        return contract

    await service.update_contract(
        pool, project_id, change, actor=actor(), note=note or f"set-request {environment}"
    )
    return f"Updated the {environment} request of {project_id}."


async def set_profile(
    pool: AsyncConnectionPool, project_id: str, name: str, profile: dict[str, Any]
) -> str:
    settings = {key: value for key, value in profile.items() if value is not None}

    def change(contract: dict[str, Any]) -> dict[str, Any]:
        contract["profiles"][name] = settings
        return contract

    await service.update_contract(
        pool, project_id, change, actor=actor(), note=f"set-profile {name}"[:200]
    )
    return f"Set profile {name} of {project_id}."


async def remove_profile(pool: AsyncConnectionPool, project_id: str, name: str) -> str:
    def change(contract: dict[str, Any]) -> dict[str, Any]:
        if name not in contract["profiles"]:
            raise service.RegistryError(f"Project {project_id} has no profile {name}.")
        del contract["profiles"][name]
        return contract

    await service.update_contract(
        pool, project_id, change, actor=actor(), note=f"remove-profile {name}"[:200]
    )
    return f"Removed profile {name} of {project_id}."


async def set_status(pool: AsyncConnectionPool, project_id: str, status: str) -> str:
    def change(contract: dict[str, Any]) -> dict[str, Any]:
        contract["status"] = status
        return contract

    note = "enable" if status == "active" else "disable"
    await service.update_contract(pool, project_id, change, actor=actor(), note=note)
    return f"Project {project_id} is now {status}; requests see it within 10 seconds."


async def rotate_key(
    pool: AsyncConnectionPool,
    pepper: bytes,
    project_id: str,
    environment: Environment,
    grace_minutes: int,
) -> str:
    key = await service.rotate_key(
        pool, project_id, environment, grace_minutes=grace_minutes, pepper=pepper
    )
    if grace_minutes:
        previous = f"Previous {environment} keys keep working for {grace_minutes} minutes."
    else:
        previous = f"Previous {environment} keys stopped working now."
    return f"{previous}\n{_keys_text([key])}"


async def revoke_key(pool: AsyncConnectionPool, key_id: str) -> str:
    await service.revoke_key(pool, key_id)
    return f"Revoked key {key_id}."


async def export_contract(pool: AsyncConnectionPool, project_id: str) -> str:
    contract, _version = await service.get_contract(pool, project_id)
    return json.dumps(contract.to_json(), indent=2)


async def import_contract(pool: AsyncConnectionPool, text: str, *, note: str | None) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"not valid JSON (line {exc.lineno})") from None
    try:
        contract = await service.import_contract(pool, data, actor=actor(), note=note or "import")
    except service.ProjectNotFoundError as exc:
        raise service.RegistryError(
            f"{exc} Register it first with `llm-gateway-admin project register`."
        ) from None
    return f"Imported a new version of {contract.project_id}; keys are unchanged."


async def recompute_grants(pool: AsyncConnectionPool, settings: Settings) -> str:
    result = await allocation.recompute(pool, settings)
    if not result.changed:
        return "Grants unchanged."
    reduced = sum(1 for grant in result.grants if grant.state == "reduced")
    return f"Grants recomputed: {len(result.grants)} grants, {reduced} reduced."


async def set_weight(
    pool: AsyncConnectionPool, settings: Settings, project_id: str, weight: int
) -> str:
    if not 1 <= weight <= 100:
        raise service.RegistryError("Weight must be between 1 and 100.")
    if not await allocation.set_weight(pool, project_id, weight):
        raise service.ProjectNotFoundError(f"Project {project_id} is not registered.")
    grants = await recompute_grants(pool, settings)
    return f"Weight of {project_id} is now {weight}.\n{grants}"


async def show_grants(pool: AsyncConnectionPool, settings: Settings) -> str:
    grants = await allocation.list_grants(pool)
    if not grants:
        return "No grants. Register or enable a project first."
    requests = {pair.sort_key: pair for pair in await allocation.current_requests(pool)}
    lines = [
        f"{'PROJECT/ENVIRONMENT':<36} {'STATE':<8} {'RPM':>9} {'CONC':>7} "
        f"{'BUDGET USD':>15} {'CEILING USD':>13}  REASONS"
    ]
    for grant in grants:
        asked = requests.get((grant.project_id, grant.environment))
        if asked is None:
            continue  # Its contract changed since the last recompute.
        rpm = f"{grant.requests_per_minute}/{asked.requests_per_minute}"
        concurrency = f"{grant.concurrency}/{asked.concurrency}"
        budget = (
            f"{format_usd(grant.monthly_budget_micro_usd)}/"
            f"{format_usd(asked.monthly_budget_micro_usd)}"
        )
        ceiling = format_usd(grant.per_call_cost_ceiling_micro_usd)
        reasons = ", ".join(f"{r.dimension}:{r.code}" for r in grant.reasons) or "-"
        pair = f"{grant.project_id}/{grant.environment}"
        lines.append(
            f"{pair:<36} {grant.state:<8} {rpm:>9} {concurrency:>7} {budget:>15} "
            f"{ceiling:>13}  {reasons}"
        )
    capacity = allocation.capacity_pool(settings)
    used_rpm = sum(grant.requests_per_minute for grant in grants)
    used_concurrency = sum(grant.concurrency for grant in grants)
    used_budget = sum(grant.monthly_budget_micro_usd for grant in grants)
    lines.append(
        "Granted/requested per grant. Pool use: "
        f"requests per minute {used_rpm}/{capacity.requests_per_minute}, "
        f"concurrency {used_concurrency}/{capacity.concurrency}, "
        f"monthly budget {format_usd(used_budget)}/"
        f"{format_usd(capacity.monthly_budget_micro_usd)} USD"
    )
    return "\n".join(lines)
