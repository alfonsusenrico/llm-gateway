# Verification: add-streaming-responses

Date: 2026-10-13
Branch: `feat/streaming-responses`. The tested revision is the branch head in
its pull request.
Data: synthetic only. The stub model is used throughout the tests; one live
gpt-6-luna stream ran on the local stack.

## Automated checks

`./scripts/check` with `REQUIRE_DB=1`:

- lock, format, and lint: passed.
- `pyright`: 0 errors.
- `pytest`: 628 passed:
  - 18 gateway streaming tests (`tests/test_streaming.py`);
  - 7 client streaming unit tests and 3 client streaming contract tests;
  - 2 image build guards (`tests/test_container_build.py`).
- compose config: passed.
- `openspec validate --all`: 12 passed.

### `llm-calls` coverage

| Requirement | Tests |
|---|---|
| Calls can be streamed as gateway events | `test_stream_yields_started_deltas_and_the_final_result`, `test_incomplete_and_failed_terminals`, `test_stub_streams_words_and_can_fail_mid_stream`, `test_streamed_call_end_to_end` (allowance headers, ledger row), `test_sse_frames`, `test_stream_defaults_off_and_never_changes_the_fingerprint` |
| Refusals before acceptance keep their HTTP status | `test_error_event_before_acceptance_is_classified` (each provider code), `test_refusal_at_create_keeps_its_status_category`, `test_refusal_before_acceptance_is_problem_json` (503 problem JSON) |
| Failures after acceptance end with an error event | `test_error_event_after_acceptance_is_provider_failed`, `test_connection_lost_mid_stream_is_execution_unknown`, `test_stream_without_a_terminal_event_is_execution_unknown`, `test_failure_after_acceptance_is_an_error_event` (ledger `execution_unknown`), `test_settlement_error_ends_the_stream_with_an_internal_error` |
| A disconnected client never stops settlement | `test_disconnected_client_still_settles_and_can_replay`: the provider is delayed, the consumer reads one event and closes, the call still settles `completed`, and resubmission replays the full answer |
| Finished calls are replayed as a one-event stream | `test_replays_are_one_event_streams` (completed and unknown calls; a direct resubmission of a streamed call ID also replays) |
| Streams stay open through quiet periods | `test_quiet_streams_send_keep_alives` |

### `python-client` coverage

| Requirement | Tests |
|---|---|
| Client streams calls as typed events | `test_stream_yields_typed_events` (keep-alive comments skipped, concurrency limit read), `test_refusal_before_start_is_retried_like_a_direct_call`, `test_invalid_request_raises_without_retry`, `test_error_event_raises_its_typed_error` (never resubmitted), `test_streamed_call_through_the_app`, `test_streamed_error_event_raises_through_the_app` |
| A lost stream is recovered by resubmission | `test_dropped_stream_resubmits_and_ends_with_the_replay` (drop, `call_in_progress`, replayed `Completed`, one call ID), `test_stream_ending_without_its_final_event_is_resubmitted`, `test_retry_budget_bounds_resubmission`, `test_dropped_stream_is_recovered_by_replay` (contract: the connection breaks after two events, the client ends with the replayed answer, and the ledger has one call) |

The client version is now 0.2.0.

## Local stack (2026-10-13)

The image was rebuilt from the working tree (`llm-gateway:local`, image
`4776bb49bac6`) for the runs below. The running container imports `llm_gateway.llm.streaming`
and does not contain the client package. The throwaway project
`stream_check` was registered for this run and is now disabled. Its keys
stayed in shell variables and were never printed.

- **Documented curl example** (development key, stub): `response.started`,
  three `text.delta` events, and `response.completed` with "stub: Hello
  there".
- **Stub stream with a disconnect:** the client read the first line
  (`event: response.started`) and closed the connection. Resubmitting the
  call ID directly returned `replayed: True`, `status: completed`, with the
  full echo (264 words, the stub's `_MAX_ECHO_CHARS` cap). The stub answers
  in milliseconds, so this run cannot show that the producer outlived the
  connection; the delayed case is covered by
  `test_disconnected_client_still_settles_and_can_replay`.
- **Live gpt-6-luna stream** (production key, owner-authorised, through
  `responses.stream`): 10 text deltas, status `completed`, 18 input and 14
  output tokens, cost USD 0.000009 ("one two three four five six seven eight
  nine ten").
  - The ledger charged 9 micro-USD: 18 × 0.10 + 14 × 0.50 = 8.8 micro-USD
    at the standard price, rounded up.
  - Streaming the same call ID again returned one `Completed` event,
    `replayed: True`, with no new charge.
- **Logs:** 0 lines with key material, and 0 with prompt or answer text.
  Call lines carry IDs, counts, cost, and latency only.
- **Final tree** (after the settlement fix below; image `5d47dc6ddb1b`):
  `stream_check` was enabled briefly with a rotated development key, then
  disabled again. The curl example streamed `response.started`, three
  `text.delta` events, and `response.completed`. A `fail_mid_stream` stream
  ended with an `error` event, `execution_unknown`, status 502. The logs
  held 0 lines with key material.

## Findings during verification

- **Image build broken on `main` since the client became a workspace
  member:** `docker compose up -d --build` failed at `uv sync --locked
  --no-dev --no-install-project` with "`llm-gateway-client` references a
  workspace in `tool.uv.sources` ... but is not a workspace member". The
  build context held only the root project, so uv could not parse the
  workspace. The old container kept running the previous image, so the
  first local check answered `stream` with `extra_forbidden`.
  - Fix: the Dockerfile copies `clients/python/pyproject.toml` (metadata
    only; the client is a dev dependency and stays out of the image), and
    `.dockerignore` admits it.
  - Regression guard: `test_every_workspace_member_manifest_reaches_the_image_build`
    fails without the fix and passes with it.
    `./scripts/check` does not build images, so the guard checks the build
    inputs instead.

- **Review finding, streamed settlement:** if `finish_call` raised anything
  other than a problem (a database error, for example), the producer task
  ended without closing its channel. The client then received keep-alives
  until it gave up.
  - Fix: the producer always closes the channel. An unexpected settlement
    error is logged by type name and sent as an `internal_error` event, or
    raised as a 500 before acceptance. Adapter exceptions in streams are now
    logged like direct calls.
  - Regression test: `test_settlement_error_ends_the_stream_with_an_internal_error`.
    Without the fix it times out after 5 seconds; with the fix it passes.

## Not verified

- A live provider failure mid-stream (rate limit, overload, or a dropped
  provider connection). These are covered by mocked-transport tests only.
- A live client disconnect while gpt-6-luna is still answering. That would
  add cost for a path the delayed-provider test already covers.
- Reasoning-model latency before the first delta beyond the test's
  keep-alive interval.
