-- One row per model call: admission, settlement, cost. Amounts are USD micro-units.
CREATE TABLE IF NOT EXISTS calls (
    call_key bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects (project_id),
    environment text NOT NULL CHECK (environment IN ('development', 'production')),
    provider_call_id text NOT NULL CHECK (provider_call_id ~ '^[A-Za-z0-9._:-]{1,160}$'),
    request_fingerprint text NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    operation text NOT NULL CHECK (operation ~ '^[a-z][a-z0-9_]{1,63}$'),
    provider text NOT NULL,
    model text NOT NULL,
    service_tier_requested text NOT NULL CHECK (service_tier_requested IN ('standard', 'flex')),
    service_tier_used text,
    state text NOT NULL CHECK (
        state IN ('admitted', 'completed', 'failed', 'retryable_failure', 'execution_unknown')
    ),
    reserved_micro_usd bigint NOT NULL CHECK (reserved_micro_usd >= 0),
    -- Priced usage; NULL when there was no usage or no applicable price.
    cost_micro_usd bigint CHECK (cost_micro_usd >= 0),
    -- What counts as spend once finished: the cost, zero, or the reservation.
    charged_micro_usd bigint NOT NULL DEFAULT 0 CHECK (charged_micro_usd >= 0),
    input_tokens integer,
    cached_input_tokens integer,
    cache_write_tokens integer,
    output_tokens integer,
    reasoning_tokens integer,
    -- Snapshot of the price entry charged, so history never follows later prices.
    price_tier text,
    price_effective_from date,
    failure_code text CHECK (char_length(failure_code) <= 64),
    admitted_at timestamptz NOT NULL DEFAULT now(),
    lease_expires_at timestamptz,
    finished_at timestamptz,
    UNIQUE (project_id, environment, provider_call_id)
);

CREATE INDEX IF NOT EXISTS calls_pair_state_idx ON calls (project_id, environment, state);
CREATE INDEX IF NOT EXISTS calls_pair_admitted_idx ON calls (project_id, environment, admitted_at);
CREATE INDEX IF NOT EXISTS calls_admitted_at_idx ON calls (admitted_at);
CREATE INDEX IF NOT EXISTS calls_lease_idx ON calls (lease_expires_at) WHERE state = 'admitted';

-- Append-only audit trail of every call state change.
CREATE TABLE IF NOT EXISTS call_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    call_key bigint NOT NULL REFERENCES calls (call_key),
    from_state text,
    to_state text NOT NULL,
    charged_micro_usd bigint,
    actor text NOT NULL CHECK (char_length(actor) BETWEEN 1 AND 120),
    at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS call_events_call_idx ON call_events (call_key, event_id);

CREATE OR REPLACE FUNCTION call_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'call_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS call_events_no_change ON call_events;
CREATE TRIGGER call_events_no_change
    BEFORE UPDATE OR DELETE ON call_events
    FOR EACH ROW EXECUTE FUNCTION call_events_append_only();

-- Budget thresholds already reported, so a restart never repeats them.
CREATE TABLE IF NOT EXISTS alerts_sent (
    scope text NOT NULL,
    month date NOT NULL,
    threshold_percent integer NOT NULL CHECK (threshold_percent IN (50, 80, 100)),
    sent_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, month, threshold_percent)
);
