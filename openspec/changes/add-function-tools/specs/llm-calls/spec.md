## ADDED Requirements

### Requirement: Calls can declare function tools

A call SHALL accept up to 32 function tools with a name matching
`^[A-Za-z0-9_-]{1,64}$`, an optional description, a JSON Schema object for
parameters, and strict mode on by default, plus a tool choice and a
parallel-calls flag, and SHALL refuse tools for models without function tool
support.

#### Scenario: Named tool choice not declared

- **WHEN** `tool_choice` names a function that is not in `tools`
- **THEN** the call is refused with `422 request_invalid`, before admission

### Requirement: Tool calls are returned with parsed arguments

When the model calls functions, the result SHALL list each call with its call
ID, name, raw arguments, parsed arguments or an `invalid_json` error, and an
opaque continuation tagged with provider and model.

#### Scenario: Model asks for the calendar

- **WHEN** a call with a `get_events` tool makes the model call it
- **THEN** the result holds one tool call with `name: get_events` and
  `arguments_json`, plus a continuation

### Requirement: Loops continue from an opaque continuation

A call SHALL accept a continuation item exactly as returned and function call
outputs, pass the continuation's items to the provider unchanged, and refuse a
continuation from a different provider or model with `422 continuation_mismatch`.

#### Scenario: Second turn

- **WHEN** a project sends the previous continuation and a
  `function_call_output` for its call ID
- **THEN** the provider receives the earlier reasoning and call items
  unchanged, followed by the output, and the model answers

#### Scenario: Mismatched continuation

- **WHEN** a continuation tagged `stub/stub-echo` is sent to a gpt-6-luna
  call
- **THEN** the call is refused with `continuation_mismatch`

### Requirement: Streams announce complete tool calls

A streamed call SHALL emit one `tool_call` event per function call once its
arguments are complete, and its completed event SHALL carry the same tool calls
and continuation as a direct call.

#### Scenario: Streamed tool call

- **WHEN** a streamed call makes the model call one tool
- **THEN** the stream holds one `tool_call` event before `response.completed`
