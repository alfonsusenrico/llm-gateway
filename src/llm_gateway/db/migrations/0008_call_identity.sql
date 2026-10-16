ALTER TABLE calls ADD COLUMN request_schema_version integer NOT NULL DEFAULT 1;
ALTER TABLE calls ADD COLUMN task text NOT NULL DEFAULT 'llm'
    CHECK (task IN ('llm', 'embedding', 'ocr'));
ALTER TABLE calls ADD COLUMN model_alias text;
ALTER TABLE calls ADD COLUMN definition_digest text;
ALTER TABLE calls ADD COLUMN settings_digest text;
ALTER TABLE calls ADD COLUMN credential_revision uuid;
ALTER TABLE calls ADD COLUMN billing_kind text CHECK (billing_kind IN ('token', 'zero'));
