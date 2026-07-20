"""
Tests for the Evidence Viewer API endpoints.

Tests cover:
- GET /api/v1/evidence/{run_id} (list evidence by run, empty state)
- GET /api/v1/evidence/snapshot/{snapshot_id} (get snapshot, 404)
- format_freshness_age utility (edge cases: 0s, 59s, 60s, 3599s, 3600s, 7200s)
- Evidence categorization by type

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from backend.api.evidence import (
    categorize_evidence_type,
    format_freshness_age,
)
from backend.main import app
from backend.security import DEFAULT_ORGANIZATION_ID, Principal

# ---------------------------------------------------------------------------
# Unit tests for format_freshness_age
# ---------------------------------------------------------------------------


class TestFormatFreshnessAge:
    """Tests for the evidence freshness age formatting utility."""

    def test_zero_seconds(self):
        """0 seconds ago should display '0s ago'."""
        now = datetime.now(timezone.utc)
        result = format_freshness_age(now)
        assert result == "0s ago"

    def test_59_seconds(self):
        """59 seconds ago should display '59s ago'."""
        ts = datetime.now(timezone.utc) - timedelta(seconds=59)
        result = format_freshness_age(ts)
        assert result == "59s ago"

    def test_60_seconds_boundary(self):
        """60 seconds ago should display '1m ago' (switches to minutes)."""
        ts = datetime.now(timezone.utc) - timedelta(seconds=60)
        result = format_freshness_age(ts)
        assert result == "1m ago"

    def test_3599_seconds(self):
        """3599 seconds ago should display '59m ago' (floor division)."""
        ts = datetime.now(timezone.utc) - timedelta(seconds=3599)
        result = format_freshness_age(ts)
        assert result == "59m ago"

    def test_3600_seconds_boundary(self):
        """3600 seconds ago should display '1h ago' (switches to hours)."""
        ts = datetime.now(timezone.utc) - timedelta(seconds=3600)
        result = format_freshness_age(ts)
        assert result == "1h ago"

    def test_7200_seconds(self):
        """7200 seconds (2 hours) should display '2h ago'."""
        ts = datetime.now(timezone.utc) - timedelta(seconds=7200)
        result = format_freshness_age(ts)
        assert result == "2h ago"

    def test_naive_datetime_treated_as_utc(self):
        """A naive datetime should be treated as UTC."""
        ts = datetime.utcnow() - timedelta(seconds=30)
        result = format_freshness_age(ts)
        assert result == "30s ago"

    def test_future_timestamp_returns_0s(self):
        """A future timestamp (clock skew) should return '0s ago'."""
        future = datetime.now(timezone.utc) + timedelta(seconds=60)
        result = format_freshness_age(future)
        assert result == "0s ago"

    def test_floor_division_for_minutes(self):
        """119 seconds = 1m (floor(119/60) = 1)."""
        ts = datetime.now(timezone.utc) - timedelta(seconds=119)
        result = format_freshness_age(ts)
        assert result == "1m ago"

    def test_floor_division_for_hours(self):
        """7199 seconds = 1h (floor(7199/3600) = 1)."""
        ts = datetime.now(timezone.utc) - timedelta(seconds=7199)
        result = format_freshness_age(ts)
        assert result == "1h ago"


# ---------------------------------------------------------------------------
# Unit tests for categorize_evidence_type
# ---------------------------------------------------------------------------


class TestCategorizeEvidenceType:
    """Tests for the evidence type categorization."""

    def test_pg_settings_is_configuration(self):
        assert categorize_evidence_type("pg_settings") == "configuration"

    def test_pg_stat_database_is_performance(self):
        assert categorize_evidence_type("pg_stat_database") == "performance"

    def test_pg_stat_statements_is_performance(self):
        assert categorize_evidence_type("pg_stat_statements") == "performance"

    def test_locks_is_locks(self):
        assert categorize_evidence_type("locks") == "locks"

    def test_replication_is_replication(self):
        assert categorize_evidence_type("replication") == "replication"

    def test_wal_checkpoint_is_wal_checkpoint(self):
        assert categorize_evidence_type("wal_checkpoint") == "wal_checkpoint"

    def test_os_metrics_is_os_metrics(self):
        assert categorize_evidence_type("os_metrics") == "os_metrics"

    def test_unknown_type_returns_itself(self):
        assert categorize_evidence_type("unknown_type") == "unknown_type"


# ---------------------------------------------------------------------------
# API endpoint tests with mocked database
# ---------------------------------------------------------------------------


class MockRecord(dict):
    """Mock asyncpg.Record for testing."""

    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def _make_evidence_record(
    snapshot_id=None,
    run_id=None,
    host_id=None,
    evidence_type="pg_settings",
    collected_at=None,
    data=None,
    quality_score=None,
    data_size_bytes=0,
):
    """Create a mock evidence_snapshots record."""
    if snapshot_id is None:
        snapshot_id = uuid.uuid4()
    if run_id is None:
        run_id = uuid.uuid4()
    if host_id is None:
        host_id = uuid.uuid4()
    if collected_at is None:
        collected_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    if data is None:
        data = {"sample_key": "sample_value"}

    return MockRecord(
        id=snapshot_id,
        run_id=run_id,
        host_id=host_id,
        evidence_type=evidence_type,
        collected_at=collected_at,
        data=data,
        quality_score=quality_score,
        data_size_bytes=data_size_bytes,
    )


class MockConnection:
    """Mock asyncpg connection for testing."""

    def __init__(self, records=None, single_record=None, rollup_records=None):
        self.records = records or []
        self.single_record = single_record
        self.rollup_records = rollup_records or []
        self.last_fetch_args = None

    async def fetch(self, query, *args):
        self.last_fetch_args = args
        if "FROM evidence_rollups" in query:
            return self.rollup_records
        if "COUNT(*)::integer AS count" in query:
            counts = {}
            for record in self.records:
                evidence_type = record["evidence_type"]
                counts[evidence_type] = counts.get(evidence_type, 0) + 1
            return [
                MockRecord(evidence_type=evidence_type, count=count)
                for evidence_type, count in counts.items()
            ]
        return self.records

    async def fetchrow(self, query, *args):
        if self.single_record is not None:
            return self.single_record
        # Try to find by ID in records
        if args:
            target_id = args[0]
            for r in self.records:
                if r["id"] == target_id:
                    return r
        return None


def _override_db(mock_conn):
    """Create a dependency override generator for get_db."""
    from backend.dependencies import get_db

    async def override():
        yield mock_conn

    return get_db, override


# ---------------------------------------------------------------------------
# GET /api/v1/evidence/{run_id} tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_evidence_empty_state():
    """GET /api/v1/evidence/{run_id} returns empty state when no evidence exists."""
    run_id = uuid.uuid4()
    mock_conn = MockConnection(records=[])
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/evidence/{run_id}")
            assert response.status_code == 200
            assert mock_conn.last_fetch_args[1] == DEFAULT_ORGANIZATION_ID
            data = response.json()
            assert data["run_id"] == str(run_id)
            assert data["snapshots"] == []
            assert data["categories"] == []
            assert data["total"] == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_evidence_with_snapshots():
    """GET /api/v1/evidence/{run_id} returns evidence grouped by category."""
    run_id = uuid.uuid4()
    host_id = uuid.uuid4()

    records = [
        _make_evidence_record(
            run_id=run_id,
            host_id=host_id,
            evidence_type="pg_settings",
            collected_at=datetime.now(timezone.utc) - timedelta(seconds=30),
            quality_score=0.95,
        ),
        _make_evidence_record(
            run_id=run_id,
            host_id=host_id,
            evidence_type="pg_stat_database",
            collected_at=datetime.now(timezone.utc) - timedelta(seconds=20),
            quality_score=0.88,
        ),
        _make_evidence_record(
            run_id=run_id,
            host_id=host_id,
            evidence_type="pg_stat_statements",
            collected_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        ),
        _make_evidence_record(
            run_id=run_id,
            host_id=host_id,
            evidence_type="locks",
            collected_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        ),
    ]

    mock_conn = MockConnection(records=records)
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/evidence/{run_id}")
            assert response.status_code == 200
            data = response.json()
            assert data["run_id"] == str(run_id)
            assert data["total"] == 4
            assert len(data["snapshots"]) == 4
            assert data["limit"] == 100
            assert data["offset"] == 0
            assert all("data" not in snapshot for snapshot in data["snapshots"])

            # Check categories: configuration=1, performance=2, locks=1
            categories = {c["category"]: c["count"] for c in data["categories"]}
            assert categories["configuration"] == 1
            assert categories["performance"] == 2
            assert categories["locks"] == 1

            # Check freshness_age is present
            for snapshot in data["snapshots"]:
                assert "freshness_age" in snapshot
                assert "ago" in snapshot["freshness_age"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_evidence_categories_sum_to_total():
    """Category counts should sum to total number of snapshots."""
    run_id = uuid.uuid4()
    host_id = uuid.uuid4()

    records = [
        _make_evidence_record(run_id=run_id, host_id=host_id, evidence_type="pg_settings"),
        _make_evidence_record(run_id=run_id, host_id=host_id, evidence_type="os_metrics"),
        _make_evidence_record(run_id=run_id, host_id=host_id, evidence_type="os_metrics"),
        _make_evidence_record(run_id=run_id, host_id=host_id, evidence_type="replication"),
    ]

    mock_conn = MockConnection(records=records)
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/evidence/{run_id}")
            assert response.status_code == 200
            data = response.json()
            total_from_categories = sum(c["count"] for c in data["categories"])
            assert total_from_categories == data["total"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_evidence_includes_archived_rollups_when_raw_is_empty():
    """Expired payloads remain visible as compact per-session history."""
    run_id = uuid.uuid4()
    rollups = [
        MockRecord(
            evidence_type="pg_stat_statements",
            count=120,
            total_bytes=4_096,
            first_collected_at=datetime.now(timezone.utc) - timedelta(days=40),
            last_collected_at=datetime.now(timezone.utc) - timedelta(days=31),
        ),
        MockRecord(
            evidence_type="pg_stat_database",
            count=30,
            total_bytes=1_024,
            first_collected_at=datetime.now(timezone.utc) - timedelta(days=40),
            last_collected_at=datetime.now(timezone.utc) - timedelta(days=31),
        ),
    ]
    mock_conn = MockConnection(records=[], rollup_records=rollups)
    dep, override = _override_db(mock_conn)
    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/evidence/{run_id}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["snapshots"] == []
        assert payload["total"] == 0
        assert payload["archived_total"] == 150
        assert payload["archived_bytes"] == 5_120
        archived = payload["archived_categories"][0]
        assert archived["category"] == "performance"
        assert archived["count"] == 150
        assert archived["total_bytes"] == 5_120
        assert datetime.fromisoformat(archived["first_collected_at"].replace("Z", "+00:00")) == min(
            row["first_collected_at"] for row in rollups
        )
        assert datetime.fromisoformat(archived["last_collected_at"].replace("Z", "+00:00")) == max(
            row["last_collected_at"] for row in rollups
        )
    finally:
        app.dependency_overrides.clear()


def _lifecycle_payload():
    now = datetime.now(timezone.utc)
    return {
        "policy": {
            "raw_retention_days": 30,
            "referenced_retention_days": 90,
            "rollup_retention_days": 365,
            "batch_size": 1000,
            "max_batches_per_run": 20,
            "cleanup_enabled": True,
            "cleanup_interval_seconds": 3600,
        },
        "cutoffs": {"raw": now, "referenced": now, "rollup": now},
        "raw": {
            "snapshot_count": 10,
            "total_bytes": 1000,
            "unmeasured_snapshot_count": 0,
            "oldest_collected_at": now,
            "newest_collected_at": now,
        },
        "eligible": {"snapshot_count": 2, "total_bytes": 200},
        "rollups": {
            "snapshot_count": 20,
            "total_bytes": 2000,
            "rollup_rows": 1,
            "oldest_collected_at": now,
            "newest_collected_at": now,
        },
        "last_run": None,
    }


@pytest.mark.asyncio
async def test_lifecycle_status_and_preview_are_tenant_scoped(monkeypatch):
    seen = []

    class FakeLifecycleManager:
        def __init__(self, database):
            self.database = database

        async def status(self, organization_id):
            seen.append(organization_id)
            return _lifecycle_payload()

    monkeypatch.setattr(
        "backend.services.evidence_lifecycle.EvidenceLifecycleManager",
        FakeLifecycleManager,
    )
    mock_conn = MockConnection()
    dep, override = _override_db(mock_conn)
    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            status_response = await client.get("/api/v1/evidence/lifecycle/status")
            preview_response = await client.post(
                "/api/v1/evidence/lifecycle/cleanup", json={"dry_run": True}
            )
        assert status_response.status_code == 200
        assert status_response.json()["eligible"]["snapshot_count"] == 2
        assert preview_response.status_code == 200
        assert preview_response.json()["status"] == "preview"
        assert seen == [DEFAULT_ORGANIZATION_ID, DEFAULT_ORGANIZATION_ID]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_lifecycle_cleanup_executes_with_admin_identity(monkeypatch):
    seen = []

    class FakeLifecycleManager:
        def __init__(self, database):
            self.database = database

        async def run_cleanup(self, organization_id, **kwargs):
            seen.append((organization_id, kwargs))
            return {"status": "completed", "snapshots_deleted": 3}

    monkeypatch.setattr(
        "backend.services.evidence_lifecycle.EvidenceLifecycleManager",
        FakeLifecycleManager,
    )
    mock_conn = MockConnection()
    dep, override = _override_db(mock_conn)
    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/evidence/lifecycle/cleanup",
                json={"dry_run": False, "max_batches": 2},
            )
        assert response.status_code == 200
        assert response.json()["result"]["snapshots_deleted"] == 3
        assert seen == [
            (
                DEFAULT_ORGANIZATION_ID,
                {"triggered_by": "api:development-admin", "max_batches": 2},
            )
        ]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_lifecycle_cleanup_rejects_non_admin(monkeypatch):
    """Viewing status is broad, but raw deletion remains an admin operation."""
    from backend.config import settings as app_settings

    async def viewer_request(_request):
        return Principal(
            id=uuid.uuid4(),
            organization_id=DEFAULT_ORGANIZATION_ID,
            subject="viewer",
            display_name="Viewer",
            role="viewer",
        )

    monkeypatch.setattr(app_settings, "auth_required", True)
    monkeypatch.setattr("backend.security.authenticate_request", viewer_request)
    mock_conn = MockConnection()
    dep, override = _override_db(mock_conn)
    app.dependency_overrides[dep] = override
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/evidence/lifecycle/cleanup", json={"dry_run": False}
            )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /api/v1/evidence/snapshot/{snapshot_id} tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_snapshot_success():
    """GET /api/v1/evidence/snapshot/{snapshot_id} returns full snapshot data."""
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    host_id = uuid.uuid4()
    collected_at = datetime.now(timezone.utc) - timedelta(seconds=45)

    record = _make_evidence_record(
        snapshot_id=snapshot_id,
        run_id=run_id,
        host_id=host_id,
        evidence_type="locks",
        collected_at=collected_at,
        data={"blocked_queries": 3, "lock_types": ["RowExclusiveLock"]},
        quality_score=0.75,
    )

    mock_conn = MockConnection(single_record=record)
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/evidence/snapshot/{snapshot_id}")
            assert response.status_code == 200
            data = response.json()
            assert data["id"] == str(snapshot_id)
            assert data["run_id"] == str(run_id)
            assert data["host_id"] == str(host_id)
            assert data["evidence_type"] == "locks"
            assert data["data"] == {"blocked_queries": 3, "lock_types": ["RowExclusiveLock"]}
            assert data["quality_score"] == 0.75
            assert data["data_truncated"] is False
            assert "freshness_age" in data
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_snapshot_returns_bounded_preview_for_large_payload():
    """A single evidence lookup must not serialize an unbounded raw payload."""
    snapshot_id = uuid.uuid4()
    record = _make_evidence_record(
        snapshot_id=snapshot_id,
        data={"locks": [{"query": "x" * 2_000}] * 1_000},
        data_size_bytes=2_000_000,
    )
    mock_conn = MockConnection(single_record=record)
    dep, override = _override_db(mock_conn)
    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/evidence/snapshot/{snapshot_id}")
            assert response.status_code == 200
            data = response.json()
            assert data["data_truncated"] is True
            assert data["data_size_bytes"] == 2_000_000
            assert data["data"]["_payload_truncated"] is True
            assert len(response.content) < 100_000
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_snapshot_not_found():
    """GET /api/v1/evidence/snapshot/{snapshot_id} returns 404 for unknown snapshot."""
    snapshot_id = uuid.uuid4()
    mock_conn = MockConnection(single_record=None)
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/evidence/snapshot/{snapshot_id}")
            assert response.status_code == 404
            data = response.json()
            assert "not found" in data["detail"].lower() or "unavailable" in data["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_snapshot_invalid_uuid():
    """GET /api/v1/evidence/snapshot/{snapshot_id} returns 422 for invalid UUID."""
    mock_conn = MockConnection(records=[])
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/evidence/snapshot/not-a-uuid")
            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()
