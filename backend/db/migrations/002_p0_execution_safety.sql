-- Migration 002: P0 execution safety and durable write evidence
-- Adds explicit target secret references, write interlocks, pre-change state,
-- and idempotent operation records. Target credentials are never stored here.

ALTER TABLE hosts
    ADD COLUMN IF NOT EXISTS environment VARCHAR(20) NOT NULL DEFAULT 'development'
        CHECK (environment IN ('development', 'staging', 'production')),
    ADD COLUMN IF NOT EXISTS target_dsn_env VARCHAR(255),
    ADD COLUMN IF NOT EXISTS writes_enabled BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS pre_change_snapshot JSONB,
    ADD COLUMN IF NOT EXISTS apply_result JSONB,
    ADD COLUMN IF NOT EXISTS verification_result JSONB,
    ADD COLUMN IF NOT EXISTS execution_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS verification_completed_at TIMESTAMPTZ;

ALTER TABLE plans DROP CONSTRAINT IF EXISTS plans_status_check;
ALTER TABLE plans ADD CONSTRAINT plans_status_check CHECK (
    status IN (
        'pending_approval', 'approved', 'rejected', 'pending_forwarding',
        'forwarding_failed', 'dry_run_passed', 'dry_run_failed', 'applied',
        'apply_failed', 'rolled_back', 'rollback_failed', 'blocked'
    )
);

CREATE TABLE IF NOT EXISTS write_operations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES plans(id),
    host_id UUID NOT NULL REFERENCES hosts(id),
    operation_type VARCHAR(20) NOT NULL CHECK (operation_type IN ('apply', 'rollback')),
    idempotency_key VARCHAR(255) NOT NULL UNIQUE,
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_progress', 'succeeded', 'failed')),
    pre_change_snapshot JSONB,
    result JSONB,
    error TEXT,
    claimed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (plan_id, operation_type)
);

CREATE INDEX IF NOT EXISTS idx_write_operations_status
    ON write_operations(status, created_at);
