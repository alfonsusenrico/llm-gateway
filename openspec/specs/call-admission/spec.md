# call-admission Specification

## Purpose
Admit or refuse each model call before it reaches a provider, so every project
environment stays within its granted rate, concurrency, budget, and per-call
ceiling, and all projects together stay under the global monthly cap.

## Requirements

### Requirement: Calls are admitted only within the grant

Before execution, every new attempt SHALL require a project/environment grant and
fit its rate and concurrency limits. Paid attempts SHALL additionally fit granted
monetary budgets and per-call ceilings. Explicitly zero-fee attempts SHALL bypass
monetary checks while retaining all non-monetary checks.

#### Scenario: No grant yet

- **WHEN** a call arrives for a pair with no grant
- **THEN** it is refused with `503 allocation_pending` and `Retry-After: 5`

#### Scenario: Concurrency full

- **WHEN** a pair granted concurrency 2 has 2 calls in progress
- **THEN** a third call receives `429 concurrency_exhausted` and `Retry-After: 1`

#### Scenario: Rate full

- **WHEN** a pair has admitted its granted requests per minute
- **THEN** its next call receives `429 rate_limited` with the window reset delay

#### Scenario: Zero-fee call after paid exhaustion

- **WHEN** the paid budget is exhausted but the project has rate/concurrency capacity
- **THEN** an explicitly zero-fee call is admitted with zero reservation

### Requirement: Admission is exact across processes

Admission SHALL be decided atomically, so that concurrent calls from any number
of API processes never exceed a grant or the global cap.

#### Scenario: Simultaneous calls

- **WHEN** 10 calls for a pair with granted concurrency 3 arrive at the same
  moment from different processes
- **THEN** exactly 3 are admitted

### Requirement: Each call reserves its worst-case cost

Paid admission SHALL reserve a worst-case cost from the request size and maximum
output at the registered price, and SHALL return `422 cost_ceiling_exceeded` if
it exceeds the granted ceiling. Explicitly zero-fee execution SHALL reserve zero.
Unavailable paid pricing SHALL be refused, never converted into free execution.

#### Scenario: Ceiling too low for the requested output

- **WHEN** a paid call's worst-case reservation exceeds its granted per-call ceiling
- **THEN** it is refused with non-retryable `cost_ceiling_exceeded`

### Requirement: Budgets and the global cap are enforced

Paid admission SHALL return `429 budget_exhausted` when charged spend plus open
reservations plus the new reservation exceeds either the project/environment
grant or global monthly cap, with retry at the next UTC month. The default global
cap SHALL remain USD 5. Zero-fee calls SHALL not be refused for monetary exhaustion.

#### Scenario: Grant budget spent

- **WHEN** a pair has spent its monthly grant
- **THEN** its next paid call is refused with `budget_exhausted`

#### Scenario: Global cap reached

- **WHEN** a new paid reservation would exceed the global cap
- **THEN** the call is refused with `budget_exhausted` and a content-free critical
  alert is raised

### Requirement: Call IDs are idempotent per project environment

A reused call ID SHALL return `409 idempotency_conflict` for a different request
or task, `409 call_in_progress` while running, and the recorded terminal outcome
without new execution. A confirmed-refusal retry SHALL be admitted only with the
same effective model/profile configuration and credential revision as its prior
attempt.

#### Scenario: Retry after a temporary refusal

- **WHEN** a temporarily refused call is resubmitted with unchanged request,
  effective configuration and credential revision
- **THEN** it can be admitted again under the same call ID

#### Scenario: Same ID, different request

- **WHEN** a call ID is reused for different input or another inference task
- **THEN** it returns `idempotency_conflict` without execution

#### Scenario: Profile edited before retry

- **WHEN** a retryable call names a profile whose effective settings have changed
- **THEN** it returns non-retryable `409 call_configuration_changed` without
  readmission or execution

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

### Requirement: Admission records the resolved execution identity

Each new admission SHALL bind task, model alias, immutable definition digest,
effective settings digest and provider credential revision, if needed, together
with its original request fingerprint. Resolution and credential capture SHALL
not allow a concurrent edit to change the account or configuration executed under
the recorded identity.

#### Scenario: Concurrent credential replacement

- **WHEN** credential replacement races with a paid admission
- **THEN** the recorded revision and the credential used by that attempt agree

#### Scenario: No provider credential needed

- **WHEN** a zero-fee local/stub call is admitted
- **THEN** its credential revision is absent and no paid credential is accessed
