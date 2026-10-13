# Verification: add-openai-direct-calls

Date: 2026-10-13
Branch: `feat/openai-direct-calls`. The tested revision is the branch head in
its pull request.
Data:

- Automated tests use synthetic prompts, the stub, and fake adapters.
- The live run, authorised by the owner when approving PR #4, used fixed test
  prompts on gpt-6-luna under the throwaway project `live_check`, now
  disabled.

## Automated checks

`./scripts/check` with `REQUIRE_DB=1`:

- lock, format, and lint: passed.
- `pyright`: 0 errors.
- `pytest`: 564 passed.
- compose config: passed.
- `openspec validate --all`: 9 passed.

### `llm-calls` coverage

| Requirement | Tests |
|---|---|
| Call ID, operation, and model or profile | `test_invalid_requests_are_rejected` (8 cases), `test_resolution_refusals` (`profile_not_found`) |
| Checked against the catalogue | `test_resolution_refusals`, `test_input_type_the_model_does_not_accept`, `test_temperature_with_effort_none_is_accepted`, `test_oversized_and_invalid_bodies_are_refused` (413, then 422 without echoing values) |
| Answer, usage, cost | `test_stub_call_returns_answer_usage_cost_and_allowance`, `test_json_schema_reply_is_parsed` (`output_json` and `invalid_json`), `test_incomplete_scenario_completes_with_its_reason`, `test_success_extracts_text_usage_tier_and_ids`, `test_refusal_and_incomplete_are_reported` |
| Provider retention disabled | `test_request_body_and_headers` (`store: false`, `X-Client-Request-Id`, input mapping, JSON schema, no temperature with reasoning) |
| Failures classified by safe next step | `test_status_errors_are_classified` (10 rows), `test_connect_error_means_not_sent`, `test_read_timeout_means_execution_unknown`, `test_failed_status_is_provider_failed_with_usage`, `test_unparseable_success_body_is_execution_unknown`, `test_stub_scenarios_map_to_their_outcomes`, `test_retryable_failure_is_executed_again_on_resubmission`, `test_unknown_execution_is_never_run_again`, `test_account_problems_alert_the_owner` |
| Replay for 24 hours | `test_resubmitted_call_is_replayed_without_a_second_charge` (1 provider call, 1 charge), `test_expired_result_is_gone` (410), `test_different_request_with_the_same_id_conflicts` |
| Encrypted results, cleanup | `test_stored_results_are_encrypted`, `test_unreadable_result_counts_as_expired`, `test_expired_result_is_gone` (worker cleanup function deletes 1) |
| Stub for development keys | `test_production_key_cannot_use_the_stub`, `test_stub_scenarios_map_to_their_outcomes` |
| No content in logs | `test_call_logs_hold_no_content`, `test_provider_messages_never_reach_logs` |

Also tested:

- `test_a_disconnected_client_still_settles_the_call`: the client task is
  cancelled while the provider works, and the call is still settled as
  completed.
- `test_admission_refusals_reach_the_client`: `rate_limited` with
  `Retry-After`.

**Found and fixed during implementation:**

- **Non-JSON success:** the SDK returns a plain string, not an error, for a
  non-JSON 200 response, such as a proxy page. The adapter now classifies
  that as `execution_unknown` explicitly
  (`test_unparseable_success_body_is_execution_unknown`).
- **Test fixtures:** the CLI and settings tests needed the new required
  `RESULT_ENCRYPTION_KEY` in their environment.

## Live run (gpt-6-luna, local stack, 2026-10-13)

- **Readiness** reported `{"openai": "configured"}`.
- **Migration:** `0005_call_results` applied.
- The key stayed in a shell variable and was never printed.

| Call | Usage (in / cached / cache write / out) | Gateway cost | Formula check |
|---|---|---|---|
| text ("gateway works") | 12 / 0 / 0 / 6 | 0.000005 | 1.2 + 3.0 = 4.2, rounded up to 5 |
| text resubmitted | replayed, no provider call, no new ledger row, no allowance headers | — | — |
| JSON schema (`{"capital": "Paris"}`) | 33 / 0 / 0 / 12 | 0.00001 | 3.3 + 6.0 = 9.3, rounded up to 10 |
| image, 8×8 red PNG, detail low | 18 / 0 / 0 / 5 | 0.000005 | 1.8 + 2.5 = 4.3, rounded up to 5 |
| image, 64×64 red PNG, detail high | 20 / 0 / 0 / 5 | 0.000005 | 2.0 + 2.5 = 4.5, rounded up to 5 |
| shared prefix, first | 2,080 / 0 / 2,077 / 6 | 0.000263 | 0.3 + 259.625 + 3.0 = 262.925, rounded up to 263 |
| shared prefix, second | 2,080 / 2,070 / 7 / 10 | 0.000027 | 0.3 + 20.7 + 0.875 + 5.0 = 26.875, rounded up to 27 |

- **Total charged:** 315 micro-USD, about USD 0.0003.
- **Ledger:**
  - one row per call ID;
  - 10 audit events for the first 5 calls (admitted, completed);
  - each row has the price snapshot `standard` / 2026-10-13.
- **Cache:** the second shared-prefix call read 2,070 cached tokens, cutting
  its cost by about 90%.
- **Image:** the 8×8 low-detail image was answered "Black". A local decode
  confirmed the image is red. The 64×64 high-detail image was answered
  "Red", so the image path works, and the first answer was the model's
  perception of a tiny image.
- **Logs:** 0 lines with key material and 0 with prompt text. Call lines
  show only identifiers, model, outcome, token counts, cost, latency, and the
  provider request ID.

## Not verified

- Live provider failures (rate limit, overload, account block). They are
  tested against a mocked transport and stub scenarios; producing them for
  real would need a misconfigured account.
- A long-context (more than 272K tokens) live call. It is priced by tests
  only; a live one would cost more than this check's budget.
