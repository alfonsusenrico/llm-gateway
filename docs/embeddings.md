# Embedding inference

`POST /v1/embeddings` returns vectors for one text or an ordered batch. Use an
existing gateway project key and select an explicit model alias or project profile.
Discovery is `GET /v1/models?task=embedding`; model details include the executable
request schema. Projects own chunking, vector storage, indexing and search.

## Models

| Alias | Dimensions | Input limit per text | Provider fee per million input tokens |
| --- | --- | --- | --- |
| `local-minilm-l6-v2-v1` | 384, fixed | 256 tokens, including special tokens | Zero |
| `local-bge-small-en-v1.5-retrieval-v1` | 384, fixed | 512 tokens, including prefix/special tokens | Zero |
| `openai-text-embedding-3-small` | 1536 default, explicitly reducible | Safe bound of 8192 UTF-8 bytes | USD 0.02 |
| `openai-text-embedding-3-large` | 3072 default, explicitly reducible | Safe bound of 8192 UTF-8 bytes | USD 0.13 |

Paid prices were reviewed on 2026-10-16 and are dated catalogue entries. Sources:
[small model](https://developers.openai.com/api/docs/models/text-embedding-3-small),
[large model](https://developers.openai.com/api/docs/models/text-embedding-3-large).

Each request contains 1 to 64 nonempty texts, with at most 200,000 aggregate UTF-8
input bytes. Whitespace-only text is rejected. Local native body serialization
must also fit 300,000 bytes. The local service counts exact tokens before encoding
and rejects overflow; it does not truncate. OpenAI input bytes conservatively bound
its byte-level tokenizer's billed tokens, so some otherwise fitting inputs are
rejected. There is also a 300,000 aggregate token bound. The gateway never splits
or shortens input automatically.

The response's `definition_digest` binds the execution/preprocessing policy. Local
definitions additionally pin the artifact/metadata digest and revision, pooling,
prefixes, float32 precision and normalization policy through verified service
metadata. OpenAI definitions bind its public model name and supported parameters;
the gateway cannot inspect the provider's internal weights.

Keep the model alias, definition digest and dimensions with stored vectors. Equal
dimensions alone do not make vectors from different models compatible. Changing a
local artifact, tokenizer, pooling, prefix policy or normalization requires a new
alias. Drain local execution before changing service definitions.

## Request and result

```json
{
  "provider_call_id": "embedding-example-1",
  "operation": "index_document",
  "model": "local-bge-small-en-v1.5-retrieval-v1",
  "input": ["First synthetic passage.", "Second synthetic passage."],
  "input_type": "document",
  "encoding_format": "float"
}
```

Local `input_type` is `document` by default or explicitly `query`. BGE adds its
pinned retrieval instruction for queries only; MiniLM adds no prefix. OpenAI
receives the input text unchanged and does not accept `input_type`. Only the two
OpenAI aliases accept `dimensions`, from 1 to their default dimension count.
Only float encoding is supported.

Exactly one `model` or `profile` is required. Profile defaults use the existing
registry service and append a contract version. For example, an embedding profile
may select the small OpenAI alias with `parameters: {"dimensions": 256}`, or the
BGE alias with `parameters: {"input_type": "query"}`. Parameters unsupported by
the chosen model are rejected. LLM settings, tools, streaming, pre-tokenized input
and arbitrary provider fields are unsupported, including explicit null/false values.

Results contain `data: [{"index": 0, "embedding": [...]}]` in input order,
dimensions, normalized policy, input type, model identity, actual input-token usage,
decimal `cost_usd`, and `replayed`. Counts, index coverage, vector length, finite
components and unit normalization are validated before completion. The gateway
does not silently normalize or repair malformed vectors.

Gateway and client are released together as `0.6.0`:

```python
result = await client.embeddings.create(
    provider_call_id="embedding-example-1",
    operation="index_document",
    model="local-minilm-l6-v2-v1",
    input=["First synthetic passage.", "Second synthetic passage."],
)
```

`SyncGatewayClient.embeddings.create(...)` accepts the same fields and returns the
same typed `EmbeddingResult`. Client instances share their concurrency limiter
between responses and embeddings.

## Configuration and accounting

Set optional backend `EMBEDDINGS_BASE_URL` to the separately managed GPU embedding
service. Use a URL reachable by the API container. URLs cannot contain credentials,
query strings or fragments; redirects and ambient proxies are disabled. The
existing optional `compose.inference.example.yaml` can attach the API to a private
service network, using an ignored local override. Normal development needs no GPU
or inference network. Service destinations are not published in discovery.

Local readiness must declare CUDA and matching metadata must be available before
input is submitted. No usable GPU means refusal; CPU inference, model switching
and paid-provider fallback are prohibited. CPU tokenization/transport support is
bounded by the separate service's resource limits. `LLM_CALL_TIMEOUT_SECONDS`
bounds the complete native attempt. A service outage does not fail overall gateway
readiness; discovery availability reports configuration, not a live service probe.

OpenAI uses the existing per-project encrypted credential slot. Register it through
protected admin input; never send provider credentials in an inference request.
There is no global key fallback. The stateless Embeddings API has no `store`
parameter or application-state retention; standard abuse monitoring still applies.
This owner-approved exception is recorded in AGENTS.md. Responses continues sending
`store: false`. See [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data).

All calls obey shared project rate/concurrency grants. Paid calls reserve a safe
input-token upper bound against the project ceiling, monthly grant and USD 5
global cap, then settle actual valid native usage using integer micro-USD. Missing
or malformed paid usage is unknown and charges the reservation. Local calls have
zero provider fee and may run with no monetary budget, subject to their other grants.
The normal migration step runs `models sync`; no new SQL migration is required.

## Failures and recovery

| Situation | Outcome |
| --- | --- |
| Unsupported input/settings before admission | Validation problem; no provider execution |
| Local token overflow confirmed before encoding | Terminal `provider_invalid_request`; shorten input and use a new ID |
| Local model/metadata mismatch | Terminal `provider_configuration_mismatch`; no content sent |
| Confirmed GPU/busy refusal, unestablished connection or unavailable metadata | Retryable `provider_unavailable` |
| OpenAI rate refusal | Retryable `provider_rate_limited` |
| OpenAI credential/account refusal | Terminal `provider_account_blocked` |
| Ambiguous submission, generic server error, malformed vectors or absent usage | Terminal `execution_unknown`; never execute that ID again |

Reuse the same ID and unchanged request for transport recovery. IDs share one
namespace across all inference endpoints; reusing a response ID for embeddings
returns `idempotency_conflict`. A confirmed-refusal retry also checks definition,
effective settings and captured credential revision. No adapter retries internally.

Successful settlement, its ledger event and encrypted result commit together.
Consumer disconnect leaves execution running to settlement. Completed replay works
after service/credential removal and defaults to 24-hour retention; expired or
unknown calls never execute again. Inputs and vectors are not logged. Only vectors
needed for result recovery are stored encrypted; the ledger keeps content-free
identity and certainty after result expiry.
