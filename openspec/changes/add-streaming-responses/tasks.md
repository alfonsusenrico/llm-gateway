# Tasks

## 1. Adapters

- [x] 1.1 Add `stream` to the request model and the internal stream event
  types. Verify with request validation tests.
- [x] 1.2 Add OpenAI `execute_stream` (`include_obfuscation` false, headers,
  outcome mapping table, `finally` close). Verify with mocked-transport SSE
  tests for each row of the outcome table.
- [x] 1.3 Add stub streaming with the `fail_mid_stream` scenario. Verify with
  tests.

Expected Result: both adapters stream, and every upstream outcome maps to one
tested result.

## 2. Gateway streaming

- [x] 2.1 Add the streaming path to `run_call`: wait for acceptance, then the
  producer task, bounded queue, keep-alive, and shielded settlement and
  storage. Verify with API tests for streamed success, refusals before
  acceptance as JSON, an `error` event mid-stream, client disconnect (still
  settled and replayable), and replay or repeat submissions as one-event
  streams.
- [x] 2.2 Document streaming in `README.md` and `AGENTS.md`. Verify that the
  documented curl example runs against the local stack with the stub.

Expected Result: streamed calls keep every direct-call guarantee.

## 3. Client

- [x] 3.1 Add `responses.stream(...)` with typed events and the resubmission
  rule, and raise the client minor version. Verify with unit tests on a mocked
  SSE transport and contract tests against the app (stream, error event,
  dropped connection recovered by replay).

Expected Result: projects can stream with the same safety as direct calls.

## 4. Verification

- [x] 4.1 Run `./scripts/check`. Run one live streamed gpt-6-luna call
  (owner-authorised, under USD 0.001) and one stub stream with a disconnect
  on the local stack. Record the results in `verification.md`.

## Workflow follow-up

- Report the branch and verification record; the owner decides merge and
  archive.
