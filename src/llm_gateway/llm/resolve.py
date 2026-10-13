"""Resolving a request to one catalogue model with validated settings."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from llm_gateway.catalogue.model import Catalogue, ModelEntry
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.llm.request import ResponseRequest
from llm_gateway.projects.contract import ContractV1

_MAX_CACHE_KEY_CHARS = 64


@dataclass(frozen=True, slots=True)
class ResolvedCall:
    entry: ModelEntry
    reasoning_effort: str | None
    temperature: float | None
    max_output_tokens: int
    prompt_cache_key: str

    @property
    def provider(self) -> str:
        return self.entry.provider

    @property
    def model(self) -> str:
        return self.entry.model


def default_cache_key(project_id: str, operation: str) -> str:
    key = f"{project_id}:{operation}"
    if len(key) <= _MAX_CACHE_KEY_CHARS:
        return key
    return "h:" + hashlib.sha256(key.encode()).hexdigest()[: _MAX_CACHE_KEY_CHARS - 2]


def resolve(
    request: ResponseRequest,
    *,
    project_id: str,
    environment: str,
    contract: ContractV1,
    catalogue: Catalogue,
    default_max_output_tokens: int,
) -> ResolvedCall:
    """Pick the model and settings, or refuse before any admission or cost."""
    profile = None
    if request.profile is not None:
        profile = contract.profiles.get(request.profile)
        if profile is None:
            raise CallRejectedError("profile_not_found", detail=f"No profile {request.profile}.")
        provider, model = profile.provider, profile.model
    elif request.model is not None:
        provider, model = request.model.provider, request.model.model
    else:  # the request validator requires one of the two
        raise CallRejectedError("request_invalid", detail="Give a profile or a model.")
    entry = catalogue.model(provider, model)
    if entry is None:
        raise CallRejectedError(
            "model_not_in_catalogue", detail=f"{provider}/{model} is not in the catalogue."
        )
    if entry.development_only and environment == "production":
        raise CallRejectedError("stub_not_allowed")
    if request.stub is not None and entry.provider != "stub":
        raise CallRejectedError("request_invalid", detail="stub applies only to the stub model.")
    effort = request.reasoning_effort or (profile.reasoning_effort if profile else None)
    temperature = (
        request.temperature
        if request.temperature is not None
        else (profile.temperature if profile else None)
    )
    max_output = (
        request.max_output_tokens
        or (profile.max_output_tokens if profile else None)
        or default_max_output_tokens
    )
    problems = _setting_problems(entry, effort, temperature, max_output)
    if problems:
        raise CallRejectedError("invalid_model_settings", detail="; ".join(problems))
    unsupported = sorted(request.input_types() - set(entry.inputs))
    if unsupported:
        raise CallRejectedError(
            "input_not_supported",
            detail=f"{entry.provider}/{entry.model} does not accept {', '.join(unsupported)}.",
        )
    if request.tools and not entry.function_tools:
        raise CallRejectedError(
            "input_not_supported",
            detail=f"{entry.provider}/{entry.model} does not accept function tools.",
        )
    for continuation in request.continuations():
        if (continuation.provider, continuation.model) != entry.key:
            raise CallRejectedError(
                "continuation_mismatch",
                detail=(
                    f"The continuation is from {continuation.provider}/{continuation.model}, "
                    f"not {entry.provider}/{entry.model}."
                ),
            )
    return ResolvedCall(
        entry=entry,
        reasoning_effort=effort,
        temperature=temperature,
        max_output_tokens=max_output,
        prompt_cache_key=request.prompt_cache_key
        or default_cache_key(project_id, request.operation),
    )


def _setting_problems(
    entry: ModelEntry, effort: str | None, temperature: float | None, max_output: int
) -> list[str]:
    problems = []
    if effort is not None and effort not in entry.reasoning_efforts:
        problems.append(f"reasoning_effort {effort} is not accepted by this model")
    effective_effort = effort or entry.default_reasoning_effort
    if temperature is not None:
        if entry.temperature == "never":
            problems.append("this model does not accept temperature")
        elif entry.temperature == "only_with_effort_none" and effective_effort != "none":
            problems.append("this model accepts temperature only with reasoning_effort none")
    if max_output > entry.max_output_tokens:
        problems.append(f"max_output_tokens may be at most {entry.max_output_tokens}")
    return problems
