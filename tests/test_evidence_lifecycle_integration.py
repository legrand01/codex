"""Real PostgreSQL proof for Task 27 evidence retention semantics."""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from backend.config import settings
from backend.services.evidence_lifecycle import (
    EvidenceLifecycleManager,
    EvidenceRetentionPolicy,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_EVIDENCE_LIFECYCLE_INTEGRATION") != "1",
    reason="set RUN_EVIDENCE_LIFECYCLE_INTEGRATION=1 to use local control PostgreSQL",
)


@pytest.mark.asyncio
async def test_real_postgres_preserves_active_and_recent_references_then_rolls_up_delete():
    conn = await asyncpg.connect(settings.database_url)
    now = datetime.now(timezone.utc)
    organization_id = uuid.uuid4()
    host_id = uuid.uuid4()
    terminal_run_id = uuid.uuid4()
    active_run_id = uuid.uuid4()

    outer = conn.transaction()
    await outer.start()
    try:
        await conn.execute(
            "INSERT INTO organizations (id, slug, name) VALUES ($1, $2, $3)",
            organization_id,
            f"task27-{organization_id}",
            "Task 27 integration tenant",
        )
        await conn.execute(
            """
            INSERT INTO hosts (id, organization_id, hostname, database_name)
            VALUES ($1, $2, $3, 'task27')
            """,
            host_id,
            organization_id,
            f"task27-{host_id}",
        )
        await conn.execute(
            """
            INSERT INTO loop_runs (
                id, organization_id, host_id, goal, status, current_step,
                completed_at, configuration_backend
            ) VALUES
                ($1, $3, $4, 'terminal retention proof', 'completed', 'report', $5,
                 'alter_system'),
                ($2, $3, $4, 'active retention proof', 'running', 'observe', NULL,
                 'alter_system')
            """,
            terminal_run_id,
            active_run_id,
            organization_id,
            host_id,
            now - timedelta(days=35),
        )

        async def insert_snapshot(run_id, age_days, evidence_type):
            return await conn.fetchval(
                """
                INSERT INTO evidence_snapshots (
                    run_id, host_id, evidence_type, collected_at, data, quality_score
                ) VALUES ($1, $2, $3, $4, $5::jsonb, 0.8)
                RETURNING id
                """,
                run_id,
                host_id,
                evidence_type,
                now - timedelta(days=age_days),
                json.dumps({"proof": evidence_type, "payload": "x" * 200}),
            )

        ordinary_old = await insert_snapshot(terminal_run_id, 40, "pg_settings")
        referenced_recent = await insert_snapshot(
            terminal_run_id, 40, "pg_stat_database"
        )
        referenced_expired = await insert_snapshot(terminal_run_id, 100, "locks")
        active_old = await insert_snapshot(active_run_id, 100, "os_metrics")
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM evidence_snapshots "
            "WHERE host_id = $1 AND data_size_bytes IS NULL",
            host_id,
        ) == 0

        await conn.execute(
            """
            INSERT INTO plans (
                run_id, host_id, organization_id, proposed_changes,
                evidence_references, rollback_instructions, risk_score,
                confidence_score
            ) VALUES ($1, $2, $3, '[]'::jsonb, $4::jsonb, '[]'::jsonb, 0, 1.0)
            """,
            terminal_run_id,
            host_id,
            organization_id,
            json.dumps(
                [
                    {"snapshot_id": str(referenced_recent)},
                    {"snapshot_id": str(referenced_expired)},
                ]
            ),
        )

        manager = EvidenceLifecycleManager(
            conn,
            EvidenceRetentionPolicy(
                raw_retention_days=30,
                referenced_retention_days=90,
                rollup_retention_days=365,
                batch_size=10,
                max_batches_per_run=2,
            ),
        )
        result = await manager.run_cleanup(organization_id, triggered_by="integration-test")

        assert result["status"] == "completed"
        assert result["snapshots_deleted"] == 2
        remaining = await conn.fetch(
            "SELECT id FROM evidence_snapshots WHERE host_id = $1", host_id
        )
        remaining_ids = {row["id"] for row in remaining}
        assert remaining_ids == {referenced_recent, active_old}
        assert ordinary_old not in remaining_ids
        assert referenced_expired not in remaining_ids

        rollup = await conn.fetchrow(
            """
            SELECT SUM(snapshot_count)::bigint AS snapshots,
                   COUNT(*)::integer AS rows,
                   SUM(total_bytes)::bigint AS bytes
            FROM evidence_rollups
            WHERE organization_id = $1
            """,
            organization_id,
        )
        assert rollup["snapshots"] == 2
        assert rollup["rows"] == 2
        assert rollup["bytes"] > 0

        status = await manager.status(organization_id)
        assert status["raw"]["snapshot_count"] == 2
        assert status["eligible"]["snapshot_count"] == 0
        assert status["rollups"]["snapshot_count"] == 2
        assert status["last_run"]["status"] == "completed"

        event = await conn.fetchval(
            """
            SELECT event_code FROM host_events
            WHERE organization_id = $1
            ORDER BY occurred_at DESC LIMIT 1
            """,
            organization_id,
        )
        assert event == "EVIDENCE_RETENTION_COMPLETED"

    finally:
        # Roll back every integration fixture even when an assertion fails.
        await outer.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_real_postgres_delete_failure_rolls_back_rollup_and_records_failure():
    conn = await asyncpg.connect(settings.database_url)
    organization_id = uuid.uuid4()
    host_id = uuid.uuid4()
    run_id = uuid.uuid4()
    outer = conn.transaction()
    await outer.start()
    try:
        await conn.execute(
            "INSERT INTO organizations (id, slug, name) VALUES ($1, $2, 'Task 27 failure')",
            organization_id,
            f"task27-failure-{organization_id}",
        )
        await conn.execute(
            "INSERT INTO hosts (id, organization_id, hostname) VALUES ($1, $2, $3)",
            host_id,
            organization_id,
            f"task27-failure-{host_id}",
        )
        await conn.execute(
            """
            INSERT INTO loop_runs (
                id, organization_id, host_id, goal, status, current_step,
                completed_at, configuration_backend
            ) VALUES (
                $1, $2, $3, 'atomic failure proof', 'completed', 'report', NOW(),
                'alter_system'
            )
            """,
            run_id,
            organization_id,
            host_id,
        )
        snapshot_id = await conn.fetchval(
            """
            INSERT INTO evidence_snapshots (
                run_id, host_id, evidence_type, collected_at, data, quality_score
            ) VALUES ($1, $2, 'pg_settings', NOW() - INTERVAL '40 days',
                      '{"proof":"rollback"}'::jsonb, 1.0)
            RETURNING id
            """,
            run_id,
            host_id,
        )
        await conn.execute(
            f"""
            CREATE FUNCTION task27_reject_evidence_delete() RETURNS TRIGGER AS $$
            BEGIN
                IF OLD.host_id = '{host_id}'::uuid THEN
                    RAISE EXCEPTION 'synthetic delete rejection';
                END IF;
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER task27_reject_evidence_delete_trigger
            BEFORE DELETE ON evidence_snapshots
            FOR EACH ROW EXECUTE FUNCTION task27_reject_evidence_delete();
            """
        )

        manager = EvidenceLifecycleManager(
            conn,
            EvidenceRetentionPolicy(
                raw_retention_days=30,
                referenced_retention_days=90,
                rollup_retention_days=365,
                batch_size=10,
                max_batches_per_run=1,
            ),
        )
        with pytest.raises(asyncpg.RaiseError, match="synthetic delete rejection"):
            await manager.run_cleanup(organization_id, triggered_by="failure-test")

        assert await conn.fetchval(
            "SELECT COUNT(*) FROM evidence_snapshots WHERE id = $1", snapshot_id
        ) == 1
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM evidence_rollups WHERE organization_id = $1",
            organization_id,
        ) == 0
        maintenance = await conn.fetchrow(
            """
            SELECT status, snapshots_deleted, rollup_rows_written, error_message
            FROM evidence_maintenance_runs
            WHERE organization_id = $1 ORDER BY started_at DESC LIMIT 1
            """,
            organization_id,
        )
        assert maintenance["status"] == "failed"
        assert maintenance["snapshots_deleted"] == 0
        assert maintenance["rollup_rows_written"] == 0
        assert "synthetic delete rejection" in maintenance["error_message"]
        assert await conn.fetchval(
            """
            SELECT event_code FROM host_events
            WHERE organization_id = $1 ORDER BY occurred_at DESC LIMIT 1
            """,
            organization_id,
        ) == "EVIDENCE_RETENTION_FAILED"
    finally:
        await outer.rollback()
        await conn.close()
