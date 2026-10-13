# Design

## Context

Phase 0 left an API process, a migration step, and Postgres; there is no worker
yet. Alerts must be durable (a restart must not lose them), deduplicated (one
broken client must not flood the owner), and best-effort (they must never fail
or slow a request). See proposal.md for motivation.

## Goals / Non-Goals

**Goals:**

- A single function that gateway code calls to raise an alert.
- Durable delivery through a worker.
- Channel independence, so Discord can be added later.

**Non-Goals:**

- Message templating per channel beyond plain text.
- Two-way chat.
- Delivery receipts beyond the channel's API response.

## Decisions

### 1. Events are rows in Postgres; a worker delivers them (outbox)

`raise_alert(...)` writes a row and returns. A worker claims due rows with
`FOR UPDATE SKIP LOCKED` and sends them.

*Rejected:*

- **Sending inline from the request:** slow, and lost on a crash.
- **An in-process queue:** lost on restart.
- **A separate broker:** too much infrastructure for one host.

### 2. Deduplication counts occurrences instead of sending them

Each event has a `dedupe_key` built by the caller from safe identifiers (for
example, the event type plus project ID).

- A new alert whose key matches an event first seen within
  `NOTIFIER_DEDUPE_WINDOW_SECONDS` (default 900) only increments that event's
  `occurrences` and `last_seen_at`.
- An alert after the window opens a new event, whose message reports the
  count.

The insert-or-increment is one statement guarded by a partial unique index on
the open window, so concurrent API workers cannot double-insert.

*Rejected:* per-process memory deduplication, because it duplicates across
processes and restarts.

### 3. Raising an alert can never fail the caller

`raise_alert` runs in its own short transaction on its own pooled connection.

- It catches database errors and logs only the exception class.
- It never takes part in the caller's transaction, so a rolled-back request
  still alerts.
- It never runs while the caller holds row locks.

### 4. Retries and expiry

Delivery failures retry with backoff (30 s, 2 min, 10 min, 30 min, then hourly)
for up to 8 attempts, then the event is marked `failed`.

- A Telegram `429` uses its `retry_after` value.
- Events older than `NOTIFIER_MAX_EVENT_AGE_SECONDS` (default 86400) are
  marked `expired` instead of sent, so a long outage or an unconfigured
  channel does not flood the owner later.

### 5. Channel interface; Telegram first

A channel takes a rendered plain-text message and returns `delivered`,
`retry` (with an optional delay), or `rejected` (a permanent error, such as a
wrong chat ID or a revoked token).

The Telegram adapter calls `sendMessage` with `httpx`.

- The bot token is part of the URL path, so the adapter never logs URLs or
  exception messages, only the exception class and the HTTP status.
- When no channel is configured, events remain `pending`, and the worker logs
  once per start that delivery is disabled.

### 6. Message content is built by gateway code from safe fields

A message consists of:

- the severity and event type;
- a fixed summary sentence chosen by the caller;
- the occurrence count;
- the first and last times in UTC.

Callers pass identifiers (project ID, environment, codes), never prompts,
keys, or provider text. The summary is limited to 300 characters.

### 7. Worker process shape

`llm-gateway-worker` runs an async loop:

- it claims up to 10 due events at a time;
- it polls every 2 s when idle;
- it stops cleanly on SIGTERM or SIGINT, finishing the current send.

Compose adds a `worker` service from the same image, started after
`migrate`. Later changes add their own jobs to this worker.

### 8. Admin CLI starts here

`llm-gateway-admin notifier test` raises an `info` event (`notifier_test`)
that skips deduplication, waits up to 30 s for delivery, and prints the
outcome. The registry change extends this CLI.

## Risks / Trade-offs

- **[Risk]** The Telegram token is visible to anyone who can read the
  environment. **Mitigation:** it is a dedicated notifier bot whose only
  power is sending to the owner's chat, and it is never logged.
- **[Risk]** Postgres is down, so alerts cannot be stored. **Mitigation:** log
  a warning (class only). Readiness already reports the database. The alert
  is lost, which is acceptable for best-effort alerts.
- **[Trade-off]** Up to about 2 s of delivery latency from polling, instead of
  `LISTEN/NOTIFY`. That is acceptable for alerts, and polling is simpler and
  robust to dropped connections.

## Migration Plan

- Migration `0001_notifier_events.sql` adds a new table, so it is additive and
  needs no backfill.
- **Rollback:** stop the worker service. The table can stay.
