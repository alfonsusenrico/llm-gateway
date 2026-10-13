# Verification: add-request-grant-allocation

Date: 2026-10-13
Branch: `feat/request-grant-allocation`. The tested revision is the branch
head in its pull request.
Data: synthetic projects only (`alloc_alpha`, `alloc_beta`, and the fixtures),
with no provider calls.

## Automated checks

`./scripts/check` with `REQUIRE_DB=1` against local Postgres 16:

- lock, format, and lint: passed.
- `pyright`: 0 errors.
- `pytest`: 459 passed, 0 skipped. 300 of them are seeded property runs of
  the allocation function.
- `docker compose config -q`: passed.
- `openspec validate --all`: 4 passed.

### `resource-allocation` coverage

| Requirement | Tests |
|---|---|
| Every active environment has a grant | `test_registration_gets_grants_for_both_environments`, `test_disabling_removes_grants_and_returns_the_share`, `test_register_recomputes_and_grants_show_lists_both_environments` |
| Never above requests or capacity | `test_allocation_properties` (300 seeds), `test_undersubscribed_pool_grants_every_request`, `test_global_budget_cap_is_respected`, `test_global_budget_cap_holds_across_projects` |
| Shortage shared by minimum and weight | `test_weighted_sharing` (7 and 3), `test_satisfied_project_releases_its_share` (2 and 6), `test_minimums_exceeding_capacity_are_split_by_weight_with_a_reason`, `test_weight_change_shifts_the_shares` |
| Ceiling follows the granted budget | `test_ceiling_follows_a_reduced_budget`, plus the property assertion |
| Deterministic | the order-independence assertion in `test_allocation_properties` |
| State and reasons | `test_only_the_reduced_dimension_has_a_reason`, plus the property assertion that `approved` means no reasons |
| Recomputed after changes | `test_unchanged_inputs_write_nothing_and_alert_nothing`, `test_lower_capacity_settings_recompute_on_the_next_run`, `test_worker_recomputes_grants_on_start`, `test_set_weight_validates_and_recomputes` |
| Projects read their own grant | `test_approved_grant_shows_requested_and_granted`, `test_reduced_grant_lists_reasons_side_by_side`, `test_missing_grant_is_pending_with_retry_after`, `test_a_project_sees_only_its_own_grant`, `test_allocation_requires_a_key` |
| Owner sees grants and is alerted | `test_reduction_and_restoration_alert_the_owner`, `test_grants_and_weight_through_the_menu` |

## Local stack (Docker, 2026-10-13, default capacity: 120 rpm, 8 concurrency, USD 5.00)

Keys were captured into shell variables and never printed.

- **Migration:** `migrate` applied `0003_grants`. The worker's start-up
  recompute logged "Grants recomputed: 0 pairs", because the only earlier
  project, `smoke_test`, is disabled.
- **Undersubscribed:** registering `alloc_alpha` gave "2 grants, 0 reduced".
- **Oversubscribed:** registering `alloc_beta` (each production request is 4
  concurrency) gave "4 grants, 2 reduced".
  - `grants show`: both production grants at 2 of 4 concurrency, reason
    `concurrency:capacity_shared`.
  - Pool use: concurrency 8/8, requests per minute 80/120, monthly budget
    2.50/5.00 USD.
  - `GET /v1/allocation` with alpha's production key: `state: reduced`,
    requested concurrency 4, granted 2, reason
    `{concurrency, capacity_shared}`.
- **Weight:** `set-weight alloc_alpha 3` moved the production grants to
  alpha 3/4 and beta 1/4.
- **Disable:** `disable alloc_beta` gave "2 grants, 0 reduced".
  - Alpha's production grant returned to 4/4, `approved`, and
    `GET /v1/allocation` confirmed it.
  - Beta's key now receives `project_disabled`.
- **Alerts:** all delivered to Telegram.
  - `grant_reduced` for alpha.
  - `grant_reduced` for beta, with 2 occurrences: its second reduction
    (2 to 1, after the weight change) fell inside the 15-minute
    deduplication window, so it was counted, not re-sent. The delivered
    message shows the first reduction.
  - `grant_restored` for alpha.
- **Logs:** 0 lines with key material.

## Not verified

- Grant alerts show the reduction that opened the event. A further reduction
  inside the deduplication window is only counted. This is notifier
  behaviour, unchanged by this change; the owner sees current values with
  `grants show`.
