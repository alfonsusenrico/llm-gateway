"""Validate supported profile defaults using the model's execution schema."""

from llm_gateway.catalogue.model import Catalogue
from llm_gateway.catalogue.schemas import setting_errors
from llm_gateway.projects.contract import ModelProfile


def profile_errors(name: str, profile: ModelProfile, catalogue: Catalogue) -> list[str]:
    entry = catalogue.model(profile.model)
    if entry is None:
        return [f"profiles.{name}.model: alias is not registered"]
    payload = {
        "provider_call_id": "profile-check",
        "operation": "profile_check",
        "model": entry.alias,
        "input": "validation",
        **profile.parameters.model_dump(exclude_none=True),
    }
    if entry.task == "ocr":
        payload.pop("input")
        payload.update(media_type="image/png", data_base64="AAAA")
    if entry.default_reasoning_effort is not None:
        payload.setdefault("reasoning_effort", entry.default_reasoning_effort)
    return [f"profiles.{name}.{error}" for error in setting_errors(entry, payload)]
