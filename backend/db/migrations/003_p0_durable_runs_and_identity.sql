-- Migration 003: Durable run jobs and authenticated organization boundary.

CREATE TABLE IF NOT EXISTS organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug VARCHAR(100) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO organizations (id, slug, name)
VALUES ('00000000-0000-0000-0000-000000000001', 'default', 'Default Organization')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS api_principals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    subject VARCHAR(255) NOT NULL,
    display_name VARCHAR(255) NOT NULL,
    role VARCHAR(20) NOT NULL CHECK (role IN ('viewer', 'operator', 'approver', 'admin')),
    api_key_hash CHAR(64) NOT NULL UNIQUE,
    disabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    UNIQUE (organization_id, subject)
);

ALTER TABLE hosts
    ADD COLUMN IF NOT EXISTS organization_id UUID REFERENCES organizations(id),
    ADD COLUMN IF NOT EXISTS agent_token_hash CHAR(64);
UPDATE hosts
SET organization_id = '00000000-0000-0000-0000-000000000001'
WHERE organization_id IS NULL;
ALTER TABLE hosts ALTER COLUMN organization_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_hosts_organization ON hosts(organization_id, hostname);

ALTER TABLE loop_runs ADD COLUMN IF NOT EXISTS organization_id UUID REFERENCES organizations(id);
UPDATE loop_runs r
SET organization_id = h.organization_id
FROM hosts h
WHERE r.host_id = h.id AND r.organization_id IS NULL;
UPDATE loop_runs
SET organization_id = '00000000-0000-0000-0000-000000000001'
WHERE organization_id IS NULL;
ALTER TABLE loop_runs ALTER COLUMN organization_id SET NOT NULL;

ALTER TABLE plans ADD COLUMN IF NOT EXISTS organization_id UUID REFERENCES organizations(id);
UPDATE plans p
SET organization_id = h.organization_id
FROM hosts h
WHERE p.host_id = h.id AND p.organization_id IS NULL;
UPDATE plans
SET organization_id = '00000000-0000-0000-0000-000000000001'
WHERE organization_id IS NULL;
ALTER TABLE plans ALTER COLUMN organization_id SET NOT NULL;

DROP RULE IF EXISTS no_update_audit ON audit_log;
DROP RULE IF EXISTS no_delete_audit ON audit_log;
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS organization_id UUID REFERENCES organizations(id);
UPDATE audit_log a
SET organization_id = h.organization_id
FROM hosts h
WHERE a.target_host_id = h.id AND a.organization_id IS NULL;
UPDATE audit_log
SET organization_id = '00000000-0000-0000-0000-000000000001'
WHERE organization_id IS NULL;
ALTER TABLE audit_log ALTER COLUMN organization_id SET NOT NULL;
CREATE RULE no_update_audit AS ON UPDATE TO audit_log DO INSTEAD NOTHING;
CREATE RULE no_delete_audit AS ON DELETE TO audit_log DO INSTEAD NOTHING;

ALTER TABLE loop_runs DROP CONSTRAINT IF EXISTS loop_runs_status_check;
ALTER TABLE loop_runs ADD CONSTRAINT loop_runs_status_check CHECK (
    status IN (
        'queued', 'running', 'waiting_approval', 'completed', 'failed',
        'manually_halted', 'unresponsive', 'timed_out'
    )
);

CREATE TABLE IF NOT EXISTS run_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL UNIQUE REFERENCES loop_runs(id),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    status VARCHAR(30) NOT NULL DEFAULT 'queued' CHECK (
        status IN (
            'queued', 'claimed', 'waiting_approval', 'cancel_requested',
            'cancelled', 'succeeded', 'failed'
        )
    ),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_by VARCHAR(255),
    claimed_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    last_error TEXT,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_run_jobs_claim
    ON run_jobs(status, available_at, lease_expires_at);
