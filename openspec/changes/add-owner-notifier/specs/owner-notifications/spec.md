## Purpose

Deliver content-free gateway alerts to the owner through a configured channel,
durably and with deduplication, without ever affecting request handling.

## ADDED Requirements

### Requirement: Alerts are recorded durably

The gateway SHALL record every raised alert as a durable event with a type, a
severity (`info`, `warning`, `error`, or `critical`), a deduplication key, a
summary, an occurrence count, and its first and last occurrence times.

#### Scenario: Alert survives a worker restart

- **WHEN** an alert is raised while the worker is stopped
- **THEN** the event is delivered after the worker starts again

### Requirement: Repeated alerts are counted, not re-sent

The gateway SHALL count an alert whose deduplication key matches an event
first seen within the deduplication window as an occurrence of that event, and
SHALL NOT send a separate message for it.

#### Scenario: Burst of identical alerts

- **WHEN** the same alert is raised 20 times within the deduplication window
- **THEN** the owner receives one message, and the event records 20
  occurrences

#### Scenario: Alert after the window

- **WHEN** an alert is raised again after the deduplication window has passed
- **THEN** a new event is created and delivered

#### Scenario: Concurrent identical alerts

- **WHEN** two processes raise the same alert at the same moment
- **THEN** exactly one open event exists for that key, with two occurrences

### Requirement: Raising an alert never affects the caller

Raising an alert SHALL NOT raise an error to its caller, SHALL NOT take part in
the caller's transaction, and SHALL NOT wait for delivery.

#### Scenario: Database unavailable

- **WHEN** an alert is raised while the database is unreachable
- **THEN** the caller continues normally, and a warning naming only the error
  class is logged

#### Scenario: Caller transaction rolls back

- **WHEN** a request raises an alert and its own transaction then rolls back
- **THEN** the alert event still exists

### Requirement: Delivery is retried with backoff and bounded

The worker SHALL retry a failed delivery with increasing delays, SHALL honour a
channel-provided retry delay, and SHALL mark the event `failed` after 8
attempts. A permanent channel rejection SHALL mark the event `failed` without
retrying.

#### Scenario: Channel temporarily unavailable

- **WHEN** the channel returns a temporary error
- **THEN** the event stays pending with a later attempt time, and is delivered
  when the channel recovers

#### Scenario: Channel rate limit

- **WHEN** the channel answers with a rate limit and a retry delay
- **THEN** the next attempt waits at least that delay

#### Scenario: Wrong chat or revoked token

- **WHEN** the channel rejects the message permanently
- **THEN** the event is marked failed, and no further attempts are made

### Requirement: Stale events are expired, not sent

The worker SHALL mark an undelivered event older than the maximum event age as
`expired` and SHALL NOT send it.

#### Scenario: Long outage

- **WHEN** an event has waited longer than the maximum age because delivery
  was unavailable
- **THEN** it is marked expired, and it is never sent

### Requirement: Alerts carry no content or secrets

Alert messages and logs SHALL contain only the severity, type, summary,
identifiers, counts, and times. They SHALL NOT contain prompts, model output,
API keys, channel tokens, or provider error text.

#### Scenario: Delivery error logging

- **WHEN** a Telegram call fails
- **THEN** the log line names only the error class and the HTTP status, and
  never the request URL or the token

#### Scenario: Summary length

- **WHEN** a caller passes a summary longer than 300 characters
- **THEN** the stored summary is truncated to 300 characters

### Requirement: Delivery is disabled safely when no channel is configured

When no channel is configured, the worker SHALL keep events pending, SHALL log
once per start that delivery is disabled, and the API SHALL keep serving
normally.

#### Scenario: Telegram not configured

- **WHEN** the Telegram token or chat ID is missing
- **THEN** events accumulate as pending, until they are delivered after
  configuration or expire

### Requirement: Owner can test delivery

The admin CLI SHALL provide a command that raises a test alert, bypassing
deduplication, and reports whether it was delivered within 30 seconds.

#### Scenario: Successful test

- **WHEN** the owner runs the notifier test command with a valid
  configuration and a running worker
- **THEN** a test message arrives in the configured chat, and the command
  reports `delivered`

#### Scenario: Test without a running worker

- **WHEN** the owner runs the test command while no worker is running
- **THEN** the command reports that the event is still pending after 30
  seconds
