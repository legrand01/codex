-- Migration 014: durable configuration backends and managed-file command channel.
-- Requirements: 19.1-19.10, Task 24.

ALTER TABLE hosts
    ADD COLUMN IF NOT EXISTS managed_conf_path TEXT;

ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS configuration_backend VARCHAR(30);

UPDATE plans p
SET configuration_backend = COALESCE(r.configuration_backend, h.configuration_backend)
FROM loop_runs r, hosts h
WHERE p.run_id = r.id
  AND p.host_id = h.id
  AND p.configuration_backend IS NULL;

ALTER TABLE plans DROP CONSTRAINT IF EXISTS plans_configuration_backend_check;
ALTER TABLE plans ADD CONSTRAINT plans_configuration_backend_check CHECK (
    configuration_backend IS NULL OR
    configuration_backend IN ('alter_system', 'managed_conf_file', 'provider')
);

ALTER TABLE write_operations
    ADD COLUMN IF NOT EXISTS configuration_backend VARCHAR(30),
    ADD COLUMN IF NOT EXISTS backend_snapshot JSONB;

ALTER TABLE write_operations DROP CONSTRAINT IF EXISTS write_operations_backend_check;
ALTER TABLE write_operations ADD CONSTRAINT write_operations_backend_check CHECK (
    configuration_backend IS NULL OR
    configuration_backend IN ('alter_system', 'managed_conf_file', 'provider')
);

CREATE TABLE IF NOT EXISTS configuration_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    host_id UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    plan_id UUID REFERENCES plans(id) ON DELETE SET NULL,
    write_operation_id UUID REFERENCES write_operations(id) ON DELETE SET NULL,
    configuration_backend VARCHAR(30) NOT NULL CHECK (
        configuration_backend IN ('alter_system', 'managed_conf_file', 'provider')
    ),
    status VARCHAR(30) NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending', 'applying', 'active', 'pending_restart',
            'rolling_back', 'rolled_back', 'failed'
        )
    ),
    managed_conf_path TEXT,
    parameters JSONB NOT NULL DEFAULT '[]'::jsonb,
    pre_change_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    backend_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    apply_result JSONB,
    rollback_result JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_at TIMESTAMPTZ,
    rolled_back_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_configuration_versions_host
    ON configuration_versions(organization_id, host_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_configuration_versions_plan
    ON configuration_versions(plan_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_commands (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    host_id UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    configuration_version_id UUID REFERENCES configuration_versions(id) ON DELETE CASCADE,
    action VARCHAR(40) NOT NULL CHECK (
        action IN ('managed_conf_preflight', 'managed_conf_apply', 'managed_conf_rollback')
    ),
    idempotency_key VARCHAR(255) NOT NULL UNIQUE,
    status VARCHAR(20) NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'claimed', 'succeeded', 'failed', 'expired')
    ),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    error TEXT,
    claimed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '5 minutes'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_commands_claim
    ON agent_commands(host_id, status, created_at)
    WHERE status IN ('queued', 'claimed');
