# python-client Specification

## Purpose
Give projects one typed Python client, tested against the gateway itself, that
makes the safe retry and concurrency behaviour the default instead of logic each
project writes for itself.

## Requirements

### Requirement: Client covers the gateway's project API

The client SHALL provide asynchronous and synchronous typed access to alias-based
LLM calls, model listing/details, allocation and project identity. It SHALL replace
legacy catalogue access with `models.list()` and `models.get(alias)`, and SHALL
authenticate using the existing project key without carrying provider credentials.

#### Scenario: Simple call

- **WHEN** a project calls `responses.create` with a call ID, operation and model
  alias or alias-backed profile
- **THEN** it receives a typed result including the resolved alias/definition identity

#### Scenario: Model discovery

- **WHEN** a project calls `models.list(task="llm")` and then `models.get(alias)`
- **THEN** it receives typed summaries and public model details with the effective
  request schema

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

The client SHALL raise typed errors for all documented gateway outcomes, including
missing/unreadable provider credentials, required contract upgrades, wrong model
task and changed retry configuration/credentials. Such configuration failures SHALL
not trigger automatic retries even when their HTTP status is 503.

#### Scenario: Budget exhausted

- **WHEN** the gateway refuses with `budget_exhausted`
- **THEN** the client exposes the problem code, request ID and retry timing through
  its budget error behavior

#### Scenario: Unknown problem code

- **WHEN** an unknown code is returned
- **THEN** the client raises a generic gateway error and does not retry

#### Scenario: Missing provider credential

- **WHEN** a paid call returns `provider_credentials_missing`
- **THEN** the client raises its typed configuration error without another request

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

### Requirement: Client streams calls as typed events

The client SHALL provide `responses.stream(...)`, an async iterator yielding
typed started, text delta, refusal delta, and completed events, and raising the
typed error for a problem.

#### Scenario: Streamed text

- **WHEN** a project iterates a streamed call
- **THEN** it receives text deltas, then a completed event whose result
  carries usage and cost

### Requirement: A lost stream is recovered by resubmission

If the connection drops after a streamed call started, the client SHALL resubmit
the same call ID, retry `call_in_progress` with backoff within its retry budget,
and finish with the replayed completed event.

#### Scenario: Connection dropped mid-stream

- **WHEN** the connection to the gateway breaks after two text deltas
- **THEN** the iterator ends with a completed event marked replayed, whose
  result holds the full answer

### Requirement: Client helps build tool loops

The client SHALL provide helpers to declare a function tool, to build a function
call output, and to turn a result's continuation into the next input item, and
SHALL expose tool calls as typed objects in results and stream events.

#### Scenario: Two-turn loop with the stub

- **WHEN** a project runs a call that returns a tool call, then sends the
  continuation and the function output
- **THEN** the second call returns text, with no hand-built provider items

### Requirement: Request serialization preserves explicit parameter presence

The client SHALL serialize caller-supplied settings without adding unsupported
model-specific default fields. It SHALL not silently remove an explicitly supplied
unsupported field or replace a chosen alias. Server validation SHALL remain
authoritative.

#### Scenario: Non-reasoning call

- **WHEN** the caller omits reasoning settings for a non-reasoning definition
- **THEN** the client does not add a reasoning field to the request

#### Scenario: Explicit unsupported setting

- **WHEN** the caller supplies a setting the chosen model rejects
- **THEN** the caller receives a validation error instead of silent parameter removal

### Requirement: OpenAPI describes the implemented public contract

The API SHALL expose accurate typed request, response, authentication and problem
schemas for its implemented endpoints. Model details SHALL supply effective
per-model schemas where the shared operation schema is broader. Client/app contract
tests SHALL check those public shapes together. Final onboarding documentation
SHALL follow backend completion.

#### Scenario: Alias request documented

- **WHEN** OpenAPI is generated for `/v1/responses`
- **THEN** its model selector is a string alias rather than a provider/model object,
  its response includes resolved identity, and new problem outcomes are documented

#### Scenario: Development API exploration

- **WHEN** the application runs in development
- **THEN** its existing Swagger UI exposes the updated implemented API schemas
