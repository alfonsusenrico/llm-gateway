## MODIFIED Requirements

### Requirement: Catalogue lists models with their limits

The gateway SHALL hold reviewed, strict model definitions identified by alias,
task, provider, upstream model, adapter and definition digest. Each definition
SHALL declare execution kind, supported inputs/settings, defaults, limits, pricing
basis and development-only status. Task-specific attributes SHALL describe only
the capabilities the selected adapter can preserve.

#### Scenario: Invalid catalogue file

- **WHEN** a definition contains an unknown field, missing value or setting its
  adapter cannot implement
- **THEN** catalogue validation fails before inference, naming fields without
  exposing private runtime configuration

### Requirement: Every offered model has a current standard price

A registered model SHALL have a current standard price when paid, or an explicit
zero provider fee when local/stub. Price entries for the same provider, model,
tier and effective date SHALL NOT conflict. Missing paid pricing SHALL never be
interpreted as a zero-fee declaration.

#### Scenario: Model added without a price

- **WHEN** a paid model has no standard price in effect
- **THEN** catalogue validation fails

#### Scenario: Zero-fee model

- **WHEN** a local/stub definition explicitly declares zero provider fee
- **THEN** it is discoverable without inventing a paid price or source URL

### Requirement: Profiles are checked against the catalogue when saved

Saving a new or changed profile SHALL fail when its alias is unregistered, its
parameters contain unsupported fields or values, or the resulting settings break
the selected model's conditional rules or limits.

#### Scenario: Temperature with reasoning on gpt-6-luna

- **WHEN** a profile for the gpt-6-luna alias sets effort `low` and temperature 0.7
- **THEN** the edit is refused, explaining the requirement for effort `none`

#### Scenario: Unknown model

- **WHEN** a profile names an unregistered alias
- **THEN** the edit is refused without changing the current contract

### Requirement: Projects can read the catalogue for their key

Authenticated `GET /v1/models` SHALL return registered model summaries, optionally
filtered by task, omitting development-only models for production keys. Each
summary SHALL expose alias, task, resolved identity, public limits/capabilities,
pricing and configuration availability, without secrets or private destinations.
All projects SHALL have the same model selection subject to those restrictions.

#### Scenario: Production key

- **WHEN** a production key lists models
- **THEN** the result includes registered production models and omits development-only stubs

#### Scenario: Development key

- **WHEN** a development key lists models
- **THEN** the result can include development stubs with explicit zero provider fee

#### Scenario: Missing project provider credential

- **WHEN** a project without an OpenAI credential lists registered OpenAI models
- **THEN** those models remain listed with unavailable configuration and
  `provider_credentials_missing`, without a provider request

#### Scenario: Task filter

- **WHEN** a project lists models with `task=llm`
- **THEN** only LLM definitions are returned; an unknown task is rejected with
  `422 request_invalid`

### Requirement: Catalogue records function tool support

Each LLM model SHALL declare whether it supports function tools. Model discovery
and its effective request schema SHALL agree with that declaration.

#### Scenario: Catalogue read

- **WHEN** a project reads the gpt-6-luna definition
- **THEN** function tools are advertised and its request schema includes tool fields

## ADDED Requirements

### Requirement: Registered aliases cannot be reassigned

Registering a new alias SHALL persist its canonical execution-definition digest.
Reusing a registered alias for a different task, adapter, upstream model or
output-affecting configuration SHALL fail, even after retirement. Replacements
SHALL use new aliases. Documentation and effective-dated pricing updates SHALL NOT
change the bound execution definition.

#### Scenario: Changed embedding configuration

- **WHEN** registration attempts to reuse an alias with different normalization
- **THEN** it fails atomically and preserves the existing alias binding

#### Scenario: Retired alias reused

- **WHEN** an alias removed from discovery is later submitted with another definition
- **THEN** registration refuses the reassignment

### Requirement: A model exposes its effective request schema

Authenticated `GET /v1/models/{alias}` SHALL return the definition's public details
and effective direct-call JSON Schema. The schema SHALL include allowed fields,
types, defaults, bounds and conditional restrictions and reject unknown fields.
Server-side validation of the resolved call SHALL use that same effective schema.

#### Scenario: Non-reasoning model

- **WHEN** the selected definition has no reasoning support
- **THEN** discovery excludes reasoning fields and a call supplying them is rejected
  before admission or provider execution

#### Scenario: Conditional setting

- **WHEN** temperature is valid only with a particular reasoning effort
- **THEN** the schema expresses the condition and the gateway enforces it

#### Scenario: Unknown or hidden alias

- **WHEN** model details are requested for an unknown alias or a development-only
  alias using a production key
- **THEN** the result is `404 model_not_found`

### Requirement: Model definitions select a supported execution contract

Each definition SHALL name a registered task-compatible adapter and a
backend-configured destination reference. Client requests SHALL NOT choose
adapters or destinations. Models SHALL not be advertised as usable when their
execution contract is unavailable, and unrecognized adapters SHALL fail catalogue
validation rather than silently use another implementation.

#### Scenario: Uninstalled provider adapter

- **WHEN** a definition selects an adapter the gateway does not implement
- **THEN** catalogue validation fails; adding a JSON file does not enable that provider

### Requirement: Model registration is checked before use

The gateway SHALL refuse new calls when a loaded alias has not been registered
with its exact execution-definition digest. Model registry synchronization SHALL
be atomic and SHALL expose identifiers and counts only.

#### Scenario: Definition deployed without registration

- **WHEN** a new call selects a valid definition whose binding has not been synchronized
- **THEN** it fails with `503 model_registry_unavailable` before provider execution

### Requirement: Legacy catalogue discovery is removed

The gateway SHALL expose model discovery through `/v1/models` and model details,
and SHALL NOT retain `/v1/catalogue` as a compatibility endpoint.

#### Scenario: Old catalogue request

- **WHEN** a project requests `/v1/catalogue` after the API upgrade
- **THEN** the route is unavailable and the replacement client uses `/v1/models`
