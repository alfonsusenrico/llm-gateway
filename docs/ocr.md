# OCR inference

`POST /v1/ocr` extracts text from one PNG or JPEG. Authenticate with a project key
and explicitly select `local-rapidocr-ppocrv4-v1` or a project profile. Discover
models with `GET /v1/models?task=ocr`; details include an executable request schema.
Projects own document interpretation, page handling and downstream workflows.

## Request and result

```python
from llm_gateway_client import GatewayClient, ocr_image

async with GatewayClient(gateway_url, project_key) as client:
    result = await client.ocr.create(
        provider_call_id="ocr-example-1",
        operation="read_image",
        model="local-rapidocr-ppocrv4-v1",
        **ocr_image("example.png", "image/png"),
    )
```

Gateway and Python client release together as `0.7.0`. The sync client exposes
`client.ocr.create(...)` with the same arguments and typed `OcrResult`.
`ocr_image` reads bytes or a path on the caller's machine and produces
`media_type` and `data_base64`; the gateway never reads a supplied path or URL.
A raw JSON request contains these two fields plus `provider_call_id`, `operation`
and exactly one of `model` or `profile`.

Limits: 8,000,000 decoded bytes, sides at most 4096 pixels, at most 12,000,000
pixels, one frame. Only PNG/JPEG are accepted. Base64, format, dimensions, frame
count and compressed image integrity are checked before admission. The ordinary
REQUEST_MAX_BYTES cap also applies to the entire JSON body. Lowering that cap
can reduce the largest accepted image. PDFs, URLs, files, image batches,
streaming and LLM or embedding parameters are rejected.

Results include:

- `text`: block text joined by newlines; a blank image returns empty text.
- `blocks`: `{text, confidence, box}`; each box has four `[x, y]` points, and
  confidence is finite in `[0, 1]`. Coordinates permit the native service's
  one-pixel tolerance at an image edge.
- `width`, `height`, `media_type`, `coordinate_system: "original_image_pixels"`
  and `exif_rotation: false`. Coordinates refer to stored image pixels; EXIF
  orientation is not applied. The gateway does not resize or rotate images.
- Explicit model alias, model definition digest, native model/provider identity,
  call ID, decimal `cost_usd` and `replayed`. OCR has no token usage field.

Output is limited to 2048 blocks and 1,000,000 aggregate UTF-8 block-text bytes.
The gateway checks model identity, original dimensions, confidence, geometry and
text consistency before recording completion. It does not repair malformed output.
An OCR profile selects the alias with empty `parameters`, saved through the
registry service to append a contract version.

## GPU service contract and portability

Set optional backend `OCR_BASE_URL` to a private HTTP(S) base URL reachable from
the API container. Credentials, query strings and fragments in that URL are
forbidden. Redirects and ambient proxies are disabled. The optional
`compose.inference.example.yaml` attaches only the API to an independently managed
network; copy it to an ignored local override and supply INFERENCE_NETWORK.
No private network name, host, artifact path or GPU runtime is built into the gateway.

The native service must implement GET /health/ready, GET /v1/models and POST
/v1/ocr using the pinned `rapidocr-gpu-native-v1` protocol. The catalogue pins
RapidOCR 1.4.4, ONNX Runtime GPU 1.20.2 and the canonical manifest digest, which
binds detection/classification/recognition artifacts and preprocessing config.
All three sessions must require CUDA, call `disable_fallback()` and refuse startup
or execution without a usable GPU. Readiness and live manifest identity are
verified before the gateway sends image content.

No GPU means refusal. The gateway has no CPU, alternative-model or paid fallback.
Metadata verification relies on the trusted service enforcing this contract.
CPU image decoding and transport remain necessary support work. Gateway decoding
uses one nonblocking background-thread slot per API process, held until decoding
ends even if its caller disconnects. Configure API process count and container CPU
quotas for the host's support-work allowance; the separately managed inference
service must also have bounded CPU threads and quotas. The gateway itself contains
Pillow for validation and no Torch, ONNX Runtime or OCR inference engine.

`LLM_CALL_TIMEOUT_SECONDS` bounds the entire native attempt. Response JSON is
bounded to 16 MB, including duplicated plain/block text and escaped characters.
Overall gateway readiness checks the database; discovery reports configuration,
not a current GPU probe. Service outages do not disable other inference endpoints.
Run the usual migration step and `models sync` to register the added alias. No new
SQL migration is required. A model/config/runtime change needs a new reviewed alias.

## Accounting and recovery

OCR uses shared project rate and concurrency grants and has explicit zero provider
fee, including when monetary budget is zero. It does not need provider credentials.
All three client endpoints share their client's concurrency limiter.

| Situation | Outcome |
| --- | --- |
| Invalid image or unsupported fields | Validation refusal before admission |
| Gateway decoder slot busy | Retryable `provider_unavailable`, before admission |
| Missing OCR configuration | `provider_not_configured`, before admission |
| CPU provider or manifest mismatch | Terminal `provider_configuration_mismatch`, no image submitted |
| Confirmed GPU/busy refusal, unavailable metadata, unestablished connection | Retryable `provider_unavailable` |
| Native image rejection confirmed before inference | Terminal `provider_invalid_request` |
| Ambiguous submission, generic server error or invalid result | Terminal `execution_unknown` |

Reuse the same ID with the same request for transport recovery. Only confirmed
refusals may execute again. Unknown calls never execute again under that ID;
start a new ID only if another execution is intended. IDs share one namespace
across LLM, embeddings and OCR; a cross-task reuse conflicts.

Admitted producers survive client cancellation. Completion, ledger event and
encrypted result commit atomically. Replay precedes current decoder/provider
checks, defaults to 24-hour retention and never performs fresh inference. Inputs
are not persisted. OCR text/boxes needed for replay are stored encrypted and expire
under the normal result policy. Images and recognized text are never logged.

Provider references reviewed for this change: [RapidOCR API](https://rapidai.github.io/RapidOCRDocs/v1.4.4/install_usage/api/RapidOCR/),
[ONNX Runtime API](https://onnxruntime.ai/docs/api/python/api_summary.html),
[Pillow image decoding](https://pillow.readthedocs.io/en/stable/reference/Image.html).
