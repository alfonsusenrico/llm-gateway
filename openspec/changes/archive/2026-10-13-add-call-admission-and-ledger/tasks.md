# Tasks

## 1. Ledger and admission

- [x] 1.1 Add migration `0004_calls.sql` (`calls` with a state check and price
  snapshot, append-only `call_events`, `alerts_sent`, indexes), and add
  `money` to `AGENTS.md` capabilities. Verify with the migrate tests.
- [x] 1.2 Implement the worst-case reservation and admission (lock, checks in
  order, refusal codes and `Retry-After`, allowance values). Verify with db
  tests for every `call-admission` refusal scenario, and a concurrency test
  with 10 simultaneous admissions across separate connections.
- [x] 1.3 Implement re-admission rules (fingerprint conflict, in progress,
  re-admission after a temporary failure, existing outcome). Verify with db
  tests per rule.

Expected Result: calls are admitted or refused exactly, across connections.

## 2. Settlement, recovery, and alerts

- [x] 2.1 Implement settlement per outcome (priced usage, zero, reservation
  kept, unpriced alert), state-machine enforcement with audit events, and the
  month-to-date spend queries. Include a test that rejects an illegal
  transition and a test for the audit trail of a retried call. Verify with db
  tests for each `usage-ledger` scenario, including a month boundary (by
  backdating rows).
- [x] 2.2 Add worker recovery of expired leases and the budget threshold
  pass, with `alerts_sent`. Verify with db and worker tests for an abandoned
  call, 50/80/100% crossings, and no repeat after a restart.
- [x] 2.3 Add `LLM_CALL_TIMEOUT_SECONDS` to settings and `.env.example`, and
  document admission codes and headers in `README.md` and `AGENTS.md`. Verify
  with settings tests.

Expected Result: every call ends with an exact, auditable record, and budgets
warn before they run out.

## 3. Integration

- [x] 3.1 Run `./scripts/check` and record the counts in `verification.md`.
  End-to-end use waits for `add-openai-direct-calls`.

Expected Result: admission and ledger are ready for the call endpoint.

## Workflow follow-up

- Report the branch and verification record; the owner decides merge and
  archive.
