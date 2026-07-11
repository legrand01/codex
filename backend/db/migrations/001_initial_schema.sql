-- Migration 001: Initial Schema
-- Creates all core tables for the Autonomous Postgres DBA Agent Platform
-- Requirements: 10.2, 6.1, 6.2, 6.3, 6.4, 9.1

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================================
-- Hosts and Fleet Management
-- Requirements: 1.1, 1.2, 1.4
-- ============================================================================
CREATE TABLE hosts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hostname VARCHAR(255) NOT NULL UNIQUE,
    pg_version VARCHAR(50),
    server_role VARCHAR(20) CHECK (server_role IN ('primary', 'replica')),
    health_status VARCHAR(20) DEFAULT 'unknown' CHECK (health_status IN ('healthy', 'unhealthy', 'unknown')),
    connection_status VARCHAR(20) DEFAULT 'disconnected' CHECK (connection_status IN ('connected', 'degraded', 'disconnected')),
    last_heartbeat TIMESTAMPTZ,
    restart_required_enabled BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- Loop Runs
-- Requirements: 2.1, 11.1, 11.2
-- ============================================================================
CREATE TABLE loop_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    host_id UUID REFERENCES hosts(id),
    goal TEXT NOT NULL,
    status VARCHAR(30) DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed', 'manually_halted', 'unresponsive', 'timed_out')),
    current_step VARCHAR(30) CHECK (current_step IN ('observe', 'snapshot', 'diagnose', 'propose_plan', 'safety_check', 'approval_gate', 'dry_run', 'apply', 'verify', 'measure', 'keep_rollback', 'report')),
    current_iteration INTEGER DEFAULT 1,
    max_iterations INTEGER DEFAULT 10,
    max_steps INTEGER DEFAULT 20,
    approval_timeout_hours INTEGER DEFAULT 24,
    verification_window_seconds INTEGER DEFAULT 60,
    degradation_threshold_pct NUMERIC(5,2) DEFAULT 10.0,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    last_step_transition_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    failure_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- Evidence Snapshots
-- Requirements: 6.1, 6.2, 6.3, 6.4, 6.7
-- ============================================================================
CREATE TABLE evidence_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID REFERENCES loop_runs(id),
    host_id UUID REFERENCES hosts(id),
    evidence_type VARCHAR(30) NOT NULL CHECK (evidence_type IN ('pg_settings', 'pg_stat_database', 'pg_stat_statements', 'locks', 'replication', 'wal_checkpoint', 'os_metrics')),
    collected_at TIMESTAMPTZ NOT NULL,
    data JSONB NOT NULL,
    quality_score NUMERIC(3,2) CHECK (quality_score BETWEEN 0.0 AND 1.0),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_evidence_run_type ON evidence_snapshots(run_id, evidence_type);
CREATE INDEX idx_evidence_collected_at ON evidence_snapshots(collected_at);
CREATE INDEX idx_evidence_host ON evidence_snapshots(host_id);

-- ============================================================================
-- Plans
-- Requirements: 4.1, 4.2, 9.1
-- ============================================================================
CREATE TABLE plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID REFERENCES loop_runs(id),
    host_id UUID REFERENCES hosts(id),
    status VARCHAR(30) DEFAULT 'pending_approval' CHECK (status IN ('pending_approval', 'approved', 'rejected', 'pending_forwarding', 'forwarding_failed', 'dry_run_passed', 'dry_run_failed', 'applied', 'rolled_back', 'rollback_failed', 'blocked')),
    proposed_changes JSONB NOT NULL,
    evidence_references JSONB NOT NULL,
    risk_score INTEGER CHECK (risk_score BETWEEN 0 AND 100),
    confidence_score NUMERIC(3,2) CHECK (confidence_score BETWEEN 0.0 AND 1.0),
    uncertainty_explanation TEXT,
    rollback_instructions JSONB NOT NULL,
    rejection_reason TEXT,
    approved_by VARCHAR(255),
    approved_at TIMESTAMPTZ,
    rejected_by VARCHAR(255),
    rejected_at TIMESTAMPTZ,
    applied_at TIMESTAMPTZ,
    rolled_back_at TIMESTAMPTZ,
    submission_time TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_plans_status ON plans(status);
CREATE INDEX idx_plans_run ON plans(run_id);
CREATE INDEX idx_plans_submission ON plans(submission_time);

-- ============================================================================
-- Guardrail Allowlist
-- Requirements: 8.1, 8.2, 8.3
-- ============================================================================
CREATE TABLE guardrail_allowlist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    host_id UUID REFERENCES hosts(id),
    setting_name VARCHAR(255) NOT NULL,
    parameter_context VARCHAR(50) NOT NULL CHECK (parameter_context IN ('reload', 'restart')),
    max_deviation_pct NUMERIC(5,2),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(host_id, setting_name)
);

-- ============================================================================
-- Audit Log (append-only)
-- Requirements: 10.1, 10.2, 10.4
-- ============================================================================
CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor_type VARCHAR(20) NOT NULL CHECK (actor_type IN ('human', 'system')),
    actor_name VARCHAR(255) NOT NULL,
    action_type VARCHAR(50) NOT NULL,
    target_host_id UUID,
    result VARCHAR(20) NOT NULL CHECK (result IN ('success', 'failure', 'blocked')),
    result_reason TEXT,
    details JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Append-only rules: prevent UPDATE and DELETE on audit_log
-- Requirement 10.2: The Audit_Log SHALL be append-only such that no existing entry
-- can be updated or deleted through platform interfaces
CREATE RULE no_update_audit AS ON UPDATE TO audit_log DO INSTEAD NOTHING;
CREATE RULE no_delete_audit AS ON DELETE TO audit_log DO INSTEAD NOTHING;

CREATE INDEX idx_audit_run ON audit_log(run_id);
CREATE INDEX idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX idx_audit_action ON audit_log(action_type);
CREATE INDEX idx_audit_target_host ON audit_log(target_host_id);

-- ============================================================================
-- DBA Reports
-- Requirements: 13.1, 13.4, 13.6
-- ============================================================================
CREATE TABLE dba_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID REFERENCES loop_runs(id) UNIQUE,
    goal TEXT NOT NULL,
    host_id UUID REFERENCES hosts(id),
    outcome_status VARCHAR(30) CHECK (outcome_status IN ('success', 'partial_success', 'failure')),
    report_content JSONB NOT NULL,
    generated_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '90 days')
);

CREATE INDEX idx_reports_generated ON dba_reports(generated_at);
CREATE INDEX idx_reports_host ON dba_reports(host_id);
CREATE INDEX idx_reports_expires ON dba_reports(expires_at);

-- ============================================================================
-- Host Agent Configuration
-- Requirements: 6.1, 6.2, 6.3, 6.4
-- ============================================================================
CREATE TABLE agent_config (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    host_id UUID REFERENCES hosts(id) UNIQUE,
    pg_settings_interval_sec INTEGER DEFAULT 60 CHECK (pg_settings_interval_sec BETWEEN 10 AND 3600),
    pg_stats_interval_sec INTEGER DEFAULT 30 CHECK (pg_stats_interval_sec BETWEEN 5 AND 600),
    locks_replication_interval_sec INTEGER DEFAULT 15 CHECK (locks_replication_interval_sec BETWEEN 5 AND 300),
    os_metrics_interval_sec INTEGER DEFAULT 15 CHECK (os_metrics_interval_sec BETWEEN 5 AND 300),
    max_query_entries INTEGER DEFAULT 100,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- Guardrail Configuration
-- Requirements: 9.1, 9.2, 9.3
-- ============================================================================
CREATE TABLE guardrail_config (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    risk_threshold INTEGER DEFAULT 70 CHECK (risk_threshold BETWEEN 0 AND 100),
    dry_run_timeout_sec INTEGER DEFAULT 30,
    approval_timeout_hours INTEGER DEFAULT 24,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- Schema Migrations Tracking Table
-- Used by init_db.py to track which migrations have been applied
-- ============================================================================
CREATE TABLE IF NOT EXISTS schema_migrations (
    id SERIAL PRIMARY KEY,
    filename VARCHAR(255) NOT NULL UNIQUE,
    applied_at TIMESTAMPTZ DEFAULT NOW()
);
