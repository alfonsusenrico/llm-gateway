# python-client Specification

## Purpose
Give projects one typed Python client, tested against the gateway itself, that
makes the safe retry and concurrency behaviour the default instead of logic each
project writes for itself.

## Requirements

### Requirement: Client covers the gateway's project API

The client SHALL provide asynchronous and synchronous access to model calls, the
project's allocation, the catalogue, and the key identity, returning typed
results.

#### Scenario: Simple call

- **WHEN** a project calls `responses.create` with a call ID, operation,
  profile, and text input
- **THEN** it receives a `CallResult` with output text, usage, and cost

### Requirement: Retryable refusals are retried with the same call ID

The client SHALL resubmit the same call ID after a retryable gateway refusal or a
transport failure to the gateway, waiting at least the given `Retry-After` plus
jitter, within a bounded number of attempts and total time.

#### Scenario: Provider overloaded once

- **WHEN** the first attempt returns `503 provider_unavailable` with
  `Retry-After: 1`, and the second succeeds
- **THEN** the caller receives the result, and the gateway ledger shows one
  call ID

#### Scenario: Lost response from the gateway

- **WHEN** the connection to the gateway drops after the call completed
- **THEN** the client resubmits the same call ID, and receives the replayed
  result

#### Scenario: Retry budget exceeded

- **WHEN** the next wait would exceed the remaining retry time
- **THEN** the client raises `RetryableError` with the suggested wait,
  instead of sleeping

### Requirement: Unknown execution is never retried

The client SHALL raise `ExecutionUnknownError` for an `execution_unknown`
outcome and SHALL NOT resubmit that call ID.

#### Scenario: Timeout after send at the provider

- **WHEN** the gateway answers `execution_unknown`
- **THEN** the client raises at once, without another request

### Requirement: Non-retryable outcomes raise typed errors

The client SHALL raise a typed error for budget exhaustion, invalid requests,
authentication problems, and gateway or provider unavailability that is not
retryable, carrying the problem code and request ID.

#### Scenario: Budget exhausted

- **WHEN** the gateway refuses with `budget_exhausted`
- **THEN** the client raises `BudgetExhaustedError` with the time until the
  budget resets

#### Scenario: Unknown problem code

- **WHEN** the gateway returns a code this client version does not know
- **THEN** the client raises a generic `GatewayError`, and does not retry

### Requirement: Client concurrency follows the grant

The client SHALL limit its own concurrent calls to the latest
`X-Concurrency-Limit` the gateway reported.

#### Scenario: Grant of two

- **WHEN** a project starts 5 calls at once with a granted concurrency of 2
- **THEN** at most 2 are in flight at any moment once the limit is known

### Requirement: Client and gateway are tested together

Every documented gateway outcome SHALL be covered by tests that run the client
against the gateway application in the same revision.

#### Scenario: New gateway code

- **WHEN** a change adds a problem code without client handling
- **THEN** the contract tests fail until the client maps it, or it is
  explicitly documented as generic
