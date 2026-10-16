# LLM Gateway

## Purpose and boundaries

The owner's personal AI inference gateway. Projects call its separate LLM, embedding
and OCR APIs instead of directly managing inference providers. Local inference
requires GPU execution; bounded CPU support work is allowed.

- **The gateway owns:**
  - encrypted project/provider credentials and local or paid inference execution;
  - project contracts and API keys;
  - request-to-grant allocation of limits and budget;
  - failure classification;
  - pricing and the usage ledger, under a hard paid-call monthly cap (USD 5 for now);
  - owner alerts (Telegram now, Discord later).
- **Projects own:** their prompts, the meaning of outputs, tools, memory, jobs,
  and UI.
- **Scope rule:** the gateway owns shared inference, model discovery, supported-field
  validation, execution certainty and accounting. Projects own workflows, prompts,
  conversation history, memory, embedding storage/indexing/search, tool execution,
  web search, document interpretation and third-party application integrations.
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

## Code references

- `src/llm_gateway/api/`: app/lifespan, bearer project authentication, allocation,
  model discovery, `/v1/responses`, `/v1/embeddings` and `/v1/ocr`; RFC 9457 errors
  in `problems.py`.
- `catalogue/`: individual `models/*.json`, dated `prices.json`, strict loaders,
  adapter declarations, executable request schemas and immutable alias bindings.
- `projects/`: current/historical contracts, versioned registry edits, access
  keys, explicit contract upgrades and encrypted provider credentials.
- `llm/`: request/resolution, adapter factories, OpenAI/stub execution, encrypted
  result replay, direct service and detached streaming producers.
- `embeddings/`: bounded text batches, task schemas, native local/OpenAI adapters,
  vector validation and typed results. Vector storage/search remains in projects.
- `ocr/`: bounded PNG/JPEG validation, pinned GPU-only native execution and original-pixel
  text/box results; shared admission, settlement and encrypted replay.
- `calls/`: admission, settlement/state audit, spend and budget thresholds.
  `allocation/`: deterministic integer policy and locked recomputation.
- `notifier/events.py`: `raise_alert`, the sole content-free alert entry point;
  delivery/retries run in `worker.py`.
- `admin/`: shared CLI/menu actions. `db/`: pool and forward-only migration runner.
  `settings.py`: the sole typed settings object.
- `clients/python/`: matching sync/async client, typed errors/results, models
  discovery and certainty-aware retries. Contract guards: `tests/test_client_contract.py`.
- Tests marked `db` require `TEST_DATABASE_URL`; `REQUIRE_DB=1` forbids skips.
  Private phased plans/evidence live in ignored `research/`; approved contracts
  live in `openspec/specs/` and the active change.

## Project rules

- Phase work follows the approved plan. Each phase's design starts by
  reviewing current official provider documentation.
- Uncertain provider execution is never replayed. Stable call IDs return the
  recorded outcome.
- Every contract change goes through the registry service, which writes a
  new version. Never edit `contract_versions` or `project_keys` by hand.
- Models and prices change only by editing the catalogue data files in a
  reviewed change. A price change adds a new `effective_from` entry with its
  `source_url` and never edits an old one. Every paid model needs a standard
  price in effect, or start-up fails. Zero-fee models explicitly declare that billing
  policy and still obey rate/concurrency grants. Run `models sync` after schema
  migration; a registered alias cannot be reassigned, even after retirement.
- Provider calls follow the failure table in `llm/providers.py`:
  - a confirmed refusal is retryable under the same call ID;
  - a send that may have reached the provider is `execution_unknown` and
    never runs again;
  - provider messages are never logged or returned, only status and code.
- Provider credentials are registered with protected input per project/provider;
  never pass them in an inference request, contract, argv value or export. The
  dedicated `PROVIDER_CREDENTIAL_ENCRYPTION_KEY` is separate from the result key.
  A global provider key is never an execution fallback. New/current contracts use
  aliases; upgrade version-1 documents explicitly through the registry.
- Every OpenAI Responses request sends `store: false`. The stateless Embeddings
  API has no `store` parameter and is allowed without it (owner, 2026-10-16).
  Never enable provider-side application storage or conversation state; standard
  provider abuse-monitoring retention still applies.
- A new problem code needs a mapping in
  `clients/python/src/llm_gateway_client/errors.py`. The contract guard test
  fails until it has one. Client and gateway are released together under
  one tag.
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
