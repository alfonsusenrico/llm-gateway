"""Authenticated discovery of registered models and their effective call schemas."""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from llm_gateway.api.auth import ProjectIdentity, require_project
from llm_gateway.api.problems import GatewayError, document_problems
from llm_gateway.api.routes_projects import AUTH_RESPONSES
from llm_gateway.catalogue.adapters import Task
from llm_gateway.catalogue.model import ModelEntry
from llm_gateway.catalogue.schemas import request_schema
from llm_gateway.catalogue.store import registered
from llm_gateway.llm.service import CallDeps
from llm_gateway.projects.credentials import CredentialStore

router = APIRouter(prefix="/v1/models", tags=["models"])


class Availability(BaseModel):
    configured: bool
    reason: str | None = None


class ModelSummary(BaseModel):
    alias: str
    task: Task
    provider: str
    model: str
    definition_digest: str
    execution: str
    billing: str
    limits: dict[str, int]
    capabilities: dict[str, Any]
    prices: dict[str, Any]
    availability: Availability


class ModelDetails(ModelSummary):
    request_schema: dict[str, Any]


class ModelList(BaseModel):
    models: list[ModelSummary]


async def _summary(deps: CallDeps, entry: ModelEntry, identity: ProjectIdentity) -> ModelSummary:
    reason = None
    if not await registered(deps.pool, entry):
        reason = "model_registry_unavailable"
    elif entry.adapter not in deps.adapters:
        reason = "provider_not_configured"
    elif entry.billing != "zero":
        metadata = await CredentialStore(
            deps.pool, deps.settings.provider_credential_encryption_key
        ).metadata(identity.project_id)
        if not any(row["provider"] == entry.provider and row["configured"] for row in metadata):
            reason = "provider_credentials_missing"
    prices = {}
    if entry.billing != "zero":
        for tier in entry.service_tiers:
            price = deps.catalogue.price(
                entry.provider, entry.model, tier, datetime.now(UTC).date()
            )
            if price is not None:
                prices[tier] = {
                    "usd_per_million": price.usd_per_million.model_dump(mode="json"),
                    "effective_from": price.effective_from.isoformat(),
                }
    capabilities: dict[str, Any] = {
        "reasoning_efforts": list(entry.reasoning_efforts),
        "temperature": entry.temperature,
        "inputs": list(entry.inputs),
        "function_tools": entry.function_tools,
        "streaming": entry.streaming,
        "structured_output": entry.structured_output,
    }
    if entry.local is not None:
        capabilities["local_contract"] = entry.local.model_dump(mode="json")
    limits = {
        "context_window_tokens": entry.context_window_tokens,
        "max_output_tokens": entry.max_output_tokens,
    }
    if entry.embedding is not None:
        capabilities = entry.embedding.model_dump(mode="json")
        limits = {
            "max_input_tokens": entry.context_window_tokens,
            "max_batch_size": entry.embedding.max_batch_size,
            "max_input_bytes": entry.embedding.max_input_bytes,
            "max_batch_tokens": entry.embedding.max_batch_tokens,
        }
    if entry.ocr is not None:
        capabilities = entry.ocr.model_dump(mode="json")
        limits = {
            name: getattr(entry.ocr, name)
            for name in (
                "max_image_bytes",
                "max_pixels",
                "max_side",
                "max_blocks",
                "max_text_bytes",
            )
        }
    return ModelSummary(
        alias=entry.alias,
        task=entry.task,
        provider=entry.provider,
        model=entry.model,
        definition_digest=entry.definition_digest,
        execution=entry.execution,
        billing=entry.billing,
        limits=limits,
        capabilities=capabilities,
        prices=prices,
        availability=Availability(configured=reason is None, reason=reason),
    )


@router.get(
    "", responses=document_problems({**AUTH_RESPONSES, 422: {"description": "request_invalid"}})
)
async def list_models(
    request: Request,
    response: Response,
    identity: Annotated[ProjectIdentity, Depends(require_project)],
    task: Task | None = None,
) -> ModelList:
    deps: CallDeps = request.app.state.call_deps
    response.headers["Cache-Control"] = "private, no-store"
    entries = sorted(deps.catalogue.models.values(), key=lambda entry: entry.alias)
    models = [
        await _summary(deps, entry, identity)
        for entry in entries
        if (task is None or entry.task == task)
        and not (entry.development_only and identity.environment == "production")
    ]
    return ModelList(models=models)


@router.get(
    "/{alias}",
    responses=document_problems(
        {
            **AUTH_RESPONSES,
            404: {"description": "model_not_found"},
            422: {"description": "request_invalid"},
        }
    ),
)
async def get_model(
    alias: str,
    request: Request,
    response: Response,
    identity: Annotated[ProjectIdentity, Depends(require_project)],
) -> ModelDetails:
    deps: CallDeps = request.app.state.call_deps
    entry = deps.catalogue.model(alias)
    if entry is None or (entry.development_only and identity.environment == "production"):
        raise GatewayError("model_not_found", status=404, title="Model not found")
    response.headers["Cache-Control"] = "private, no-store"
    summary = await _summary(deps, entry, identity)
    return ModelDetails(**summary.model_dump(), request_schema=request_schema(entry))
