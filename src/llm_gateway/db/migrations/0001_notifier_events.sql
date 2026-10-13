-- Owner alerts: a durable outbox delivered by the worker.
CREATE TABLE IF NOT EXISTS notifier_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type text NOT NULL CHECK (event_type ~ '^[a-z][a-z0-9_]{1,63}$'),
    severity text NOT NULL CHECK (severity IN ('info', 'warning', 'error', 'critical')),
    dedupe_key text NOT NULL CHECK (char_length(dedupe_key) BETWEEN 1 AND 200),
    summary text NOT NULL CHECK (char_length(summary) BETWEEN 1 AND 300),
    occurrences integer NOT NULL DEFAULT 1 CHECK (occurrences >= 1),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    -- Repeats with the same key before this time are counted on this event.
    window_ends_at timestamptz NOT NULL,
    -- True while repeats may still be counted here; at most one open event per key.
    is_open boolean NOT NULL DEFAULT true,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'delivered', 'failed', 'expired')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    -- A worker holds a claim while sending; an expired claim is retried.
    claimed_until timestamptz,
    delivered_at timestamptz,
    -- A safe outcome code such as http_429 or ConnectError, never provider text.
    last_error text CHECK (char_length(last_error) <= 64)
);

CREATE UNIQUE INDEX IF NOT EXISTS notifier_events_open_key_idx
    ON notifier_events (dedupe_key) WHERE is_open;

CREATE INDEX IF NOT EXISTS notifier_events_due_idx
    ON notifier_events (next_attempt_at) WHERE status = 'pending';
