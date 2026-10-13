-- Backend-owned grants: what each project environment may use.
CREATE TABLE IF NOT EXISTS grants (
    project_id text NOT NULL REFERENCES projects (project_id),
    environment text NOT NULL CHECK (environment IN ('development', 'production')),
    requests_per_minute integer NOT NULL CHECK (requests_per_minute >= 0),
    concurrency integer NOT NULL CHECK (concurrency >= 0),
    monthly_budget_micro_usd bigint NOT NULL CHECK (monthly_budget_micro_usd >= 0),
    per_call_cost_ceiling_micro_usd bigint NOT NULL CHECK (per_call_cost_ceiling_micro_usd >= 0),
    state text NOT NULL CHECK (state IN ('approved', 'reduced')),
    -- [{"dimension": ..., "code": ...}] for each reduced dimension.
    reasons jsonb NOT NULL DEFAULT '[]',
    computed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, environment)
);

-- The hash of every allocation input at the last recompute; unchanged inputs skip it.
CREATE TABLE IF NOT EXISTS allocation_state (
    singleton integer PRIMARY KEY CHECK (singleton = 1),
    inputs_hash text NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now()
);
