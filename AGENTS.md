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

Capabilities: database, api-consumers, llm, personal-data, background-jobs
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
```

## Architecture map

- `src/llm_gateway/settings.py`: the one typed settings object. Errors name
  variables, never values.
- `src/llm_gateway/api/`:
  - `app.py`: the app factory, request IDs, health routes.
  - `problems.py`: RFC 9457 errors and `GatewayError`.
- `src/llm_gateway/notifier/`:
  - `events.py`: `raise_alert`, the only way gateway code alerts the owner. It
    is best-effort and deduplicated per key.
  - `channels.py`: the channel interface and the Telegram adapter.
  - `delivery.py`: claiming, retries, and expiry, run by the worker.
- `src/llm_gateway/worker.py`: the background worker process.
- `src/llm_gateway/admin/`: the `llm-gateway-admin` CLI.
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
- Every limit and budget is a backend grant. A project's contract holds only
  its request; projects never set their own limits.
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
