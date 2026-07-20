-- Migration 018: bounded raw-evidence retention and durable aggregate history.
-- Requirements: 21.1-21.7, Task 27.

CREATE TABLE IF NOT EXISTS evidence_rollups (
    id BIGSERIAL PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organizations(id),
    host_id UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    run_id UUID REFERENCES loop_runs(id) ON DELETE SET NULL,
    -- PostgreSQL UNIQUE treats NULLs as distinct. Keep a deterministic key for
    -- host evidence that had not yet been attached to a tuning session.
    run_key UUID NOT NULL,
    evidence_type VARCHAR(30) NOT NULL CHECK (
        evidence_type IN (
            'pg_settings', 'pg_stat_database', 'pg_stat_statements', 'locks',
            'replication', 'wal_checkpoint', 'os_metrics'
        )
    ),
    bucket_start TIMESTAMPTZ NOT NULL,
    bucket_end TIMESTAMPTZ NOT NULL,
    snapshot_count BIGINT NOT NULL CHECK (snapshot_count > 0),
    total_bytes BIGINT NOT NULL CHECK (total_bytes >= 0),
    quality_sample_count BIGINT NOT NULL DEFAULT 0 CHECK (quality_sample_count >= 0),
    min_quality_score NUMERIC(3,2),
    average_quality_score NUMERIC(5,4),
    max_quality_score NUMERIC(3,2),
    first_collected_at TIMESTAMPTZ NOT NULL,
    last_collected_at TIMESTAMPTZ NOT NULL,
    rolled_up_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (organization_id, host_id, run_key, evidence_type, bucket_start)
);

CREATE INDEX IF NOT EXISTS idx_evidence_rollups_run
    ON evidence_rollups(organization_id, run_id, bucket_start DESC)
    WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_evidence_rollups_host
    ON evidence_rollups(organization_id, host_id, bucket_start DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_rollups_expiry
    ON evidence_rollups(bucket_end);

CREATE TABLE IF NOT EXISTS evidence_maintenance_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    status VARCHAR(20) NOT NULL CHECK (
        status IN ('running', 'completed', 'failed', 'skipped')
    ),
    triggered_by VARCHAR(100) NOT NULL,
    raw_cutoff TIMESTAMPTZ NOT NULL,
    referenced_cutoff TIMESTAMPTZ NOT NULL,
    rollup_cutoff TIMESTAMPTZ NOT NULL,
    batch_size INTEGER NOT NULL CHECK (batch_size > 0),
    batches_completed INTEGER NOT NULL DEFAULT 0 CHECK (batches_completed >= 0),
    snapshots_deleted BIGINT NOT NULL DEFAULT 0 CHECK (snapshots_deleted >= 0),
    raw_bytes_reclaimed BIGINT NOT NULL DEFAULT 0 CHECK (raw_bytes_reclaimed >= 0),
    rollup_rows_written BIGINT NOT NULL DEFAULT 0 CHECK (rollup_rows_written >= 0),
    expired_rollups_deleted BIGINT NOT NULL DEFAULT 0 CHECK (expired_rollups_deleted >= 0),
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_evidence_maintenance_history
    ON evidence_maintenance_runs(organization_id, started_at DESC);

INSERT INTO event_code_catalog (
    event_code, default_severity, component, description
) VALUES
    ('EVIDENCE_RETENTION_COMPLETED', 'info', 'evidence',
     'Expired raw evidence was rolled up and removed'),
    ('EVIDENCE_RETENTION_FAILED', 'error', 'evidence',
     'The evidence lifecycle maintenance job failed')
ON CONFLICT (event_code) DO UPDATE SET
    default_severity = EXCLUDED.default_severity,
    component = EXCLUDED.component,
    description = EXCLUDED.description;
