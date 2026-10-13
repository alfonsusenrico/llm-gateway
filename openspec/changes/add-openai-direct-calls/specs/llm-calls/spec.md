## Purpose

Let registered projects run model calls through the gateway: resolve the model
from the catalogue, execute each admitted call at most once, classify failures by
what is safe to do next, and replay recorded outcomes for the same call ID.

## ADDED Requirements

### Requirement: Calls name an operation, a call ID, and a model or profile

`POST /v1/responses` SHALL require a call ID, an operation name, and exactly one
of a contract profile or a catalogue model, and SHALL reject unknown fields.

#### Scenario: Both profile and model

- **WHEN** a request names both a profile and a model
- **THEN** it is refused with `422 request_invalid`

#### Scenario: Unknown profile

- **WHEN** a request names a profile the contract does not have
- **THEN** it is refused with `422 profile_not_found`, before admission

### Requirement: Requests are checked against the catalogue

The gateway SHALL refuse a call whose model is not in the catalogue, whose
effective settings break the model's rules, or whose input types the model does
not accept, before admission and without cost.

#### Scenario: Image to a text-only model

- **WHEN** a request sends an image part to a model that accepts only text
- **THEN** it is refused with `422 input_not_supported`

#### Scenario: Oversized request

- **WHEN** a request body exceeds the configured maximum size
- **THEN** it is refused with `413 request_too_large`

### Requirement: Completed calls return the answer, usage, and cost

A completed call SHALL return the call ID, status (`completed` or `incomplete`
with its reason), model, service tier used, output text, parsed JSON for JSON
schema requests or a structured-output error, any refusal, token usage, and cost
as a decimal USD string.

#### Scenario: JSON schema call

- **WHEN** a call with a strict JSON schema completes with valid JSON
- **THEN** the response carries `output_json`, and no structured-output
  error

#### Scenario: Unparseable JSON

- **WHEN** the model returns text that is not valid JSON for a JSON schema
  call
- **THEN** the call completes with `structured_output_error:
  invalid_json`, and is charged

### Requirement: Provider data retention is disabled

Every provider request SHALL ask the provider not to store the response, and
SHALL carry the call ID as the client request identifier.

#### Scenario: Outgoing OpenAI request

- **WHEN** the gateway calls OpenAI
- **THEN** the request has `store: false` and an `X-Client-Request-Id`
  equal to the call ID

### Requirement: Failures are classified by what is safe to do next

The gateway SHALL map each provider failure to one bounded outcome: a confirmed
temporary refusal is retryable under the same call ID; a request the provider
may have executed is `execution_unknown` and never retried; an invalid request is
final; account problems are final and alert the owner.

#### Scenario: Timeout after sending

- **WHEN** the provider connection times out after the request was sent
- **THEN** the call ends as `502 execution_unknown`, with `retryable: false`,
  and resubmitting the same ID returns the same outcome without a provider
  call

#### Scenario: Provider overloaded

- **WHEN** the provider answers 503 `server_is_overloaded`
- **THEN** the call ends as `503 provider_unavailable`, with `Retry-After`,
  and a resubmission of the same ID is executed again

#### Scenario: Exhausted credit

- **WHEN** the provider answers 429 `credit_balance_exhausted`
- **THEN** the call ends as `503 provider_account_blocked`, not retryable,
  and the owner receives a critical alert

### Requirement: Completed calls are replayed for 24 hours

Resubmitting the call ID of a completed call with the same request SHALL return
the stored answer marked as replayed, without a provider call or charge, for 24
hours, and SHALL afterwards return `410 call_result_expired`.

#### Scenario: Lost connection, resubmitted

- **WHEN** a project resubmits a call ID that completed 5 minutes earlier
- **THEN** it receives the same answer with `replayed: true`, and the ledger
  has one charge

#### Scenario: After retention

- **WHEN** a completed call ID is resubmitted 25 hours later
- **THEN** the response is `410 call_result_expired`

### Requirement: Stored results are encrypted and deleted on schedule

Stored results SHALL be encrypted at rest with a gateway-held key and SHALL be
deleted after the retention period; an unreadable stored result SHALL be treated
as expired.

#### Scenario: Retention cleanup

- **WHEN** the worker runs after a result's retention has passed
- **THEN** the result is deleted

### Requirement: Development keys can use a scripted stub model

The stub model SHALL be callable only with development keys, SHALL never contact
a provider, SHALL cost nothing, and SHALL return a scripted answer or simulate a
scripted failure category.

#### Scenario: Production key asks for the stub

- **WHEN** a production key calls `stub/stub-echo`
- **THEN** it is refused with `403 stub_not_allowed`

#### Scenario: Scripted overload

- **WHEN** a development key calls the stub with scenario `overloaded`
- **THEN** the call ends as `503 provider_unavailable`, exactly as a real
  overload would

### Requirement: Call logs carry no content

The gateway SHALL log each call with identifiers, model, outcome category, token
counts, cost, and latency only, and SHALL NOT log prompts, outputs, or provider
error messages.

#### Scenario: Provider error text

- **WHEN** the provider returns an error message that quotes the prompt
- **THEN** neither the logs nor the response contain that message
