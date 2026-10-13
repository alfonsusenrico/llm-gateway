# Design

## Context

The local phase 3 research note (2026-10-13) records the owner's choice and
the Brave facts, from Brave's official pricing, API reference, and terms:

- the endpoints and their parameters;
- $5 per 1,000 searches, with $5 free credit a month;
- no storing or caching of results, except transient operation.

Admission and ledger already handle any provider and model.

## Goals / Non-Goals

**Goals:**

- A cheap, stable search that fits the grant and budget system.
- Compliance with the search provider's terms.

**Non-Goals:**

- Search quality tuning (goggles, re-ranking).
- Page fetching.

## Decisions

### 1. Search is a gateway service, used as a function tool

`POST /v1/search` returns structured results. Projects declare a `web_search`
function tool (from the client helper) and, when the model calls it, call
`/v1/search` and send the compact text back as the function output.

*Rejected:* gateway-executed tool loops. They break "projects own their
loops" and complicate billing and streaming (owner choice, 2026-10-13).

### 2. Reusing admission and the ledger

A search is admitted like a call:

- `provider_call_id` is generated (`search-<uuid>`);
- `request_fingerprint` is a SHA-256 of the parameters;
- `operation` is `web_search`, `provider` is `brave`, and `model` is
  `web-search` or `llm-context`;
- the reservation is the per-request price, so the grant budget, global cap,
  rate, and concurrency all apply.

Settlement:

- completed: charged the price;
- refused before running: 0;
- timeout: `execution_unknown`, charged the reservation (the search may have
  been billed).

No result is stored. A search is never replayed; asking again runs a new
search.

### 3. Per-request prices

`catalogue/search_prices.json` holds dated entries:

```json
{"provider": "brave", "model": "web-search", "effective_from": "2026-10-13",
 "usd_per_request": "0.005", "source_url": "https://api-dashboard.search.brave.com/documentation/pricing"}
```

`llm-context` has no entry until the owner confirms its price, and an
unpriced search is refused with `price_unavailable` (never free). The loader
validates the file strictly, as for model prices.

### 4. The monthly cap

Before admission, the gateway counts searches admitted this UTC month (all
projects, all modes). When the count reaches `SEARCH_MONTHLY_CAP`, it refuses
with `429 search_quota_exhausted` and a `Retry-After` of the next month.

The threshold pass alerts at 80% and 100% of the cap (`search_quota`, once
per month).

The default of 1,000 keeps usage inside Brave's $5 monthly credit.

### 5. Brave adapter

`httpx2` calls with `X-Subscription-Token`, `Accept: application/json`, and a
`SEARCH_TIMEOUT_SECONDS` timeout, default 30 s as Brave's LLM Context guide
recommends.

- An empty `grounding.generic` returns an empty result list, meaning no
  relevant content was found. It is not an error.
- Brave rate-limits on a 1-second sliding window; its 429 is honoured
  through `Retry-After`.

- **Results mode:** `GET /res/v1/web/search?q&count&freshness&country&search_lang&safesearch`.
  Results map to `{title, url, snippet: description, age, extra_snippets?}`.
- **Context mode:** `GET /res/v1/llm/context?q&count&maximum_number_of_tokens&freshness...`.
  `grounding.generic[]` maps to `{url, title, snippets[]}`.

**Failures:**

| Brave answer | Gateway problem |
|---|---|
| Connection not established | `search_unavailable` (retryable) |
| 429 | `search_rate_limited` (retryable, `Retry-After`) |
| 5xx | `search_unavailable` (retryable) |
| 401 or 403 | `search_account_blocked`, with a critical alert |
| Other 4xx | `search_invalid_request` |
| Read timeout | `execution_unknown` |

No key configured means `503 search_not_configured`.

### 6. Privacy and compliance

- **Logs** hold the call key, mode, result count, cost, and latency. Never
  the query or the results.
- **Results** pass through memory only. Nothing is written to the database
  except the ledger row (no content).
- **Terms:** the owner's agent uses results to answer, not to train models,
  which is allowed. The README states the no-storage rule for projects too.

### 7. Client helpers

- `search(query, mode="results", ...)` returns `SearchResult`.
- `web_search_tool()` returns the function tool definition (a strict schema
  with `query` and an optional `freshness`).
- `run_web_search(gateway, arguments_json)` runs the search and returns
  compact numbered text (title, URL, snippet), capped at about 6,000
  characters, ready for `function_output`.
  - It uses **context mode by default**, with Brave's "simple factual"
    budget: `count` 5, `maximum_number_of_tokens` 2,048. That is about USD
    0.0002 of gpt-6-luna input per search.
  - It falls back to results mode while context is `price_unavailable`.

Brave's documentation recommends LLM Context "for any web search where an
agent or model is the intended recipient"
(https://api-dashboard.search.brave.com/documentation/services/llm-context).
The extracted snippets also spare a separate page-fetch step.

## Risks / Trade-offs

- **[Risk]** Brave changes its prices or free credit. **Mitigation:** prices
  are dated data, and the cap is configurable.
- **[Risk]** Snippets are thin for some questions. **Mitigation:** context
  mode, once priced, gives extracted page text. A page-reading change can
  follow.

## Migration Plan

- No database change: searches use `calls`.
- **Rollback:** remove the route. Ledger rows stay valid.
