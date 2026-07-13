-- Migration 010: Repeatable baseline measurements and non-executable advisories.
-- Requirements: 17.5, 17.9, 17.10

CREATE TABLE IF NOT EXISTS baseline_measurements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    run_id UUID NOT NULL UNIQUE REFERENCES loop_runs(id) ON DELETE CASCADE,
    host_id UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    workload_fingerprint_id UUID REFERENCES workload_fingerprints(id),
    status VARCHAR(30) NOT NULL CHECK (
        status IN ('ready', 'paused', 'advisory_only')
    ),
    objective_type VARCHAR(40) NOT NULL,
    objective_formula TEXT NOT NULL,
    objective_direction VARCHAR(10) NOT NULL CHECK (
        objective_direction IN ('minimize', 'maximize')
    ),
    objective_score DOUBLE PRECISION,
    metric_units JSONB NOT NULL DEFAULT '{}'::jsonb,
    fingerprint_membership JSONB NOT NULL DEFAULT '[]'::jsonb,
    warmup_window_seconds INTEGER NOT NULL,
    requested_measurement_window_seconds INTEGER NOT NULL,
    observed_measurement_window_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
    workload_coverage_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
    runtime_variance_pct DOUBLE PRECISION,
    safety_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_references JSONB NOT NULL DEFAULT '[]'::jsonb,
    root_cause_category VARCHAR(30) NOT NULL CHECK (
        root_cause_category IN (
            'configuration', 'query_index', 'lock_contention', 'vacuum_bloat',
            'resource_saturation', 'connection_pressure', 'insufficient_evidence'
        )
    ),
    root_cause_confidence DOUBLE PRECISION NOT NULL
        CHECK (root_cause_confidence BETWEEN 0 AND 1),
    root_cause_summary TEXT NOT NULL,
    root_cause_details JSONB NOT NULL DEFAULT '{}'::jsonb,
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_baseline_measurements_session
    ON baseline_measurements(organization_id, run_id);
CREATE INDEX IF NOT EXISTS idx_baseline_measurements_host
    ON baseline_measurements(organization_id, host_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS advisory_findings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    run_id UUID NOT NULL REFERENCES loop_runs(id) ON DELETE CASCADE,
    host_id UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    category VARCHAR(30) NOT NULL CHECK (
        category IN (
            'query_index', 'lock_contention', 'vacuum_bloat',
            'resource_saturation', 'connection_pressure', 'insufficient_evidence'
        )
    ),
    severity VARCHAR(20) NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    title VARCHAR(255) NOT NULL,
    summary TEXT NOT NULL,
    recommendations JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_references JSONB NOT NULL DEFAULT '[]'::jsonb,
    executable BOOLEAN NOT NULL DEFAULT FALSE CHECK (executable = FALSE),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, category)
);

CREATE INDEX IF NOT EXISTS idx_advisory_findings_run
    ON advisory_findings(organization_id, run_id, created_at);
