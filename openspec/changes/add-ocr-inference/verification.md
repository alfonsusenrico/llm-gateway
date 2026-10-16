# OCR verification — 2026-10-16

## Automated gate

`./scripts/check` passed with a disposable PostgreSQL database and REQUIRE_DB=1:

- Locked dependency resolution; Ruff formatting and lint passed.
- Pyright: 0 errors, 0 warnings.
- 929 tests passed, including every database test; none skipped.
- Compose configuration passed.
- OpenSpec: 13/13 passed; separate strict validation also passed.

New coverage includes request/schema/profile validation; malformed, truncated,
animated, oversized and mismatched images; PNG/JPEG and EXIF handling; bounded
CPU decoder ownership through cancellation; mandatory CUDA metadata and manifest
identity; bounded output, confidence, coordinates, identity and text validation;
confirmed-refusal retries versus terminal unknown execution; client sync/async
retry behavior; zero-fee ledger, encrypted storage and atomic rollback; replay
without current provider/decoder availability; shared rate/concurrency limits;
and call-ID conflicts across all three inference tasks. Historical schema-1/2/3
definition hashes remain covered and unchanged. No historical price was changed.

## Built image and real local GPU

The packaged gateway image was run with generated credentials, a separate empty
database, one CPU core quota and synthetic content only. Its source matched the
working tree's Python/JSON/SQL files exactly:

- Image: `sha256:7c16bb409e4147b914aa81eabdd682e6d1340edcb39def6a1f4d48a62655f121`
- Source: `0ac4b98868e9d6a4da79a7b50f79fe21a45a70f538b06038e2d3868833e889a2`
- Gateway runtime UID: 10001.
- Torch, sentence-transformers and ONNX Runtime absent from the gateway image.

The real pinned GPU OCR service passed:

1. PNG and JPEG synthetic-text recognition with text/box validation.
2. Blank PNG completed with empty text and blocks.
3. EXIF orientation-6 JPEG retained original 800×200 pixel dimensions and coordinates.
4. Identical requests replayed their encrypted results.
5. Invalid base64 failed before admission.
6. Model discovery and typed OpenAPI exposed the focused OCR contract.

Regression checks against the same packaged gateway passed a real local MiniLM
embedding and its replay, plus direct and streamed stub responses and replay.
Cross-task call-ID reuse conflicted. The exact ledger held seven completed calls,
14 events and seven encrypted results, with zero reserved/charged provider fees.
Temporary API/network/artifact-database resources were removed after verification.

## Limits of evidence

GPU-unavailable and CPU-provider rejection were simulated in adapter/API tests;
the shared GPU service was not deliberately interrupted. Successful live execution
verified the service's matching CUDA contract. The gateway trusts that separately
managed service to disable runtime fallback; HTTP metadata cannot independently
prove remote hardware behavior. CPU support decoding remains bounded per process,
and service/container quotas remain an operator responsibility.

No live paid provider calls, production database changes or gateway deployment
were performed. Acceptance and deployment remain owner-gated. General paid-LLM
reservation/missing-usage accounting review remains a separate acceptance task.
