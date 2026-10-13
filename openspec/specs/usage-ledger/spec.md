# usage-ledger Specification

## Purpose
Record every admitted call and its outcome and cost, so month-to-date spend per
grant and in total is exact and the owner is warned before budgets run out.

## Requirements

### Requirement: Every admitted call has one ledger record

The gateway SHALL keep one record per admitted call ID per project environment,
holding the operation, provider, model, requested and used service tier, token
usage, cost in micro-USD, state, failure code, and times, and SHALL NOT store
prompts or output in it.

#### Scenario: Completed call

- **WHEN** a call completes with usage
- **THEN** its record holds the usage and its priced cost, in state
  `completed`

### Requirement: Settlement charges what the outcome justifies

Settlement SHALL charge the priced usage of completed calls, nothing for calls
the provider refused before running, and the reservation for calls whose
execution is unknown or whose price is unknown.

#### Scenario: Unknown execution

- **WHEN** a call times out after its request was sent
- **THEN** it is recorded as `execution_unknown`, and its reservation counts
  as spend

#### Scenario: Definite refusal

- **WHEN** the provider rejects a request as invalid
- **THEN** the call is recorded as `failed`, with zero cost

#### Scenario: Unpriced model

- **WHEN** a completed call has no applicable price
- **THEN** its reservation counts as spend, and the owner receives one error
  alert for that model and tier

### Requirement: Every state change is audited and priced at the time

Every call state change SHALL append an audit event with the old and new state,
cost, actor, and time that is never updated or deleted, and each charged call
SHALL keep the price entry it was charged under.

#### Scenario: Price changes later

- **WHEN** a price entry changes after a call was settled
- **THEN** the call's recorded cost and price entry stay as charged

#### Scenario: Retried call history

- **WHEN** a call fails temporarily and is admitted again, then completes
- **THEN** its audit events show `admitted`, `retryable_failure`,
  `admitted`, and `completed`, in order

### Requirement: Month-to-date spend is exact

Month-to-date spend for a pair and in total SHALL equal the sum of charged costs
and open reservations of that UTC calendar month.

#### Scenario: Month boundary

- **WHEN** a new UTC month starts
- **THEN** month-to-date spend restarts at zero, and earlier calls stay in
  the ledger

### Requirement: Budget thresholds alert the owner once per month

When a pair's spend or the total spend first reaches 50%, 80%, or 100% of its
granted budget or the global cap in a month, the gateway SHALL raise one alert of
increasing severity, and SHALL NOT repeat it that month, even after a restart.

#### Scenario: Crossing 80%

- **WHEN** a pair's spend crosses 80% of its monthly grant
- **THEN** the owner receives one warning naming the pair and the amounts in
  USD

#### Scenario: Restart after an alert

- **WHEN** the gateway restarts after a 50% alert this month
- **THEN** the 50% alert is not sent again
