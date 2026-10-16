# Design

## Endpoint and definitions

POST /v1/embeddings accepts provider_call_id, operation, exactly one model/profile,
input (one string or ordered string batch), optional encoding_format:float, and
only parameters supported by the selected alias. No streaming, pre-tokenized
input, automatic chunking or LLM fields. Profiles remain registry-versioned;
embedding defaults add input_type/dimensions and existing LLM schemas reject them.

Schema 3 adds a task-specific embedding contract: dimensions and allowed overrides,
normalization, bounded batch/text bytes, local revision/preprocessing identity.
Schema-1 and schema-2 canonical fields stay frozen. Embedding definitions have no
generated tokens. Discovery publishes the correct task schema and capabilities.

Local aliases select MiniLM (384, 256 tokens, mean pooling) and BGE (384, 512,
CLS pooling). Query/document selects the pinned prefix policy. The service rejects
token overflow before GPU inference; the gateway bounds batch/text/body bytes before
admission. It does not duplicate the local tokenizer or silently truncate input.

OpenAI aliases select text-embedding-3-small (1536 default dimensions) and large
(3072); reduced dimensions are explicit. No query/document prefix is added, and
input_type is unsupported for these aliases. encoding_format is always float.
Responses expose dimensions, normalized policy, model alias/definition digest,
ordered indexed vectors, actual input-token usage, decimal cost and replay status.
Consumers keep model identity/dimensions with their vector storage.

## Portable local service

Optional EMBEDDINGS_BASE_URL uses the same private URL validation as Ollama, with
no redirects, ambient proxy or project provider credential. Before content, verify
CUDA readiness and the complete model metadata digest, revision, dimensions,
normalization and prefix/pooling/overflow policy. The service verifies artifact
checksums at startup; metadata includes their digest. Never infer a destination,
model, GPU or fallback. Service outage does not break gateway readiness.

## Shared admission and accounting

Extract the current project-lock/current-contract/credential-snapshot/admission
sequence into a task-neutral helper. Resolve each endpoint's schema under that
lock. Preserve credential revisions, alias bindings, settings digests, grants and
request fingerprints. Global call-ID reuse across tasks conflicts before replay.

Paid embedding inputs use a conservative UTF-8 byte upper bound (byte-level
tokenizer), bounded per input and aggregate. Pass that backend-derived token bound
to reservation with zero output tokens; never use the old bytes/2 heuristic for
this endpoint. Native usage must be complete, nonnegative and consistent. Price
only input tokens using new dated standard entries; unknown usage reserves spend.
Local execution is zero fee but retains rate/concurrency grants.

Add an optional result write to the existing settlement transaction. Encryption
occurs before settlement commits; settlement state, event and encrypted result
commit together. Use the same path for LLM success, closing its existing replay
save gap without changing its wire shape. Failure alert delivery stays after commit.

## Execution certainty and privacy

Use per-attempt bounded HTTP clients and an overall deadline. Local service_busy
and gpu_unavailable are confirmed refusals; input/token rejections occur before
encoding and are terminal invalid requests. Generic server errors, malformed data,
timeouts after submission or missing usage are execution_unknown. No internal
transport retry. OpenAI account/rate/validation refusals use reviewed fixed codes;
generic server/proxy failures remain unknown. Provider prose never escapes.

Validate exact model identity, count/index coverage, vector dimensions, finite
numbers (excluding booleans), normalization tolerance and usage before success.
Client disconnect detaches execution through existing background ownership.
Replay precedes current runtime/credential availability. No input/vector logging;
only encrypted result retention, with the existing default 24-hour expiry.

OpenAI embeddings have no store parameter and no application-state retention.
The owner explicitly permitted this exception; standard abuse monitoring remains
documented. Credentials stay per project; no global key fallback or live paid test.

## Verification and rollout

Use synthetic native HTTP matrices, actual API/client plus disposable PostgreSQL,
cross-task conflict, retry/refusal/unknown replay, paid bounds/cap and atomic result
rollback. Keep ordinary checks independent of GPUs. Verify the built image with
bounded synthetic local GPU calls only. Run the full gate and strict OpenSpec,
record limitations, publish an owner-review PR; no merge or deployment.

Official documentation reviewed 2026-10-16:

- https://developers.openai.com/api/reference/resources/embeddings/methods/create
- https://developers.openai.com/api/docs/guides/embeddings
- https://developers.openai.com/api/docs/guides/your-data
- https://developers.openai.com/api/docs/models/text-embedding-3-small
- https://developers.openai.com/api/docs/models/text-embedding-3-large
- https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2
- https://huggingface.co/BAAI/bge-small-en-v1.5

Prepared-service source and read-only live metadata establish its private native
contract; model cards alone do not establish wrapper behavior.
