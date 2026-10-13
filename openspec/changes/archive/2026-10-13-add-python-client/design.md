# Design

## Context

The gateway gives each outcome a stable problem `code`, a `retryable` flag
(through the code family), `Retry-After`, and allowance headers, and it
deduplicates calls by call ID. See proposal.md for motivation.

## Goals / Non-Goals

**Goals:**

- Safe by default, small, and typed.
- No logic that the gateway already owns.

**Non-Goals:**

- Caching.
- Prompt helpers.
- Provider-specific options.

## Decisions

### 1. A workspace package in the gateway repository

`clients/python/pyproject.toml` defines `llm-gateway-client`. The root
`pyproject.toml` declares a uv workspace.

- Projects install it with
  `llm-gateway-client @ git+https://github.com/alfonsusenrico/llm-gateway@<tag>#subdirectory=clients/python`.
- Client and gateway are released together, so contract tests in one
  commit prove they match.

*Rejected:*

- **A separate repository:** it splits contract tests from the API.
- **Generating the client from OpenAPI:** the generated code would still
  need hand-written retry and limiter logic.

### 2. Retry rules

| Code | Client behaviour |
|---|---|
| `provider_rate_limited`, `provider_unavailable`, `rate_limited`, `concurrency_exhausted`, `call_in_progress`, `allocation_pending` | Wait `Retry-After` (minimum 1 s) + up to 20% jitter, then resubmit the same call ID |
| Transport failure to the gateway (connect, read, or 5xx with no problem body) | Resubmit the same call ID; the gateway replays or reports `call_in_progress` |
| `execution_unknown` | Raise `ExecutionUnknownError`; never retry; the project decides whether to use a new ID |
| `budget_exhausted` | Raise `BudgetExhaustedError` with `retry_after` (the month boundary) |
| `cost_ceiling_exceeded`, `request_invalid`, `profile_not_found`, `model_not_in_catalogue`, `invalid_model_settings`, `input_not_supported`, `request_too_large`, `provider_invalid_request`, `idempotency_conflict` | Raise `InvalidRequestError` |
| `key_missing`, `key_malformed`, `key_invalid`, `project_disabled`, `auth_throttled` | Raise `AuthenticationError` |
| `provider_account_blocked`, `provider_not_configured`, `provider_failed`, `project_quarantined`, `call_result_expired` | Raise `GatewayUnavailableError` (not retried) |

- **Budgets:** the defaults are 4 attempts and 90 s total. When a wait
  would exceed the remaining budget, the client raises `RetryableError`
  with `retry_after`, so a job scheduler can defer the work.
- **Unknown codes:** a code the client does not know is raised as a generic
  `GatewayError` and never retried. New gateway codes then fail closed.

### 3. Concurrency limiter

An async semaphore, starting at 1 and resized to `X-Concurrency-Limit` from
every response, wraps each call. The sync client uses a thread semaphore with
the same rule.

A `concurrency_exhausted` answer still retries (another process of the same
project may hold slots).

### 4. Types

Pydantic models mirror the gateway's request and response:

- request: `CallRequest`, with message and part helpers
  `text(...)`, `image(path_or_bytes, media_type)`, and
  `pdf(path_or_bytes, filename)`;
- response: `CallResult`, `Usage`, `Allocation`, `CatalogueModel`.

Response models ignore unknown fields, so newer gateways stay compatible.
Request models forbid them.

### 5. Contract tests

The tests live in the gateway repository. They build the real app on the
migrated test schema and use the client through `httpx2.ASGITransport`, with
the stub model, covering:

- success and JSON;
- every stub scenario mapped to the right error type;
- a retry that succeeds on resubmission;
- replay after a simulated lost response;
- a limiter adjusted from headers;
- unknown-code fail-closed.

## Risks / Trade-offs

- **[Risk]** Client and gateway versions diverge in a project.
  **Mitigation:** both share a tag. The client sends
  `User-Agent: llm-gateway-client/<version>`, so the gateway logs can show
  stale clients.
- **[Trade-off]** The limiter starts at 1 until the first response. This
  causes slight underuse on a cold start, which is accepted.

## Migration Plan

- New package only.
- The root `pyproject.toml` gains `[tool.uv.workspace]`, and `uv.lock`
  updates.
