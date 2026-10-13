# Design

## Context

Provider facts are in the local phase 3 research note (official OpenAI docs,
2026-10-13):

- with `store=false`, pass back all reasoning items, function calls, and
  messages since the last user message, untouched;
- `encrypted_content` is included by default without storage;
- take streamed reasoning items from `output_item.done`;
- strict schemas need `additionalProperties: false` and every property listed
  in `required`.

## Goals / Non-Goals

**Goals:**

- Full-quality multi-turn tool loops with no provider-side state.
- Projects never see provider item formats.

**Non-Goals:**

- Executing tools.
- Storing conversations.

## Decisions

### 1. Continuation is opaque, tagged, and bounded

The response field `continuation` is:

```json
{"provider": "openai", "model": "gpt-6-luna", "items": [...]}
```

`items` is the provider's `output` list, exactly as returned.

The project sends it back as an input item `{type: "continuation", ...}`, in
the position where it belongs in the conversation.

**Validation on input:**

- `provider` and `model` must equal the resolved call's, else
  `422 continuation_mismatch`;
- each item must be an object whose `type` is `reasoning`, `message`,
  `function_call`, or a later documented output type;
- each item must be at most 1 MB.

The whole request stays within `REQUEST_MAX_BYTES`.

*Rejected:*

- **Gateway-held conversation state:** it breaks "projects own state" and
  needs storage.
- **Re-deriving items from `tool_calls`:** it loses reasoning context and
  message phases, which the docs warn degrades quality.

### 2. Tool definitions

`{type: "function", name, description?, parameters, strict = true}`.

- `parameters` must be a JSON Schema object. With `strict`, the gateway
  checks only that it is an object. The provider enforces the strict rules,
  and a violation surfaces as `provider_invalid_request` with the provider's
  code.
- `tool_choice`: `"auto" | "none" | "required" | {type: "function", name}`.
  A named function must be in `tools`.
- At most 32 tools; at most 16 KB per description and schema.

### 3. Tool calls in the result

`tool_calls: [{call_id, name, arguments, arguments_json, arguments_error}]`:

- `arguments` is the raw JSON text;
- `arguments_json` is the parsed object, or null;
- `arguments_error` is `invalid_json` when parsing fails.

A response with tool calls is `status: completed`, and `output_text` may be
empty.

### 4. Function outputs

The input item `{type: "function_call_output", call_id, output}`: `output` is
a string of at most 1 MB, passed through as the provider's
`function_call_output`.

### 5. Streaming

When a `function_call` output item is done, the stream emits a `tool_call`
event with `{call_id, name, arguments, arguments_json, arguments_error}`.
`response.completed` carries `tool_calls` and `continuation`, so the stream
and the direct body agree.

### 6. Catalogue flag

`function_tools: bool` per model (true for gpt-6-luna and stub-echo). A
request with `tools` to a model without it is refused with
`422 input_not_supported`.

### 7. Stub

- `stub.tool_calls: [{name, arguments}]` makes the stub return those calls,
  with call IDs `stub_call_<n>`.
- Its continuation holds simple placeholder items, tagged `stub/stub-echo`.
- A request carrying `function_call_output` items and no scripted calls
  returns text, so a full loop can be tested for free.

## Risks / Trade-offs

- **[Risk]** Continuations grow with each turn, through encrypted reasoning.
  **Mitigation:** the request-size limit applies, and the README tells
  projects to start a new continuation chain at each new user message. That
  matches the docs' "since the last user message".
- **[Risk]** Provider item formats change. **Mitigation:** items are opaque
  and passed through untouched, and only their `type` is checked against an
  allowlist that is easy to extend.

## Migration Plan

- No database change.
- The catalogue data gains `function_tools`; the loader defaults it to false
  for other models.
- **Rollback:** reject `tools` with `request_invalid`.
