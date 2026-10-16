# Verification: add-ollama-llm-provider

- Date: 2026-10-16; development environment only.
- Branch: `feat/ollama-llm-provider`, based on `437b6e7`.
- Scope: the owner's instruction to continue implementation by focused endpoint;
  this change implements local LLM execution on `/v1/responses`.
- Gateway/client version: `0.5.0`, released together. No new SQL migration.
- No paid calls, production deployment, real credential registration, consumer
  migration or application-database changes were performed.

## Checks

| Check | Result |
| --- | --- |
| Complete repository gate | `./scripts/check`: passed; lock, format, Ruff, Pyright (0 errors), 769 tests with DB required, Compose configuration, 11 OpenSpec items |
| Strict specification validation | `openspec validate --all --strict --no-interactive`: 11 passed, 0 failed |
| Gateway image | Built successfully from the implementation working tree; package version 0.5.0 |
| Real artifact and GPU service | Passed: real HTTP readiness/discovery, native direct and streamed JSON Schema calls, replay of both calls, OpenAPI and zero-fee ledger |
| Ledger in isolated artifact check | Exactly 2 completed calls and 4 events, zero reservations and charges, generated project access key, no paid credentials |
| Packaged source identity | Host/image SHA-256 of sorted relative package paths and bytes (`.py`, `.json`, `.sql`) matched: `0483d4a9f6eaa552d6d507bbe34eede7da951e85c092f9bff12e1066b4e04eef` |
| Image identity | `sha256:74cf1d14329f00d81e9f4f444ebc50cc0c584f6a0c42b81e9be67927450b98bf` |
| Diff/privacy review | No secrets, service destinations or personal machine details in tracked changes; immutable SQL migrations and historical prices unchanged |

The full gate first found five older tests assuming catalogue order or universal
OpenAI capabilities. Those assertions now select explicit aliases and check
declared capability differences. The passing gate supersedes that initial run.

## Acceptance evidence

| Area | Evidence |
| --- | --- |
| Immutable definitions | `test_local_llm_rules.py` freezes the existing OpenAI/stub digests and verifies that changed local execution metadata changes the digest; existing alias-binding regression tests also pass |
| Portable configuration/discovery | Optional URL validation, no ambient proxies/redirects, no destination in model discovery, optional Compose network example; ordinary checks need no GPU/service |
| Input validation | UTF-8 byte boundary including output reservation, leading developer/final user semantics, newline text parts, invalid Unicode and unsupported explicit fields; API tests verify refusal before admission/network |
| Output schema | Bounded object schemas; reference/regex rejection; property names resembling schema keywords remain valid; nonconforming completed output reports schema mismatch and replays |
| Execution translation | Native model/messages/options with no provider authorization header; explicit context/output/temperature, bounded response and complete-attempt deadlines |
| Certainty | Confirmed guard refusals and connection failure versus generic 503, post-execution verification failure, read timeout, malformed/oversize response, missing usage and redirect |
| Streaming | Chunk translation, missing/malformed terminal handling, broken transport, overall deadline without cancelling consumers outside a network read, early close and native response cleanup |
| API/client/accounting | Disposable PostgreSQL plus synthetic HTTP adapter: no paid credential or monetary budget required, grants enforced, confirmed refusal retries one ledger record, unknown direct/stream calls never execute again, non-retryable configuration errors, direct and disconnect replay |
| Existing functionality | Complete gate retains paid-cap, allocation, credential isolation, OpenAI `store: false`, registry, tools, migration and client contract checks |

The bounded real-service artifact check used the Qwen alias. It started an
isolated gateway container and disposable PostgreSQL with generated settings and
synthetic inputs, connected the gateway to the separately managed GPU service,
and removed its gateway container/network afterward. The artifact driver and
content-free report remain in ignored `research/evidence/ollama/`.

No GPU outage was injected into the shared service. Refusal/no-reexecution paths
are covered by synthetic guard responses; the separate service owns GPU residency
enforcement and CPU resource limits. Llama gateway generation was not repeated
in this artifact check. No paid-provider network call was made.

## Operator handoff

See `docs/local-llm.md` for service configuration, supported fields, conservative
context/schema constraints, error certainty and recovery. An operator supplies
the private guard URL/network through local settings. The normal migration step
registers the new aliases using `models sync`; existing bindings stay unchanged.

Removing local configuration disables new execution without deleting recorded
outcomes. Artifact/runtime/template replacement requires new reviewed aliases and
draining in-flight service execution. The gateway requires the GPU guard contract;
an arbitrary native Ollama installation is not sufficient.

Embedding and OCR execution remain subsequent endpoint changes. The owner reviews
and merges this change and separately accepts deployment.
