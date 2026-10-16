## MODIFIED Requirements

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

## ADDED Requirements

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
