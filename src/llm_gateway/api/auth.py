"""Project authentication: bearer keys, per-source throttling, contract status checks."""

from __future__ import annotations

import hashlib
import ipaddress
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request
from psycopg_pool import AsyncConnectionPool

from llm_gateway.api.problems import GatewayError
from llm_gateway.logging_setup import project_context_var
from llm_gateway.notifier.events import dedupe_key, raise_alert_soon
from llm_gateway.projects.contract import ContractError, ContractV1, Environment, parse_contract
from llm_gateway.projects.keys import parse_key, secret_matches
from llm_gateway.settings import Settings

THROTTLE_WINDOW_SECONDS = 60
CONTRACT_CACHE_SECONDS = 10.0
# Bounds memory if many distinct sources fail; the oldest are forgotten first.
_MAX_TRACKED_SOURCES = 10_000
_BEARER_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="llm-gateway"'}


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    project_id: str
    environment: Environment
    key_id: str
    contract_version: int
    contract: ContractV1


@dataclass(frozen=True, slots=True)
class _LoadedProject:
    contract: ContractV1 | None  # None: the stored contract is invalid (quarantined)
    version: int


class FailureThrottle:
    """Counts failed authentications per source in a sliding window (per process)."""

    def __init__(
        self,
        limit: int,
        *,
        window_seconds: float = THROTTLE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def _recent(self, source: str) -> deque[float]:
        failures = self._failures.get(source)
        if failures is None:
            return deque()
        cutoff = self._clock() - self._window
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            del self._failures[source]
        return failures

    def blocked(self, source: str) -> bool:
        return len(self._recent(source)) > self._limit

    def record_failure(self, source: str) -> bool:
        """Count a failure; return True when this one crosses the limit."""
        failures = self._recent(source)
        failures.append(self._clock())
        self._failures[source] = failures
        self._failures.move_to_end(source)
        while len(self._failures) > _MAX_TRACKED_SOURCES:
            self._failures.popitem(last=False)
        return len(failures) == self._limit + 1


class ProjectDirectory:
    """Current contracts by project, cached briefly so edits apply without a restart."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        dedupe_window_seconds: int,
        ttl_seconds: float = CONTRACT_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool
        self._dedupe_window_seconds = dedupe_window_seconds
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, tuple[float, _LoadedProject | None]] = {}

    async def load(self, project_id: str) -> _LoadedProject | None:
        cached = self._entries.get(project_id)
        if cached is not None and self._clock() - cached[0] < self._ttl:
            return cached[1]
        loaded = await self._read(project_id)
        self._entries[project_id] = (self._clock(), loaded)
        return loaded

    async def _read(self, project_id: str) -> _LoadedProject | None:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT p.current_version, v.contract FROM projects p "
                "JOIN contract_versions v "
                "  ON v.project_id = p.project_id AND v.version = p.current_version "
                "WHERE p.project_id = %s",
                (project_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        version = int(row[0])
        try:
            contract = parse_contract(row[1])
        except ContractError:
            raise_alert_soon(
                self._pool,
                event_type="project_quarantined",
                severity="error",
                summary=(
                    f"The stored contract of project {project_id} (version {version}) is "
                    "invalid; its requests are refused until it is corrected."
                ),
                key=dedupe_key("project_quarantined", project_id),
                dedupe_window_seconds=self._dedupe_window_seconds,
            )
            return _LoadedProject(None, version)
        return _LoadedProject(contract, version)


class Authenticator:
    def __init__(
        self,
        settings: Settings,
        pool: AsyncConnectionPool,
        *,
        directory: ProjectDirectory | None = None,
        throttle: FailureThrottle | None = None,
    ) -> None:
        self._pool = pool
        self._pepper = settings.project_key_pepper_bytes
        self._client_ip_header = settings.trusted_client_ip_header
        self._dedupe_window_seconds = settings.notifier_dedupe_window_seconds
        self.directory = directory or ProjectDirectory(
            pool, dedupe_window_seconds=self._dedupe_window_seconds
        )
        self.throttle = throttle or FailureThrottle(settings.auth_failure_limit_per_minute)

    async def authenticate(self, request: Request) -> ProjectIdentity:
        source = self._client_source(request)
        if self.throttle.blocked(source):
            raise GatewayError(
                "auth_throttled",
                status=429,
                title="Too many failed authentications",
                retry_after_seconds=THROTTLE_WINDOW_SECONDS,
            )
        header = request.headers.get("authorization")
        if not header:
            raise GatewayError(
                "key_missing", status=401, title="API key required", headers=_BEARER_CHALLENGE
            )
        scheme, _, raw_key = header.partition(" ")
        presented = parse_key(raw_key.strip()) if scheme.lower() == "bearer" else None
        if presented is None:
            self._fail(source)
            raise GatewayError(
                "key_malformed", status=401, title="API key is malformed", headers=_BEARER_CHALLENGE
            )
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT project_id, environment, secret_hmac, "
                "  revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now()) "
                "FROM project_keys WHERE key_id = %s",
                (presented.key_id,),
            )
            row = await cursor.fetchone()
        valid = (
            row is not None
            and bool(row[3])
            and row[1] == presented.environment
            and secret_matches(self._pepper, presented.secret, str(row[2]))
        )
        if row is None or not valid:
            self._fail(source)
            if row is not None:
                self._alert_rejected_key(presented.key_id, str(row[0]), str(row[1]))
            # One response for every invalid case, so key IDs cannot be probed.
            raise GatewayError(
                "key_invalid", status=401, title="API key is invalid", headers=_BEARER_CHALLENGE
            )
        project_id, environment = str(row[0]), presented.environment
        loaded = await self.directory.load(project_id)
        if loaded is None or loaded.contract is None:
            raise GatewayError(
                "project_quarantined",
                status=503,
                title="Project contract is invalid",
                detail="The owner has been alerted.",
            )
        if loaded.contract.status == "disabled":
            raise GatewayError("project_disabled", status=403, title="Project is disabled")
        project_context_var.set(f"{project_id}/{environment}")
        return ProjectIdentity(
            project_id, environment, presented.key_id, loaded.version, loaded.contract
        )

    def _client_source(self, request: Request) -> str:
        if self._client_ip_header:
            candidate = request.headers.get(self._client_ip_header, "").strip()
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                pass
        return request.client.host if request.client else "unknown"

    def _fail(self, source: str) -> None:
        if self.throttle.record_failure(source):
            # A hash keeps the address out of the alert store while still grouping it.
            source_tag = hashlib.sha256(source.encode()).hexdigest()[:12]
            raise_alert_soon(
                self._pool,
                event_type="auth_failures_throttled",
                severity="warning",
                summary=(
                    "Failed authentications from one source exceeded the limit; it is "
                    f"throttled for {THROTTLE_WINDOW_SECONDS} s."
                ),
                key=dedupe_key("auth_failures_throttled", source_tag),
                dedupe_window_seconds=self._dedupe_window_seconds,
            )

    def _alert_rejected_key(self, key_id: str, project_id: str, environment: str) -> None:
        raise_alert_soon(
            self._pool,
            event_type="project_key_rejected",
            severity="warning",
            summary=(
                f"A revoked, expired, or wrong key {key_id} of project {project_id} "
                f"({environment}) was rejected."
            ),
            key=dedupe_key("project_key_rejected", key_id),
            dedupe_window_seconds=self._dedupe_window_seconds,
        )


async def require_project(request: Request) -> ProjectIdentity:
    """FastAPI dependency: the authenticated project of this request."""
    authenticator: Authenticator = request.app.state.authenticator
    return await authenticator.authenticate(request)
