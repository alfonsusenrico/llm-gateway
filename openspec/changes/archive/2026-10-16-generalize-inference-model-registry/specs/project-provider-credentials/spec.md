## Purpose

Let the owner manage each project's provider credentials through the registry,
with encrypted storage, project isolation and no secret readback or export.

## ADDED Requirements

### Requirement: Credentials are encrypted outside contract history

The registry SHALL store provider credentials as authenticated ciphertext scoped
to a project and provider, separately from contract versions. Contract exports,
history, API responses, logs and alerts SHALL contain neither plaintext credentials
nor their ciphertext. Metadata SHALL contain no key prefix or suffix.

#### Scenario: Registered credential

- **WHEN** the owner registers a provider credential for a project
- **THEN** the database holds ciphertext and safe revision metadata, and contract
  export, history and captured logs contain no credential material

### Requirement: Credential management is write-only and audited

The admin CLI and menu SHALL support setting, replacing, removing and listing safe
credential metadata. Secret input SHALL use a non-echoing prompt or explicit stdin,
never a command-line value. Each mutation SHALL record actor, project, provider,
time and a fresh credential revision without recording credential content.

#### Scenario: Replace through the menu

- **WHEN** the owner replaces a project's credential
- **THEN** input is hidden, a new revision is recorded, and the old secret cannot
  be retrieved through registry commands

#### Scenario: Provider key in command arguments

- **WHEN** credential management is invoked with an API-key value argument
- **THEN** that invocation is rejected without printing the supplied value

### Requirement: Inference uses the authenticated project's credential

A paid inference attempt SHALL use only the registered credential for its
authenticated project and selected provider. It SHALL NOT use another project's
credential, an inference-request credential or a process-wide fallback. Credentials
SHALL be forwarded only to the backend-configured provider destination.

#### Scenario: Concurrent projects

- **WHEN** two projects call the same OpenAI model concurrently
- **THEN** each provider request uses its project's credential and no authenticated
  client state leaks between the requests

#### Scenario: Missing credential

- **WHEN** a new paid call has no registered credential for its provider
- **THEN** it fails with non-retryable `503 provider_credentials_missing`, before
  admission or provider execution

#### Scenario: Inference request supplies a credential

- **WHEN** an inference body contains a provider credential field
- **THEN** it fails validation without echoing the field value

### Requirement: Credential replacement does not redirect an admitted call

An admitted attempt SHALL retain its selected credential revision through
execution. A confirmed-refusal retry SHALL fail with non-retryable
`409 credential_changed` if its project's credential was replaced or removed.
Terminal outcome replay SHALL require gateway authentication but not the current
provider credential.

#### Scenario: Rotation during execution

- **WHEN** the owner replaces a credential while an admitted call is running
- **THEN** that attempt keeps its original credential, and subsequent new calls use
  the replacement

#### Scenario: Retry after rotation

- **WHEN** a temporarily refused call is resubmitted after credential replacement
- **THEN** it returns `credential_changed` without another provider execution

#### Scenario: Replay after removal

- **WHEN** a completed call is resubmitted unchanged within retention after its
  provider credential was removed
- **THEN** the authenticated project receives its recorded result without a provider call

### Requirement: Removed or unreadable credentials fail locally

Removal SHALL erase the stored ciphertext and retain only audit/revision metadata.
Decryption or ownership-binding failure SHALL return non-retryable
`503 provider_credentials_unreadable`, alert with identifiers only, and SHALL NOT
prevent unrelated projects or zero-fee models from operating.

#### Scenario: Ciphertext moved between projects

- **WHEN** a project's credential row contains ciphertext bound to another project
- **THEN** execution is refused with `provider_credentials_unreadable` without
  contacting a provider or exposing either credential

### Requirement: Provider credential failures are safely classified

A provider authentication refusal SHALL remain distinct from gateway-key
authentication errors, SHALL not be retried automatically, and SHALL expose only
the gateway problem code and correlation identifiers. Alerts SHALL identify the
affected project and provider without provider error prose.

#### Scenario: Invalid registered OpenAI key

- **WHEN** OpenAI rejects a registered credential
- **THEN** the call records a confirmed failure with zero charge, returns
  `provider_account_blocked`, and no provider message reaches logs or the caller

### Requirement: Existing shared credentials are never imported implicitly

Upgrades SHALL preserve existing gateway project keys but SHALL NOT copy a
process-wide provider credential into project records. The owner SHALL register
each project's credential explicitly through the registry.

#### Scenario: Upgraded project without a credential

- **WHEN** a project's contract is upgraded but no provider credential is registered
- **THEN** gateway authentication still works, zero-fee calls remain available,
  and a new paid call returns `provider_credentials_missing`
