# Proposal

## Why

The gateway's shared OpenAI credential and provider/model pairs cannot represent
project-owned credentials and a common catalogue for local and paid inference.
Projects need stable model aliases and accurate supported-field discovery before
the separate LLM, embedding and OCR integrations can share the gateway safely.

## Expected Outcome

A registered project authenticates with its existing project access key, discovers
registered models and their request schemas, and selects a fixed alias or a profile
that names one. Its paid LLM calls use its own encrypted, registry-managed OpenAI
credential. Invalid fields fail before provider execution, and results retain the
existing streaming, spend-control and encrypted 24-hour replay behavior.

This is the shared foundation. Embedding/OCR execution and the local LLM adapter
are later changes under the approved phased plan.

## What Changes

- Replace the single models document with one strict definition per fixed alias,
  a task-aware adapter registry, immutable alias bindings, and generated effective
  request schemas used for both discovery and server validation.
- Add authenticated `GET /v1/models?task=llm` and
  `GET /v1/models/{alias}`. Catalogue all registered models subject to existing
  development-only restrictions; report configuration availability separately.
- Add write-only provider-credential management to the project registry CLI/menu.
  Store authenticated ciphertext separately from contract versions; isolate
  credentials by project/provider and audit metadata without secrets.
- **BREAKING:** replace explicit provider/model objects with model alias strings;
  move project profiles to contract version 2 with alias-backed parameters. Add an
  explicit registry upgrade command that appends versions without rewriting history.
- **BREAKING:** remove `/v1/catalogue` and the process-wide OpenAI key execution
  path. No shared-key fallback or inference-request provider-key field is provided.
- Bind new admissions to task, resolved model definition and credential revision.
  Replay ended calls using their recorded outcome; refuse a retryable call if a
  credential or effective profile changed before another execution attempt.
- Keep paid global/per-project monetary enforcement and give explicitly zero-fee
  local/stub calls an independent path through non-monetary grant checks.
- Update the typed Python client and endpoint OpenAPI contracts together. Final
  consumer onboarding prose follows backend maturity; consumer code is out of scope.

## Non-goals

Local service provisioning, model selection, hardware checks, embedding/OCR
execution endpoints, additional paid providers, live paid tests, automatic routing,
vector indexes, receipt interpretation, project pipelines, production operations,
and personal-agent edits. There is no legacy API compatibility layer.

## Capabilities

### New Capabilities

- `project-provider-credentials`: encrypted project/provider credentials,
  write-only management, isolation, removal/rotation and safe failure behavior.

### Modified Capabilities

- `model-catalogue`: task-aware definitions, fixed aliases, model discovery and
  executable supported-field schemas.
- `project-contracts`: explicit version upgrade and alias-backed profiles.
- `llm-calls`: alias resolution, per-project credentials and resolved identity.
- `call-admission`: resolution/credential binding and zero-fee monetary bypass.
- `usage-ledger`: resolved task/model/credential metadata without credential content.
- `python-client`: model discovery, alias calls and the new typed errors.

The framework capabilities remain database, api-consumers, llm, personal-data,
background-jobs and money. No new framework capability is introduced. Existing
privacy, migration, API/client, integrity and verification standards apply to the
new secret store and the generalized call contract.

## Impact

Affected code: catalogue loading/pricing, request resolution and provider adapters,
project registry/CLI, admission and ledger metadata, application dependency wiring,
settings/Compose materialization, the Python client and contract tests. Add new
forward-only migrations; never edit applied migrations or existing price history.
Update the project scope and OpenSpec context alongside implementation.

Existing project keys, grant balances, ledger rows, contract histories and replay
results are preserved. Owners explicitly upgrade active contracts and register
provider credentials before using the replacement API. Existing consumers require
a matching client upgrade and selection changes; this proposal does not perform
that migration or deploy anything.
