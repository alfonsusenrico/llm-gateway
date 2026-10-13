# Proposal

## Why

The owner decided that every limit belongs to the backend. A project only
requests; the gateway grants.

When limits live in scattered environment variables and nothing tells a
project what it may use, projects size themselves blind. This change computes
a grant for every project environment from its request and the gateway's
capacity, and shows that grant to the project.

## Expected Outcome

- After any contract or policy change, every active project environment has a
  grant within seconds:
  - its granted requests per minute, concurrency, monthly budget, and per-call
    cost ceiling;
  - a state, `approved` or `reduced`;
  - the reason for any reduction.
- A project reads its own grant at `GET /v1/allocation`.
- The owner sees all grants with one command, and is alerted when a grant is
  reduced or restored.
- The grants' combined monthly budget never exceeds the global cap (USD 5).

## What Changes

- **Capacity and guaranteed-minimum settings** (backend-owned):
  - requests per minute, concurrency, and the global monthly budget cap;
  - a guaranteed minimum for each of those three dimensions.
- **Per-project weights:** stored by the gateway and set with the admin CLI.
- **A deterministic allocation:**
  1. Each request receives up to its guaranteed minimum.
  2. Remaining capacity is shared by weight, capped at each request.
- **Grants stored per project environment,** recomputed when contracts,
  weights, or capacity change. Alerts are raised on reductions and
  restorations.
- **`GET /v1/allocation`** for the authenticated project environment.
- **Admin CLI:** `grants show` and `project set-weight`.

## Non-goals

- Enforcing grants: the request rate, concurrency, budget reservation, and
  cost ceiling are phase 2.
- Dynamic reallocation from provider pushback or budget pacing, and borrowing
  idle capacity (phase 4).
- Owner approval workflows for request changes (later, through Telegram
  buttons).

## Capabilities

### New Capabilities

- `resource-allocation`: how requests become grants, the grant states and
  reasons, recomputation, visibility to projects and the owner, and the
  global budget cap.

### Modified Capabilities

None.

## Impact

- **Code:**
  - new `allocation` package (a pure allocation function, repository, and
    recompute service);
  - the worker gains a recompute check;
  - the admin CLI gains commands;
  - migration `0003`.
- **Configuration:** `ALLOCATION_*` and `GLOBAL_MONTHLY_BUDGET_USD` settings.
- **Contract and client package:** no contract change. It adds the read-only
  endpoint `GET /v1/allocation`; the phase 2 client will consume it.
- **Depends on:** `add-project-contract-registry` and `add-owner-notifier`.
