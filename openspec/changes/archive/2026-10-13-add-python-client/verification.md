# Verification: add-python-client

Date: 2026-10-13
Branch: `feat/python-client`. The tested revision is the branch head in its
pull request.
Data: synthetic only. The stub model is used throughout, so no provider cost.

## Automated checks

`./scripts/check` with `REQUIRE_DB=1` (local database now on port 54330):

- lock, format, and lint: passed, covering `clients/python`.
- `pyright`: 0 errors, covering client source and tests.
- `pytest`: 595 passed:
  - 20 client unit tests;
  - 11 contract tests against the real gateway app.
- compose config: passed.
- `openspec validate --all`: 9 passed.

### `python-client` coverage

| Requirement | Tests |
|---|---|
| Covers the project API | `test_call_and_json_answer`, `test_allocation_catalogue_and_identity` (allocation, catalogue, `whoami`) |
| Retries retryable refusals with the same call ID | `test_retryable_refusal_is_resubmitted_with_the_same_call_id` (waits `Retry-After` plus jitter), `test_transport_failure_is_resubmitted`, `test_gateway_5xx_without_a_problem_body_is_retried`, `test_lost_response_is_recovered_by_replay` (contract: the response is dropped after the gateway completed, the client resubmits, the answer is `replayed`, the ledger has one call), `test_a_wait_beyond_the_retry_budget_raises_instead_of_sleeping`, `test_attempts_are_bounded` |
| Unknown execution never retried | `test_execution_unknown_is_never_retried` (1 request, no sleep), `test_unknown_execution_is_attempted_once` (contract: one admission event in the ledger) |
| Typed non-retryable errors | `test_codes_map_to_typed_errors`, `test_budget_exhaustion_raises_with_the_reset_time`, `test_unknown_code_fails_closed`, `test_failures_raise_their_typed_errors`, `test_production_key_and_stub_is_an_invalid_request` |
| Concurrency follows the grant | `test_concurrency_follows_the_reported_limit` (peak 2 of 5 calls with a limit of 2), plus the contract check that `concurrency_limit == 2` after a call |
| Client and gateway tested together | `test_every_gateway_problem_code_is_mapped_by_the_client`: it collects codes from `PROBLEM_STATUS`, the admission titles, every raise site, and the HTTP code table. Only `http_error` is explicitly generic. |

Also covered: `test_sync_client_follows_the_same_rules` and
`test_requests_identify_the_client` (`User-Agent: llm-gateway-client/0.1.0`).

## Local stack (2026-10-13)

The README flow ran against `http://127.0.0.1:8360` with a development key of
the throwaway project `client_check_two`, now disabled. The key stayed in a
shell variable.

- `whoami`, then `allocation` (state `approved`, concurrency 2).
- A text call returned "stub: Summarise this week's notes", cost 0.00. The
  client's concurrency limit was 2 afterwards.
- The same call ID again returned `replayed: True`.
- A message holding an image, with a JSON schema, returned
  `{"shape": "square"}`.
- The `timeout_after_send` scenario raised `ExecutionUnknownError` carrying
  a `request_id`.
- `SyncGatewayClient` returned the expected stub reply.

## Findings during verification

- **Local database port:** port 5433 was taken by another project's
  database, which made every database test fail authentication. At the
  owner's request, the local database moved to port 54330. Changed: local
  `.env` (port values only), the compose default, `.env.example`, and
  `setup-local`.
- **Gateway defect, separate from this change:** after the database
  restarted, the API's connection pool handed a dead connection to the next
  request, which failed with `psycopg.errors.AdminShutdown` (500).
  - Reproduced by terminating the pool's backends with
    `pg_terminate_backend`: the first request then failed and the next ones
    succeeded.
  - The worker recovered on its own; its `PoolTimeout`s were the database
    being down.
  - Cause: `create_pool` sets no checkout check. A fix is proposed
    separately.

## Not verified

- Installing the client from a git tag. No tag exists yet; the workspace
  install and the contract tests cover the package.
