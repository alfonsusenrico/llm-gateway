## Purpose

Give registered projects a cheap, stable web search through the gateway, governed
by their grants and the global budget, used as an ordinary function tool in their
own loops, and handled in line with the search provider's terms.

## ADDED Requirements

### Requirement: Projects can search the web through the gateway

`POST /v1/search` SHALL accept a query of at most 400 characters with a mode
(`results` or `context`), a result count of 1-20, and optional freshness,
country, language, and safe-search settings, and SHALL return results as title,
URL, snippet, and age, or, in context mode, URL, title, and extracted snippets.

#### Scenario: Results search

- **WHEN** a project searches "brave search api pricing" in results mode
- **THEN** it receives up to the requested number of results, each with a
  title, URL, and snippet, plus the search cost

### Requirement: Searches go through admission and the ledger

Each search SHALL be admitted against the project's grant and the global cap like
a model call, charged its dated per-request price in the ledger, and refused with
`price_unavailable` when no price applies.

#### Scenario: Context mode not yet priced

- **WHEN** a project searches in context mode before a context price exists
- **THEN** it is refused with `price_unavailable`, and nothing is charged

### Requirement: Monthly search cap

The gateway SHALL refuse searches with `429 search_quota_exhausted` once the
searches admitted this UTC month reach the configured cap, and SHALL alert the
owner once at 80% and once at 100% of the cap per month.

#### Scenario: Cap reached

- **WHEN** the 1,000th search of the month has been admitted under a cap of
  1,000
- **THEN** the next search is refused with `search_quota_exhausted`, and
  `Retry-After` points to the next month

### Requirement: Search results are never stored and queries never logged

The gateway SHALL NOT store or cache search results or log queries; only a
content-free ledger record of each search SHALL persist.

#### Scenario: After a search

- **WHEN** a search completes
- **THEN** the database holds only its ledger row, and the logs hold no
  query text

### Requirement: Search failures are classified

The gateway SHALL map provider rate limits and server errors to retryable
problems with a retry hint, authentication failures to a non-retryable problem
with an owner alert, other client errors to `search_invalid_request`, a missing
key to `search_not_configured`, and timeouts to `execution_unknown`.

#### Scenario: Provider rate limit

- **WHEN** the provider answers 429
- **THEN** the search fails with `429 search_rate_limited` and `Retry-After`

### Requirement: Client ships a ready web search tool

The Python client SHALL provide a search method, a function tool definition for
web search, and an executor that runs a search from the model's tool arguments
and returns compact text suitable as the function output.

#### Scenario: Tool loop with search

- **WHEN** the model calls the `web_search` tool in a project's loop
- **THEN** the executor returns numbered results as text, ready to send back
  as the function output
