-- Completed call responses, encrypted by the gateway, kept for replay until expiry.
CREATE TABLE IF NOT EXISTS call_results (
    call_key bigint PRIMARY KEY REFERENCES calls (call_key),
    encrypted bytea NOT NULL,
    expires_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS call_results_expiry_idx ON call_results (expires_at);
