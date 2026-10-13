# Tasks

## 1. Catalogue and prices

- [x] 1.1 Add the catalogue and price data files (gpt-6-luna standard and
  flex, stub-echo zero), with strict loaders and a source URL per price.
  Verify with tests for invalid files, a missing standard price, and
  overlapping price periods.
- [x] 1.2 Implement the cost function (uncached, cached, cache write, output,
  long-context multipliers, rounding up, effective dates, unknown prices).
  Verify with tests for each `model-catalogue` pricing scenario, including
  the 358 micro-USD example.

Expected Result: models and prices load from reviewed data, and any usage can
be priced exactly.

## 2. Profiles and visibility

- [x] 2.1 Validate profiles against the catalogue when a contract version is
  saved (registry service, CLI, menu, import), and keep loading lenient.
  Verify with service and CLI tests for an unknown model, an effort not
  allowed, the temperature rule, an output limit, and a stored contract whose
  model was removed.
- [x] 2.2 Add `GET /v1/catalogue` with environment filtering and caching
  headers, documented in OpenAPI. Verify with API tests for production and
  development keys.
- [x] 2.3 Document the catalogue and how to add a model or price in
  `README.md` and `AGENTS.md`. Verify that the documented check command
  runs.

Expected Result: profiles are valid on save, and projects can read the
catalogue.

## 3. Integration

- [x] 3.1 Run `./scripts/check` and call `/v1/catalogue` on the local stack
  with a production and a development key. Record the results in
  `verification.md`.

Expected Result: the catalogue is served end to end.

## Workflow follow-up

- Report the branch and verification record; the owner decides merge and
  archive.
