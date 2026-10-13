# Design

## Context

The source for every fact below is the provider review in local research
notes (2026-10-13), drawn from the official OpenAI model, pricing, reasoning,
and prompt-caching pages. Contracts already hold profiles that are only
structurally validated. See proposal.md for motivation.

## Goals / Non-Goals

**Goals:**

- One place that answers "may this request go to this model, and what will
  it cost".

**Non-Goals:**

- Live price or model discovery.
- Per-project price overrides.

## Decisions

### 1. Data files in the package, validated at startup

`catalogue/models.json` and `catalogue/prices.json` ship inside the image.
They are parsed into frozen Pydantic models with unknown fields rejected.

- An invalid file stops start-up.
- A test checks that every catalogue model has a price at "now" for its
  standard tier, and that prices never overlap in time for the same model
  and tier.

*Rejected:*

- **Prices in code:** harder to review as data.
- **Prices in the database:** they need an admin flow and lose the review
  history git gives.

### 2. Model entry shape

```json
{
  "provider": "openai", "model": "gpt-6-luna",
  "context_window_tokens": 1050000, "max_output_tokens": 128000,
  "reasoning_efforts": ["none", "low", "medium", "high", "xhigh", "max"],
  "default_reasoning_effort": "medium",
  "temperature": "only_with_effort_none",
  "inputs": ["text", "image", "file"],
  "service_tiers": ["standard", "flex"],
  "development_only": false
}
```

- `temperature` is `always`, `never`, or `only_with_effort_none`.
- `inputs` lists the input types the gateway accepts for the model:
  - `file` means PDF via `file_data`;
  - `image` means PNG, JPEG, WEBP, or GIF via base64 data URLs.

The stub entry is `{"provider": "stub", "model": "stub-echo", ...}`, with
`development_only: true`, every input type, and no tiers except standard.

### 3. Price entry shape and the cost function

```json
{
  "provider": "openai", "model": "gpt-6-luna", "service_tier": "standard",
  "effective_from": "2026-10-13",
  "usd_per_million": {"input": "0.10", "cached_input": "0.01",
                      "cache_write": "0.125", "output": "0.50"},
  "long_context": {"threshold_input_tokens": 272000,
                   "input_multiplier": "2", "output_multiplier": "1.5"},
  "source_url": "https://developers.openai.com/api/docs/pricing"
}
```

**Cost, in micro-USD, with `Decimal` arithmetic:**

- `uncached = input - cached - cache_write` (never negative).
- Cost is the sum of `uncached × input`, `cached × cached_input`,
  `cache_write × cache_write`, and `output × output`, where output already
  includes reasoning tokens.
- When `input_tokens` is over the threshold, the input-side rates are
  multiplied by `input_multiplier` and the output rate by
  `output_multiplier`, for the whole request.
- The result is rounded up to whole micro-USD.
- The applicable entry is the latest `effective_from` on or before the
  call's date (UTC).
- No entry means `None` (unpriced). Callers treat that as unknown, never as
  free.

**Flex tier:** the flex entry carries flex prices. The tier used is the one
the provider reports. The stub has a zero-price entry.

### 4. Profile checks at save time, not load time

The registry service validates every profile against the catalogue before
storing a version:

- the model exists;
- the effort is allowed;
- the temperature rule is respected;
- `max_output_tokens` is within the model's maximum.

Loading never quarantines a contract because the catalogue changed. A
retired model is refused at call time instead (next changes). This keeps a
catalogue edit from silently taking projects offline.

### 5. `GET /v1/catalogue`

Key-authenticated. It returns the models the key's environment may use:

- production keys never see `development_only` models;
- each model shows its entry plus today's standard prices as decimal
  strings.

Its responses carry `Cache-Control: private, max-age=300`.

## Risks / Trade-offs

- **[Risk]** OpenAI changes prices. **Mitigation:** add a new dated entry in
  a reviewed change. The `source_url` and the date make the review easy.
  Estimates are list-price, not invoices.
- **[Risk]** Long-context billing details may change. **Mitigation:** the
  multipliers live in data, and tests pin the documented rule.

## Migration Plan

- No database change.
- Existing local test contracts have no profiles, or valid gpt-6-luna
  profiles.
