from __future__ import annotations

import copy
from typing import Any

import pytest

from llm_gateway.projects.contract import ContractError, parse_contract


def contract_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "contract_version": 1,
        "project_id": "personal_agent",
        "display_name": "Personal Agent",
        "status": "active",
        "request": {
            "production": {
                "requests_per_minute": 30,
                "concurrency": 4,
                "monthly_budget_usd": "3.00",
                "per_call_cost_ceiling_usd": "0.05",
            },
            "development": {
                "requests_per_minute": 10,
                "concurrency": 2,
                "monthly_budget_usd": "0.50",
                "per_call_cost_ceiling_usd": "0.02",
            },
        },
        "profiles": {
            "chat": {
                "provider": "openai",
                "model": "gpt-6-luna",
                "reasoning_effort": "low",
                "max_output_tokens": 1200,
            }
        },
    }
    data.update(overrides)
    return data


def test_valid_contract_round_trips_with_money_as_strings() -> None:
    contract = parse_contract(contract_data())

    assert contract.to_json() == contract_data()


def test_missing_environment_request_names_the_field() -> None:
    data = contract_data()
    del data["request"]["development"]

    with pytest.raises(ContractError, match=r"request\.development: Field required"):
        parse_contract(data)


def test_ceiling_above_budget_is_rejected() -> None:
    data = contract_data()
    data["request"]["production"].update(
        monthly_budget_usd="1.00", per_call_cost_ceiling_usd="2.00"
    )

    with pytest.raises(ContractError, match="must not exceed monthly_budget_usd"):
        parse_contract(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("concurrency", 0),
        ("concurrency", 101),
        ("requests_per_minute", 10_001),
        ("monthly_budget_usd", "1000.01"),
        ("monthly_budget_usd", "1.1234567"),
        ("monthly_budget_usd", 1.5),
    ],
)
def test_out_of_range_request_values_are_rejected(field: str, value: object) -> None:
    data = contract_data()
    data["request"]["production"][field] = value

    with pytest.raises(ContractError, match=f"request.production.{field}"):
        parse_contract(data)


def test_unknown_provider_is_rejected() -> None:
    data = contract_data()
    data["profiles"]["chat"]["provider"] = "acme"

    with pytest.raises(ContractError, match=r"profiles\.chat\.provider"):
        parse_contract(data)


def test_misspelled_field_is_rejected_with_its_path() -> None:
    data = contract_data()
    data["request"]["production"]["request_per_minute"] = 30

    with pytest.raises(ContractError, match=r"request\.production\.request_per_minute"):
        parse_contract(data)


def test_invalid_profile_name_and_too_many_profiles_are_rejected() -> None:
    profile = {"provider": "stub", "model": "fixed"}
    with pytest.raises(ContractError, match="profiles"):
        parse_contract(contract_data(profiles={"Bad Name": profile}))
    too_many = {f"p{index:02d}": copy.deepcopy(profile) for index in range(21)}
    with pytest.raises(ContractError, match="profiles"):
        parse_contract(contract_data(profiles=too_many))


def test_project_id_pattern_is_enforced() -> None:
    with pytest.raises(ContractError, match="project_id"):
        parse_contract(contract_data(project_id="Personal-Agent"))
