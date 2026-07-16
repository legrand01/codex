-- Backfill verification provenance for configuration versions created before Task 25.

UPDATE configuration_versions
SET verification_result = CASE
        WHEN status = 'rolled_back' AND rollback_result IS NOT NULL THEN
            jsonb_build_object(
                'succeeded', TRUE,
                'rolled_back', TRUE,
                'legacy_backfill', TRUE,
                'result', rollback_result
            )
        WHEN status IN ('active', 'pending_restart', 'superseded')
             AND apply_result IS NOT NULL THEN
            jsonb_build_object(
                'succeeded', COALESCE((apply_result->>'succeeded')::boolean, TRUE),
                'legacy_backfill', TRUE,
                'verified_values', COALESCE(apply_result->'verified_values', '{}'::jsonb),
                'pending_restart', COALESCE(apply_result->'pending_restart', '[]'::jsonb)
            )
        ELSE verification_result
    END,
    verified_at = CASE
        WHEN status = 'rolled_back' AND rollback_result IS NOT NULL THEN
            COALESCE(verified_at, rolled_back_at)
        WHEN status IN ('active', 'pending_restart', 'superseded')
             AND apply_result IS NOT NULL THEN
            COALESCE(verified_at, applied_at)
        ELSE verified_at
    END
WHERE verification_result IS NULL;
