-- Persist the exact comparable measurement so worker recovery cannot drift.

ALTER TABLE tuning_candidates
    ADD COLUMN IF NOT EXISTS measurement_result JSONB;
