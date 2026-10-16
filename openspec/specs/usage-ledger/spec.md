# usage-ledger Specification

## Purpose
Record every admitted call and its outcome and cost, so month-to-date spend per
grant and in total is exact and the owner is warned before budgets run out.

## Requirements

### Requirement: Every admitted call has one ledger record

The gateway SHALL keep one record per project/environment/call ID with operation,
task, resolved provider/model, alias, definition/settings digests, credential
revision when required, tiers, available usage, micro-USD cost, state, failure code
and times. It SHALL NOT store inference content or credential material in the ledger.
Historical records lacking new identity fields SHALL remain identifiable as legacy.

#### Scenario: Completed call

- **WHEN** a new call completes
- **THEN** its record holds the resolved execution identity and justified usage/cost,
  without inference content or credentials

#### Scenario: Historical row

- **WHEN** the schema upgrade encounters an old call without alias/revision metadata
- **THEN** it preserves that row's identity, fingerprint, state and money values
  without inventing historical configuration

### Requirement: Settlement charges what the outcome justifies

Paid settlement SHALL charge priced completed usage, zero for confirmed refusals,
and the reservation for unknown execution or unpriced usage. An explicitly
zero-fee call SHALL settle at zero provider cost without an unpriced-usage alert,
regardless of whether its usage counters are available.

#### Scenario: Unknown execution

- **WHEN** a paid call times out after sending
- **THEN** it becomes `execution_unknown` and its reservation counts as spend

#### Scenario: Definite refusal

- **WHEN** the provider confirms a request was rejected before execution
- **THEN** the failed call is settled with zero charge

#### Scenario: Unpriced model

- **WHEN** completed paid usage cannot be priced
- **THEN** the reservation counts as spend and a content-free pricing alert is raised

#### Scenario: Explicit zero provider fee

- **WHEN** a local/stub call completes or ends with unknown execution
- **THEN** it retains its recorded certainty state with zero provider cost and no
  invented paid price or pricing-failure alert

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

Paid spend crossing 50%, 80% or 100% of a project grant or the global cap SHALL
raise the existing content-free threshold alert once per UTC month, including
across restarts. Zero-fee execution alone SHALL not raise a monetary threshold alert.

#### Scenario: Crossing 80%

- **WHEN** paid spend crosses 80% of a pair's monthly grant
- **THEN** one warning identifies the pair and monetary amounts

#### Scenario: Restart after an alert

- **WHEN** the gateway restarts after a threshold alert this month
- **THEN** it does not repeat that alert

#### Scenario: Zero paid budget with free work

- **WHEN** a project with zero monetary grant runs zero-fee calls
- **THEN** those calls alone do not emit 50%, 80% or 100% spend alerts
