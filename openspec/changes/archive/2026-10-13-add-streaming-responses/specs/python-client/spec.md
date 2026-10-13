## ADDED Requirements

### Requirement: Client streams calls as typed events

The client SHALL provide `responses.stream(...)`, an async iterator yielding
typed started, text delta, refusal delta, and completed events, and raising the
typed error for a problem.

#### Scenario: Streamed text

- **WHEN** a project iterates a streamed call
- **THEN** it receives text deltas, then a completed event whose result
  carries usage and cost

### Requirement: A lost stream is recovered by resubmission

If the connection drops after a streamed call started, the client SHALL resubmit
the same call ID, retry `call_in_progress` with backoff within its retry budget,
and finish with the replayed completed event.

#### Scenario: Connection dropped mid-stream

- **WHEN** the connection to the gateway breaks after two text deltas
- **THEN** the iterator ends with a completed event marked replayed, whose
  result holds the full answer
