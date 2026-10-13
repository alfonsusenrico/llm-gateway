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
| Owner notifier (Telegram) | Done |
| Project contracts, per-environment API keys, admin CLI | Done |
| Request-to-grant allocation | Done |
| Model catalogue and pricing | Done |
| Call admission, usage ledger, budget enforcement | Done |
| Direct OpenAI calls, stub provider, result replay | Done |
| Python client | Done |
| Streamed responses | Done |
| Function tools | Done |
| Async background jobs, flex tier, dynamic allocation | Planned |

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

### Owner alerts (optional)

The worker sends owner alerts to Telegram.

1. Create a dedicated bot with [@BotFather](https://t.me/BotFather) and send
   it a message.
2. Read your chat ID (`message.chat.id`) from
   `https://api.telegram.org/bot<token>/getUpdates?allowed_updates=["message"]`.
   The filter matters: Telegram remembers the last `allowed_updates` a bot
   used, and without it the result can stay empty.
3. Set both values in `.env`:

   ```bash
   NOTIFIER_TELEGRAM_BOT_TOKEN=<token>
   NOTIFIER_TELEGRAM_CHAT_ID=<chat id>
   ```

4. Restart and send a test alert:

   ```bash
   docker compose up -d
   docker compose run --rm api llm-gateway-admin notifier test
   ```

Without these values, alerts stay pending and expire after 24 hours; the
gateway keeps working.

### Registering a project

Each project gets a contract and one API key per environment (`production` and
`development`). Keys are printed once and never stored, so save them right
away.

```bash
docker compose run --rm api llm-gateway-admin project register my_project --display-name "My Project"
docker compose run --rm api llm-gateway-admin project set-request my_project --env production --rpm 60 --concurrency 4
docker compose run --rm api llm-gateway-admin project rotate-key my_project --env production --grace-minutes 30
docker compose run --rm api llm-gateway-admin            # interactive menu with every action
```

Grants are recomputed after every change. To see them all, run
`llm-gateway-admin grants show`. A heavier weight wins more of a contested
dimension:

```bash
docker compose run --rm api llm-gateway-admin grants show
docker compose run --rm api llm-gateway-admin project set-weight my_project 3
```

Profiles name a catalogue model (`GET /v1/catalogue` lists them with
current prices). A profile is checked when saved: unknown models, unsupported
reasoning efforts, and a temperature with reasoning on gpt-6-luna are
refused.

```bash
docker compose run --rm api llm-gateway-admin project set-profile my_project chat --provider openai --model gpt-6-luna --reasoning-effort low
```

To add a model or change a price, edit `src/llm_gateway/catalogue/models.json`
or add a dated entry to `prices.json`, then run `./scripts/check`.

### Python client

Projects should use the typed client in [`clients/python`](clients/python/README.md).
It resubmits the same call ID only when that is safe, raises typed errors, and
keeps its concurrency within the project's grant.

### Making a call

A development key can call the free stub model, which never contacts a
provider:

```bash
curl -s http://127.0.0.1:8360/v1/responses \
  -H "Authorization: Bearer $DEV_KEY" -H "Content-Type: application/json" \
  -d '{"provider_call_id": "demo-1", "operation": "demo",
       "model": {"provider": "stub", "model": "stub-echo"}, "input": "Hello"}'
```

- **Real models:** use a profile (`"profile": "chat"`) or a catalogue model
  (`{"provider": "openai", "model": "gpt-6-luna"}`). Setting
  `OPENAI_API_KEY` enables OpenAI.
- **Input:** text, or messages with `input_text`, `input_image` (base64
  PNG, JPEG, WEBP, or GIF), and `input_file` (base64 PDF) parts.
- **Structured output:** `text_format` asks for a strict JSON schema, and
  the parsed answer comes back in `output_json`.
- **Allowance headers:** every response carries
  - `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`;
  - `X-Concurrency-Limit`, `X-Concurrency-Remaining`;
  - `X-Budget-Remaining-USD`.
- **Resubmitting the same `provider_call_id`** is always safe:
  - a completed call returns its stored answer (`replayed: true`) for 24
    hours, with no new charge;
  - a call refused for a temporary reason runs again;
  - a call whose outcome is unknown (`execution_unknown`) never runs
    again. Use a new ID to try once more.
- **Streaming:** `"stream": true` answers with server-sent events (see
  below).
- **Function tools:** the model can ask the project to run its functions
  (see below).
- **Stub scenarios:** the stub accepts
  `"stub": {"reply": ..., "scenario": "overloaded"}` to simulate each
  failure type, so a project can test its error handling for free.

### Streaming a call

```bash
curl -sN http://127.0.0.1:8360/v1/responses \
  -H "Authorization: Bearer $DEV_KEY" -H "Content-Type: application/json" \
  -d '{"provider_call_id": "demo-stream-1", "operation": "demo", "stream": true,
       "model": {"provider": "stub", "model": "stub-echo"}, "input": "Hello there"}'
```

- **Events:** `response.started`, then `text.delta` and `refusal.delta`
  events, and finally `response.completed` with the same body as a direct
  call, including usage and cost. A failure after the start arrives as one
  `error` event with the problem fields.
- **Refusals before the start** (budget, grant, provider unavailable) are
  ordinary problem JSON with their status code, as for a direct call.
- **Keep-alive:** a `: keep-alive` comment every 15 seconds keeps quiet
  streams open while a model thinks.
- **Disconnects:** if the client leaves, the gateway still reads the answer
  to its end, charges its real usage, and stores it. Resubmitting the call
  ID returns it.
- **Replays:** resubmitting a finished call ID with `"stream": true` answers
  with one event: `response.completed` (`replayed: true`) or `error` with the
  recorded code.

### Function tools

The model can ask the project to run its own functions. The project runs them
and its own loop; the gateway only carries the turns. On the stub, the free
`stub.tool_calls` script makes the model ask for a call:

```bash
TOOLS='[{"type": "function", "name": "get_events",
  "parameters": {"type": "object", "properties": {"day": {"type": "string"}},
                 "required": ["day"], "additionalProperties": false}}]'
FIRST=$(curl -s http://127.0.0.1:8360/v1/responses \
  -H "Authorization: Bearer $DEV_KEY" -H "Content-Type: application/json" \
  -d "{\"provider_call_id\": \"tools-1\", \"operation\": \"demo\",
       \"model\": {\"provider\": \"stub\", \"model\": \"stub-echo\"},
       \"input\": \"What is on today?\", \"tools\": $TOOLS,
       \"stub\": {\"tool_calls\": [{\"name\": \"get_events\", \"arguments\": {\"day\": \"today\"}}]}}")
echo "$FIRST" | jq '.tool_calls'

jq -n --argjson first "$FIRST" --argjson tools "$TOOLS" '{
  provider_call_id: "tools-2", operation: "demo",
  model: {provider: "stub", model: "stub-echo"}, tools: $tools,
  input: [
    ({type: "continuation"} + $first.continuation),
    {type: "function_call_output", call_id: $first.tool_calls[0].call_id,
     output: "[\"standup at 9\"]"}
  ]}' |
curl -s http://127.0.0.1:8360/v1/responses \
  -H "Authorization: Bearer $DEV_KEY" -H "Content-Type: application/json" -d @- |
jq '.output_text'
```

- **Tools:** up to 32 functions, each with a name, an optional description,
  and a JSON Schema for its parameters. Strict mode is on by default, so
  schemas need `additionalProperties: false` and every property in
  `required`. `tool_choice` (`auto`, `none`, `required`, or one named
  function) and `parallel_tool_calls` are optional.
- **Tool calls:** each one has `call_id`, `name`, the raw `arguments` text,
  and `arguments_json`. If the model sent invalid JSON, `arguments_json` is
  null and `arguments_error` says why; the call itself still completed.
- **Continuation:** a turn with tool calls also returns `continuation`. Send
  it back unchanged as one input item, followed by one
  `function_call_output` per call. It carries the model's earlier reasoning
  and calls, because the provider stores nothing between turns. In a loop of
  several steps, keep every step's continuation and outputs, in order. A
  continuation only fits the model that produced it
  (`continuation_mismatch` otherwise).
- **One chain per user message:** a final answer has no continuation. For the
  next user message, the project sends its own history as messages and starts
  a new chain, so continuations never grow without bound.
- **Streams** emit a `tool_call` event when each call's arguments are
  complete. `response.completed` holds the same `tool_calls` and
  `continuation` as a direct call.

Projects send their key as `Authorization: Bearer <key>`.
`GET /v1/projects/self` confirms which project and environment a key belongs
to. `GET /v1/allocation` shows what that key requested, what it was granted,
and why anything was reduced.

- `PROJECT_KEY_PEPPER` signs every key hash. Back it up with the database;
  changing or losing it invalidates every key.
- Behind a reverse proxy or tunnel, set `TRUSTED_CLIENT_IP_HEADER` (for
  example `CF-Connecting-IP` for a Cloudflare tunnel), so failed-attempt
  throttling counts real clients, not the proxy.

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
