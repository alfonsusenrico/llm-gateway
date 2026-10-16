## Context

Official documentation reviewed before implementation (2026-10-16):

- [RapidOCR 1.4.4 API](https://rapidai.github.io/RapidOCRDocs/v1.4.4/install_usage/api/RapidOCR/)
- [ONNX Runtime Python API](https://onnxruntime.ai/docs/api/python/api_summary.html)
- [Pillow Image API](https://pillow.readthedocs.io/en/stable/reference/Image.html)

The separate native service uses RapidOCR 1.4.4 and ONNX Runtime GPU 1.20.2.
Its manifest binds all model/config artifacts. Detection, classification and
recognition sessions require CUDA and disable execution-provider fallback.
The gateway has no inference runtime and cannot select CPU execution.

## Goals / Non-Goals

Goals: explicit selection, bounded single-image transport, verified native output,
portable HTTP configuration and shared admission/accounting/replay semantics.
Projects retain document interpretation and workflow ownership. PDF rasterization,
page batching, URL fetching, storage, search and image transformation are outside
this endpoint. No production deployment or live paid calls form part of this change.

## Decisions

- Strict PNG/JPEG base64 input only, 8 MB decoded, side <=4096, <=12 million
  pixels, one frame. Pillow verifies and decodes before admission in a background
  thread with one nonblocking slot per process. Cancellation retains slot ownership
  until decoding finishes. No image resizing or EXIF rotation occurs.
- Stable call IDs bind the explicit request fingerprint across all tasks. Existing
  outcomes replay before current decoder/provider availability checks. Current
  contract resolution and model registration remain under the project row lock.
- Schema 4 adds only the OCR contract to canonical definition hashing. Schema 1,
  2 and 3 hashes remain unchanged. OCR has zero token limits and zero provider fee,
  with ordinary rate and concurrency grants.
- Before sending image content, check readiness and three CUDA providers, then
  recompute the live native manifest digest against the reviewed definition.
- Native response has bounded JSON size, at most 2048 blocks and 1 MB block text.
  Validate exact model/digest, dimensions, finite confidence and four-point boxes
  within the native service's one-pixel tolerance. Plain text equals joined blocks.
  A blank image is a successful empty result. Coordinates use stored image pixels.
- Only confirmed pre-inference refusals are retryable. Interrupted or malformed
  submitted executions become terminal execution_unknown. No implicit retries,
  model changes, CPU fallback or paid fallback occur in the adapter.
- Shield the admitted producer against consumer cancellation. Commit completion,
  ledger event and encrypted result atomically through shared settlement.
- OCR_BASE_URL names an independently managed private HTTP service. No topology,
  GPU library, artifact path or host identity is embedded in gateway configuration.

## Risks / Trade-offs

Decoding is bounded CPU support work, not GPU inference. API process count and
container CPU quotas determine total host support capacity. The gateway verifies a
trusted native GPU contract; it does not independently inspect remote GPU hardware.
The required service must enforce CUDA-only sessions and disable runtime fallback.
Malformed output after submission remains unknown, even for zero-fee calls.
EXIF orientation remains unchanged and consumers must use the returned dimensions.

## Migration Plan

Release gateway and client together as 0.7.0. Run the existing migration step and
models sync to register the added alias. Configure OCR_BASE_URL and, if needed,
the optional external inference network. Acceptance/deployment remains owner-gated.
Existing model aliases, request contracts and database migrations are preserved.

## Open Questions

None for this single-image endpoint.
