-- Migration 011: Durable, bounded candidate optimization.
-- Requirements: 17.6, 17.7, 17.8

CREATE TABLE IF NOT EXISTS candidate_parameter_domains (
    version VARCHAR(100) NOT NULL,
    setting_name VARCHAR(63) NOT NULL,
    pg_major_min INTEGER NOT NULL DEFAULT 15,
    pg_major_max INTEGER,
    parameter_context VARCHAR(20) NOT NULL CHECK (
        parameter_context IN ('reload', 'restart')
    ),
    value_kind VARCHAR(30) NOT NULL CHECK (
        value_kind IN ('memory_kb', 'integer', 'decimal')
    ),
    definition JSONB NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (version, setting_name)
);

INSERT INTO candidate_parameter_domains (
    version, setting_name, parameter_context, value_kind, definition
) VALUES
    (
        'p0-bounded-v1', 'work_mem', 'reload', 'memory_kb',
        '{"strategy":"multipliers","values":[2.0,4.0,8.0,0.5,1.5],"minimum":64,"maximum":1048576,"default_max_deviation_pct":700}'::jsonb
    ),
    (
        'p0-bounded-v1', 'random_page_cost', 'reload', 'decimal',
        '{"strategy":"absolute","values":[1.1,1.5,2.0,3.0,4.0],"minimum":1.0,"maximum":8.0,"default_max_deviation_pct":75}'::jsonb
    ),
    (
        'p0-bounded-v1', 'effective_io_concurrency', 'reload', 'integer',
        '{"strategy":"absolute","values":[1,16,32,64,128,200],"minimum":0,"maximum":1000,"default_max_deviation_pct":400}'::jsonb
    ),
    (
        'p0-bounded-v1', 'checkpoint_completion_target', 'reload', 'decimal',
        '{"strategy":"absolute","values":[0.7,0.8,0.9,0.95],"minimum":0.5,"maximum":0.95,"default_max_deviation_pct":40}'::jsonb
    )
ON CONFLICT (version, setting_name) DO NOTHING;

CREATE TABLE IF NOT EXISTS tuning_candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    run_id UUID NOT NULL REFERENCES loop_runs(id) ON DELETE CASCADE,
    host_id UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    plan_id UUID NOT NULL UNIQUE REFERENCES plans(id) ON DELETE CASCADE,
    iteration INTEGER NOT NULL,
    domain_version VARCHAR(100) NOT NULL,
    parameter_values JSONB NOT NULL,
    pre_change_snapshot JSONB NOT NULL,
    baseline_score DOUBLE PRECISION NOT NULL,
    best_score_before DOUBLE PRECISION NOT NULL,
    objective_score DOUBLE PRECISION,
    baseline_delta_pct DOUBLE PRECISION,
    best_delta_pct DOUBLE PRECISION,
    objective_formula TEXT NOT NULL,
    objective_direction VARCHAR(10) NOT NULL CHECK (
        objective_direction IN ('minimize', 'maximize')
    ),
    metric_units JSONB NOT NULL DEFAULT '{}'::jsonb,
    warmup_window_seconds INTEGER NOT NULL,
    measurement_window_seconds INTEGER NOT NULL,
    observed_measurement_window_seconds DOUBLE PRECISION,
    workload_coverage_pct DOUBLE PRECISION,
    runtime_variance_pct DOUBLE PRECISION,
    safety_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    safety_deltas JSONB NOT NULL DEFAULT '{}'::jsonb,
    guardrail_violations JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_references JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence_score DOUBLE PRECISION CHECK (
        confidence_score IS NULL OR confidence_score BETWEEN 0 AND 1
    ),
    decision VARCHAR(30) NOT NULL CHECK (
        decision IN (
            'pending_approval', 'blocked', 'measuring', 'kept',
            'rolled_back', 'inconclusive', 'rejected'
        )
    ),
    decision_reason TEXT,
    warmup_started_at TIMESTAMPTZ,
    warmup_completed_at TIMESTAMPTZ,
    measurement_started_at TIMESTAMPTZ,
    measured_at TIMESTAMPTZ,
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, iteration)
);

CREATE INDEX IF NOT EXISTS idx_tuning_candidates_run
    ON tuning_candidates(organization_id, run_id, iteration);
CREATE INDEX IF NOT EXISTS idx_tuning_candidates_decision
    ON tuning_candidates(decision, created_at);
