# Design

## Context

Direct calls already run as a shielded task that settles and stores the result
even when the client disconnects. Provider streaming facts are in the local
phase 3 research note (official OpenAI docs, 2026-10-13):

- usage arrives only in the terminal event;
- the SDK never retries stream consumption;
- headers are on `stream.response`.

## Goals / Non-Goals

**Goals:**

- Same guarantees as direct calls.
- A small stable event schema.
- Honest outcomes for broken streams.

**Non-Goals:**

- Passing provider events through.
- Resuming mid-stream.

## Decisions

### 1. The gateway's own event schema (SSE)

| Event | Data | When |
|---|---|---|
| `response.started` | `{provider_call_id, provider, model}` | the provider accepted the call (`response.created`) |
| `text.delta` | `{delta}` | each output text delta |
| `refusal.delta` | `{delta}` | each refusal delta |
| `response.completed` | the full direct-call body (`replayed` included) | the call finished: `completed` or `incomplete` |
| `error` | `{code, status, title, detail?, retry_after_seconds?}` | the call ended in a problem; the stream then closes |

A comment line `: keep-alive` is sent every 15 s with no event, so proxies
keep the connection.

*Rejected:* forwarding OpenAI events. They are provider-specific, they change,
and they would make every project parse OpenAI's schema.

### 2. Producer task and consumer

- **Producer:** a shielded task, the same idea as direct calls. It reads the
  upstream stream, accumulates text, refusal, and usage, puts gateway events
  on a bounded queue, and settles and stores at the end.
- **Consumer:** the SSE response generator drains the queue.
- **Client gone:** when the generator is cancelled because the client
  disconnected, the producer keeps going. Puts on a closed queue are
  dropped, so a slow or absent client never stalls the provider stream.

*Rejected:* cancelling upstream on disconnect. Usage would be unknown, so the
whole reservation would be charged, and the finished answer could not be
replayed.

### 3. Outcome mapping for streams

| Upstream | Outcome |
|---|---|
| An error raised by `create(stream=True)` before any event (status or connection) | The existing direct-call table: before streaming starts, so problem JSON is answered normally |
| `error` event before `response.created` | Classified by its code, like a status error (for example `rate_limit_exceeded` becomes `temporary_throttle`) |
| `response.completed` / `response.incomplete` | completed, with usage, text, refusal, and incomplete reason |
| `response.failed` | `provider_failed`, charged usage if present |
| `error` event after `response.created` | `provider_failed` (the provider ended the call) |
| Transport error, read timeout, or end of stream without a terminal event | `execution_unknown` (the call may have finished or been billed) |

**Problem as JSON or as an `error` event?** Once the HTTP 200 SSE response
has started, any problem is an `error` event. Before that, it is problem JSON
with its status.

The adapter starts upstream before sending the 200, and waits for the first
upstream event (or a failure) with the normal acknowledgement time. So
refusals before acceptance keep their HTTP status, and a project can tell
"never started" from "started then failed".

### 4. Replay and repeat submissions with `stream: true`

| The call ID is | Answer |
|---|---|
| completed | a stream holding one `response.completed` event with `replayed: true` |
| failed or unknown | a stream holding one `error` event with the stored code |
| running | `409 call_in_progress` (problem JSON) |
| a retryable failure | executed again, as for a direct call |

### 5. OpenAI adapter stream method

- `execute_stream(resolved, request)` yields typed internal events:
  `Started`, `TextDelta`, `RefusalDelta`, `Finished(ProviderSuccess)`,
  `Failed(ProviderFailure)`.
- It uses `responses.create(..., stream=True, stream_options=
  {"include_obfuscation": False}, store=False, extra_headers=...)`.
- It reads `x-request-id` from `stream.response.headers`.
- It closes the stream in `finally`.
- The read timeout per chunk is `LLM_CALL_TIMEOUT_SECONDS`.

### 6. Stub streaming

- The answer is split into words, sent as `text.delta`.
- Existing scenarios fail before `response.started`.
- A new `fail_mid_stream` scenario sends two deltas, then a transport loss
  (`execution_unknown`).

### 7. Client streaming

`GatewayClient.responses.stream(...)` is an async iterator of `Started`,
`TextDelta`, `RefusalDelta`, and `Completed(result: CallResult)`. A problem
raises the typed error.

**Retries:**

- **Before `Started`:** the same rules as direct calls.
- **After `Started`, on a lost connection:** the client resubmits the same
  call ID. A `call_in_progress` answer is retried with backoff until the
  gateway replays `Completed`.
- **Text after a resubmission:** the caller must treat `Completed.result` as
  the authoritative text, because deltas from the lost connection may be
  partial. The README states this.

## Risks / Trade-offs

- **[Risk]** A reasoning model can think for a long time before its first
  delta. **Mitigation:** the per-chunk read timeout is the full call timeout
  (180 s), and keep-alives keep the client connection open.
- **[Trade-off]** A disconnected client's call still costs its full answer.
  In return, the result is exact and replayable.

## Migration Plan

- No database change.
- **Rollback:** reject `stream: true` with `request_invalid`.
