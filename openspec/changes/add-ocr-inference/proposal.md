## Why

The gateway already separates LLM and embedding inference. Projects also need a
portable OCR endpoint backed by the separately managed, GPU-required service.

## What Changes

- Add POST /v1/ocr for one bounded PNG/JPEG, with explicit model/profile selection.
- Return plain text, confidence and quadrilateral boxes in original image pixels.
- Pin a schema-4 local RapidOCR definition without changing historical digests.
- Use existing grants, zero-fee accounting, encrypted replay and failure policy.
- Publish matching discovery, Python clients and portable service configuration.

## Capabilities

### New Capabilities

- `ocr-inference`: Single-image OCR with explicit identity and mandatory GPU execution.

### Modified Capabilities

None.

## Impact

Gateway API, catalogue, client, settings, documentation and tests. Pillow adds
bounded image decoding; inference runtimes remain in the separate GPU service.
No database migration, production deployment or paid provider call is required.
