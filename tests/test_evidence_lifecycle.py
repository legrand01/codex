"""Task 27 evidence retention, atomic rollup, and scheduling tests."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.services.evidence_lifecycle import (
    ELIGIBLE_EVIDENCE_SQL,
    EvidenceLifecycleManager,
    EvidenceRetentionPolicy,
)
from backend.workers.run_worker import _evidence_maintenance


class FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.calls.append(("transaction_begin", None, ()))
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.calls.append(
            ("transaction_rollback" if exc else "transaction_commit", None, ())
        )
        return False


class LifecycleConnection:
    def __init__(self, batches=None, *, lock_acquired=True, fail_rollup=False):
        self.batches = list(batches or [])
        self.lock_acquired = lock_acquired
        self.fail_rollup = fail_rollup
        self.calls = []
        self.maintenance_id = uuid.uuid4()

    def transaction(self):
        return FakeTransaction(self)

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "pg_try_advisory_lock" in query:
            return self.lock_acquired
        if "INSERT INTO evidence_maintenance_runs" in query:
            return self.maintenance_id
        if "pg_advisory_unlock" in query:
            return True
        return None

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if "SET data_size_bytes = octet_length" in query:
            return []
        if "FROM evidence_snapshots e" in query:
            return self.batches.pop(0) if self.batches else []
        if "DELETE FROM evidence_snapshots" in query:
            return [{"id": snapshot_id} for snapshot_id in args[0]]
        if "DELETE FROM evidence_rollups" in query:
            return []
        if "SELECT id FROM organizations" in query:
            return []
        return []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        if self.fail_rollup and "INSERT INTO evidence_rollups" in query:
            raise RuntimeError("synthetic rollup failure")
        return "OK"


def snapshot(
    *,
    run_id=None,
    host_id=None,
    evidence_type="pg_settings",
    age_days=40,
    size=10,
    quality=0.8,
):
    return {
        "id": uuid.uuid4(),
        "run_id": run_id,
        "host_id": host_id or uuid.uuid4(),
        "evidence_type": evidence_type,
        "collected_at": datetime.now(timezone.utc) - timedelta(days=age_days),
        "quality_score": quality,
        "data_size_bytes": size,
    }


def test_policy_requires_monotonic_retention_windows():
    with pytest.raises(ValueError, match="referenced_retention_days"):
        EvidenceRetentionPolicy(raw_retention_days=30, referenced_retention_days=29)
    with pytest.raises(ValueError, match="rollup_retention_days"):
        EvidenceRetentionPolicy(
            raw_retention_days=30,
            referenced_retention_days=90,
            rollup_retention_days=89,
        )


def test_eligibility_query_preserves_active_and_durable_references():
    assert "r.status = ANY" in ELIGIBLE_EVIDENCE_SQL
    for table in (
        "plans",
        "baseline_measurements",
        "advisory_findings",
        "tuning_candidates",
        "workload_fingerprints",
    ):
        assert table in ELIGIBLE_EVIDENCE_SQL
    assert "h.organization_id = $1" in ELIGIBLE_EVIDENCE_SQL
    assert "FOR UPDATE OF e SKIP LOCKED" in ELIGIBLE_EVIDENCE_SQL


@pytest.mark.asyncio
async def test_cleanup_writes_rollups_before_deleting_bounded_batch():
    organization_id = uuid.uuid4()
    host_id = uuid.uuid4()
    run_id = uuid.uuid4()
    rows = [
        snapshot(run_id=run_id, host_id=host_id, size=10, quality=0.5),
        snapshot(run_id=run_id, host_id=host_id, size=20, quality=0.9),
        snapshot(
            run_id=run_id,
            host_id=host_id,
            evidence_type="locks",
            size=30,
            quality=None,
        ),
    ]
    connection = LifecycleConnection(batches=[rows, []])
    policy = EvidenceRetentionPolicy(batch_size=3, max_batches_per_run=2)

    result = await EvidenceLifecycleManager(connection, policy).run_cleanup(
        organization_id,
        triggered_by="test",
    )

    assert result["status"] == "completed"
    assert result["snapshots_deleted"] == 3
    assert result["raw_bytes_reclaimed"] == 60
    assert result["rollup_rows_written"] == 2
    rollup_indexes = [
        index
        for index, call in enumerate(connection.calls)
        if call[0] == "execute" and "INSERT INTO evidence_rollups" in call[1]
    ]
    delete_index = next(
        index
        for index, call in enumerate(connection.calls)
        if call[0] == "fetch" and "DELETE FROM evidence_snapshots" in call[1]
    )
    assert rollup_indexes and max(rollup_indexes) < delete_index
    candidate_call = next(
        call for call in connection.calls if call[0] == "fetch" and call[1] == ELIGIBLE_EVIDENCE_SQL
    )
    assert candidate_call[2][0] == organization_id
    assert candidate_call[2][-1] == 3


@pytest.mark.asyncio
async def test_failed_rollup_rolls_back_without_raw_delete():
    row = snapshot(run_id=uuid.uuid4())
    connection = LifecycleConnection(batches=[[row]], fail_rollup=True)
    manager = EvidenceLifecycleManager(connection, EvidenceRetentionPolicy(batch_size=1))

    with pytest.raises(RuntimeError, match="synthetic rollup failure"):
        await manager.run_cleanup(uuid.uuid4(), triggered_by="test")

    assert any(call[0] == "transaction_rollback" for call in connection.calls)
    assert not any(
        call[0] == "fetch" and "DELETE FROM evidence_snapshots" in call[1]
        for call in connection.calls
    )
    assert any(
        call[0] == "execute"
        and "UPDATE evidence_maintenance_runs" in call[1]
        and "status = 'failed'" in call[1]
        for call in connection.calls
    )


@pytest.mark.asyncio
async def test_cleanup_skips_when_tenant_lock_is_held():
    connection = LifecycleConnection(lock_acquired=False)
    result = await EvidenceLifecycleManager(connection, EvidenceRetentionPolicy()).run_cleanup(
        uuid.uuid4(), triggered_by="test"
    )
    assert result["status"] == "skipped"
    assert result["snapshots_deleted"] == 0
    assert not any(
        call[0] == "fetchval" and "INSERT INTO evidence_maintenance_runs" in call[1]
        for call in connection.calls
    )


@pytest.mark.asyncio
async def test_scheduled_maintenance_visits_every_tenant(monkeypatch):
    organizations = [uuid.uuid4(), uuid.uuid4()]
    visited = []

    async def fake_list(self):
        return organizations

    async def fake_cleanup(self, organization_id, **kwargs):
        visited.append((organization_id, kwargs["triggered_by"]))
        return {"status": "completed", "snapshots_deleted": 0, "raw_bytes_reclaimed": 0}

    async def stop_after_cycle(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(EvidenceLifecycleManager, "list_organizations", fake_list)
    monkeypatch.setattr(EvidenceLifecycleManager, "run_cleanup", fake_cleanup)
    monkeypatch.setattr(asyncio, "sleep", stop_after_cycle)

    with pytest.raises(asyncio.CancelledError):
        await _evidence_maintenance(object())
    assert visited == [(organization_id, "worker:scheduler") for organization_id in organizations]
