"""`GET /v1/catalogue`: the models a key may use, with limits and today's prices."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from llm_gateway.api.auth import ProjectIdentity, require_project
from llm_gateway.api.routes_projects import AUTH_RESPONSES
from llm_gateway.catalogue.model import get_catalogue

router = APIRouter(prefix="/v1", tags=["catalogue"])
_CACHE_CONTROL = "private, max-age=300"


class PriceOut(BaseModel):
    """USD per million tokens, as decimal strings."""

    input: str
    cached_input: str
    cache_write: str
    output: str
    effective_from: str
    long_context_threshold_input_tokens: int | None


class ModelOut(BaseModel):
    provider: str
    model: str
    context_window_tokens: int
    max_output_tokens: int
    reasoning_efforts: list[str]
    default_reasoning_effort: str | None
    temperature: str
    inputs: list[str]
    service_tiers: list[str]
    function_tools: bool
    prices: dict[str, PriceOut]


class CatalogueOut(BaseModel):
    models: list[ModelOut]


@router.get("/catalogue", responses=AUTH_RESPONSES)
async def catalogue(
    response: Response, identity: Annotated[ProjectIdentity, Depends(require_project)]
) -> CatalogueOut:
    """Every model this key may use; production keys never see development-only models."""
    current = get_catalogue()
    today = datetime.now(UTC).date()
    models = []
    for entry in sorted(current.models.values(), key=lambda item: item.key):
        if entry.development_only and identity.environment == "production":
            continue
        prices = {}
        for tier in entry.service_tiers:
            price = current.price(entry.provider, entry.model, tier, today)
            if price is None:
                continue
            rates = price.usd_per_million
            prices[tier] = PriceOut(
                input=str(rates.input),
                cached_input=str(rates.cached_input),
                cache_write=str(rates.cache_write),
                output=str(rates.output),
                effective_from=price.effective_from.isoformat(),
                long_context_threshold_input_tokens=(
                    price.long_context.threshold_input_tokens if price.long_context else None
                ),
            )
        models.append(
            ModelOut(
                provider=entry.provider,
                model=entry.model,
                context_window_tokens=entry.context_window_tokens,
                max_output_tokens=entry.max_output_tokens,
                reasoning_efforts=list(entry.reasoning_efforts),
                default_reasoning_effort=entry.default_reasoning_effort,
                temperature=entry.temperature,
                inputs=list(entry.inputs),
                service_tiers=list(entry.service_tiers),
                function_tools=entry.function_tools,
                prices=prices,
            )
        )
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return CatalogueOut(models=models)
