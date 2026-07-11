-- Migration 004: Persist the performance baseline used for keep/rollback.

ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS pre_metric_evidence JSONB;
