# LLM Gateway

## Purpose and boundaries

The owner's personal LLM gateway. Every AI project calls it instead of a provider.

- **The gateway owns:**
  - provider keys and execution;
  - project contracts and API keys;
  - request-to-grant allocation of limits and budget;
  - failure classification;
  - pricing and the usage ledger, under a hard monthly cap (USD 5 for now);
  - owner alerts (Telegram now, Discord later).
- **Projects own:** their prompts, the meaning of outputs, tools, memory, jobs,
  and UI.
- **It never:**
  - provides browser login or proxying;
  - logs prompts or model output;
  - infers a model.

Capabilities: database, api-consumers, llm, personal-data, background-jobs, money
Design surface: none
Language: no user interface; API messages, logs, and docs are English.

## Commands

```bash
./scripts/setup-local                  # create .env once (generated local DB password)
uv sync --locked
docker compose up -d db                # database for tests marked db
./scripts/check                        # every check: lock, format, lint, pyright, tests, compose, OpenSpec
uv run ruff format                     # apply formatting
docker compose up -d --build           # local stack: migrate, then API on 127.0.0.1:8360 and worker
curl -fsS http://127.0.0.1:8360/health/ready
docker compose run --rm api llm-gateway-admin notifier test   # one real alert (needs NOTIFIER_TELEGRAM_*)
docker compose run --rm api llm-gateway-admin                  # interactive admin menu
docker compose run --rm api llm-gateway-admin project --help   # registry subcommands
docker compose run --rm api llm-gateway-admin grants show      # every grant and pool use
```

## Architecture map

- `src/llm_gateway/settings.py`: the one typed settings object. Errors name
  variables, never values.
- `src/llm_gateway/api/`:
  - `app.py`: the app factory, request IDs, health routes.
  - `problems.py`: RFC 9457 errors and `GatewayError`.
  - `auth.py`: the bearer-key `require_project` dependency, failed-attempt
    throttling, and the 10 s contract cache with per-project quarantine.
  - `routes_projects.py`: `GET /v1/projects/self`.
  - `routes_allocation.py`: `GET /v1/allocation`, with a 10 s grant cache.
- `src/llm_gateway/allocation/`:
  - `policy.py`: the pure, deterministic allocation (guaranteed minimum, then
    weighted water-filling, in integer units).
  - `store.py`: `recompute()` under an advisory lock, skipped when the inputs
    hash is unchanged, with reduction and restoration alerts after commit.
- `src/llm_gateway/money.py`: USD as integer micro-USD.
- `src/llm_gateway/catalogue/`:
  - `models.json` and `prices.json`: the reviewed data.
  - `model.py`: strict loading, checked at start-up.
  - `pricing.py`: `cost_micro_usd`, rounded up; `None` means unpriced.
  - `profiles.py`: contract profile checks, run on save for new or changed
    profiles only.
- `src/llm_gateway/api/routes_catalogue.py`: `GET /v1/catalogue`.
- `src/llm_gateway/calls/`:
  - `admission.py`: `admit()`, one transaction under a global advisory lock,
    with checks in a fixed order.
  - `settlement.py`: `settle()`, `recover_abandoned()`, and the state
    machine with audit events.
  - `spend.py`: month-to-date spend.
  - `budget.py`: the 50/80/100% threshold alerts.
- `src/llm_gateway/projects/`:
  - `contract.py`: contract version 1, strict, with unknown fields rejected.
  - `keys.py`: the key format and HMAC storage.
  - `service.py`: versioned edits under row locks, keys, import and export.
- `src/llm_gateway/notifier/`:
  - `events.py`: `raise_alert`, the only way gateway code alerts the owner. It
    is best-effort and deduplicated per key.
  - `channels.py`: the channel interface and the Telegram adapter.
  - `delivery.py`: claiming, retries, and expiry, run by the worker.
- `src/llm_gateway/worker.py`: the background worker process.
- `src/llm_gateway/admin/`: the `llm-gateway-admin` CLI. Subcommands and the
  menu share `project_commands.py`.
- `src/llm_gateway/db/`:
  - `pool.py`: the async pool.
  - `migrate.py`: the checksummed, forward-only runner.
  - `migrations/NNNN_name.sql`: hand-written migrations.
- `tests/`: tests marked `db` need `TEST_DATABASE_URL`; `REQUIRE_DB=1` makes a
  missing database fail.
- The phased plan is local-only in `research/notes/`; this repository is
  public, so `research/` is ignored by git. Specs are in `openspec/specs/`.

## Project rules

- Phase work follows the approved plan. Each phase's design starts by
  reviewing current official provider documentation.
- Uncertain provider execution is never replayed. Stable call IDs return the
  recorded outcome.
- Every contract change goes through the registry service, which writes a
  new version. Never edit `contract_versions` or `project_keys` by hand.
- Models and prices change only by editing the catalogue data files in a
  reviewed change. A price change adds a new `effective_from` entry with its
  `source_url` and never edits an old one. Every model needs a standard
  price in effect, or start-up fails.
- Raw project keys are printed once, at issue, and never stored or logged.
  `PROJECT_KEY_PEPPER` must never change; changing it invalidates every key.
- Every limit and budget is a backend grant. A project's contract holds only
  its request; projects never set their own limits.
  - Capacity and minimums are `ALLOCATION_*` and `GLOBAL_MONTHLY_BUDGET_USD`
    settings; weights live on `projects.weight`.
  - Any change to their inputs must call `allocation.store.recompute()`. The
    admin CLI does so after contract, status, and weight changes, and the
    worker every 60 s.
  - Money is integer micro-USD internally and a decimal string at the API.
    Never use floats.
- The ledger is the `money` capability:
  - `calls` changes state only through `admit` and `settle`, which run
    `check_transition` and append to `call_events` in the same
    transaction;
  - `call_events` is append-only, enforced by a database trigger;
  - unknown executions and unpriced usage are charged their reservation.
- Logs hold IDs, counts, durations, and states only. HTTP client libraries are
  capped at WARNING because they log request URLs, and some URLs carry
  credentials.
- Alerts carry fixed sentences and identifiers only. Callers build the
  deduplication key with `dedupe_key(event_type, *ids)`, and nothing on a
  request path waits for delivery.
- Migrations run as the compose `migrate` step, never at app startup. An
  applied migration file never changes.
- Live provider calls cost real money: only run them for tasks the owner
  authorised.
- The repository is public on GitHub. Tracked files hold no secrets, no
  personal infrastructure details, and nothing about the owner's employer
  or other organisations' systems. Planning notes stay in the git-ignored
  `research/`.
- Code reaches `main` only through owner-reviewed pull requests from topic
  branches. Deployment waits until the owner accepts the gateway.

## Overrides

- `default:ci`: hosted CI is on hold until the owner accepts the gateway for
  deployment (owner, 2026-10-13). `./scripts/check` is the gate and runs
  before every commit. The framework hooks enforce commit messages and
  protected-branch pushes.

## References

`openspec/specs/`; `openspec/changes/`; the local plan in `research/notes/`.
