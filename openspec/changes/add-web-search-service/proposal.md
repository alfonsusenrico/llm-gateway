# Proposal

## Why

Agents need fresh information from the web. The owner chose (2026-10-13) a
gateway search service backed by the Brave Search API over OpenAI's hosted
search:

- **Brave:** its own index, stable, about 1,000 searches a month free, then
  $0.005 per search.
- **OpenAI hosted search:** $0.01 per search plus content tokens.

The search is offered as a gateway service that projects use as an ordinary
function tool in their own loop, so the gateway keeps owning infrastructure
and projects keep owning their loops.

## Expected Outcome

- `POST /v1/search` with a query returns either:
  - ranked results (title, URL, snippet, age), in `results` mode; or
  - pre-extracted page snippets for grounding, in `context` mode, once its
    price is confirmed.
- Each search goes through the project's grant (rate, concurrency, budget)
  and is recorded in the ledger at list price.
- A global monthly search cap (default 1,000, inside Brave's free credit)
  refuses further searches and alerts the owner at 80% and 100%.
- Search results are never stored or cached, as Brave's terms require.
  Logs never contain queries.
- The Python client ships `search(...)` and a ready-made web search tool
  definition with an executor, for a project's tool loop.

## What Changes

- **`POST /v1/search`** (key-authenticated):
  - `query` (at most 400 characters);
  - `mode` (`results` | `context`);
  - `count` (1 to 20);
  - `freshness`, `country`, `search_lang`, `safesearch`;
  - `max_tokens` (context mode, 1,024 to 32,768).
- **Brave adapter:** `/res/v1/web/search` and `/res/v1/llm/context` with
  `X-Subscription-Token`. Failures are mapped: 429 and 5xx are retryable,
  401 and 403 are a blocked account with an alert, 4xx is an invalid
  request.
- **Search price catalogue:** a per-request price, dated.
  - `brave/web-search`: USD 0.005 per request.
  - `brave/llm-context`: unpriced until the owner confirms it in the Brave
    dashboard. Until then, context mode is refused with `price_unavailable`.
- **Ledger and admission:** searches reuse both. Provider `brave`, model
  `web-search` or `llm-context`, operation `web_search`; the reservation
  equals the per-request price. Searches take no call ID and are never
  replayed, because their results must not be stored.
- **Cap and settings:**
  - `SEARCH_MONTHLY_CAP` (default 1,000), with threshold alerts;
  - `SEARCH_TIMEOUT_SECONDS` (default 30, as Brave recommends);
  - `BRAVE_SEARCH_API_KEY` (already in `.env.example`).
- **Client:** `search()`, `web_search_tool()`, and
  `run_web_search(arguments)`, which returns compact text for a
  `function_call_output`. It prefers context mode (Brave's recommendation for
  agents) and falls back to results mode until context is priced.

## Non-goals

- SearXNG or Tavily providers. The adapter interface leaves room; each is
  added in a later change if needed.
- OpenAI hosted web search.
- Fetching full pages (a later "read page" change, possibly Tavily extract).
- Caching or storing results (forbidden by Brave's terms).

## Capabilities

### New Capabilities

- `web-search`: the search request and result shapes, the provider adapter,
  per-request pricing, the monthly cap, no-storage handling, and the client
  tool helpers.

### Modified Capabilities

None. Searches use `call-admission` and `usage-ledger` as they are.

## Impact

- **Code:**
  - new `search` package (request, Brave adapter, prices, service);
  - the route;
  - catalogue data `catalogue/search_prices.json`;
  - settings;
  - the client.
- **Configuration:** `BRAVE_SEARCH_API_KEY` (owner provides),
  `SEARCH_MONTHLY_CAP`, `SEARCH_TIMEOUT_SECONDS`.
- **Contract and client package:** additive.
- **Owner decision needed later:** the LLM Context price, read from the Brave
  dashboard, before context mode is enabled.
