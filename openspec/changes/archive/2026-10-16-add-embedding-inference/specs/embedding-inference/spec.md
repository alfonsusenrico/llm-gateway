## ADDED Requirements

### Requirement: Embeddings have an independent typed endpoint

The gateway SHALL expose POST /v1/embeddings with explicit registered model/profile
selection, bounded nonempty ordered text inputs and task-specific request/result
schemas. Unsupported fields SHALL fail before admission. Projects own vector storage.

#### Scenario: LLM-only settings sent for embeddings

- **WHEN** a request includes streaming, reasoning or tools
- **THEN** it is rejected without admission or provider execution

### Requirement: Vector identity and native output are verified

Definitions SHALL bind artifact/preprocessing identity, dimensions and normalization.
The gateway SHALL validate vector count/index coverage, finite components, dimensions,
usage and model identity. Local content SHALL be sent only after matching CUDA/model
metadata; changed identity SHALL cause a configuration failure.

#### Scenario: Local model was replaced

- **WHEN** live metadata differs from the registered definition
- **THEN** the call fails without sending input to inference

#### Scenario: Provider returns a malformed vector or absent usage

- **WHEN** a submitted execution returns invalid native output
- **THEN** the outcome is execution_unknown and cannot execute again under that ID

### Requirement: Embeddings share limits and conservative paid accounting

Calls SHALL obey existing project grants, credential isolation and the global paid
cap. Paid reservation SHALL use a backend-derived upper bound on input tokens.
Local zero-fee calls SHALL retain request/concurrency limits. Historical prices and
schema-1/2 definition digests SHALL remain unchanged.

#### Scenario: Exhausted monetary budget with local inference

- **WHEN** a valid local call has available rate/concurrency grants
- **THEN** it can execute and settle at zero fee

#### Scenario: Paid worst-case cost exceeds the remaining cap

- **WHEN** an embedding request cannot fit its conservative reservation
- **THEN** it is refused before provider submission

### Requirement: Replay is durable and task isolated

Successful settlement, ledger event and encrypted result SHALL commit atomically.
Replay SHALL precede current provider availability checks. Cross-task call-ID reuse
SHALL conflict. Disconnected consumers SHALL not abandon execution ownership.

#### Scenario: Call ID was used for a response

- **WHEN** the same project/environment submits that ID to embeddings
- **THEN** idempotency_conflict is returned without replaying or executing a vector call

#### Scenario: Encrypted result persistence fails

- **WHEN** persistence fails during successful settlement
- **THEN** neither completed state nor its settlement event commits without the result

### Requirement: Embedding inference has no implicit fallback or stored conversation

Local GPU unavailability SHALL refuse inference without CPU/model/paid fallback.
OpenAI credentials SHALL be per project. The stateless Embeddings API MAY omit its
unsupported store parameter; no provider application state SHALL be enabled.
Only confirmed refusals SHALL be retryable; ambiguous submissions SHALL stay unknown.

#### Scenario: GPU unavailable before encoding

- **WHEN** the local service confirms gpu_unavailable
- **THEN** the gateway returns a retryable refusal and does not choose another provider
