# Proposal

## Why

Agents act through tools. A project needs the model to call its own functions
(look up a calendar, query an app, run a search) and to continue once it has
the results.

Because the gateway sends `store: false`, OpenAI keeps no conversation. Each
turn must therefore resend the earlier output items the model needs:

- reasoning items, whose encrypted content is included by default without
  storage;
- function calls;
- messages, with their `phase`.

The project should never handle OpenAI's formats to do that.

## Expected Outcome

- A call can declare function tools (name, description, a strict JSON
  Schema), a `tool_choice`, and `parallel_tool_calls`.
- When the model calls tools, the result holds `tool_calls` (call ID, name,
  arguments as text and as parsed JSON) and an opaque `continuation`.
- On the next turn, the project sends back:
  - the continuation unchanged, as one input item;
  - one `function_call_output` per tool call.

  The model then continues with full context. The project never parses
  provider items.
- Streams emit a `tool_call` event when each call's arguments are complete.
- The Python client offers helpers: `tool(...)`, `function_output(...)`, and
  `continuation_of(result)`.

## What Changes

- **Request:**
  - `tools` (function tools only, at most 32, names
    `^[A-Za-z0-9_-]{1,64}$`, strict by default);
  - `tool_choice` (`auto`, `none`, `required`, or one named function);
  - `parallel_tool_calls`.
- **Input items** gain `{type: "function_call_output", call_id, output}` and
  `{type: "continuation", provider, model, items}`.
- **Response** gains `tool_calls` and `continuation`, which is the
  provider's full output list, kept intact.
- **The OpenAI adapter** maps tools, function outputs, and continuation
  items. When streaming, reasoning items come from `output_item.done`.
- **Catalogue:** models gain a `function_tools` flag (true for gpt-6-luna and
  the stub).
- **Stub:** can script tool calls (`stub.tool_calls`), and produces a
  continuation.
- **Client:** helpers, the typed `tool_calls`, and stream `ToolCall` events.

## Non-goals

- Gateway-executed tools. The project runs its own tools and its own loop.
- Provider hosted tools such as OpenAI web search. Search is a project
  feature: a project that needs it calls its own search provider as one of
  its function tools.
- Custom (free-form) tools, MCP tools, tool search, and namespaces.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `llm-calls`: adds function tools and continuation.
- `python-client`: adds tool helpers.
- `model-catalogue`: adds the per-model `function_tools` flag.

All are additive.

## Impact

- **Code:** the request model, resolution (a tool-support check), both
  adapters, the response body, the catalogue data and loader, and the client.
- **Contract and client package:** additive. Continuations are opaque to
  projects and tagged with provider and model, so a continuation from another
  provider or model is refused (`continuation_mismatch`).
- **Data:** continuations are part of the stored encrypted result (24 h),
  like any output.
- **Depends on:** `add-streaming-responses`, for the `tool_call` stream
  event.
