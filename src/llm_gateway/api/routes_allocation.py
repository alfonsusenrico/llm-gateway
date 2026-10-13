"""`GET /v1/allocation`: the calling project environment's grant."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from llm_gateway.allocation.store import StoredGrant, get_grant
from llm_gateway.api.auth import ProjectIdentity, require_project
from llm_gateway.api.problems import GatewayError
from llm_gateway.api.routes_projects import AUTH_RESPONSES
from llm_gateway.money import format_usd
from llm_gateway.projects.contract import Environment

router = APIRouter(prefix="/v1", tags=["allocation"])
GRANT_CACHE_SECONDS = 10.0
_PENDING_RETRY_SECONDS = 5


class Allowance(BaseModel):
    requests_per_minute: int
    concurrency: int
    monthly_budget_usd: str
    per_call_cost_ceiling_usd: str


class ReasonOut(BaseModel):
    dimension: str
    code: str


class AllocationOut(BaseModel):
    project_id: str
    environment: Environment
    state: str
    requested: Allowance
    granted: Allowance
    reasons: list[ReasonOut]
    profiles: list[str]
    computed_at: datetime


class GrantDirectory:
    """Grants by project environment, cached briefly like contracts."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        ttl_seconds: float = GRANT_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[tuple[str, str], tuple[float, StoredGrant | None]] = {}

    async def get(self, project_id: str, environment: str) -> StoredGrant | None:
        key = (project_id, environment)
        cached = self._entries.get(key)
        if cached is not None and self._clock() - cached[0] < self._ttl:
            return cached[1]
        grant = await get_grant(self._pool, project_id, environment)
        self._entries[key] = (self._clock(), grant)
        return grant


@router.get(
    "/allocation",
    responses={
        **AUTH_RESPONSES,
        503: {"description": "Grant not computed yet (`allocation_pending`), or contract invalid."},
    },
)
async def allocation(
    request: Request, identity: Annotated[ProjectIdentity, Depends(require_project)]
) -> AllocationOut:
    """This key's grant: what it requested, what it may use, and why any of it is reduced."""
    directory: GrantDirectory = request.app.state.grant_directory
    grant = await directory.get(identity.project_id, identity.environment)
    if grant is None:
        raise GatewayError(
            "allocation_pending",
            status=503,
            title="Grant not computed yet",
            retry_after_seconds=_PENDING_RETRY_SECONDS,
        )
    asked = getattr(identity.contract.request, identity.environment)
    return AllocationOut(
        project_id=identity.project_id,
        environment=identity.environment,
        state=grant.state,
        requested=Allowance(
            requests_per_minute=asked.requests_per_minute,
            concurrency=asked.concurrency,
            monthly_budget_usd=str(asked.monthly_budget_usd),
            per_call_cost_ceiling_usd=str(asked.per_call_cost_ceiling_usd),
        ),
        granted=Allowance(
            requests_per_minute=grant.requests_per_minute,
            concurrency=grant.concurrency,
            monthly_budget_usd=format_usd(grant.monthly_budget_micro_usd),
            per_call_cost_ceiling_usd=format_usd(grant.per_call_cost_ceiling_micro_usd),
        ),
        reasons=[ReasonOut(dimension=r.dimension, code=r.code) for r in grant.reasons],
        profiles=sorted(identity.contract.profiles),
        computed_at=grant.computed_at,
    )
