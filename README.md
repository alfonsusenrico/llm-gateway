# LLM Gateway

A self-hosted gateway that AI projects call instead of a language-model
provider. It holds the provider keys, runs every call, classifies failures,
and keeps spending under a hard monthly cap.

> **Status:** early development. The foundation is in place: settings, error
> format, health checks, migrations, and the local stack. Features below are
> planned and land phase by phase through the specs in `openspec/`.

## Why a gateway

Projects that use language models keep solving the same problems: holding
provider keys safely, deciding what to do when a call fails halfway, keeping
costs in check, and not letting one runaway loop starve everything else. The
gateway solves them once.

- **Projects request, the gateway grants.** Each project asks for requests per
  minute, concurrency, and a monthly budget. The gateway decides what it
  actually gets, and tells the project, from shared capacity and a global
  budget cap.
- **Uncertain calls are never replayed.** A call that may have reached the
  provider is never silently repeated. A stable call ID returns the recorded
  outcome.
- **Cost is tracked per call,** in a usage ledger with effective-dated prices,
  and the cap is enforced before the provider is ever called.
- **Content-free observability.** Logs and alerts carry identifiers, counts,
  and states, never prompts or model output.
- **Owner alerts** go to Telegram, through a channel interface.

## Planned scope

| Area | Status |
|---|---|
| Foundation: settings, RFC 9457 errors, health, migrations | Done |
| Owner notifier (Telegram) | Proposed |
| Project contracts, per-environment API keys, admin CLI | Proposed |
| Request-to-grant allocation | Proposed |
| Direct calls, model profiles, usage ledger, budget enforcement, Python client | Planned |
| Streaming with function tools, hosted tools | Planned |
| Async background jobs, flex tier, dynamic allocation | Planned |
| Embeddings | Planned |

OpenAI is the first provider; the adapter interface and model catalogue are
designed for more.

## Local setup

Requirements: [uv](https://docs.astral.sh/uv/), Docker with Compose, and
Python 3.13 (uv can install it).

1. Create `.env` with a generated local database password:

   ```bash
   ./scripts/setup-local
   ```

2. Install dependencies and start the stack:

   ```bash
   uv sync --locked
   docker compose up -d --build
   ```

   Compose starts Postgres, applies migrations with the one-shot `migrate`
   service, and then starts the API on `127.0.0.1:8360`.

3. Confirm the gateway is ready:

   ```bash
   curl -fsS http://127.0.0.1:8360/health/ready
   ```

Stop the stack with `docker compose down`. Leave out `-v` to keep the local
database.

## Development

```bash
docker compose up -d db   # tests marked db need the local database
./scripts/check           # lock, format, lint, types, tests, compose, OpenSpec
```

Changes follow [OpenSpec](https://github.com/Fission-AI/OpenSpec): every
behaviour change starts as a proposal under `openspec/changes/`.

## License

[MIT](LICENSE)
