# Proposal

## Why

A chat or agent project should show the answer as it is written, not after
the whole call ends. Streaming has to keep every guarantee a direct call
already has:

- admission before any provider work;
- an exact ledger;
- execution at most once;
- replay of a finished call;
- no content in logs.

## Expected Outcome

- With `"stream": true`, `POST /v1/responses` answers with server-sent events
  in the gateway's own small schema:
  - `response.started`;
  - `text.delta`;
  - `refusal.delta`;
  - `response.completed`, carrying the same body a direct call returns;
  - `error`, carrying a problem with its code and retry hint.

  Text appears as the model writes it.
- **Refusals before work starts** (validation, admission) are answered as
  ordinary problem JSON before any stream begins.
- **If the client disconnects,** the call still runs to its end in the
  gateway and is settled exactly. Resubmitting the call ID returns
  `call_in_progress` while it runs, then the finished result.
- **The Python client** gains `responses.stream(...)`, an async iterator of
  typed events with the same retry rules.

## What Changes

- **Request:** an optional `stream` field on `POST /v1/responses`.
- **OpenAI adapter streaming:**
  - `stream=True` with `stream_options.include_obfuscation=false`;
  - headers read from the stream;
  - terminal events mapped to the existing outcomes;
  - a mid-stream transport loss, or a stream without a terminal event,
    becomes `execution_unknown`.
- **Upstream consumption** runs in a task separate from the client
  connection; deltas are dropped when no client is listening.
- **A replayed or already-finished call** with `stream: true` answers with a
  one-event stream (`response.completed`, `replayed: true`), or with the
  stored problem as an `error` event.
- **Stub streaming:** words as deltas, plus a `fail_mid_stream` scenario.
- **Client:** `GatewayClient.responses.stream(...)` and typed stream events.

## Non-goals

- Function tools and tool-call events (`add-function-tools`).
- Reasoning summary streaming.
- Resuming a stream at an event offset.
- A separate idle watchdog. The call timeout bounds a stream.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `llm-calls`: adds streamed calls.
- `python-client`: adds the streaming iterator.

Both are additive requirements.

## Impact

- **Code:**
  - the request model;
  - the OpenAI and stub adapters (a stream method);
  - a streaming path in `llm/service.py`;
  - `routes_responses.py`;
  - the client package.
- **Contract and client package:** additive. A non-streamed call is
  unchanged. The client's minor version is raised.
- **Capability:** none new.
