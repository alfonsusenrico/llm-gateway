# Proposal

## Why

Registered projects can use paid OpenAI and the stub, but cannot execute local
language models through the gateway. The prepared inference service exposes a
GPU-enforced native Ollama contract that needs explicit translation, identity
checks and conservative failure handling.

## Expected Outcome

A project selects a registered local alias through POST /v1/responses and receives
the existing typed direct or streamed result, with zero provider fee, enforced
grants and encrypted replay. Missing GPU capacity refuses execution without CPU,
paid-provider or model fallback. Configuration comes from backend settings, so the
same gateway code can connect to a compatible service on another computer.

## What Changes

- Add a native Ollama adapter for the prepared GPU-only service contract.
- Add optional OLLAMA_BASE_URL and environment/Compose wiring without embedding a
  destination or GPU dependency in gateway code or public discovery.
- Add versioned local model definitions binding artifact/template identity and
  conservative context limits; preserve every existing v1 definition digest.
- Translate text/messages/instructions, temperature, output limits, JSON schema and
  NDJSON streams. Reject unsupported fields and unsafe input sizes before admission.
- Verify runtime contract, model digest and template before sending inference content.
- Preserve zero-fee accounting, request/concurrency grants, detached execution,
  terminal replay, confirmed-refusal retry and execution_unknown behavior.
- Add a non-retryable configuration-mismatch problem and its matching client mapping.
- Keep the Python client and generated OpenAPI aligned with the additive capability.

## Non-goals

Embedding/OCR endpoints, new paid providers, model selection/fallback, model or GPU
installation, service deployment, project workflows, tool execution, vector storage,
consumer migration and live paid calls. Separate endpoint changes follow this one.

## Capabilities

### New Capabilities

- `local-llm-execution`: portable GPU-only native Ollama execution through responses.

### Modified Capabilities

None. Existing LLM, catalogue, admission, ledger and client guarantees continue to
apply. The new capability specifies their application to the local adapter.

No framework capability is added. Existing API, privacy, money, integrity and
verification rules apply. Client/request shapes remain compatible; discovery gains
local aliases and a newly mapped configuration problem. Gateway/client versions
are released together. No database migration or price-history edit is required.

## Impact

Catalogue schema/definitions, settings and Compose, adapter registration/wiring,
local request validation, failure mapping, client errors and focused tests. The
gateway image remains free of Torch, CUDA, OCR libraries and model weights.
