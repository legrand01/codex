-- Migration 019: make lifecycle footprint reads O(rows/index), not O(JSON bytes).
-- Requirements: 21.3, 21.6, Task 27.

ALTER TABLE evidence_snapshots
    ADD COLUMN IF NOT EXISTS data_size_bytes BIGINT
        CHECK (data_size_bytes IS NULL OR data_size_bytes >= 0);

CREATE OR REPLACE FUNCTION set_evidence_data_size_bytes()
RETURNS TRIGGER AS $$
BEGIN
    NEW.data_size_bytes := octet_length(NEW.data::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_evidence_data_size_bytes ON evidence_snapshots;
CREATE TRIGGER trg_evidence_data_size_bytes
BEFORE INSERT OR UPDATE OF data ON evidence_snapshots
FOR EACH ROW EXECUTE FUNCTION set_evidence_data_size_bytes();

ALTER TABLE evidence_maintenance_runs
    ADD COLUMN IF NOT EXISTS sizes_backfilled BIGINT NOT NULL DEFAULT 0
        CHECK (sizes_backfilled >= 0);
