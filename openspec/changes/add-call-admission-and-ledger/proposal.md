# Proposal

## Why

Grants exist, but nothing enforces them yet. Before any provider call runs,
the gateway must make sure the project environment is within its granted
requests per minute, concurrency, monthly budget, and per-call cost ceiling,
and that every gateway grant together stays under the USD 5 monthly cap. Each
call also needs a durable record: what it cost, and how it ended.

Enforcement has to hold across several API processes, which per-process
counters cannot do.

## Expected Outcome

- A call over any grant limit is refused before it reaches the provider,
  with a stable code and a `Retry-After` where waiting helps:
  - `rate_limited`, `concurrency_exhausted`, or `budget_exhausted` (429);
  - `cost_ceiling_exceeded` (422).
- Every admitted call has one ledger row holding its project, environment,
  call ID, model, tier, token usage, cost, and outcome. Month-to-date spend
  per grant and in total is always exact.
- The owner is alerted when a grant's budget or the global cap reaches 50%,
  80%, and 100% in a month.
- Responses tell the project how much of its grant remains, through headers.

## What Changes

- **A `calls` table (migration `0004`):** one row per (project, environment,
  `provider_call_id`), holding the state, operation, model, tier, reserved
  cost, usage, cost, failure code, and timestamps.
- **Admission:** one short transaction under an advisory lock. It checks
  concurrency, the request rate, the grant budget, the global cap, and the
  per-call ceiling against a worst-case reservation, then records the call as
  `admitted`.
- **Settlement:** replaces the reservation with the priced actual usage.
  - Calls whose outcome is unknown keep their reservation as spend, which
    is conservative.
- **Recovery:** the worker marks admitted calls whose execution lease has
  expired as `execution_unknown`.
- **Budget threshold alerts** per grant and globally.
- **Allowance headers** on call responses:
  - `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`;
  - `X-Concurrency-Limit`, `X-Concurrency-Remaining`;
  - `X-Budget-Remaining-USD`.

## Non-goals

- The call endpoint and provider adapter (`add-openai-direct-calls`). This
  change provides the admission and settlement functions it uses.
- Dynamic reallocation and borrowing idle capacity (phase 4).
- Invoicing. Costs are list-price estimates.

## Capabilities

### New Capabilities

- `call-admission`: how a call is admitted or refused against its grant and
  the global cap, how concurrency and rate are counted, and how abandoned
  calls are recovered.
- `usage-ledger`: the per-call record, settlement, month-to-date spend, and
  budget threshold alerts.

### Modified Capabilities

None.

## Impact

- **Code:**
  - new `calls` package (admission, settlement, recovery, spend queries);
  - the worker gains a recovery and alert pass;
  - migration `0004`.
- **Configuration:** `LLM_CALL_TIMEOUT_SECONDS` (default 180), which also
  sets the execution lease.
- **Contract and client package:** no contract change. The headers and
  refusal codes become part of the API the phase 2 client consumes.
- **Capability:** this change **adds `money`** (a cost ledger enforcing a
  hard budget). The integrity-kit standards apply:
  - an idempotency key per call (unique call ID);
  - integer micro-USD amounts, all in USD;
  - a price snapshot on every call;
  - an explicit state machine, enforced in code and by a database check;
  - admission and settlement under locks in one transaction;
  - an append-only `call_events` audit table;
  - critical-path tests for concurrency, expiry, and duplicates.

  `AGENTS.md` adds `money` to its capabilities.
- **Depends on:** `add-model-catalogue-and-pricing`.
