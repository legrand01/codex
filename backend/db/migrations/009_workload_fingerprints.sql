-- Migration 009: Durable, measurable workload fingerprints.
-- Requirements: 17.1, 17.2, 17.3, 17.4

CREATE TABLE IF NOT EXISTS workload_fingerprints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    host_id UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    database_name VARCHAR(63),
    name VARCHAR(120) NOT NULL,
    kind VARCHAR(20) NOT NULL CHECK (kind IN ('recommended', 'custom')),
    status VARCHAR(30) NOT NULL DEFAULT 'insufficient_history' CHECK (
        status IN ('ready', 'low_coverage', 'unstable', 'high_variance',
                   'insufficient_history', 'no_workload')
    ),
    selection_criteria JSONB NOT NULL DEFAULT '{}'::jsonb,
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_coverage_pct DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (observed_coverage_pct BETWEEN 0 AND 100),
    membership_stability_pct DOUBLE PRECISION
        CHECK (membership_stability_pct IS NULL OR membership_stability_pct BETWEEN 0 AND 100),
    runtime_variance_pct DOUBLE PRECISION
        CHECK (runtime_variance_pct IS NULL OR runtime_variance_pct >= 0),
    source_snapshot_id UUID REFERENCES evidence_snapshots(id) ON DELETE SET NULL,
    source_collected_at TIMESTAMPTZ,
    created_by VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workload_fingerprints_name
    ON workload_fingerprints(organization_id, host_id, lower(name));
CREATE INDEX IF NOT EXISTS idx_workload_fingerprints_catalog
    ON workload_fingerprints(organization_id, host_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS workload_fingerprint_members (
    fingerprint_id UUID NOT NULL REFERENCES workload_fingerprints(id) ON DELETE CASCADE,
    query_id TEXT NOT NULL,
    query_text TEXT,
    calls BIGINT NOT NULL DEFAULT 0 CHECK (calls >= 0),
    average_query_runtime_ms DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (average_query_runtime_ms >= 0),
    total_runtime_ms DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (total_runtime_ms >= 0),
    runtime_coverage_pct DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (runtime_coverage_pct BETWEEN 0 AND 100),
    impact_score DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (impact_score >= 0),
    last_seen_at TIMESTAMPTZ,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    PRIMARY KEY (fingerprint_id, query_id)
);

CREATE INDEX IF NOT EXISTS idx_workload_fingerprint_members_order
    ON workload_fingerprint_members(fingerprint_id, ordinal);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'loop_runs_workload_fingerprint_id_fkey'
    ) THEN
        ALTER TABLE loop_runs
            ADD CONSTRAINT loop_runs_workload_fingerprint_id_fkey
            FOREIGN KEY (workload_fingerprint_id)
            REFERENCES workload_fingerprints(id);
    END IF;
END $$;
