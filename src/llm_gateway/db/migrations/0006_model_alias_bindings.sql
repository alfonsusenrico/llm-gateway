CREATE TABLE model_alias_bindings (
    alias text PRIMARY KEY,
    task text NOT NULL CHECK (task IN ('llm', 'embedding', 'ocr')),
    definition_digest text NOT NULL CHECK (definition_digest ~ '^[0-9a-f]{64}$'),
    registered_at timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION immutable_registry_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Registry records are append-only';
END;
$$;

CREATE TRIGGER model_alias_bindings_immutable
    BEFORE UPDATE OR DELETE ON model_alias_bindings
    FOR EACH ROW EXECUTE FUNCTION immutable_registry_record();
