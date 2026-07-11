"""Real PostgreSQL proof for the P0 apply and rollback boundary.

Set both ``P0_TEST_CONTROL_DSN`` and ``P0_TEST_TARGET_DSN`` to run this test.
The target must be disposable: the test temporarily changes ``work_mem`` with
ALTER SYSTEM and restores its original provenance in a ``finally`` block.
"""

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest

from backend.config import settings
from backend.security import DEFAULT_ORGANIZATION_ID
from backend.services.run_queue import RunQueue
from backend.services.target_executor import (
    EXECUTION_LOCK_ID,
    TargetPostgresExecutor,
    TargetVerificationError,
    WriteInterlockError,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_alter_system_verify_and_rollback(monkeypatch):
    if os.environ.get("RUN_P0_TARGET_INTEGRATION") != "1":
        pytest.skip("Set RUN_P0_TARGET_INTEGRATION=1 for the disposable target test")
    control_dsn = os.environ.get("P0_TEST_CONTROL_DSN", settings.database_url)
    target_dsn = os.environ.get("P0_TEST_TARGET_DSN", settings.database_url)

    pool = await asyncpg.create_pool(control_dsn, min_size=1, max_size=3)
    host_id = uuid4()
    hostname = f"p0-target-{host_id}"
    executor = TargetPostgresExecutor(
        pool,
        environ={"P0_TEST_TARGET_DSN": target_dsn},
    )
    snapshot = None
    applied = False
    monkeypatch.setattr(settings, "write_execution_enabled", True)
    monkeypatch.setattr(settings, "target_verify_timeout_sec", 5)

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO hosts (
                    id, organization_id, hostname, environment, server_role,
                    target_dsn_env, writes_enabled
                ) VALUES ($1, $2, $3, 'staging', 'primary', 'P0_TEST_TARGET_DSN', TRUE)
                """,
                host_id,
                DEFAULT_ORGANIZATION_ID,
                hostname,
            )
            await conn.execute(
                """
                INSERT INTO guardrail_allowlist (
                    host_id, setting_name, parameter_context, max_deviation_pct
                ) VALUES ($1, 'work_mem', 'reload', 200)
                """,
                host_id,
            )

        dry_run = await executor.dry_run(
            host_id,
            [{"setting_name": "work_mem", "proposed_value": "8MB"}],
        )
        assert dry_run.passed, dry_run.errors
        snapshot = dry_run.snapshot

        outcome = await executor.apply(
            host_id,
            [{"setting_name": "work_mem", "proposed_value": "8MB"}],
            snapshot,
        )
        applied = True
        assert outcome.succeeded is True
        assert outcome.verified_values["work_mem"].lower() == "8mb"

        rollback = await executor.rollback(host_id, snapshot)
        applied = False
        assert rollback.succeeded is True
        assert rollback.rolled_back is True
        assert rollback.verified_values["work_mem"].lower() == snapshot["work_mem"][
            "value"
        ].lower()
    finally:
        if applied and snapshot:
            await executor.rollback(host_id, snapshot)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM guardrail_allowlist WHERE host_id = $1", host_id)
            await conn.execute("DELETE FROM hosts WHERE id = $1", host_id)
        await pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_queue_claim_is_exclusive():
    if os.environ.get("RUN_P0_TARGET_INTEGRATION") != "1":
        pytest.skip("Set RUN_P0_TARGET_INTEGRATION=1 for the disposable queue test")
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    host_id = uuid4()
    run_id = uuid4()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO hosts (id, organization_id, hostname) VALUES ($1, $2, $3)",
                host_id,
                DEFAULT_ORGANIZATION_ID,
                f"p0-queue-{host_id}",
            )
            await conn.execute(
                """
                INSERT INTO loop_runs (id, organization_id, host_id, goal, status)
                VALUES ($1, $2, $3, 'queue exclusivity proof', 'queued')
                """,
                run_id,
                DEFAULT_ORGANIZATION_ID,
                host_id,
            )
            await conn.execute(
                "INSERT INTO run_jobs (run_id, organization_id) VALUES ($1, $2)",
                run_id,
                DEFAULT_ORGANIZATION_ID,
            )
            queued = await conn.fetchval(
                "SELECT COUNT(*) FROM run_jobs WHERE run_id = $1 AND status = 'queued' "
                "AND attempts < 5 AND available_at <= NOW()",
                run_id,
            )
            assert queued == 1

        first, second = await asyncio.gather(
            RunQueue(pool).claim("worker-a"),
            RunQueue(pool).claim("worker-b"),
        )
        async with pool.acquire() as conn:
            state = dict(
                await conn.fetchrow(
                    "SELECT status, attempts, claimed_by, available_at FROM run_jobs "
                    "WHERE run_id = $1",
                    run_id,
                )
            )
        assert sum(job is not None for job in (first, second)) == 1, state
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM run_jobs WHERE run_id = $1", run_id)
            await conn.execute("DELETE FROM loop_runs WHERE id = $1", run_id)
            await conn.execute("DELETE FROM hosts WHERE id = $1", host_id)
        await pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_role_lock_and_failed_rollback_boundaries(monkeypatch):
    """Exercise role drift, advisory locking, and rollback failure on PostgreSQL."""
    if os.environ.get("RUN_P0_TARGET_INTEGRATION") != "1":
        pytest.skip("Set RUN_P0_TARGET_INTEGRATION=1 for the disposable boundary test")
    target_dsn = os.environ.get("P0_TEST_TARGET_DSN", settings.database_url)
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    host_id = uuid4()
    executor = TargetPostgresExecutor(
        pool,
        environ={"P0_BOUNDARY_TARGET_DSN": target_dsn},
    )
    snapshot = None
    monkeypatch.setattr(settings, "write_execution_enabled", True)
    monkeypatch.setattr(settings, "target_verify_timeout_sec", 5)

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO hosts (
                    id, organization_id, hostname, environment, server_role,
                    target_dsn_env, writes_enabled
                ) VALUES ($1, $2, $3, 'staging', 'primary', $4, TRUE)
                """,
                host_id,
                DEFAULT_ORGANIZATION_ID,
                f"p0-boundary-{host_id}",
                "P0_BOUNDARY_TARGET_DSN",
            )

        snapshot = await executor.capture_snapshot(host_id, ["work_mem"])
        changes = [{"setting_name": "work_mem", "proposed_value": "8MB"}]

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE hosts SET server_role = 'replica' WHERE id = $1",
                host_id,
            )
        with pytest.raises(WriteInterlockError, match="confirmed primary"):
            await executor.apply(host_id, changes, snapshot)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE hosts SET server_role = 'primary' WHERE id = $1",
                host_id,
            )

        locker = await asyncpg.connect(target_dsn)
        await locker.execute("SELECT pg_advisory_lock($1)", EXECUTION_LOCK_ID)
        apply_task = asyncio.create_task(executor.apply(host_id, changes, snapshot))
        await asyncio.sleep(0.1)
        assert not apply_task.done()
        await locker.execute("SELECT pg_advisory_unlock($1)", EXECUTION_LOCK_ID)
        await locker.close()
        outcome = await apply_task
        assert outcome.verified_values["work_mem"].lower() == "8mb"

        class ReloadFailConnection:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            async def fetchval(self, query, *args):
                if "pg_reload_conf" in query:
                    return False
                return await self.connection.fetchval(query, *args)

        async def failing_connector(*args, **kwargs):
            return ReloadFailConnection(await asyncpg.connect(*args, **kwargs))

        failing_executor = TargetPostgresExecutor(
            pool,
            connector=failing_connector,
            environ={"P0_BOUNDARY_TARGET_DSN": target_dsn},
        )
        with pytest.raises(TargetVerificationError, match="rollback reload"):
            await failing_executor.rollback(host_id, snapshot)

        rollback = await executor.rollback(host_id, snapshot)
        assert rollback.verified_values["work_mem"].lower() == snapshot["work_mem"][
            "value"
        ].lower()
        snapshot = None
    finally:
        if snapshot is not None:
            await executor.rollback(host_id, snapshot)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM hosts WHERE id = $1", host_id)
        await pool.close()
