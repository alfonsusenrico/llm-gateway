## ADDED Requirements

### Requirement: Calls can be streamed as gateway events

With `stream: true`, a call that is admitted SHALL be answered with server-sent
events in the gateway's schema: `response.started` when the provider accepts it,
`text.delta` and `refusal.delta` as content arrives, and `response.completed`
with the same body a direct call returns.

#### Scenario: Streamed answer

- **WHEN** a project streams a call that completes
- **THEN** it receives `response.started`, one or more `text.delta`
  events, and `response.completed` with usage and cost, and the ledger
  records one completed call

### Requirement: Refusals before acceptance keep their HTTP status

Validation failures, admission refusals, and provider refusals that happen
before the provider accepts a streamed call SHALL be answered as problem JSON
with their HTTP status, not as a stream.

#### Scenario: Concurrency full

- **WHEN** a streamed call is refused for concurrency
- **THEN** the answer is `429 concurrency_exhausted` problem JSON with
  `Retry-After`

### Requirement: Failures after acceptance end the stream with an error event

A streamed call that fails after the provider accepted it SHALL end with one
`error` event carrying the problem code and retry hint, and SHALL be settled with
the same outcome rules as direct calls.

#### Scenario: Connection lost mid-stream

- **WHEN** the provider connection breaks after some text was streamed
- **THEN** the stream ends with an `error` event `execution_unknown`, and
  the call is never executed again under that call ID

### Requirement: A disconnected client never stops settlement

When the client disconnects from a streamed call, the gateway SHALL keep reading
the provider stream to its end, settle the exact outcome, and store a completed
result for replay.

#### Scenario: Client closes the tab

- **WHEN** the client disconnects halfway through a streamed answer
- **THEN** the call completes in the gateway, its ledger cost is the real
  usage, and resubmitting the call ID returns the full answer

### Requirement: Finished calls are replayed as a one-event stream

Resubmitting a finished call ID with `stream: true` SHALL answer with a stream of
one event: `response.completed` marked `replayed` for a completed call, or
`error` with the recorded code for a failed or unknown call.

#### Scenario: Replay of a completed stream

- **WHEN** a project resubmits a streamed call ID that completed
- **THEN** it receives one `response.completed` event with `replayed: true`,
  and no provider call is made

### Requirement: Streams stay open through quiet periods

The gateway SHALL send a keep-alive comment when no event has been sent for 15
seconds.

#### Scenario: Long reasoning before the first token

- **WHEN** the model emits nothing for 40 seconds
- **THEN** the client receives keep-alive comments, and the connection
  stays open
