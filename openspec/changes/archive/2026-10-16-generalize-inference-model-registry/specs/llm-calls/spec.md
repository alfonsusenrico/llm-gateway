## MODIFIED Requirements

### Requirement: Calls name an operation, a call ID, and a model or profile

`POST /v1/responses` SHALL require a call ID, an operation name, and exactly one
of a model alias string or an alias-backed contract profile. It SHALL reject
unknown fields, provider/model selection objects and supplied provider credentials
without echoing values.

#### Scenario: Both profile and model

- **WHEN** a request names both a profile and a model alias
- **THEN** it is refused with `422 request_invalid`

#### Scenario: Unknown profile

- **WHEN** a request names a profile the contract does not have
- **THEN** it is refused with `422 profile_not_found`, before admission

#### Scenario: Legacy model object

- **WHEN** a request selects a model using a provider/model object
- **THEN** it is refused with `request_invalid`, without a provider call

### Requirement: Requests are checked against the catalogue

Before new admission, the gateway SHALL resolve the alias/profile, merge explicit
overrides with supported profile/model defaults, and validate against the selected
LLM definition's effective schema. Unknown aliases, incompatible tasks, unsupported
fields/inputs and invalid combinations SHALL be rejected without provider execution.

#### Scenario: Image to a text-only model

- **WHEN** a request sends an image to a text-only definition
- **THEN** it is refused with `422 input_not_supported`

#### Scenario: Oversized request

- **WHEN** a request body exceeds the configured maximum size
- **THEN** it is refused with `413 request_too_large`

#### Scenario: Wrong task

- **WHEN** an embedding alias is selected for `/v1/responses`
- **THEN** it is refused with `422 model_task_mismatch`

#### Scenario: Unsupported parameter

- **WHEN** the selected model does not accept a supplied parameter
- **THEN** the call is refused with `422 invalid_model_settings` before admission,
  and the parameter is not silently dropped

### Requirement: Completed calls return the answer, usage, and cost

A completed call SHALL return call ID, status and incomplete reason, alias,
definition digest, resolved provider/model, service tier, text, parsed JSON or its
structured-output error, refusal, token usage and decimal-string USD cost. Unknown
usage SHALL remain unavailable rather than be invented.

#### Scenario: JSON schema call

- **WHEN** a strict JSON-schema call completes with valid JSON
- **THEN** the result carries `output_json` without a structured-output error

#### Scenario: Unparseable JSON

- **WHEN** a JSON-schema call returns invalid JSON
- **THEN** the result carries `structured_output_error: invalid_json` and is charged

#### Scenario: Resolved model identity

- **WHEN** a project calls a registered alias
- **THEN** its result exposes that alias, definition digest and actual provider/model

### Requirement: Tool calls are returned with parsed arguments

When the model calls functions, the result SHALL list each call's ID, name, raw
arguments, parsed arguments or an `invalid_json` error, and an opaque continuation
tagged with provider, upstream model, alias and execution-definition digest.

#### Scenario: Model asks for the calendar

- **WHEN** a call with a `get_events` tool makes the model call it
- **THEN** the result holds the tool call and an identity-bound continuation

### Requirement: Loops continue from an opaque continuation

A call SHALL accept its returned continuation and function-call outputs, pass its
provider items unchanged, and refuse continuation identity that differs from the
resolved provider/model/alias/definition digest with `422 continuation_mismatch`.

#### Scenario: Second turn

- **WHEN** a project sends the previous continuation and its function output to
  the same resolved definition
- **THEN** provider items are passed through unchanged followed by the function output

#### Scenario: Mismatched continuation

- **WHEN** a continuation from a stub alias is submitted to an OpenAI alias
- **THEN** the call is refused with `continuation_mismatch`

#### Scenario: Different alias for the same upstream model

- **WHEN** a continuation is submitted to a different execution-definition alias
- **THEN** it is refused even if the upstream model name matches

## ADDED Requirements

### Requirement: Registered provider credentials select paid execution

New paid calls SHALL retrieve the authenticated project's registered provider
credential through the registry. OpenAI SHALL be the initial paid adapter. Adapter
selection SHALL come from the resolved model definition, and future adapters SHALL
preserve the gateway result, certainty and settlement contracts.

#### Scenario: Project-owned OpenAI execution

- **WHEN** a project calls a paid OpenAI alias with a registered credential
- **THEN** its request uses that credential, `store: false`, disabled SDK retries
  and the stable client request identifier

### Requirement: Finished outcome replay precedes live resolution

For an existing terminal call ID, the gateway SHALL check authenticated ownership
and the original request fingerprint and then return its recorded outcome without
requiring a live model definition, profile or provider credential. A different
request SHALL fail with `idempotency_conflict` and never execute.

#### Scenario: Retired model replay

- **WHEN** a completed call is resubmitted unchanged within retention after its
  model was retired
- **THEN** its original result is replayed without resolution or execution

#### Scenario: Unknown execution after credential rotation

- **WHEN** an unchanged unknown-execution call is resubmitted after credential rotation
- **THEN** its recorded uncertainty is returned and the replacement key is not used
