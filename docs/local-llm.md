# Local LLM execution

Local text generation uses `POST /v1/responses`. Discovery remains
`GET /v1/models?task=llm` and `GET /v1/models/{alias}`. Embedding and OCR execution
are separate subsequent changes, with their own request and result contracts.

## Service configuration

Set the backend's optional `OLLAMA_BASE_URL` to the GPU guard service, using an
address reachable from the gateway API container. The adapter requires the
`ollama-gpu-native-v1` contract: GPU-only readiness, pinned runtime/model metadata
and native `/api/chat`. An unguarded Ollama engine does not satisfy this contract.
Inference libraries, model files and GPU drivers belong to the separate service.

HTTP and HTTPS are supported. URLs must not contain credentials, query strings
or fragments. Redirects and ambient HTTP proxies are disabled. The URL is never
included in model discovery. `LLM_CALL_TIMEOUT_SECONDS` bounds the entire attempt,
including metadata checks; its default is 180 seconds.

If containers need a shared network, copy `compose.inference.example.yaml` to the
ignored `compose.inference.local.yaml`, set `INFERENCE_NETWORK` to the separately
managed service network, and include both Compose files in operator commands:

```sh
docker compose -f compose.yaml -f compose.inference.local.yaml config -q
```

The ordinary Compose configuration and automated checks need no inference network
or GPU. An unset URL leaves local aliases discoverable with
`provider_not_configured`; configured availability indicates configuration, not
live GPU readiness. The gateway's readiness does not depend on this service.

During an owner-approved rollout, the existing Compose migration step runs
`llm-gateway-admin models sync`. There is no new SQL migration or contract upgrade.
Removing the URL disables new local execution while terminal calls remain
replayable. Never edit registered alias bindings to change or remove their history.

## Models and supported input

The initial zero-fee aliases are:

| Alias | Context | Maximum output |
| --- | --- | --- |
| `ollama-qwen2.5-3b-instruct-q4km-v1` | 4096 tokens | 1024 tokens |
| `ollama-llama3.2-3b-instruct-q4km-v1` | 4096 tokens | 1024 tokens |

Definitions pin Ollama 0.5.11, the artifact digest, template and default system
text. The adapter verifies these before submitting inference content. Replacing
any of them requires a reviewed replacement alias. Drain service execution before
changing model tags; do not mutate tags while admitted calls can run.

Use existing project access keys and a direct alias or registry-managed profile.
Local execution requires no paid-provider credential. It has zero provider fee
and obeys request and concurrency grants even with a zero monetary budget.

Example request body:

```json
{
  "provider_call_id": "example-1",
  "operation": "summarise",
  "model": "ollama-qwen2.5-3b-instruct-q4km-v1",
  "input": "Summarise this synthetic example in one sentence.",
  "max_output_tokens": 128
}
```

Supported fields include text, text messages, leading developer messages,
instructions, temperature, bounded output, JSON Schema output and streaming.
Temperature defaults to zero; the standard service tier is the only supported
tier. Developer messages must precede conversation messages, and input must end
with a user message. Text parts join in order with newlines; instructions become
a leading system message. Reasoning, tools, continuations, images, PDFs and prompt
cache settings are rejected before admission. Explicit unsupported fields are
rejected even when their value is null or false.

Ollama can otherwise truncate old messages. For these pinned templates, admission
uses this conservative bound:

```text
512 + sum(UTF-8 content bytes + 128 for each native message)
    + maximum output tokens <= 4096
```

This reserves space for the template, default system text and role separators.
It deliberately accepts fewer prompts than exact tokenization might allow.
The effective discovery schema publishes these semantic constraints.

Output schemas must describe an object, fit 16 KiB, have depth at most 32 and
contain no references or regex keywords. Completed output is also validated by the
gateway. Invalid JSON or schema mismatch appears in `structured_output_error`;
the call stays completed and is not executed again. Output limits can still
produce an incomplete response.

## GPU policy, errors and recovery

The separate GPU guard enforces full GPU inference and refuses execution when
the GPU cannot run it. CPU work for tokenization, scheduling and transport remains
bounded by that service's resource limits. The gateway neither selects hardware
nor falls back to a different model, CPU inference or a paid provider.

| Situation | Gateway outcome | Same-ID execution retry |
| --- | --- | --- |
| Missing local configuration | `provider_not_configured` | No automatic retry |
| Runtime/artifact/template mismatch | `provider_configuration_mismatch` | Never; fix configuration and use a fresh ID |
| Metadata unavailable, connection never established, or guard confirms GPU/busy/empty-load refusal | `provider_unavailable` | Allowed after backoff |
| Generic server error, ambiguous send/read, malformed response, missing native usage or missing stream terminal | `execution_unknown` | Never |

The adapter uses fixed guard error codes to establish a pre-generation refusal;
HTTP status alone does not prove that inference was refused. Provider prose is not
returned or logged. No request path retries inference internally.

Streams use existing gateway events. A disconnected consumer leaves its producer
running through settlement and encrypted result storage. Reuse the same ID and
unchanged request for recovery: completed calls replay without contacting the
service, and unknown executions remain unknown. Retention defaults to 24 hours;
the ledger retains certainty after result expiry. Prompts are not stored for replay.

Gateway and Python client are released together as version `0.5.0`.
