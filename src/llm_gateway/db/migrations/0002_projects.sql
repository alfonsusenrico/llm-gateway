-- Registered projects, their versioned contracts, and their API keys.
CREATE TABLE IF NOT EXISTS projects (
    project_id text PRIMARY KEY CHECK (project_id ~ '^[a-z][a-z0-9_]{2,39}$'),
    current_version integer NOT NULL CHECK (current_version >= 1),
    -- Backend allocation policy, not part of the contract.
    weight integer NOT NULL DEFAULT 1 CHECK (weight BETWEEN 1 AND 100),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS contract_versions (
    project_id text NOT NULL REFERENCES projects (project_id),
    version integer NOT NULL CHECK (version >= 1),
    contract jsonb NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now(),
    actor text NOT NULL CHECK (char_length(actor) BETWEEN 1 AND 120),
    change_note text NOT NULL CHECK (char_length(change_note) BETWEEN 1 AND 200),
    PRIMARY KEY (project_id, version)
);

CREATE TABLE IF NOT EXISTS project_keys (
    key_id text PRIMARY KEY CHECK (key_id ~ '^[a-z2-7]{12}$'),
    project_id text NOT NULL REFERENCES projects (project_id),
    environment text NOT NULL CHECK (environment IN ('development', 'production')),
    -- HMAC-SHA256 of the secret under the gateway's pepper, hex encoded.
    secret_hmac text NOT NULL CHECK (secret_hmac ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    revoked_at timestamptz
);

CREATE INDEX IF NOT EXISTS project_keys_project_idx ON project_keys (project_id, environment);
