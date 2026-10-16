# Design

## 1. Keep endpoint implementations focused

Extend /v1/responses in this change. Embeddings and OCR have separate proposals,
request/result types and adapters. Reuse registry, auth, admission, settlement and
replay rather than duplicating those invariants in each router.

Rejected: one generic inference body containing every operation's fields. It makes
validation and discovery less precise and couples unrelated endpoint releases.

## 2. Configure an HTTP boundary

OLLAMA_BASE_URL is optional and backend-owned. When unset, local aliases remain
discoverable but unavailable; OpenAI and the stub operate independently. Reject
URLs containing userinfo, query strings or fragments. Disable redirects and ambient
proxy configuration for local HTTP calls. Per-attempt clients have no project paid
credential. No network operation runs while an admission/registry lock is held.

The adapter targets the GPU guard contract: readiness must report gpu_only and the
pinned Ollama runtime must match. It never selects the raw engine, GPU device or CPU.
Compose network attachment is an optional operator override, not a dependency on
one machine or a mandatory external network for ordinary gateway development.

Rejected: importing inference libraries into the gateway, host networking as the
default, or treating an OpenAI-compatible URL as the Responses API.

## 3. Preserve immutable definitions

Catalogue schema 2 adds an explicit local execution contract: runtime version,
artifact digest, template digest, default-system allowance, conservative template
overhead and deterministic temperature default. Existing schema-1 canonical fields
are frozen so adding this schema cannot reassign existing registered aliases.
Local definitions select the registered ollama-native adapter and logical ollama
deployment. New capabilities/configuration require replacement aliases.

Before generation, bounded metadata calls verify GPU-only readiness, runtime,
upstream model digest and template. A mismatch returns a terminal, non-retryable
provider_configuration_mismatch with no inference content sent. Metadata calls
are read-only; active model tags may only be changed by draining service execution
and registering replacement aliases, never by mutating an admitted model in place.

## 4. Preserve supported request semantics

Initial definitions support text, text messages, leading developer instructions,
temperature, bounded output, schema output and streaming. Text parts concatenate
in order with newlines. Developer messages must precede user/assistant messages;
the final message must be a user message. Instructions become a leading system
message. These restrictions are published as semantic schema constraints.

No reasoning, tools, continuations, image/PDF input or prompt_cache_key is accepted.
Every explicit unsupported field fails through the existing effective schema.

Ollama 0.5.11 can drop older messages when a prompt exceeds context. For the pinned
templates, reserve a conservative UTF-8-byte upper bound: content bytes plus a fixed
allowance and a per-message allowance, plus maximum generated tokens, must fit the
configured context. Bounds include default system text and message joining. These
over-reserve token space intentionally; a bytes/4 heuristic would not prove safety.
Pinning the template is necessary for this bound. Oversize calls fail before
admission. There is no claim of accepting every prompt that could fit with an exact
tokenizer. Context and generation limits are sent explicitly on every execution.

Schema documents must be valid and bounded, with no external references. Output
schema validation runs locally for this adapter; a completed but nonconforming
answer reports structured_output_error without rerunning inference.

## 5. Classify certainty from the service contract

Only fixed, documented pre-generation refusals (GPU unavailable, busy, failed empty
model load) allow same-ID retries. A generic 503, proxy timeout, malformed response,
EOF before a terminal event or a failure after generation may have started is
execution_unknown. Never copy the paid adapter's status-only retry rules.

Connection failure before generation is retryable; send/read ambiguity is unknown.
No transport or adapter automatically retries inference. Metadata failures are safe
to retry because no content was submitted. Bound both body size and the complete
attempt duration; the duration remains below the admitted call's lease.

Only gateway-owned failure codes escape the adapter. Provider error prose is not
logged or returned. The new configuration problem is mapped in both API and client.

## 6. Reuse settlement and replay

The adapter emits ProviderSuccess or ProviderFailure and native usage counts, then
the existing service settles and stores the existing result shape. Zero provider
fee bypasses monetary checks but retains request/concurrency grants. Paid behavior
and the USD 5 cap are unchanged. Unknown local executions still become terminal
with zero provider fee. Completed/failed replay precedes current runtime checks.

Streams translate native chunks into existing gateway events. Missing terminal
events are unknown. Client disconnect detaches the consumer while the producer
finishes settlement/replay. No second producer is started for the same ID.

## 7. Migration and recovery

No SQL migration or contract upgrade is needed. Run models sync to register new
aliases using the existing command. Existing v1 digests, histories and money stay
unchanged. Disable new local execution by removing its configured URL; terminal
outcomes remain replayable. Never rewrite alias bindings or ledger rows to roll back.

## 8. Verification

Synthetic HTTP fixtures cover native translation, metadata mismatch, confirmed
refusal versus ambiguity, truncated/malformed streams, bounds and cleanup. Real
disposable PostgreSQL covers admission, zero-fee settlement, unknown no-reexecution,
replay and API/client behavior. A bounded local HTTP smoke uses generated project
keys and synthetic inputs in an isolated schema, with no real credentials or paid
calls. Package verification keeps inference dependencies outside the gateway image.

Official sources reviewed 2026-10-16:

- https://github.com/ollama/ollama/blob/v0.5.11/docs/api.md
- https://github.com/ollama/ollama/blob/v0.5.11/server/prompt.go
- https://github.com/ollama/ollama/blob/v0.5.11/template/template.go

The prepared service's current source and model metadata supplement provider docs;
they establish its GPU guard error codes and model/template digests, not a universal
promise about arbitrary Ollama installations.
