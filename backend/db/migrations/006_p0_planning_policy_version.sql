-- Persist the exact planning ruleset that produced every executable plan.

ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS planning_policy_version VARCHAR(100)
        NOT NULL DEFAULT 'deterministic-postgres-policy-v1',
    ADD COLUMN IF NOT EXISTS planner_kind VARCHAR(30)
        NOT NULL DEFAULT 'deterministic';

CREATE INDEX IF NOT EXISTS idx_plans_policy_version
    ON plans(planning_policy_version, submission_time);
