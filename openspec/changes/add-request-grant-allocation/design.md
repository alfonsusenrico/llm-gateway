# Design

## Context

Contracts hold a request per environment, and each project has a backend-owned
`weight`. Nothing turns those into allowances yet. Phase 2 will enforce what
this change computes; phase 4 will make it dynamic. See proposal.md for
motivation.

## Goals / Non-Goals

**Goals:**

- One pure, deterministic allocation function, easy to test exhaustively.
- Grants stored and readable cheaply on every request in phase 2.

**Non-Goals:**

- Runtime counters.
- Enforcement.
- Provider feedback.

## Decisions

### 1. Pool and dimensions

The pool is every `(project, environment)` pair of active, non-quarantined
projects. Development and production compete in the same pool, because they
share the same provider account and the same USD 5.

There are three pooled dimensions:

| Dimension | Capacity setting | Default |
|---|---|---|
| requests per minute | `ALLOCATION_CAPACITY_REQUESTS_PER_MINUTE` | 120 |
| concurrency | `ALLOCATION_CAPACITY_CONCURRENCY` | 8 |
| monthly budget | `GLOBAL_MONTHLY_BUDGET_USD` | 5.00 |

The per-call cost ceiling is not pooled: the granted ceiling is the smaller of
the requested ceiling and the granted budget.

### 2. Allocation function (per dimension, integer units)

The budget is handled in micro-USD (1 USD = 1,000,000 units), so every
dimension is an integer.

1. **Guaranteed floor:** each pair gets `min(request, minimum)`. The
   minimum settings default to `ALLOCATION_MIN_REQUESTS_PER_MINUTE` = 5,
   `ALLOCATION_MIN_CONCURRENCY` = 1, and
   `ALLOCATION_MIN_MONTHLY_BUDGET_USD` = 0.10.
   - If the floors alone exceed capacity, the capacity is split by weight
     instead (step 3 with every request capped at its floor). The affected
     pairs get the reason `below_guaranteed_minimum`.
2. **Remaining capacity** `R = capacity - sum(floors)`.
3. **Weighted water-filling:**
   - Repeat until `R` is exhausted or every pair is satisfied: give each
     unsatisfied pair `R * w_i / sum(w_unsatisfied)`, capped at its remaining
     need.
   - Fractions are floored. Leftover units go one at a time by largest
     remainder, with ties broken by `(project_id, environment)`.
4. **Reasons:** any pair granted less than it requested gets the reason
   `capacity_shared` for that dimension, unless step 1 already marked it.

The result is deterministic: the same inputs always give the same grants.
The pure function has no I/O and is property-tested:

- a dimension never exceeds its capacity;
- no grant exceeds its request;
- an undersubscribed pool gives every pair exactly its request.

*Rejected:*

- **First come, first served:** it depends on registration order.
- **Pure proportional to request:** it rewards inflated requests.
- **Strict weight shares without caps:** they waste capacity a satisfied
  project does not need.

### 3. States

- `approved`: every dimension is granted in full.
- `reduced`: at least one dimension is granted less, with a list of
  `{dimension, code}` reasons.

Disabled or quarantined projects have no grant row. They are already refused
at authentication, so a third state is not needed.

### 4. Storage and recomputation

- `grants` holds one row per pair:
  - the granted values;
  - the state and reasons (`jsonb`);
  - `inputs_hash`, a hash of the inputs: every request, weight, capacity,
    and minimum;
  - `computed_at`.
- `recompute()`:
  1. takes `pg_advisory_xact_lock`;
  2. reads all inputs;
  3. if the hash differs from the stored one, replaces all grant rows in one
     transaction;
  4. raises alerts for changed states after commit.
- **When it runs:**
  - by the admin CLI, right after any contract, status, or weight change;
  - by the worker on start and every 60 s, which picks up capacity changes
    after a restart with new settings.
- **API reads** of the grant table are cached for 10 s, the same as
  contracts.

*Rejected:*

- **Computing grants per request:** that is O(projects) on every call, and the
  results are not stored for history or alerts.
- **A database trigger:** it hides the logic away from tests.

### 5. Alerts

- `grant_reduced` (`warning`): a pair becomes `reduced`, or any of its granted
  values decreases.
- `grant_restored` (`info`): a pair returns to `approved`.
- Both are deduplicated per pair. The summary names the project, the
  environment, and the reduced dimensions: codes only, no amounts beyond the
  grant values.

### 6. Visibility

**`GET /v1/allocation`** (key-authenticated) returns:

- the project and environment;
- the state;
- the requested and granted values, with budget amounts as decimal USD
  strings;
- the reasons;
- the profile names;
- `computed_at`.

A pair without a grant yet (just registered, before the recompute) returns
`503 allocation_pending` with `Retry-After: 5`.

**`llm-gateway-admin grants show`** prints a table of every pair: requested
against granted values, the state, and the reasons, followed by pool
utilisation per dimension.

## Risks / Trade-offs

- **[Risk]** Many projects with small requests could fragment concurrency at a
  capacity of 8. **Mitigation:** the guaranteed minimum of 1, plus
  `below_guaranteed_minimum` reasons and alerts, make the problem visible.
  Phase 4 adds borrowing.
- **[Trade-off]** Development keys draw from the same budget as production.
  That is intended: it is one real USD 5. Small development requests keep
  their impact low.

## Migration Plan

- Migration `0003_grants.sql` adds a new table, so it is additive.
- **Rollback:** remove the endpoint. Grants are unused until phase 2.
