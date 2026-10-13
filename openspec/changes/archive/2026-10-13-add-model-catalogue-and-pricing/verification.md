# Verification: add-model-catalogue-and-pricing

Date: 2026-10-13
Branch: `feat/model-catalogue-and-pricing`. The tested revision is the branch
head in its pull request.
Data: synthetic projects only, with no provider calls.

## Price sources

The gpt-6-luna standard and flex rates and the long-context rule come from
https://developers.openai.com/api/docs/pricing and the gpt-6-luna model page,
read on 2026-10-13:

- **Standard** (USD per 1M tokens): 0.10 input, 0.01 cached input, 0.125
  cache write, 0.50 output.
- **Flex:** half of each standard rate.
- **Long context:** above 272K input tokens, input-side rates are doubled and
  the output rate is multiplied by 1.5, for the whole request.

## Automated checks

`./scripts/check` with `REQUIRE_DB=1`:

- lock, format, and lint: passed.
- `pyright`: 0 errors.
- `pytest`: 482 passed.
- compose config: passed.
- `openspec validate --all`: 8 passed.

### `model-catalogue` coverage

| Requirement | Tests |
|---|---|
| Catalogue lists models with their limits | `test_packaged_catalogue_loads_with_both_models`, `test_unknown_field_stops_loading`, `test_default_effort_must_be_allowed` |
| Every model has a current standard price; no overlap | `test_model_without_a_standard_price_stops_loading`, `test_two_prices_starting_the_same_day_stop_loading` |
| Cost from effective-dated prices | `test_cost_of_cached_and_uncached_tokens` (358 micro-USD, the spec example), `test_cache_write_tokens_use_their_own_rate`, `test_price_change_takes_effect_on_its_date`, `test_flex_costs_half`, `test_stub_is_free` |
| Long-context rate on the whole request | `test_long_context_doubles_input_and_raises_output_for_the_whole_request`, `test_at_the_threshold_the_standard_rate_applies` |
| Unknown prices never free | `test_unknown_price_is_never_zero` |
| Profiles checked when saved | `test_profile_checks` (7 cases), `test_profiles_are_checked_against_the_catalogue` (CLI) |
| Catalogue changes never quarantine | `test_a_model_removed_from_the_catalogue_never_blocks_other_edits` |
| Projects read the catalogue | `test_catalogue_hides_development_models_from_production_keys` |

## Local stack (Docker, 2026-10-13)

Keys stayed in shell variables and were never printed.

- **Build and start-up:** the image built and the API started, which
  includes the start-up catalogue check.
- **Catalogue by key**, from a throwaway project `catalogue_check`:
  - the production key listed only `openai/gpt-6-luna`, with standard
    (0.10 / 0.01 / 0.50) and flex (0.05 / 0.005 / 0.25) prices;
  - the development key also listed `stub/stub-echo`, with zero prices;
  - both responses carried `Cache-Control: private, max-age=300`.
- **Profile checks through the CLI:**
  - gpt-6-luna with effort `low` and temperature 0.7 was refused with
    "temperature only with reasoning_effort none";
  - effort `none` with temperature 0.7 was accepted.
- **Cleanup:** `catalogue_check` was then disabled.

## Not verified

- Live provider pricing against a real invoice. Costs are list-price
  estimates, by design.
