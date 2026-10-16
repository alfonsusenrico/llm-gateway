# Verification: add-embedding-inference

- Date: 2026-10-16; development only.
- Branch: `feat/embedding-inference`, based on merged `2c6c953` (PR #17).
- Gateway/client release: `0.6.0`; no SQL migration or new inference dependency.
- Authority: endpoint-focused continuation, plus the owner's explicit approval of
  both local GPU and stateless OpenAI embeddings. AGENTS.md records the Embeddings
  API exception to store:false; Responses keeps that field.
- No live paid call, real credential registration, application-data migration,
  consumer change or gateway deployment was performed.

## Checks

| Check | Result |
| --- | --- |
| Full repository gate | Passed at the 839-test checkpoint: lock, format, Ruff, Pyright 0, DB required, Compose and 12 OpenSpec items; final response-bound regression checks also require the pre-commit gate |
| Strict specifications | `openspec validate --all --strict --no-interactive`: 12 passed, 0 failed |
| Gateway image | Built gateway 0.6.0, runtime UID 10001; Torch, Sentence Transformers and ONNX Runtime absent |
| Real HTTP/GPU artifact check | Passed: MiniLM document batch; BGE document/query batches with different query representation; replay of all three; local token overflow refusal; discovery/schema/OpenAPI; direct/streamed stub regression and replay; cross-task conflict |
| Isolated artifact ledger | 3 completed embeddings, 1 confirmed input refusal, 2 completed stub calls; 12 events, 5 encrypted results; every reservation/charge zero |
| Packaged source identity | Host/image SHA-256 of sorted relative package paths and bytes (`.py`, `.json`, `.sql`) matched: `83f25718b0bea27e08ad1cab6550c6fe7d3e4d85582363eaeb8860bf06d83c78` |
| Image identity | `sha256:fb026be98130f71120cd876e49706bb3311e6ed7dd823fad67629cd39e8e10b1` |
| Existing definitions/prices | All four schema-1/2 digests frozen in regression checks; previous dated price objects unchanged; new embedding prices append dated standard entries |
| Archive | Merged local LLM change archived with native OpenSpec; new main specification has its purpose completed |

The initial gate found five expected catalogue/discovery assertions assuming no
embedding models. They now cover all eight definitions and the embedding task
filter. A profile fixture initially used the wrong registry mutation signature;
it now edits through the existing versioned service. Passing runs supersede these
transitional failures.

## Acceptance evidence

| Area | Tracked evidence |
| --- | --- |
| Requests/definitions | `test_embedding_models.py`: strict input/unsupported fields, safe UTF-8/body/token bounds, profiles, task schemas, backend token reservation, preserved schema-1/2 hashes |
| Native translation | `test_embedding_adapter.py`: local query handling, paid dimensions/authentication, no unsupported store field, ordered index reconstruction, complete-attempt deadline and bounded response bodies |
| Identity/output | Native metadata/artifact/device mismatch before content; wrong returned model/revision, duplicate/out-of-range indices, wrong dimensions, booleans, nonfinite and nonnormalized vectors, malformed body and missing/inconsistent usage |
| Certainty | Confirmed GPU/busy/input refusals versus connection failure, ambiguous server/read failures and metadata availability; no internal inference retry or provider prose leakage |
| Accounting | `test_embeddings_api.py`: local zero-budget success, exact micro-USD paid usage, missing usage charged at its safe reservation, cap refusal before submission and unchanged shared money tests |
| Credentials/profiles | Separate projects capture separate registered accounts/revisions; credential removal preserves terminal replay; rotation refuses a same-ID retry; profile defaults change through registry versions |
| Replay/durability | Completed replay without current runtime, unknown no-reexecution, both cross-task conflict directions, shared concurrency, disconnected caller ownership; result persistence failures roll back settlement state/event for both embeddings and LLM |
| Clients | `test_embedding_client.py`: sync/async route, matching default wire fields, typed API/result parity, same-ID transport recovery and no retry of unknown execution |
| Existing LLM | Full gate retains direct/streamed tool, credential, profile, admission race, budget, migration and replay checks; real image additionally exercises stub direct/streamed replay |

## Limits and operation

The real-service check uses synthetic inputs, generated private settings and a
fresh task-owned database. Its gateway container and temporary network are removed
in cleanup. Private driver/report live in ignored `research/evidence/embeddings/`.
The normal automated suite needs no GPU or private service network.

OpenAI requests are verified with synthetic HTTP fixtures only. No account access,
paid runtime availability or live provider billing was tested. No GPU failure was
injected into shared services; refusal and ambiguous-send matrices are synthetic.
The separate local service owns GPU execution and CPU support quotas. The gateway
verifies model metadata and normalized vectors, not semantic retrieval quality.

See `docs/embeddings.md` for supported aliases, query/document semantics,
conservative paid input bounds, dimension overrides, private URL configuration,
credential management, grant/cap behavior and recovery. The existing migration
step synchronizes new aliases; there is no SQL or historical contract rewrite.

Owner review/merge and deployment acceptance remain separate. OCR remains the
next independent endpoint change.
