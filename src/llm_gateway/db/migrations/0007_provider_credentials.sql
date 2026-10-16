CREATE TABLE project_provider_credentials (
    project_id text NOT NULL REFERENCES projects(project_id),
    provider text NOT NULL,
    revision uuid NOT NULL,
    ciphertext bytea,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, provider)
);

CREATE TABLE provider_credential_events (
    revision uuid PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(project_id),
    provider text NOT NULL,
    action text NOT NULL CHECK (action IN ('set', 'replace', 'remove')),
    actor text NOT NULL CHECK (char_length(actor) BETWEEN 1 AND 120),
    at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER provider_credential_events_immutable
    BEFORE UPDATE OR DELETE ON provider_credential_events
    FOR EACH ROW EXECUTE FUNCTION immutable_registry_record();
