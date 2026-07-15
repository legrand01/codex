-- Migration 013: Versioned PostgreSQL parameter catalog and run dispositions.
-- Requirements: 18.1, 18.2, 18.3, 18.4

CREATE TABLE IF NOT EXISTS parameter_catalog_versions (
    version VARCHAR(100) PRIMARY KEY,
    pg_major INTEGER NOT NULL CHECK (pg_major BETWEEN 15 AND 99),
    platform_type VARCHAR(30) NOT NULL CHECK (
        platform_type IN (
            'self_managed', 'aws_rds', 'aurora', 'cloud_sql', 'aiven',
            'other_managed'
        )
    ),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (pg_major, platform_type)
);

INSERT INTO parameter_catalog_versions (version, pg_major, platform_type)
SELECT
    'pg' || major::text || '-' || replace(platform, '_', '-') || '-v1',
    major,
    platform
FROM unnest(ARRAY[15, 16, 17, 18]) AS major
CROSS JOIN unnest(ARRAY[
    'self_managed', 'aws_rds', 'aurora', 'cloud_sql', 'aiven',
    'other_managed'
]) AS platform
ON CONFLICT (version) DO NOTHING;

CREATE TABLE IF NOT EXISTS parameter_catalog_entries (
    catalog_version VARCHAR(100) NOT NULL
        REFERENCES parameter_catalog_versions(version) ON DELETE CASCADE,
    setting_name VARCHAR(63) NOT NULL,
    apply_context VARCHAR(20) NOT NULL CHECK (
        apply_context IN ('reload', 'restart')
    ),
    display_order INTEGER NOT NULL,
    bounded_domain_available BOOLEAN NOT NULL DEFAULT FALSE,
    description TEXT NOT NULL,
    PRIMARY KEY (catalog_version, setting_name),
    UNIQUE (catalog_version, display_order)
);

WITH parameters(setting_name, apply_context, display_order,
                bounded_domain_available, description) AS (
    VALUES
        ('work_mem', 'reload', 1, TRUE, 'Memory available to each query operation'),
        ('random_page_cost', 'reload', 2, TRUE, 'Planner cost for non-sequential page access'),
        ('seq_page_cost', 'reload', 3, FALSE, 'Planner cost for sequential page access'),
        ('checkpoint_completion_target', 'reload', 4, TRUE, 'Checkpoint write spreading target'),
        ('effective_io_concurrency', 'reload', 5, TRUE, 'Expected concurrent storage operations'),
        ('max_parallel_workers_per_gather', 'reload', 6, FALSE, 'Parallel workers per Gather operation'),
        ('max_parallel_workers', 'reload', 7, FALSE, 'Cluster-wide parallel worker budget'),
        ('max_wal_size', 'reload', 8, FALSE, 'Soft WAL size limit between checkpoints'),
        ('min_wal_size', 'reload', 9, FALSE, 'Minimum recycled WAL space'),
        ('bgwriter_lru_maxpages', 'reload', 10, FALSE, 'Background-writer page limit per round'),
        ('bgwriter_delay', 'reload', 11, FALSE, 'Delay between background-writer rounds'),
        ('effective_cache_size', 'reload', 12, FALSE, 'Planner estimate of available cache'),
        ('maintenance_work_mem', 'reload', 13, FALSE, 'Memory for maintenance operations'),
        ('default_statistics_target', 'reload', 14, FALSE, 'Default planner statistics detail'),
        ('max_parallel_maintenance_workers', 'reload', 15, FALSE, 'Parallel workers per maintenance command'),
        ('shared_buffers', 'restart', 16, FALSE, 'PostgreSQL shared buffer allocation'),
        ('max_worker_processes', 'restart', 17, FALSE, 'Background worker process budget'),
        ('wal_buffers', 'restart', 18, FALSE, 'Shared memory reserved for WAL data'),
        ('huge_pages', 'restart', 19, FALSE, 'Huge-page allocation policy')
)
INSERT INTO parameter_catalog_entries (
    catalog_version, setting_name, apply_context, display_order,
    bounded_domain_available, description
)
SELECT
    versions.version, parameters.setting_name, parameters.apply_context,
    parameters.display_order, parameters.bounded_domain_available,
    parameters.description
FROM parameter_catalog_versions AS versions
CROSS JOIN parameters
ON CONFLICT (catalog_version, setting_name) DO NOTHING;

ALTER TABLE loop_runs
    ADD COLUMN IF NOT EXISTS parameter_catalog_version VARCHAR(100)
        REFERENCES parameter_catalog_versions(version);

CREATE TABLE IF NOT EXISTS run_parameter_dispositions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    run_id UUID NOT NULL REFERENCES loop_runs(id) ON DELETE CASCADE,
    host_id UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    catalog_version VARCHAR(100) NOT NULL
        REFERENCES parameter_catalog_versions(version),
    setting_name VARCHAR(63) NOT NULL,
    display_order INTEGER NOT NULL,
    apply_context VARCHAR(20) NOT NULL CHECK (
        apply_context IN ('reload', 'restart')
    ),
    bounded_domain_available BOOLEAN NOT NULL DEFAULT FALSE,
    selected BOOLEAN NOT NULL DEFAULT FALSE,
    supported_on_target BOOLEAN NOT NULL DEFAULT FALSE,
    allowlisted BOOLEAN NOT NULL DEFAULT FALSE,
    current_value TEXT,
    unit TEXT,
    source TEXT,
    sourcefile_or_provider TEXT,
    setting_context TEXT,
    pending_restart BOOLEAN NOT NULL DEFAULT FALSE,
    baseline_value TEXT,
    best_verified_value TEXT,
    pending_candidate_value TEXT,
    final_disposition VARCHAR(60) CHECK (
        final_disposition IS NULL OR final_disposition IN (
            'changed_and_verified', 'retained_at_baseline',
            'blocked_by_policy', 'restart_required',
            'unsupported_on_target', 'not_applicable_to_objective',
            'inconclusive_insufficient_evidence'
        )
    ),
    disposition_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, setting_name)
);

CREATE INDEX IF NOT EXISTS idx_run_parameter_dispositions_run
    ON run_parameter_dispositions(organization_id, run_id, display_order);
CREATE INDEX IF NOT EXISTS idx_run_parameter_dispositions_final
    ON run_parameter_dispositions(final_disposition, updated_at);
