# Design

## Context

Grants live in `grants`, prices in the catalogue, and alerts in the notifier.
The API can run several processes, and a call may be abandoned when a process
dies. See proposal.md for motivation.

## Goals / Non-Goals

**Goals:**

- Exact, multi-process-safe enforcement.
- One row per call, as the audit trail.

**Non-Goals:**

- Sub-second fairness or queueing. A refused call is retried by the client
  after `Retry-After`.

## Decisions

### 1. One table serves as concurrency, rate, and budget source

`calls` columns:

- `call_key` (identity primary key);
- `project_id`, `environment`, `provider_call_id`
  (unique per project and environment);
- `request_fingerprint` (SHA-256 of the canonical request);
- `operation`, `provider`, `model`, `service_tier_requested`;
- `state`: `admitted`, `completed`, `failed`, `retryable_failure`, or
  `execution_unknown`;
- `reserved_micro_usd`, `cost_micro_usd` (null when unpriced or unknown);
- usage token columns and `service_tier_used`;
- `failure_code`;
- `admitted_at`, `lease_expires_at`, `finished_at`.

From this one table:

- **Concurrency** is the count of `admitted` rows for the pair.
- **Rate** is the count of rows admitted in the last 60 s for the pair.
- **Spend** for a month is the sum of `cost_micro_usd`, or of
  `reserved_micro_usd` when the cost is unknown, over finished rows plus
  `reserved_micro_usd` of admitted rows, all within that UTC calendar month.

*Rejected:*

- **Per-process counters:** wrong with two processes.
- **Redis:** a new dependency for one host.
- **A separate counters table:** it can drift from the ledger.

### 2. Admission is one short transaction under a global advisory lock

The transaction:

1. `pg_advisory_xact_lock(<admission key>)`;
2. reads the pair's grant, the pair's spend and admitted count and recent
   count, and the global spend;
3. decides;
4. inserts the row.

There are no network calls inside it. One global lock keeps the global cap
exact, and at this scale a single serialization point costs about a
millisecond per call.

Before the lock, the reservation is priced. A model and tier with no price in
effect is refused with `price_unavailable` (503, not retryable), because an
unpriced call can never be admitted against a budget.

The first failing check sets the refusal code, in this order:

1. no grant: `allocation_pending` (503, retry in 5 s);
2. the reservation exceeds the per-call ceiling: `cost_ceiling_exceeded`
   (422, never retryable);
3. concurrency is full: `concurrency_exhausted` (429, `Retry-After: 1`);
4. the rate is full: `rate_limited` (429; `Retry-After` is the seconds until
   the oldest counted call leaves the 60 s window);
5. the grant budget is exceeded: `budget_exhausted` (429; `Retry-After` is
   the seconds to the next UTC month);
6. the global cap is exceeded: `budget_exhausted` (429; also raises a
   `critical` alert).

*Rejected:*

- **`SERIALIZABLE` without a lock:** more retries and harder reasoning.
- **Per-pair locks only:** they cannot keep the global cap exact.

### 3. Worst-case reservation

`reserved = cost(input_tokens = ceil(request_bytes / 2), output_tokens =
max_output_tokens)` at the model's uncached input price for the requested
tier, using the long-context rates when the estimate crosses the threshold.

- Two bytes per token deliberately over-estimates text, which averages
  about four bytes per token.
- Image and file parts are counted by their base64 size, which
  over-estimates them too.
- `max_output_tokens` is required at admission. The call endpoint defaults
  it from the profile or the model.

The reservation only gates admission. Settlement records the real cost.

### 4. Settlement and outcomes

`settle(call_key, outcome)` sets the state, usage, `cost_micro_usd` (priced
from the usage and the tier the provider reports), `finished_at`, and the
failure code.

The tier priced is the one the provider reports, mapped to catalogue tier
names: OpenAI reports its standard tier as `default`. The mapping lives in
the catalogue. An unmapped name, such as `priority`, stays unpriced, never
free.

| Outcome | State | Cost charged |
|---|---|---|
| Success, or incomplete with usage | `completed` | priced usage |
| Provider definitively refused before running (4xx validation, auth, quota) | `failed` | 0 |
| Confirmed temporary refusal (429, 503, connection not established) | `retryable_failure` | 0; the same call ID may be admitted again |
| Timeout or transport loss after sending | `execution_unknown` | the reservation is kept as spend (conservative) |
| Usage reported but the price is unknown | `completed`, `cost_micro_usd` null | the reservation is counted as spend, and an `error` alert is raised once per model and tier |

### 5. Re-admission of the same call ID

- Admitting an existing `(project, environment, provider_call_id)` with a
  different fingerprint returns `idempotency_conflict` (409).
- With the same fingerprint:
  - `retryable_failure`: the row is re-admitted, with new admitted and lease
    times and a new reservation;
  - `admitted`: `call_in_progress` (409, retryable after 2 s);
  - `completed`, `failed`, or `execution_unknown`: the admission result
    returns the existing row, and the call endpoint replays the stored
    result or outcome (next change).

### 6. Abandoned calls

`lease_expires_at = admitted_at + LLM_CALL_TIMEOUT_SECONDS + 60 s`. The worker
marks admitted rows past their lease as `execution_unknown`, with failure code
`execution_abandoned`, keeping the reservation as spend.

### 7. Budget threshold alerts

After each settlement and recovery pass, the gateway compares month-to-date
spend with each grant's budget and with the global cap. When spend crosses
50%, 80%, or 100% for the first time in a month, it raises `budget_threshold`
(`info`, `warning`, `critical`):

- deduplicated by pair, month, and threshold;
- the summary names the pair and the amounts in USD.

An `alerts_sent` table records the thresholds already reported, so a restart
never repeats them.

### 8. Allowance headers

The admission result carries the limit, remaining, and reset values that
the API layer writes:

- **`X-RateLimit-*`:** per minute.
- **`X-Concurrency-*`:** concurrent calls.
- **`X-Budget-Remaining-USD`:** the grant budget left this month. It is the
  lower of what remains in the grant and what remains under the global cap.

### 9. Integrity kit (money capability)

- **Snapshots:** each call stores the price entry it was charged under,
  `price_effective_from` and `price_tier`. History never recomputes from
  current prices.
- **State machine:**
  - `admitted` can become `completed`, `failed`, `retryable_failure`, or
    `execution_unknown`;
  - `retryable_failure` can become `admitted` again (re-admission);
  - every other transition is rejected in code. A database `CHECK` limits
    `state` to the five values.
- **Audit:** `call_events` is append-only and never updated or deleted. It
  records `call_key`, `from_state`, `to_state`, `cost_micro_usd`, `actor`
  (`api:<process>`, `worker`), and `at` for every transition, inside the same
  transaction.
- **Currency:** every amount is USD, the only currency the provider bills
  in, and is stored as integer micro-USD.

## Risks / Trade-offs

- **[Risk]** The global lock limits throughput. **Mitigation:** the work
  under the lock is small and indexed. Revisit with measured load in
  phase 4.
- **[Risk]** Over-reservation refuses calls that would have fit.
  **Mitigation:** the reservation only gates; the real cost is settled. A
  project can lower `max_output_tokens`.
- **[Trade-off]** Unknown outcomes are charged their reservation. That
  protects the cap and may over-count, which is acceptable for a hard
  budget.

## Migration Plan

- Migration `0004_calls.sql` adds the `calls`, `call_events`, and
  `alerts_sent` tables,
  with indexes on `(project_id, environment, state)` and
  `(project_id, environment, admitted_at)`. It is additive.
- **Rollback:** unused until the call endpoint exists.
