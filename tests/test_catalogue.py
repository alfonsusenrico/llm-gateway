from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from llm_gateway.catalogue.model import DATA_DIR, CatalogueError, get_catalogue, load_catalogue
from llm_gateway.catalogue.pricing import TokenUsage, cost_micro_usd
from llm_gateway.catalogue.profiles import profile_errors
from llm_gateway.projects.contract import ModelProfile

TODAY = date(2026, 10, 13)


def _files(tmp_path: Path, models: Any = None, prices: Any = None) -> tuple[Path, Path]:
    base_models = _base("models.json")
    base_prices = _base("prices.json")
    models_path, prices_path = tmp_path / "models", tmp_path / "prices.json"
    models_path.mkdir(exist_ok=True)
    for index, entry in enumerate((models if models is not None else base_models)["models"]):
        (models_path / f"{index}.json").write_text(json.dumps(entry))
    prices_path.write_text(json.dumps(prices if prices is not None else base_prices))
    return models_path, prices_path


def _base(name: str) -> dict[str, Any]:
    if name == "models.json":
        return {
            "models": [
                json.loads(path.read_text())
                for path in sorted((DATA_DIR / "models").glob("*.json"))
            ]
        }
    return json.loads((DATA_DIR / name).read_text())


def test_packaged_catalogue_loads_with_both_models() -> None:
    catalogue = get_catalogue()

    assert sorted(catalogue.models) == ["openai-gpt-6-luna", "stub-echo"]
    assert catalogue.models["stub-echo"].development_only is True


def test_unknown_field_stops_loading(tmp_path: Path) -> None:
    models = _base("models.json")
    models["models"][0]["surprise"] = True

    with pytest.raises(CatalogueError, match="surprise"):
        load_catalogue(*_files(tmp_path, models=models))


def test_model_without_a_standard_price_stops_loading(tmp_path: Path) -> None:
    prices = _base("prices.json")
    prices["prices"] = [
        p for p in prices["prices"] if p["model"] != "gpt-6-luna" or p["service_tier"] != "standard"
    ]

    with pytest.raises(CatalogueError, match="gpt-6-luna has no standard price"):
        load_catalogue(*_files(tmp_path, prices=prices))


def test_two_prices_starting_the_same_day_stop_loading(tmp_path: Path) -> None:
    prices = _base("prices.json")
    prices["prices"].append(dict(prices["prices"][0]))

    with pytest.raises(CatalogueError, match="start on 2026-10-13"):
        load_catalogue(*_files(tmp_path, prices=prices))


def test_default_effort_must_be_allowed(tmp_path: Path) -> None:
    models = _base("models.json")
    models["models"][0]["default_reasoning_effort"] = "turbo"

    with pytest.raises(CatalogueError, match="definition"):
        load_catalogue(*_files(tmp_path, models=models))


def test_price_change_takes_effect_on_its_date(tmp_path: Path) -> None:
    prices = _base("prices.json")
    newer = json.loads(json.dumps(prices["prices"][0]))
    newer["effective_from"] = "2026-11-01"
    newer["usd_per_million"]["input"] = "0.08"
    prices["prices"].append(newer)
    catalogue = load_catalogue(*_files(tmp_path, prices=prices))

    before = catalogue.price("openai", "gpt-6-luna", "standard", date(2026, 10, 31))
    after = catalogue.price("openai", "gpt-6-luna", "standard", date(2026, 11, 1))

    assert before is not None and str(before.usd_per_million.input) == "0.10"
    assert after is not None and str(after.usd_per_million.input) == "0.08"
    assert catalogue.price("openai", "gpt-6-luna", "standard", date(2026, 10, 12)) is None


def _luna(tier: str = "standard"):
    return get_catalogue().price("openai", "gpt-6-luna", tier, TODAY)


def test_cost_of_cached_and_uncached_tokens() -> None:
    usage = TokenUsage(input_tokens=2000, output_tokens=500, cached_input_tokens=1024)

    assert cost_micro_usd(usage, _luna()) == 358  # 97.6 + 10.24 + 250, rounded up


def test_cache_write_tokens_use_their_own_rate() -> None:
    usage = TokenUsage(input_tokens=2000, output_tokens=0, cache_write_tokens=2000)

    assert cost_micro_usd(usage, _luna()) == 250  # 2000 x 0.125


def test_long_context_doubles_input_and_raises_output_for_the_whole_request() -> None:
    usage = TokenUsage(input_tokens=300_000, output_tokens=1_000)

    # 300000 x 0.10 x 2 + 1000 x 0.50 x 1.5
    assert cost_micro_usd(usage, _luna()) == 60_750


def test_at_the_threshold_the_standard_rate_applies() -> None:
    usage = TokenUsage(input_tokens=272_000, output_tokens=0)

    assert cost_micro_usd(usage, _luna()) == 27_200


def test_flex_costs_half() -> None:
    usage = TokenUsage(input_tokens=10_000, output_tokens=1_000)

    assert cost_micro_usd(usage, _luna("flex")) == 750
    assert cost_micro_usd(usage, _luna()) == 1_500


def test_unknown_price_is_never_zero() -> None:
    usage = TokenUsage(input_tokens=10, output_tokens=10)

    assert cost_micro_usd(usage, None) is None


def test_stub_is_free() -> None:
    price = get_catalogue().price("stub", "stub-echo", "standard", TODAY)

    assert cost_micro_usd(TokenUsage(10_000, 10_000), price) == 0


@pytest.mark.parametrize(
    ("parameters", "valid"),
    [
        ({"reasoning_effort": "low"}, True),
        ({"reasoning_effort": "none", "temperature": 0.7}, True),
        ({"reasoning_effort": "low", "temperature": 0.7}, False),
        ({"temperature": 0.7}, False),
        ({"reasoning_effort": "turbo"}, False),
    ],
)
def test_profile_checks(parameters: dict[str, Any], valid: bool) -> None:
    profile = ModelProfile.model_validate({"model": "openai-gpt-6-luna", "parameters": parameters})
    errors = profile_errors("chat", profile, get_catalogue())
    assert (not errors) == valid
    assert all(error.startswith("profiles.chat.") for error in errors)


def test_unknown_profile_alias_is_refused() -> None:
    assert profile_errors("chat", ModelProfile(model="openai-gpt-9"), get_catalogue())


def test_provider_tier_names_map_to_catalogue_tiers() -> None:
    from llm_gateway.catalogue.model import catalogue_tier

    assert catalogue_tier("openai", "default") == "standard"
    assert catalogue_tier("openai", "flex") == "flex"
    assert catalogue_tier("openai", "priority") == "priority"  # unpriced, never free
    assert catalogue_tier("stub", "standard") == "standard"
