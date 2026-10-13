"""Checking contract profiles against the catalogue when a contract is saved."""

from __future__ import annotations

from llm_gateway.catalogue.model import Catalogue
from llm_gateway.projects.contract import ModelProfile


def profile_errors(name: str, profile: ModelProfile, catalogue: Catalogue) -> list[str]:
    """Reasons this profile cannot be used with the catalogue; empty when it can."""
    prefix = f"profiles.{name}"
    entry = catalogue.model(profile.provider, profile.model)
    if entry is None:
        return [f"{prefix}.model: {profile.provider}/{profile.model} is not in the catalogue"]
    errors = []
    effort = profile.reasoning_effort or entry.default_reasoning_effort
    if (
        profile.reasoning_effort is not None
        and profile.reasoning_effort not in entry.reasoning_efforts
    ):
        allowed = ", ".join(entry.reasoning_efforts) or "none accepted"
        errors.append(f"{prefix}.reasoning_effort: this model accepts {allowed}")
    if profile.temperature is not None:
        if entry.temperature == "never":
            errors.append(f"{prefix}.temperature: this model does not accept temperature")
        elif entry.temperature == "only_with_effort_none" and effort != "none":
            errors.append(
                f"{prefix}.temperature: this model accepts temperature only with "
                "reasoning_effort none"
            )
    if (
        profile.max_output_tokens is not None
        and profile.max_output_tokens > entry.max_output_tokens
    ):
        errors.append(
            f"{prefix}.max_output_tokens: this model allows at most {entry.max_output_tokens}"
        )
    return errors
