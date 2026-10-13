# Proposal

## Why

The gateway needs to tell the owner when something needs attention: a budget
threshold, a reduced grant, rejected keys, or a provider account failure.
Nobody reads the logs of a self-hosted service. The gateway needs its own
alert channel, independent of any project and starting on Telegram, so that
the next changes (key rejection alerts, grant changes) have somewhere to send
their events.

## Expected Outcome

- When gateway code raises an alert, the owner receives one Telegram message
  within seconds. Repeats inside the deduplication window are counted, not
  re-sent.
- If Telegram is down or not configured, gateway requests behave exactly as
  before. Delivery resumes on its own once the problem clears.
- A test command sends a sample alert, to prove the configuration end to end.

## What Changes

- **Notification events:** content-free, durable events with a type, severity,
  deduplication key, and occurrence count.
- **Worker process:** a new compose `worker` service that delivers pending
  events through a channel adapter, with retries and backoff. Telegram is the
  first adapter; the channel interface leaves room for Discord later.
- **Settings:** for the Telegram bot token and chat ID, the deduplication
  window, and the maximum event age.
- **Admin CLI:** a first `llm-gateway-admin` command, `notifier test`, used to
  verify delivery.

## Non-goals

- A Discord adapter (later).
- Interactive Telegram buttons or commands.
- Alerts about specific gateway features. Each later change adds its own event
  types: budget thresholds, grants, rejected keys, provider failures.
- Any alert content beyond identifiers, counts, and states.

## Capabilities

### New Capabilities

- `owner-notifications`: how gateway alerts are recorded, deduplicated,
  delivered, retried, and kept from affecting request handling.

### Modified Capabilities

None.

## Impact

- **Capability:** `background-jobs` becomes active, through the first worker
  process. Its standards apply:
  - claiming with `FOR UPDATE SKIP LOCKED`;
  - bounded retries;
  - graceful shutdown.
- **Code:**
  - new `notifier` package (events, the channel interface, the Telegram
    adapter);
  - the worker entry point;
  - the admin CLI entry point;
  - migration `0001`.
- **Dependencies:** `httpx` as a runtime dependency, for the Telegram API.
- **Configuration:** new `NOTIFIER_*` settings and a `worker` compose service.
- **Contract and client package:** no effect.
