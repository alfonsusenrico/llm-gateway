# Tasks

## 1. Allocation function

- [x] 1.1 Implement the pure allocation function (floors, weighted
  water-filling, integer largest-remainder, reasons, ceiling rule). Verify
  with unit tests for every `resource-allocation` sharing scenario and with
  property tests over random pools: capacity respected, grants never above
  requests, an undersubscribed pool exactly satisfied, and order independence.
- [x] 1.2 Add the `ALLOCATION_*` and `GLOBAL_MONTHLY_BUDGET_USD` settings, with
  `.env.example` entries and validation (minimums not above capacity).
  Verify with settings tests.

Expected Result: a tested, deterministic allocation that needs no database.

## 2. Storage and recomputation

- [x] 2.1 Add migration `0003_grants.sql`. Verify with the migrate tests.
- [x] 2.2 Implement `recompute()`: advisory lock, the inputs hash, replacing
  all grants in one transaction, and alerts after commit. Verify with db tests
  for registration, disabling, weight changes, an unchanged hash (no write,
  no alert), and reduction and restoration alerts.
- [x] 2.3 Call `recompute()` from the admin CLI after contract, status, and
  weight changes, and from the worker on start and every 60 s. Verify with
  db tests and a worker test using changed capacity settings.

Expected Result: grants stay current after every change and alert on
reductions.

## 3. Visibility

- [x] 3.1 Add `GET /v1/allocation`, with a 10 s cache and `503
  allocation_pending`, documented in OpenAPI. Verify with API tests for
  approved, reduced, pending, and privacy between projects.
- [x] 3.2 Add `llm-gateway-admin grants show` and `project set-weight` (also in
  the menu). Verify with CLI tests for table content and utilisation.
- [x] 3.3 Document grants, weights, and capacity settings in `README.md` and
  `AGENTS.md`. Verify that the documented commands run.

Expected Result: projects and the owner can see every grant and why it is what
it is.

## 4. Integration

- [x] 4.1 On the local stack, register two test projects whose requests
  oversubscribe concurrency, change a weight, and disable one project. Record
  the `GET /v1/allocation` responses, `grants show` output, and alert events
  in `verification.md`.
- [x] 4.2 Run `./scripts/check` and record the counts in `verification.md`.

Expected Result: allocation behaves as specified end to end.

## Workflow follow-up

- Report the branch and verification record; the owner decides merge and
  archive.
