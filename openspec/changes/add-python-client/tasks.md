# Tasks

## 1. Package

- [ ] 1.1 Create the workspace package `clients/python/` (`pyproject`,
  `httpx2` and `pydantic` dependencies, version 0.1.0, `User-Agent`), and
  extend `scripts/check` to it. Verify with `uv lock --check` and the check
  suite covering the new package.
- [ ] 1.2 Add the typed models (request with text, image, and pdf helpers;
  results; allocation; catalogue) and the error hierarchy mapped from
  problem codes. Verify with unit tests for parsing and code mapping,
  including unknown-code fail-closed.

Expected Result: an installable, typed client skeleton.

## 2. Behaviour

- [ ] 2.1 Implement the async and sync clients with `RetryPolicy` and the
  concurrency limiter. Verify with unit tests on a mocked transport for each
  retry row, the time budget, and limiter resizing.
- [ ] 2.2 Add contract tests against the real app (ASGI transport, migrated
  schema, stub model) for every outcome in the spec, and a guard test that
  every problem code the gateway documents is mapped or explicitly generic.
  Verify with `./scripts/check`.
- [ ] 2.3 Document installation and usage in `clients/python/README.md` and
  the root `README.md`. Verify that the README example runs against the local
  stack with a development key.

Expected Result: projects can use the gateway safely with a few lines.

## 3. Integration

- [ ] 3.1 Run `./scripts/check` and the README example against the local
  stack. Record the results in `verification.md`.

Expected Result: client and gateway verified together.

## Workflow follow-up

- Report the branch and verification record; the owner decides merge and
  archive.
