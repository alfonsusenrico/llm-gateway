# Tasks

## 1. Prices, settings, adapter

- [ ] 1.1 Add `catalogue/search_prices.json` (brave/web-search 0.005; no
  context price), its strict loader, and the `SEARCH_*` settings and compose
  entries. Verify with loader and settings tests.
- [ ] 1.2 Add the Brave adapter (results and context modes, failure table, no
  logging of queries). Verify with mocked-transport tests for each row and a
  log check.

Expected Result: Brave can be called and every failure maps to one tested
problem.

## 2. Service and API

- [ ] 2.1 Add `POST /v1/search`: the monthly cap check, admission with the
  per-request reservation, settlement, no storage, documented in OpenAPI.
  Verify with API tests (fake adapter) for success, the unpriced context mode,
  the cap and its alerts, grant refusals, each failure, and that no result
  content is stored or logged.
- [ ] 2.2 Document search and the no-storage rule in `README.md` and
  `AGENTS.md`. Verify that the documented example runs against the local
  stack (the live call below).

Expected Result: projects can search within their grants and the monthly cap.

## 3. Client

- [ ] 3.1 Add `search`, `web_search_tool`, and `run_web_search`. Verify with
  unit tests and a contract test through the app with a fake adapter.

## 4. Live verification (owner-authorised; needs `BRAVE_SEARCH_API_KEY`)

- [ ] 4.1 Run `./scripts/check`, then two live searches (results mode) and
  one live gpt-6-luna tool loop that uses search. Record results, ledger
  costs, and the free-credit usage in `verification.md`.

## Workflow follow-up

- Report the branch and verification record; the owner decides merge and
  archive.
