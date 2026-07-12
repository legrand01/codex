-- Migration 008: Productized tuning-session filters and capability preflight.
-- Requirements: 16.5, 16.7, 17.1, 18.5, 18.6, 18.7, 20.3

ALTER TABLE hosts
    ADD COLUMN IF NOT EXISTS database_name VARCHAR(63),
    ADD COLUMN IF NOT EXISTS platform_type VARCHAR(30) NOT NULL DEFAULT 'self_managed',
    ADD COLUMN IF NOT EXISTS configuration_backend VARCHAR(30) NOT NULL DEFAULT 'alter_system',
    ADD COLUMN IF NOT EXISTS managed_conf_enrolled BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE hosts DROP CONSTRAINT IF EXISTS hosts_platform_type_check;
ALTER TABLE hosts ADD CONSTRAINT hosts_platform_type_check CHECK (
    platform_type IN (
        'self_managed', 'aws_rds', 'aurora', 'cloud_sql', 'aiven',
        'other_managed'
    )
);

ALTER TABLE hosts DROP CONSTRAINT IF EXISTS hosts_configuration_backend_check;
ALTER TABLE hosts ADD CONSTRAINT hosts_configuration_backend_check CHECK (
    configuration_backend IN ('alter_system', 'managed_conf_file', 'provider')
);

ALTER TABLE loop_runs
    ADD COLUMN IF NOT EXISTS database_name VARCHAR(63),
    ADD COLUMN IF NOT EXISTS tuning_target VARCHAR(40) NOT NULL DEFAULT 'system_wide_aqr',
    ADD COLUMN IF NOT EXISTS tuning_mode VARCHAR(20) NOT NULL DEFAULT 'reload_only',
    ADD COLUMN IF NOT EXISTS workload_fingerprint_id UUID,
    ADD COLUMN IF NOT EXISTS selected_parameters JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS approval_policy VARCHAR(30) NOT NULL DEFAULT 'per_candidate',
    ADD COLUMN IF NOT EXISTS warmup_window_seconds INTEGER NOT NULL DEFAULT 60,
    ADD COLUMN IF NOT EXISTS measurement_window_seconds INTEGER NOT NULL DEFAULT 300,
    ADD COLUMN IF NOT EXISTS objective_guardrails JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS configuration_backend VARCHAR(30),
    ADD COLUMN IF NOT EXISTS baseline_score DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS best_score DOUBLE PRECISION;

UPDATE loop_runs r
SET database_name = h.database_name,
    configuration_backend = h.configuration_backend
FROM hosts h
WHERE r.host_id = h.id
  AND (r.database_name IS NULL OR r.configuration_backend IS NULL);

ALTER TABLE loop_runs DROP CONSTRAINT IF EXISTS loop_runs_tuning_target_check;
ALTER TABLE loop_runs ADD CONSTRAINT loop_runs_tuning_target_check CHECK (
    tuning_target IN (
        'recommended_fingerprint', 'custom_fingerprint', 'system_wide_aqr',
        'transactions_per_second', 'composite'
    )
);

ALTER TABLE loop_runs DROP CONSTRAINT IF EXISTS loop_runs_tuning_mode_check;
ALTER TABLE loop_runs ADD CONSTRAINT loop_runs_tuning_mode_check CHECK (
    tuning_mode IN ('reload_only', 'restart_enabled')
);

ALTER TABLE loop_runs DROP CONSTRAINT IF EXISTS loop_runs_approval_policy_check;
ALTER TABLE loop_runs ADD CONSTRAINT loop_runs_approval_policy_check CHECK (
    approval_policy IN ('per_candidate', 'final_only')
);

ALTER TABLE loop_runs DROP CONSTRAINT IF EXISTS loop_runs_configuration_backend_check;
ALTER TABLE loop_runs ADD CONSTRAINT loop_runs_configuration_backend_check CHECK (
    configuration_backend IS NULL OR
    configuration_backend IN ('alter_system', 'managed_conf_file', 'provider')
);

ALTER TABLE loop_runs DROP CONSTRAINT IF EXISTS loop_runs_warmup_window_check;
ALTER TABLE loop_runs ADD CONSTRAINT loop_runs_warmup_window_check
    CHECK (warmup_window_seconds BETWEEN 0 AND 3600);
ALTER TABLE loop_runs DROP CONSTRAINT IF EXISTS loop_runs_measurement_window_check;
ALTER TABLE loop_runs ADD CONSTRAINT loop_runs_measurement_window_check
    CHECK (measurement_window_seconds BETWEEN 30 AND 86400);

CREATE TABLE IF NOT EXISTS host_capabilities (
    host_id UUID PRIMARY KEY REFERENCES hosts(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organizations(id),
    connectivity BOOLEAN NOT NULL DEFAULT FALSE,
    system_information BOOLEAN NOT NULL DEFAULT FALSE,
    system_metrics BOOLEAN NOT NULL DEFAULT FALSE,
    pg_stat_statements BOOLEAN NOT NULL DEFAULT FALSE,
    query_text_collection BOOLEAN NOT NULL DEFAULT FALSE,
    configuration_read BOOLEAN NOT NULL DEFAULT FALSE,
    configuration_write BOOLEAN NOT NULL DEFAULT FALSE,
    reload_permission BOOLEAN NOT NULL DEFAULT FALSE,
    restart_capability BOOLEAN NOT NULL DEFAULT FALSE,
    provider_api BOOLEAN NOT NULL DEFAULT FALSE,
    managed_file_access BOOLEAN NOT NULL DEFAULT FALSE,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_loop_runs_history
    ON loop_runs(organization_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_loop_runs_host_history
    ON loop_runs(organization_id, host_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_loop_runs_status_history
    ON loop_runs(organization_id, status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_loop_runs_database_history
    ON loop_runs(organization_id, database_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_loop_runs_target_history
    ON loop_runs(organization_id, tuning_target, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_loop_runs_mode_history
    ON loop_runs(organization_id, tuning_mode, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_loop_runs_objective_search
    ON loop_runs USING GIN (to_tsvector('simple', goal));
CREATE INDEX IF NOT EXISTS idx_loop_runs_completed_history
    ON loop_runs(organization_id, completed_at DESC)
    WHERE completed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_host_capabilities_org
    ON host_capabilities(organization_id, updated_at DESC);
