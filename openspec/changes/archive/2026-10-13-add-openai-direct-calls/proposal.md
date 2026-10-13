# Proposal

## Why

This is the change that makes the gateway useful: projects can now run model
calls through it. Each call is checked against the catalogue and the
project's grant, executed once, classified honestly when it fails, recorded
in the ledger, and safe to resubmit when a connection drops.

## Expected Outcome

- A project sends `POST /v1/responses` with a stable call ID, an operation
  name, a profile (or an explicit catalogue model), and its input: text,
  images, or PDFs. It gets back:
  - the answer as text, or as parsed JSON for a JSON schema;
  - the usage and its cost;
  - its remaining allowance.
- **Resubmitting a completed call ID** within 24 hours returns the same
  answer, with no second provider call and no second charge.
- **A call whose provider outcome is unknown** is never silently repeated.
  The project is told so, and it needs a new call ID to try again.
- **Development keys can use the zero-cost stub model**, which returns a
  scripted answer or a scripted failure, so projects can test their whole
  flow for free.

## What Changes

- **`POST /v1/responses` (non-streaming).** The request holds:
  - `provider_call_id`, `operation`;
  - `profile` or `model`;
  - `input` (a string, or messages with text, image, and file parts);
  - `instructions`, `text_format` (text or a strict JSON schema);
  - `max_output_tokens`, `reasoning_effort`, `temperature`;
  - an optional `prompt_cache_key` (default `<project>:<operation>`);
  - for the stub only, a `stub` script.
- **OpenAI adapter** (official `openai` SDK):
  - `store: false` always;
  - SDK retries off;
  - `X-Client-Request-Id` set to the call ID;
  - a timeout of `LLM_CALL_TIMEOUT_SECONDS`;
  - rate-limit headers captured.
- **Failure classification** with execution certainty. A confirmed refusal
  is retryable under the same ID. A send that may have reached the provider
  is `execution_unknown`, never retried. Account problems (authentication,
  quota, billing) are critical alerts.
- **Encrypted result storage:** Fernet, `RESULT_ENCRYPTION_KEY`, kept 24
  hours, so completed calls can be replayed. The worker deletes expired
  results.
- **Stub provider** `stub/stub-echo`, for development keys only.
- **Settings:** `OPENAI_API_KEY`, `RESULT_ENCRYPTION_KEY`,
  `RESULT_RETENTION_SECONDS`, `REQUEST_MAX_BYTES`, and
  `DEFAULT_MAX_OUTPUT_TOKENS`.

## Non-goals

- Streaming and tools (phase 3).
- Flex, background jobs, and provider pushback reallocation (phase 4).
- Retrying inside the gateway. A temporary refusal is returned as
  retryable, and the client retries.
- Per-contract input limits. One global `REQUEST_MAX_BYTES` applies for now;
  per-contract limits come as an additive field when needed.

## Capabilities

### New Capabilities

- `llm-calls`: the call request and response, model and profile
  resolution, provider execution, failure classification, replay of
  recorded outcomes, result retention, and the stub provider.

### Modified Capabilities

None.

## Impact

- **Code:**
  - new `llm` package (request models, resolution, the OpenAI and stub
    adapters, the failure policy, the result store);
  - the route;
  - worker result cleanup;
  - migration `0005` (encrypted results).
- **Dependencies:** `openai` (which uses `httpx2`) and `cryptography`.
- **Capabilities:**
  - `personal-data`: prompts pass through, never logged. Results are stored
    encrypted for 24 hours, then deleted. OpenAI storage is disabled.
  - `llm`: content-free logs, an explicit model on every call, and output
    validation (JSON parsing and refusal detection).
- **Contract and client package:** the API the phase 2 client wraps.
- **Depends on:** `add-model-catalogue-and-pricing`,
  `add-call-admission-and-ledger`.
