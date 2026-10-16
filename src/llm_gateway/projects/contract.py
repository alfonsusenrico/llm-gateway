"""Current alias-backed contracts and preserved historical contract readers."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

Environment = Literal["production", "development"]
ENVIRONMENTS: tuple[Environment, ...] = ("production", "development")
PROJECT_ID_PATTERN = r"^[a-z][a-z0-9_]{2,39}$"
PROFILE_NAME_PATTERN = r"^[a-z][a-z0-9_-]{1,39}$"
MAX_PROFILES = 20


class ContractError(ValueError):
    """A contract that must not be stored, with readable field-level reasons."""


def _require_decimal_string(value: Any) -> Any:
    # Money arrives as a decimal string so no float rounding can creep in.
    if isinstance(value, Decimal):
        return value
    if not isinstance(value, str):
        raise ValueError('must be a decimal string such as "1.50"')
    return value


UsdAmount = Annotated[
    Decimal,
    BeforeValidator(_require_decimal_string),
    Field(ge=0, le=1000, decimal_places=6),
]
ProfileName = Annotated[str, StringConstraints(pattern=PROFILE_NAME_PATTERN)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EnvironmentRequest(_Strict):
    requests_per_minute: int = Field(ge=1, le=10_000)
    concurrency: int = Field(ge=1, le=100)
    monthly_budget_usd: UsdAmount
    per_call_cost_ceiling_usd: UsdAmount

    @model_validator(mode="after")
    def _ceiling_within_budget(self) -> EnvironmentRequest:
        if self.per_call_cost_ceiling_usd > self.monthly_budget_usd:
            raise ValueError("per_call_cost_ceiling_usd must not exceed monthly_budget_usd")
        return self


class ContractRequest(_Strict):
    production: EnvironmentRequest
    development: EnvironmentRequest


class LegacyModelProfile(_Strict):
    provider: Literal["openai", "stub"]
    model: str = Field(min_length=1, max_length=160)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=32)
    max_output_tokens: int | None = Field(default=None, ge=1, le=128_000)
    temperature: float | None = Field(default=None, ge=0, le=2)


class ContractV1(_Strict):
    contract_version: Literal[1]
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=80)
    status: Literal["active", "disabled"]
    request: ContractRequest
    profiles: dict[ProfileName, LegacyModelProfile] = Field(
        default_factory=dict, max_length=MAX_PROFILES
    )

    def to_json(self) -> dict[str, Any]:
        """The stored and exported form: money as decimal strings, unset options omitted."""
        return self.model_dump(mode="json", exclude_none=True)


class ProfileParameters(_Strict):
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=32)
    max_output_tokens: int | None = Field(default=None, ge=1, le=128_000)
    temperature: float | None = Field(default=None, ge=0, le=2)


class ModelProfile(_Strict):
    model: str = Field(pattern=r"^[a-z][a-z0-9._-]{1,159}$")
    parameters: ProfileParameters = ProfileParameters()


class ContractV2(_Strict):
    contract_version: Literal[2]
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=80)
    status: Literal["active", "disabled"]
    request: ContractRequest
    profiles: dict[ProfileName, ModelProfile] = Field(default_factory=dict, max_length=MAX_PROFILES)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def parse_historical_contract(data: object) -> ContractV1 | ContractV2:
    if isinstance(data, dict) and data.get("contract_version") == 1:
        try:
            return ContractV1.model_validate(data)
        except ValidationError as exc:
            raise ContractError(describe_validation_error(exc)) from None
    return parse_contract(data)


def parse_contract(data: object) -> ContractV2:
    """Validate a contract or raise `ContractError` naming each failing field."""
    try:
        return ContractV2.model_validate(data)
    except ValidationError as exc:
        raise ContractError(describe_validation_error(exc)) from None


def describe_validation_error(exc: ValidationError) -> str:
    reasons = []
    for error in exc.errors():
        path = ".".join(str(part) for part in error["loc"]) or "contract"
        reasons.append(f"{path}: {error['msg']}")
    return "; ".join(reasons)
