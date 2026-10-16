# local-llm-execution Specification

## Purpose
Execute registered local language models through the responses API using portable
HTTP configuration, GPU-only inference, explicit identity and safe recovery.

## Requirements

### Requirement: Local LLM execution uses the existing responses contract

The gateway SHALL execute registered local LLM aliases through POST /v1/responses,
with existing authentication, grants, typed results, streaming and encrypted replay.
It SHALL NOT select another model or paid provider when local execution is unavailable.

#### Scenario: Local execution without a paid credential

- **WHEN** an authenticated project selects a configured local alias
- **THEN** the call uses no paid-provider credential, obeys request/concurrency grants,
  and settles at zero provider fee even when the monetary budget is exhausted

### Requirement: Runtime configuration is portable and private

Service URLs SHALL be backend settings, excluded from public discovery and model
definitions. Missing local configuration SHALL affect only local execution. The
gateway SHALL depend on HTTP rather than inference libraries or GPU installation.

#### Scenario: Missing local URL

- **WHEN** OLLAMA_BASE_URL is absent
- **THEN** local models report provider_not_configured without preventing paid/stub use

### Requirement: Local execution identity is verified

Local definitions SHALL pin the runtime contract, artifact and template. The adapter
SHALL verify these through bounded metadata requests before sending inference
content. A mismatch SHALL produce non-retryable provider_configuration_mismatch.
Catalogue evolution SHALL preserve all existing schema-1 definition digests.

#### Scenario: Runtime tag changed

- **WHEN** a configured model tag resolves to a different artifact digest
- **THEN** inference content is not submitted, the call records a configuration failure,
  and the Python client does not automatically retry it

#### Scenario: Existing alias after catalogue extension

- **WHEN** the local catalogue schema is installed
- **THEN** existing OpenAI and stub aliases retain their original bound digests

### Requirement: Local input cannot be silently truncated or weakened

The effective schema SHALL reject unsupported fields before admission. Local
semantic validation SHALL require leading developer messages, a final user message,
valid bounded schemas without external references and a conservative prompt bound
covering content, instructions, template overhead and maximum generated tokens.
The adapter SHALL send the declared context and output limits explicitly.

#### Scenario: Unsupported local reasoning

- **WHEN** a non-reasoning alias receives an explicit reasoning field
- **THEN** the request is refused before admission and before inference execution

#### Scenario: Input exceeds the safe context bound

- **WHEN** the conservative complete-prompt bound exceeds the local context
- **THEN** the gateway returns a validation problem without admitting the call

### Requirement: Local failures preserve execution certainty

Only confirmed pre-generation refusals SHALL allow retrying the same ID. Generic
server errors, ambiguous sends, timeouts after generation submission and missing or
malformed terminal responses SHALL become execution_unknown. Provider prose SHALL
not be returned or logged. CPU execution or model fallback SHALL not be attempted.

#### Scenario: GPU unavailable before generation

- **WHEN** the prepared GPU guard confirms GPU unavailability before execution
- **THEN** the gateway records a retryable provider_unavailable outcome with zero fee

#### Scenario: Ambiguous local failure

- **WHEN** a response is lost after generation was submitted
- **THEN** the gateway records execution_unknown and never executes that call ID again

### Requirement: Local streams and completed results retain recovery behavior

The adapter SHALL translate native chunks into gateway events and require a valid
terminal outcome. Client disconnection SHALL not abandon the gateway producer.
Completed/failed replay SHALL use recorded outcomes before checking current service
availability. Schema-nonconforming completed output SHALL be reported explicitly
without rerunning inference.

#### Scenario: Replay while local service is unavailable

- **WHEN** an unchanged completed call is resubmitted within result retention
- **THEN** it replays its recorded result with no metadata or inference request

#### Scenario: Stream ends early

- **WHEN** a native stream ends without a valid terminal outcome
- **THEN** the gateway records execution_unknown and preserves no-reexecution
