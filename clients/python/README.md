# llm-gateway-client

The typed Python client for [LLM Gateway](../../README.md). Use it instead of
calling the HTTP API directly. It handles the retry rules that are easy to get
wrong.

## What it does for you

- **Retries only what is safe, with the same call ID:**
  - refusals the gateway marks as temporary (`provider_unavailable`,
    `rate_limited`, `concurrency_exhausted`, and others), waiting at least
    `Retry-After`;
  - a connection drop to the gateway. The gateway then replays the finished
    answer, or reports that the call is still running.
- **Never retries `execution_unknown`.** The provider may already have run
  the call, so `ExecutionUnknownError` is raised, and you choose whether to
  try again under a new call ID.
- **Raises typed errors:** `BudgetExhaustedError`, `InvalidRequestError`,
  `AuthenticationError`, `GatewayUnavailableError`. Each carries `code`,
  `status`, `request_id`, and `retry_after_seconds`. A code this version does
  not know raises a plain `GatewayError` and is never retried.
- **Keeps within your grant:** at most `X-Concurrency-Limit` calls in flight,
  updated from every response.

## Install

Pin the gateway release you run:

```bash
uv add "llm-gateway-client @ git+https://github.com/alfonsusenrico/llm-gateway@<tag>#subdirectory=clients/python"
```

## Use

```python
from llm_gateway_client import GatewayClient, ExecutionUnknownError, json_schema, message, image

async with GatewayClient("http://llm-gateway:8000", api_key) as gateway:
    result = await gateway.responses.create(
        provider_call_id=f"summary-{report_id}",  # stable: same work, same ID
        operation="weekly_summary",
        profile="chat",
        input="Summarise this week's notes: ...",
    )
    print(result.output_text, result.cost_usd)

    shape = await gateway.responses.create(
        provider_call_id=f"shape-{report_id}",
        operation="classify_image",
        model={"provider": "openai", "model": "gpt-6-luna"},
        input=[message("user", "Which shape is this?", image("shape.png", "image/png"))],
        text_format=json_schema(
            "shape",
            {
                "type": "object",
                "properties": {"shape": {"type": "string"}},
                "required": ["shape"],
                "additionalProperties": False,
            },
        ),
    )
    print(shape.output_json)
```

### Streaming

```python
from llm_gateway_client import Completed, TextDelta

async for event in gateway.responses.stream(
    provider_call_id=f"reply-{message_id}",
    operation="chat_reply",
    profile="chat",
    input="Tell me about the weather in one sentence.",
):
    if isinstance(event, TextDelta):
        print(event.delta, end="", flush=True)
    elif isinstance(event, Completed):
        answer = event.result.output_text  # authoritative text, usage, and cost
```

Events are `Started`, `TextDelta`, `RefusalDelta`, and `Completed`. A problem
raises the same typed errors as `create`. Refusals before `Started` are retried
like a direct call. If the connection drops after `Started`, the client
resubmits the same call ID and finishes with the replayed `Completed`. Deltas
from the lost connection may be partial, so always use `Completed.result` as
the final text.

### Tool loop

Your project runs its own tools and loop; the client only builds the turns.

```python
from llm_gateway_client import continuation_of, function_output, tool

events_tool = tool(
    "get_events",
    {
        "type": "object",
        "properties": {"day": {"type": "string"}},
        "required": ["day"],
        "additionalProperties": False,
    },
    description="Calendar events for one day.",
)
history = [message("user", "What is on today?")]
result = await gateway.responses.create(
    provider_call_id=f"{turn_id}-0",
    operation="assistant",
    profile="chat",
    input=history,
    tools=[events_tool],
)
chain = []  # every step since the user's message, in order
step = 0
while result.tool_calls and step < 8:
    chain.append(continuation_of(result))
    chain += [
        function_output(call.call_id, run_tool(call.name, call.arguments_json))
        for call in result.tool_calls
    ]
    step += 1
    result = await gateway.responses.create(
        provider_call_id=f"{turn_id}-{step}",
        operation="assistant",
        profile="chat",
        input=[*history, *chain],
        tools=[events_tool],
    )
print(result.output_text)
```

- `continuation_of(result)` carries the model's reasoning and calls into the
  next call unchanged; never edit it. The chain keeps every step's
  continuation and outputs, in order, after the history: the provider stores
  nothing between calls.
- `call.arguments_json` is `None` when the model sent invalid JSON; then
  `call.arguments_error` says why. Answer with an error output so the model
  can retry.
- A final answer has no continuation. Store its text in your own history and
  start a new chain at the next user message.
- Bound the loop, as the 8-step limit above does, so a confused model cannot
  run up cost.
- Streams yield a `ToolCall` event as each call's arguments complete.

`SyncGatewayClient` offers the same interface without `async`, except
streaming.

Choose a `provider_call_id` that identifies the unit of work, for example the
report and step. Resubmitting the same ID is always safe: a finished call
returns its stored answer (`result.replayed` is `True`) for 24 hours, at no
extra cost.

## Development

Tests run from the gateway repository with `./scripts/check`. The contract
tests in `tests/test_client_contract.py` exercise this client against the real
gateway application.

### OCR (gateway/client 0.7.0)

Use `client.ocr.create(provider_call_id=..., operation=..., model=...,
**ocr_image("example.png", "image/png"))` with `ocr_image` imported from
`llm_gateway_client`. Sync and async clients return `OcrResult` with text,
confidence, quadrilateral boxes and original image dimensions. The helper reads
local bytes/files on the client. See the [OCR contract](../../docs/ocr.md) for
limits, GPU requirements, profiles, accounting and recovery.
