# Tasks

## 1. Event store

- [x] 1.1 Add migration `0001_notifier_events.sql`: the events table, the
  partial unique index on open deduplication keys, and a due-time index.
  Verify with the migrate tests (empty database, then re-apply as a no-op).
- [x] 1.2 Implement `raise_alert` with insert-or-increment deduplication,
  summary truncation, and swallowed database errors. Verify with db tests:
  burst, after the window, concurrent raise, database down, and caller
  rollback.

Expected Result: alerts are stored and deduplicated, and raising one can never
fail its caller.

## 2. Channel and delivery

- [x] 2.1 Add `httpx2` (the maintained successor Starlette recommends, same
  API) as a runtime dependency and the `NOTIFIER_*` settings, with
  `.env.example` entries. Verify with `uv lock --check` and settings
  tests.
- [x] 2.2 Implement the channel interface and the Telegram adapter (`delivered`,
  `retry` with `retry_after`, `rejected`, and a log line holding only the
  error class and status). Verify with adapter tests against a mocked
  transport, including a test that the token never appears in captured logs.
- [x] 2.3 Implement the delivery loop: claim with `SKIP LOCKED`, backoff,
  8-attempt limit, expiry, disabled-channel behaviour, and graceful shutdown.
  Verify with db tests for retry, rate-limit delay, permanent rejection,
  expiry, and two concurrent workers that never double-send.

Expected Result: a worker delivers, retries, and expires events correctly, with
no secrets in logs.

## 3. Process wiring and CLI

- [x] 3.1 Add the `llm-gateway-worker` entry point and the compose `worker`
  service. Verify that `docker compose up -d --build` shows the worker running
  and that `docker compose stop worker` exits cleanly within 10 s.
- [x] 3.2 Add `llm-gateway-admin` with `notifier test`. Verify with CLI tests
  for the delivered and still-pending outcomes, using a fake channel.
- [x] 3.3 Update `AGENTS.md` (architecture map, commands) and `README.md`
  (notifier setup). Verify that the documented commands run as written.

Expected Result: the stack runs an API and a worker, and the owner can test
delivery with one command.

## 4. Live verification (owner-authorised)

- [x] 4.1 With the owner's notifier bot token and chat ID in `.env`, run
  `llm-gateway-admin notifier test` against the local stack. Record the
  result in `verification.md`.
- [x] 4.2 Run `./scripts/check` and record the counts in `verification.md`.

Expected Result: one real Telegram message is received, and every check
passes.

## Workflow follow-up

- Report the branch and verification record; the owner decides merge and
  archive.
