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
    base_models = json.loads((DATA_DIR / "models.json").read_text())
    base_prices = json.loads((DATA_DIR / "prices.json").read_text())
    models_path, prices_path = tmp_path / "models.json", tmp_path / "prices.json"
    models_path.write_text(json.dumps(models if models is not None else base_models))
    prices_path.write_text(json.dumps(prices if prices is not None else base_prices))
    return models_path, prices_path


def _base(name: str) -> dict[str, Any]:
    return json.loads((DATA_DIR / name).read_text())


def test_packaged_catalogue_loads_with_both_models() -> None:
    catalogue = get_catalogue()

    assert sorted(catalogue.models) == [("openai", "gpt-6-luna"), ("stub", "stub-echo")]
    assert catalogue.models[("stub", "stub-echo")].development_only is True


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

    with pytest.raises(CatalogueError, match="default_reasoning_effort"):
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
    ("profile", "expected"),
    [
        (ModelProfile(provider="openai", model="gpt-6-luna", reasoning_effort="low"), []),
        (
            ModelProfile(
                provider="openai", model="gpt-6-luna", reasoning_effort="none", temperature=0.7
            ),
            [],
        ),
        (
            ModelProfile(
                provider="openai", model="gpt-6-luna", reasoning_effort="low", temperature=0.7
            ),
            [
                "profiles.chat.temperature: this model accepts temperature only with reasoning_effort none"
            ],
        ),
        (
            ModelProfile(provider="openai", model="gpt-6-luna", temperature=0.7),
            [
                "profiles.chat.temperature: this model accepts temperature only with reasoning_effort none"
            ],
        ),
        (
            ModelProfile(provider="openai", model="gpt-9"),
            ["profiles.chat.model: openai/gpt-9 is not in the catalogue"],
        ),
        (
            ModelProfile(provider="openai", model="gpt-6-luna", reasoning_effort="turbo"),
            [
                "profiles.chat.reasoning_effort: this model accepts none, low, medium, high, xhigh, max"
            ],
        ),
        (
            # Built without validation: the contract's own bound would reject it first.
            ModelProfile.model_construct(
                provider="openai",
                model="gpt-6-luna",
                reasoning_effort=None,
                max_output_tokens=128_001,
                temperature=None,
            ),
            ["profiles.chat.max_output_tokens: this model allows at most 128000"],
        ),
    ],
)
def test_profile_checks(profile: ModelProfile, expected: list[str]) -> None:
    assert profile_errors("chat", profile, get_catalogue()) == expected
