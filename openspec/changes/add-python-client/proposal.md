# Proposal

## Why

Projects should never hand-copy gateway client code. Copies drift: retry
rules, error handling, and "is it safe to try again" decisions end up
different in every project. One typed client, shipped and tested with the
gateway, makes the safe behaviour the default.

## Expected Outcome

- A project installs `llm-gateway-client` (pinned to a gateway tag) and
  makes calls with a few lines.
- The client:
  - retries only what is safe, with the same call ID, honouring
    `Retry-After`;
  - never retries an `execution_unknown` call;
  - raises a typed error for everything else;
  - keeps its concurrency within the project's grant, so the project waits
    for a slot instead of collecting `429`s.

## What Changes

- **A new workspace package** at `clients/python/` (`llm-gateway-client`,
  import name `llm_gateway_client`), depending only on `httpx2` and
  `pydantic`.
- **`GatewayClient` (async) and `SyncGatewayClient`** with:
  - `responses.create(...)`, which returns a typed `CallResult`;
  - `allocation()`, `catalogue()`, `whoami()`.
- **Typed errors per problem code family:**
  - `RetryableError` (with `retry_after`);
  - `ExecutionUnknownError`;
  - `BudgetExhaustedError`;
  - `InvalidRequestError`;
  - `AuthenticationError`;
  - `GatewayUnavailableError`.
- **`RetryPolicy`** (default: 4 attempts within 90 s), which resubmits the
  same call ID after retryable refusals and after transport failures to the
  gateway. That is safe because the gateway deduplicates by call ID.
- **A concurrency limiter** sized from `X-Concurrency-Limit` and adjusted on
  each response.
- **Contract tests** in the gateway suite: the client against the real app
  (ASGI transport, stub model), for every documented outcome.

## Non-goals

- A TypeScript client (later).
- Streaming helpers (phase 3) and job helpers (phase 4).
- Publishing to PyPI. Projects install from the repository at a tag.

## Capabilities

### New Capabilities

- `python-client`: the client's call surface, retry rules, error types,
  concurrency limiting, and compatibility with the gateway API.

### Modified Capabilities

None.

## Impact

- **Repository layout:**
  - `clients/python/` with its own `pyproject.toml`, as a uv workspace
    member;
  - the gateway's dev dependencies include it, for the contract tests;
  - `scripts/check` covers it (ruff, pyright, tests).
- **Contract and client package:** creates the client at version `0.1.0`.
  It targets gateway API `/v1`.
- **Depends on:** `add-openai-direct-calls`.
