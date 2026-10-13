# Verification: add-call-admission-and-ledger

Date: 2026-10-13
Branch: `feat/call-admission-and-ledger`. The tested revision is the branch
head in its pull request.
Data: synthetic projects and calls only. No provider calls; the call
endpoint arrives in `add-openai-direct-calls`.

## Automated checks

`./scripts/check` with `REQUIRE_DB=1`:

- lock, format, and lint: passed.
- `pyright`: 0 errors.
- `pytest`: 503 passed.
- compose config: passed.
- `openspec validate --all`: 8 passed.

### `call-admission` coverage

| Requirement | Tests |
|---|---|
| Admitted only within the grant | `test_admission_records_the_call_and_reports_the_allowance`, `test_no_grant_is_allocation_pending`, `test_full_concurrency_is_refused`, `test_full_rate_window_is_refused_until_the_oldest_ages_out` |
| Exact across processes | `test_simultaneous_admissions_never_exceed_concurrency`: 10 concurrent admissions on separate pooled connections, concurrency 3, exactly 3 admitted and 7 `concurrency_exhausted` |
| Worst-case reservation and ceiling | `test_reservation_is_the_worst_case_at_list_price` (520 micro-USD), `test_reservation_over_the_ceiling_is_refused_and_not_retryable` |
| Budgets and the global cap | `test_spent_grant_budget_is_refused_until_next_month`, `test_global_cap_is_refused_and_alerts` (a critical alert) |
| Idempotent call IDs | `test_same_call_id_rules` (conflict, in progress, finished), `test_retryable_failure_is_admitted_again_with_an_audit_trail` |
| Abandoned calls become unknown | `test_abandoned_call_becomes_unknown_and_frees_its_slot` (the slot is freed; a resubmission returns the unknown outcome), `test_worker_recovers_abandoned_calls` |
| Allowance reported | `test_admission_records_the_call_and_reports_the_allowance` (30 rpm limit with 29 remaining; concurrency 4 with 3 remaining). The headers themselves are written by the call endpoint (next change). |

### `usage-ledger` coverage

| Requirement | Tests |
|---|---|
| One record per call, no content | the `calls` schema (no content columns); `test_admission_records_the_call_and_reports_the_allowance` |
| Settlement by outcome | `test_settlement_charges_by_outcome` (358 completed; 0 failed; 520 reservation unknown), `test_unpriced_usage_counts_the_reservation_and_alerts` |
| Audited, priced at the time | `test_price_snapshot_is_kept` (`standard`, 2026-10-13), `test_audit_events_are_append_only` (the trigger raises), `test_illegal_transition_is_rejected`, `test_retryable_failure_is_admitted_again_with_an_audit_trail` |
| Exact month-to-date spend | `test_last_months_spend_does_not_count` |
| Thresholds once per month | `test_budget_thresholds_alert_once_per_month` (52% gives `info`, 82% gives `warning`, a rerun as after a restart sends 0) |

## Findings fixed during implementation

- **Tier names:** OpenAI reports its standard tier as `default`, but the
  price schedule names it `standard`. Without a mapping every normal call
  would have been unpriced and charged its full reservation.
  `catalogue_tier()` now maps provider names, and an unmapped tier stays
  unpriced. Covered by `test_provider_tier_names_map_to_catalogue_tiers`
  and the settlement tests. The design records it.
- **`price_unavailable` (503) admission refusal:** for a model and tier with
  no price in effect. Recorded in the design, decision 2.

## Local stack (Docker, 2026-10-13)

- `migrate` applied `0004_calls`.
- The `call_events_no_change` trigger exists.
- The worker runs its recovery and budget pass without errors.

## Not verified

- Allowance headers on real responses, and end-to-end admission through the
  API: both wait for `add-openai-direct-calls`, whose tests cover them.
- Throughput under the global admission lock: not measured; to revisit with
  load in phase 4.
