"""Resolving a request to one catalogue model with validated settings."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from llm_gateway.catalogue.model import Catalogue, ModelEntry
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.llm.request import ResponseRequest
from llm_gateway.projects.contract import ContractV2

_MAX_CACHE_KEY_CHARS = 64


@dataclass(frozen=True, slots=True)
class ResolvedCall:
    entry: ModelEntry
    reasoning_effort: str | None
    temperature: float | None
    max_output_tokens: int
    prompt_cache_key: str

    @property
    def settings_digest(self) -> str:
        value = json.dumps(
            {
                "reasoning_effort": self.reasoning_effort,
                "temperature": self.temperature,
                "max_output_tokens": self.max_output_tokens,
            },
            sort_keys=True,
        )
        return hashlib.sha256(value.encode()).hexdigest()

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
    contract: ContractV2,
    catalogue: Catalogue,
    default_max_output_tokens: int,
) -> ResolvedCall:
    """Pick the model and settings, or refuse before any admission or cost."""
    profile = None
    if request.profile is not None:
        profile = contract.profiles.get(request.profile)
        if profile is None:
            raise CallRejectedError("profile_not_found")
        alias = profile.model
    else:
        alias = request.model
    entry = catalogue.model(alias or "")
    if entry is None:
        raise CallRejectedError("model_not_in_catalogue")
    if entry.task != "llm":
        raise CallRejectedError("model_task_mismatch")
    if entry.development_only and environment == "production":
        raise CallRejectedError("stub_not_allowed")
    from llm_gateway.catalogue.schemas import setting_errors

    supplied = request.model_dump(mode="json", by_alias=True, exclude_unset=True)
    supplied.pop("profile", None)
    supplied["model"] = entry.alias
    inherited = profile.parameters.model_dump(exclude_none=True) if profile else {}
    payload = {**inherited, **supplied}
    effort = payload.get("reasoning_effort", entry.default_reasoning_effort)
    temperature = payload.get(
        "temperature", entry.local.default_temperature if entry.local is not None else None
    )
    max_output = payload.get(
        "max_output_tokens", min(default_max_output_tokens, entry.max_output_tokens)
    )
    effective = dict(payload, max_output_tokens=max_output)
    if effort is not None:
        effective["reasoning_effort"] = effort
    if request.stub is not None and entry.provider != "stub":
        raise CallRejectedError("request_invalid")
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
    problems = setting_errors(entry, effective)
    if problems:
        raise CallRejectedError("invalid_model_settings", detail="; ".join(problems))
    if entry.local is not None:
        from llm_gateway.llm.local_rules import validate_local_request

        validate_local_request(entry, request, max_output)
    for continuation in request.continuations():
        if (
            continuation.provider,
            continuation.model,
            continuation.model_alias,
            continuation.definition_digest,
        ) != (*entry.key, entry.alias, entry.definition_digest):
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
