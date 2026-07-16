-- Migration 015: configuration history, single-writer agent leases, and events.
-- Requirements: 20.1-20.7, Task 25.

ALTER TABLE hosts
    ADD COLUMN IF NOT EXISTS agent_write_ambiguous BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS agent_lease_holder_id UUID,
    ADD COLUMN IF NOT EXISTS agent_lease_expires_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS host_agent_instances (
    host_id UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    instance_id UUID NOT NULL,
    organization_id UUID NOT NULL REFERENCES organizations(id),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_expires_at TIMESTAMPTZ NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (host_id, instance_id)
);

CREATE INDEX IF NOT EXISTS idx_host_agent_instances_active
    ON host_agent_instances(host_id, lease_expires_at DESC);

CREATE TABLE IF NOT EXISTS event_code_catalog (
    event_code VARCHAR(64) PRIMARY KEY,
    default_severity VARCHAR(20) NOT NULL CHECK (
        default_severity IN ('info', 'warning', 'error', 'critical')
    ),
    component VARCHAR(50) NOT NULL,
    description TEXT NOT NULL
);

INSERT INTO event_code_catalog (
    event_code, default_severity, component, description
) VALUES
    ('AGENT_DUPLICATE_DETECTED', 'critical', 'host_agent', 'Multiple active agents share one host identity'),
    ('AGENT_DUPLICATE_RESOLVED', 'info', 'host_agent', 'The host returned to one active agent instance'),
    ('AGENT_COMMAND_FAILED', 'error', 'host_agent', 'A durable agent command failed or expired'),
    ('AGENT_CAPABILITY_DEGRADED', 'warning', 'host_agent', 'One or more required agent capabilities became unavailable'),
    ('CANDIDATE_BLOCKED', 'warning', 'optimizer', 'A candidate was blocked before measurement'),
    ('CANDIDATE_KEPT', 'info', 'optimizer', 'A measured candidate was retained'),
    ('CANDIDATE_ROLLED_BACK', 'warning', 'optimizer', 'A measured candidate was rolled back'),
    ('CANDIDATE_INCONCLUSIVE', 'warning', 'optimizer', 'Candidate evidence was not conclusive'),
    ('PLAN_APPROVED', 'info', 'approval', 'A DBA approved a plan'),
    ('PLAN_REJECTED', 'warning', 'approval', 'A DBA rejected a plan'),
    ('CONFIG_APPLY_STARTED', 'info', 'configuration', 'Configuration apply started'),
    ('CONFIG_APPLY_SUCCEEDED', 'info', 'configuration', 'Configuration apply and verification succeeded'),
    ('CONFIG_APPLY_FAILED', 'error', 'configuration', 'Configuration apply failed'),
    ('CONFIG_REAPPLY_REQUESTED', 'info', 'configuration', 'A prior verified configuration was requested for guarded reapply'),
    ('CONFIG_RELOAD_SUCCEEDED', 'info', 'configuration', 'PostgreSQL reload converged'),
    ('CONFIG_RELOAD_FAILED', 'error', 'configuration', 'PostgreSQL reload failed'),
    ('CONFIG_RESTART_PENDING', 'warning', 'configuration', 'Configuration is staged pending restart'),
    ('CONFIG_RESTART_VERIFIED', 'info', 'configuration', 'Restart settings were verified active'),
    ('CONFIG_ROLLBACK_STARTED', 'warning', 'configuration', 'Configuration rollback started'),
    ('CONFIG_ROLLBACK_SUCCEEDED', 'info', 'configuration', 'Configuration rollback and verification succeeded'),
    ('CONFIG_ROLLBACK_FAILED', 'critical', 'configuration', 'Configuration rollback failed'),
    ('CONFIG_PRECEDENCE_CONFLICT', 'error', 'configuration', 'A higher-precedence source blocked the proposed configuration'),
    ('WORKLOAD_COVERAGE_WARNING', 'warning', 'measurement', 'Workload coverage was insufficient or unstable'),
    ('REPORT_GENERATED', 'info', 'reporting', 'A tuning report was generated'),
    ('REPORT_GENERATION_FAILED', 'error', 'reporting', 'A tuning report could not be generated')
ON CONFLICT (event_code) DO UPDATE SET
    default_severity = EXCLUDED.default_severity,
    component = EXCLUDED.component,
    description = EXCLUDED.description;

CREATE TABLE IF NOT EXISTS host_events (
    id BIGSERIAL PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organizations(id),
    host_id UUID REFERENCES hosts(id) ON DELETE CASCADE,
    run_id UUID REFERENCES loop_runs(id) ON DELETE SET NULL,
    configuration_version_id UUID REFERENCES configuration_versions(id) ON DELETE SET NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    severity VARCHAR(20) NOT NULL CHECK (
        severity IN ('info', 'warning', 'error', 'critical')
    ),
    component VARCHAR(50) NOT NULL,
    event_code VARCHAR(64) NOT NULL REFERENCES event_code_catalog(event_code),
    message TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_host_events_filters
    ON host_events(organization_id, occurred_at DESC, severity, event_code);
CREATE INDEX IF NOT EXISTS idx_host_events_host
    ON host_events(organization_id, host_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_host_events_run
    ON host_events(organization_id, run_id, occurred_at DESC)
    WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_host_events_configuration
    ON host_events(configuration_version_id, occurred_at DESC)
    WHERE configuration_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_host_events_search
    ON host_events USING GIN (
        to_tsvector('simple', message || ' ' || details::text)
    );

ALTER TABLE configuration_versions
    ADD COLUMN IF NOT EXISTS run_id UUID REFERENCES loop_runs(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS database_name VARCHAR(63),
    ADD COLUMN IF NOT EXISTS source_provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS verification_result JSONB,
    ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS origin_configuration_version_id UUID
        REFERENCES configuration_versions(id) ON DELETE SET NULL;

UPDATE configuration_versions v
SET run_id = p.run_id
FROM plans p
WHERE v.plan_id = p.id AND v.run_id IS NULL;

UPDATE configuration_versions v
SET database_name = h.database_name
FROM hosts h
WHERE v.host_id = h.id AND v.database_name IS NULL;

ALTER TABLE configuration_versions
    DROP CONSTRAINT IF EXISTS configuration_versions_status_check;
ALTER TABLE configuration_versions
    ADD CONSTRAINT configuration_versions_status_check CHECK (
        status IN (
            'pending', 'applying', 'active', 'pending_restart', 'superseded',
            'rolling_back', 'rolled_back', 'failed'
        )
    );

CREATE INDEX IF NOT EXISTS idx_configuration_versions_history
    ON configuration_versions(organization_id, host_id, database_name, created_at DESC);

ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS source_configuration_version_id UUID
        REFERENCES configuration_versions(id) ON DELETE SET NULL;
