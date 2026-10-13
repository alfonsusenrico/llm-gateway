"""Routes about the calling project itself."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from llm_gateway.api.auth import ProjectIdentity, require_project
from llm_gateway.projects.contract import Environment

router = APIRouter(prefix="/v1/projects", tags=["projects"])

_AUTH_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {
        "description": "Key missing, malformed, or invalid (`key_missing`, `key_malformed`, `key_invalid`)."
    },
    403: {"description": "Project disabled (`project_disabled`)."},
    429: {"description": "Source throttled after repeated failures (`auth_throttled`)."},
    503: {"description": "Stored contract invalid (`project_quarantined`)."},
}


class ProjectSelf(BaseModel):
    project_id: str
    display_name: str
    environment: Environment
    contract_version: int


@router.get("/self", responses=_AUTH_RESPONSES)
async def project_self(
    identity: Annotated[ProjectIdentity, Depends(require_project)],
) -> ProjectSelf:
    """Who the presented key belongs to; lets a client check its credentials."""
    return ProjectSelf(
        project_id=identity.project_id,
        display_name=identity.contract.display_name,
        environment=identity.environment,
        contract_version=identity.contract_version,
    )
