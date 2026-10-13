# Design

## Context

Admission, settlement, and the catalogue exist. Provider facts come from the
local provider review (2026-10-13, official OpenAI pages):

- `store` defaults to true;
- there is no idempotency key, only `X-Client-Request-Id`;
- the SDK uses `httpx2` and retries twice by default;
- the usage fields and error codes are as listed there.

See proposal.md for motivation.

## Goals / Non-Goals

**Goals:**

- Execute each admitted call at most once per admission.
- Tell the project exactly what is safe to do next.

**Non-Goals:**

- Provider-side conversation state (`previous_response_id`, conversations).
  Projects own their state.

## Decisions

### 1. Request model (strict; unknown fields rejected)

| Field | Rules |
|---|---|
| `provider_call_id` | `^[A-Za-z0-9._:-]{1,160}$`; unique per project environment |
| `operation` | `^[a-z][a-z0-9_]{1,63}$`; used for cache keys and the ledger |
| `profile` or `model: {provider, model}` | exactly one |
| `input` | a string, or 1 to 200 messages `{role: user\|assistant\|developer, content}` |
| `instructions` | at most 100,000 characters |
| `text_format` | `{type: text}`, or `{type: json_schema, name, schema, strict: true}` |
| `max_output_tokens` | 1 to the model's maximum; default: the profile's, else `DEFAULT_MAX_OUTPUT_TOKENS` (4096) |
| `reasoning_effort`, `temperature` | override the profile; checked against the model rules |
| `prompt_cache_key` | at most 64 characters; default `<project_id>:<operation>` |
| `stub` | stub model only; see decision 7 |

- **Message content** is a string, or parts:
  - `{type: input_text, text}`;
  - `{type: input_image, media_type: image/png|jpeg|webp|gif, data_base64, detail?}`;
  - `{type: input_file, media_type: application/pdf, data_base64, filename}`.
- **Size limit:** the whole body is capped at `REQUEST_MAX_BYTES` (default 20
  MB); a larger body returns `413 request_too_large`.
- **Input types:** an input type the model does not accept returns `422
  input_not_supported`.

*Rejected:* passing the provider request through untouched. It would leak
provider-specific fields into every project and bypass the catalogue.

### 2. Resolution

Model resolution, in order:

1. a named `profile` must exist in the contract, else `422
   profile_not_found`;
2. the model must be in the catalogue, else `422 model_not_in_catalogue`;
3. a production key asking for a development-only model gets `403
   stub_not_allowed`.

Effective settings: request overrides, then the profile, then model defaults,
validated against the catalogue rules (for example, temperature only with
effort `none` on gpt-6-luna). Violations return `422 invalid_model_settings`.

### 3. Flow of one call

1. Authenticate.
2. Validate and resolve.
3. Fingerprint the canonical request, a SHA-256 over sorted-key JSON.
4. Admit.
   - **If admission returns an existing completed call:** load its result;
     if found, return it with `replayed: true`; if expired, return `410
     call_result_expired`.
   - **If it returns an existing failed or unknown call:** return its stored
     problem again.
5. Execute through the adapter.
6. Settle.
7. Store the encrypted result, if completed.
8. Respond with allowance headers.

Settlement and result storage run even if the client has disconnected. The
endpoint shields that tail from cancellation.

### 4. OpenAI adapter

- **Client:** one `AsyncOpenAI` per process, with `max_retries=0` and
  `timeout=httpx2.Timeout(LLM_CALL_TIMEOUT_SECONDS, connect=10)`.
- **Call:** `responses.with_raw_response.create(...)`, with:
  - `store=False`;
  - `prompt_cache_key`;
  - `max_output_tokens`;
  - `reasoning={"effort": ...}` when set, and `temperature` when allowed;
  - `text.format` mapped from `text_format`;
  - `input` mapped to `input_text`, `input_image` (`image_url` data URL), or
    `input_file` (`file_data` data URL, `filename`) items;
  - `extra_headers={"X-Client-Request-Id": provider_call_id}`.
- **Response handling:**
  - `output_text`: joined message text;
  - `refusal`: from refusal parts;
  - `status` and `incomplete_details.reason`;
  - usage, the full field set from the review;
  - `service_tier`;
  - the response `id`;
  - the headers `x-request-id` and `x-ratelimit-*`, kept for logs and phase
    4.
- **JSON schema calls:** `output_json` is the parsed text. When parsing
  fails, `structured_output_error` is `invalid_json` or `empty_output`. The
  call still completes and is charged; the project decides what to do.

### 5. Failure policy (adapter returns a bounded category; nothing else)

| Provider result | Category | Gateway problem | Retry same ID? | Settled as |
|---|---|---|---|---|
| Connection not established (`ConnectError`, `ConnectTimeout`) | `connection_not_established` | `503 provider_unavailable` | yes | `retryable_failure` |
| 429 without a quota or billing code | `temporary_throttle` | `429 provider_rate_limited` + `Retry-After` | yes | `retryable_failure` |
| 500, 502, 503 (incl. `server_is_overloaded`) | `temporary_overload` | `503 provider_unavailable` + `Retry-After` | yes | `retryable_failure` |
| 429 or 403 with `insufficient_quota`, `credit_balance_exhausted`, `*_spend_limit_exceeded`, `organization_usage_limit_exceeded` | `account_blocked` | `503 provider_account_blocked` + critical alert | no | `failed` |
| 401, or 403 otherwise | `authentication` | `503 provider_account_blocked` + critical alert | no | `failed` |
| 400, 404, 422 | `invalid_request` | `422 provider_invalid_request` (the provider's `code`, not its message) | no | `failed` |
| Read timeout or transport loss after sending | `execution_unknown` | `502 execution_unknown` | no; use a new ID | `execution_unknown` |
| 200 with `status: failed` | `provider_failed` | `502 provider_failed` (`error.code`) | no | `failed`, charged if usage is present |
| 200 with an unparseable body | `execution_unknown` | `502 execution_unknown` | no | `execution_unknown` |

- Provider messages are never logged or returned, only status and code.
- `Retry-After` comes from the provider's header when present, otherwise 5
  s, with jitter added.

### 6. Result storage

- `call_results(call_key, encrypted bytes, expires_at)`, encrypted with
  Fernet under `RESULT_ENCRYPTION_KEY`, which `scripts/setup-local`
  generates.
- The stored value is the response body exactly as returned.
- The worker deletes expired rows each pass. Changing the key makes stored
  results unreadable, which returns `410 call_result_expired`, never a
  crash.

*Rejected:* storing only the outcome (owner decision, 2026-10-13). A
replayed call must return its answer without paying again.

### 7. Stub provider (development keys only)

`stub/stub-echo` never touches the network.

- **Text format:** `output_text` is `stub.reply` or `stub: <last user
  text>`, truncated to 2,000 characters.
- **JSON schema:** `output_json` is `stub.reply` (an object), or `{}`.
- **`stub.scenario`** may be `rate_limited`, `overloaded`,
  `connection_failed`, `timeout_after_send`, `invalid_request`, `incomplete`,
  or `invalid_json`. Each produces the matching category from decision 5, so
  projects can test their failure handling.
- **Usage** is bytes / 4 per side. The cost is 0 through the stub's
  zero-price entry.

### 8. Logging

One line per call, holding:

- the call key, project and environment, operation, model, and tiers;
- the outcome category and token counts;
- the cost in micro-USD, latency in ms, and the provider request ID.

Never content.

## Risks / Trade-offs

- **[Risk]** A 24-hour store of model output at rest. **Mitigation:** it is
  encrypted, auto-deleted, and never logged. Its key is separate from the
  database.
- **[Risk]** The SDK's exception mapping may change. **Mitigation:** the
  adapter maps by HTTP status and SDK class, with tests for each category
  using a mocked transport. The `openai` version is pinned to the reviewed
  major version.
- **[Trade-off]** No in-gateway retry. That is simpler and keeps one retry
  owner, the client, which decision 5 makes safe.

## Migration Plan

- Migration `0005_call_results.sql` is additive.
- **New required setting:** `RESULT_ENCRYPTION_KEY`. `setup-local` generates
  it, and existing local `.env` files need it appended.
- `OPENAI_API_KEY` stays optional. Without it, OpenAI calls return `503
  provider_not_configured`, and readiness reports the provider as not
  configured, without failing.
