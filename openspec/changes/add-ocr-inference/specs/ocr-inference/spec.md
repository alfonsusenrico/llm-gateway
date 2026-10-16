## ADDED Requirements

### Requirement: OCR has a focused single-image endpoint

The gateway SHALL expose POST /v1/ocr with explicit model/profile selection and
strict PNG/JPEG base64 input. It SHALL reject unsupported fields and invalid,
animated or excessive images before admission. CPU decoding SHALL be bounded and
run outside the event loop and project admission lock.

#### Scenario: Invalid or oversized image

- **WHEN** image decoding, format, frame count or configured bounds fail
- **THEN** the request is refused without admission or inference execution

### Requirement: OCR execution requires pinned GPU identity

The gateway SHALL verify the native manifest digest and all three CUDA execution
providers before sending image content. It SHALL never choose CPU, another model
or a paid provider as fallback. The native service SHALL disable runtime fallback.

#### Scenario: CPU provider or changed model manifest

- **WHEN** live metadata differs from the registered GPU execution contract
- **THEN** the gateway refuses the call without sending image content

#### Scenario: GPU unavailable before inference

- **WHEN** the native service confirms gpu_unavailable before execution
- **THEN** the gateway records a retryable refusal under the same call ID

### Requirement: OCR returns validated original-pixel results

Results SHALL include original width/height, text and finite confidence with
four-point boxes in original stored image pixels. EXIF rotation SHALL be disabled.
The gateway SHALL enforce bounded output and matching text, dimensions and identity.

#### Scenario: Blank image

- **WHEN** the native service returns no text blocks for a valid image
- **THEN** the result is completed with empty text and blocks

#### Scenario: Invalid output after submission

- **WHEN** output validation or transport fails after inference may have started
- **THEN** the recorded outcome is terminal execution_unknown and never reexecutes

### Requirement: OCR shares grants and durable task-isolated replay

OCR SHALL use shared rate/concurrency grants and explicit zero-fee accounting.
Completion, ledger event and encrypted result SHALL commit atomically. Call IDs
SHALL conflict across tasks. Replay SHALL precede current provider and decoder
availability. Admitted execution SHALL survive consumer cancellation.

#### Scenario: Zero monetary budget

- **WHEN** a valid local OCR call has available rate and concurrency grants
- **THEN** it can execute and settle at zero fee

#### Scenario: Reuse an OCR ID for an embedding request

- **WHEN** the same project/environment reuses a recorded ID on another endpoint
- **THEN** the gateway returns idempotency_conflict without further execution

#### Scenario: Result storage failure

- **WHEN** encrypted result persistence fails during successful settlement
- **THEN** completed state and its ledger event do not commit without the result
