# Embedding inference

## Why

Projects need an independent embedding API with the gateway's existing access,
grants, accounting and certainty guarantees. The local GPU service is ready;
OpenAI embeddings require a distinct native contract from Responses.

## What Changes

- Add POST /v1/embeddings and matching synchronous/asynchronous client methods.
- Register fixed MiniLM/BGE local and OpenAI text-embedding-3 small/large aliases.
- Add task-specific schemas, bounded ordered batches, dimensions and query/document
  semantics, immutable artifact/preprocessing identities and optional service URL.
- Reuse admission, registry credentials, settlement, encrypted replay and limits.
- Use a proven input-token upper bound for paid reservation; missing/malformed
  provider usage is uncertain and cannot settle at an invented zero cost.
- Share admission orchestration across endpoints and save successful results in
  the settlement transaction to avoid a new replay durability gap.
- Preserve schema-1/2 digests, existing LLM behavior, old prices and the USD 5 cap.
- Archive the merged local LLM change. Release gateway/client together as 0.6.0.

## Capabilities

### New Capabilities

- `embedding-inference`: bounded local/paid embeddings with explicit identities.

### Modified Capabilities

None. Existing accounting, credential and replay guarantees apply to this task.

## Authority and boundaries

The owner approved endpoint-focused implementation and both OpenAI/local embeddings.
The owner clarified that the stateless Embeddings API may omit the unsupported
store parameter. Responses retains store:false. No paid live calls or deployment
are authorized. Vector storage/indexing/search, chunking, semantic interpretation,
OCR and automatic model/provider/CPU fallback are outside this change.
