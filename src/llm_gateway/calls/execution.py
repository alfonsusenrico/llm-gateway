"""Task-neutral locked resolution, credential capture and admission; no inference IO."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from llm_gateway.calls.admission import admit, find_call
from llm_gateway.calls.records import Admitted, AlreadyFinished, CallRecord, CallRequest
from llm_gateway.catalogue.model import Catalogue, ModelEntry
from llm_gateway.catalogue.store import registered
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.notifier.events import dedupe_key, raise_alert
from llm_gateway.projects.contract import ContractV2, parse_contract
from llm_gateway.projects.credentials import Credential, CredentialError, CredentialStore
from llm_gateway.settings import Settings


class ExecutionRequest(Protocol):
    @property
    def provider_call_id(self) -> str: ...
    @property
    def operation(self) -> str: ...
    def fingerprint(self) -> str: ...


class ExecutionDeps(Protocol):
    @property
    def pool(self) -> AsyncConnectionPool: ...
    @property
    def settings(self) -> Settings: ...
    @property
    def catalogue(self) -> Catalogue: ...
    @property
    def adapters(self) -> dict[str, Callable[[Credential | None], object]]: ...
    @property
    def actor(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ExecutionPlan[R]:
    resolved: R
    entry: ModelEntry
    settings_digest: str
    max_output_tokens: int
    input_token_upper_bound: int | None = None


@dataclass(frozen=True, slots=True)
class PreparedExecution[R]:
    plan: ExecutionPlan[R]
    adapter: object
    admission: Admitted
    pair: str


async def prepare_execution[R](
    deps: ExecutionDeps,
    *,
    project_id: str,
    environment: str,
    task: str,
    request: ExecutionRequest,
    request_bytes: int,
    resolve: Callable[[ContractV2], ExecutionPlan[R]],
) -> PreparedExecution[R] | CallRecord:
    previous = await find_call(deps.pool, project_id, environment, request.provider_call_id)
    if previous is not None:
        if previous.task != task or previous.request_fingerprint != request.fingerprint():
            raise CallRejectedError("idempotency_conflict")
        if previous.state not in {"admitted", "retryable_failure"}:
            return previous
    credential = None
    try:
        async with deps.pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                "SELECT current_version FROM projects WHERE project_id = %s FOR UPDATE",
                (project_id,),
            )
            project = await cursor.fetchone()
            if project is None:
                raise CallRejectedError("project_quarantined")
            cursor = await connection.execute(
                "SELECT contract FROM contract_versions WHERE project_id = %s AND version = %s",
                (project_id, project[0]),
            )
            row = await cursor.fetchone()
            if row is None:
                raise CallRejectedError("project_quarantined")
            current = parse_contract(row[0])
            if current.status != "active":
                raise CallRejectedError("project_disabled")
            plan = resolve(current)
            entry = plan.entry
            if entry.task != task:
                raise CallRejectedError("model_task_mismatch")
            if not await registered(deps.pool, entry, connection):
                raise CallRejectedError("model_registry_unavailable")
            factory = deps.adapters.get(entry.adapter)
            if factory is None:
                raise CallRejectedError("provider_not_configured")
            if entry.billing != "zero":
                store = CredentialStore(deps.pool, deps.settings.provider_credential_encryption_key)
                try:
                    credential = await store.capture(connection, project_id, entry.provider)
                except CredentialError as exc:
                    if (
                        previous is not None
                        and previous.state == "retryable_failure"
                        and exc.code == "provider_credentials_missing"
                    ):
                        raise CallRejectedError("credential_changed") from None
                    raise
            adapter = factory(credential)
            admission = await admit(
                deps.pool,
                deps.settings,
                deps.catalogue,
                CallRequest(
                    project_id=project_id,
                    environment=environment,
                    provider_call_id=request.provider_call_id,
                    request_fingerprint=request.fingerprint(),
                    operation=request.operation,
                    provider=entry.provider,
                    model=entry.model,
                    service_tier="standard",
                    request_bytes=request_bytes,
                    max_output_tokens=plan.max_output_tokens,
                    model_alias=entry.alias,
                    definition_digest=entry.definition_digest,
                    settings_digest=plan.settings_digest,
                    credential_revision=credential.revision if credential else None,
                    billing_kind=entry.billing,
                    task=task,
                    input_token_upper_bound=plan.input_token_upper_bound,
                ),
                actor=deps.actor,
                connection=connection,
            )
    except CredentialError as exc:
        if exc.code == "provider_credentials_unreadable":
            await raise_alert(
                deps.pool,
                event_type=exc.code,
                severity="error",
                summary=f"Provider credentials are unreadable for project {project_id}.",
                key=dedupe_key(exc.code, project_id),
                dedupe_window_seconds=deps.settings.notifier_dedupe_window_seconds,
            )
        raise CallRejectedError(exc.code) from None
    if isinstance(admission, AlreadyFinished):
        return admission.call
    return PreparedExecution(plan, adapter, admission, f"{project_id}/{environment}")
