# call-admission Specification

## Purpose
Admit or refuse each model call before it reaches a provider, so every project
environment stays within its granted rate, concurrency, budget, and per-call
ceiling, and all projects together stay under the global monthly cap.

## Requirements

### Requirement: Calls are admitted only within the grant

Before a provider is called, the gateway SHALL admit the call only when the
project environment has a grant and the call fits its granted concurrency,
requests per minute, monthly budget, and per-call cost ceiling, and SHALL
otherwise refuse it without calling the provider.

#### Scenario: No grant yet

- **WHEN** a call arrives for a pair with no grant
- **THEN** it is refused with `503 allocation_pending` and `Retry-After: 5`

#### Scenario: Concurrency full

- **WHEN** a pair granted concurrency 2 has 2 calls in progress
- **THEN** a third call is refused with `429 concurrency_exhausted` and
  `Retry-After: 1`

#### Scenario: Rate full

- **WHEN** a pair granted 10 requests per minute has admitted 10 calls in
  the last 60 seconds
- **THEN** the next call is refused with `429 rate_limited`, and a
  `Retry-After` of the seconds until the oldest of them leaves the window

### Requirement: Admission is exact across processes

Admission SHALL be decided atomically, so that concurrent calls from any number
of API processes never exceed a grant or the global cap.

#### Scenario: Simultaneous calls

- **WHEN** 10 calls for a pair with granted concurrency 3 arrive at the same
  moment from different processes
- **THEN** exactly 3 are admitted

### Requirement: Each call reserves its worst-case cost

Admission SHALL reserve a worst-case cost from the request size and its maximum
output tokens at list price, and SHALL refuse the call with `422
cost_ceiling_exceeded` when the reservation exceeds the granted per-call
ceiling.

#### Scenario: Ceiling too low for the requested output

- **WHEN** a call asks for 128,000 output tokens under a 0.02 USD ceiling
- **THEN** it is refused with `cost_ceiling_exceeded`, and it is not
  retryable

### Requirement: Budgets and the global cap are enforced

Admission SHALL refuse a call with `429 budget_exhausted` when the pair's
month-to-date spend plus reservations plus the new reservation would exceed its
granted budget, or when all spend and reservations would exceed the global cap,
with `Retry-After` set to the start of the next UTC month.

#### Scenario: Grant budget spent

- **WHEN** a pair has spent its whole monthly grant
- **THEN** its next call is refused with `budget_exhausted`

#### Scenario: Global cap reached

- **WHEN** all pairs together would exceed the global monthly cap
- **THEN** the call is refused with `budget_exhausted`, and the owner
  receives a critical alert

### Requirement: Call IDs are idempotent per project environment

Admitting a call ID that already exists for the project environment SHALL return
`409 idempotency_conflict` when the request differs, `409 call_in_progress`
while it runs, a fresh admission after a confirmed temporary failure, and
otherwise the existing call's recorded outcome without a new provider call.

#### Scenario: Retry after a temporary refusal

- **WHEN** a call failed with a confirmed temporary provider refusal and is
  resubmitted unchanged
- **THEN** it is admitted again under the same call ID

#### Scenario: Same ID, different request

- **WHEN** a call ID is resubmitted with a different request
- **THEN** it is refused with `idempotency_conflict`

### Requirement: Abandoned calls become unknown, never replayed

A call still admitted after its execution lease SHALL be marked
`execution_unknown`, freeing its concurrency, keeping its reservation as spend,
and SHALL NOT be executed again under the same call ID.

#### Scenario: Process dies mid-call

- **WHEN** an API process dies while a call is admitted
- **THEN** after the lease the call becomes `execution_unknown`, and its
  concurrency slot is free again

### Requirement: Responses report the remaining allowance

Responses to admitted calls SHALL carry the request-rate limit, remaining, and
reset, the concurrency limit and remaining, and the remaining budget in USD.

#### Scenario: Headers on success

- **WHEN** a call is admitted under a grant of 30 requests per minute, with
  29 used after it
- **THEN** the response carries `X-RateLimit-Limit: 30` and
  `X-RateLimit-Remaining: 1`
