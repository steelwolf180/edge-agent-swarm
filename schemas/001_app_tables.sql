CREATE TABLE IF NOT EXISTS spec_versions (
    id SERIAL PRIMARY KEY,
    version INT NOT NULL,
    spec_json JSONB NOT NULL,
    created TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    workflow_id TEXT PRIMARY KEY,
    spec_version_id INT NOT NULL REFERENCES spec_versions(id),
    status TEXT NOT NULL DEFAULT 'pending',
    created TIMESTAMPTZ NOT NULL DEFAULT now(),
    modified TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS artifacts (
    id SERIAL PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES pipeline_runs(workflow_id),
    context_diagram TEXT,
    diagram_source TEXT,
    adr_context TEXT,
    adr_decision TEXT,
    adr_consequences TEXT,
    judge_scores JSONB,
    approved TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS revision_cycles (
    id SERIAL PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES pipeline_runs(workflow_id),
    revision_notes TEXT NOT NULL,
    rejected TIMESTAMPTZ NOT NULL DEFAULT now()
);