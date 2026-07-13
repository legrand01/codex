"""
Tests for the Fleet Management API endpoints.

Tests cover:
- GET /api/v1/fleet/ (list hosts, empty state)
- GET /api/v1/fleet/{host_id} (get single host, 404)
- POST /api/v1/fleet/ (register host, duplicate detection, validation)
- Connection status derivation from heartbeat timestamps

Requirements: 1.1, 1.2, 1.4, 1.5
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.models.enums import ConnectionStatus
from backend.services.fleet_service import classify_connection_status

# ---------------------------------------------------------------------------
# Unit tests for classify_connection_status
# ---------------------------------------------------------------------------


class TestClassifyConnectionStatus:
    """Tests for the heartbeat-to-status classification function."""

    def test_no_heartbeat_returns_disconnected(self):
        """No heartbeat means disconnected."""
        assert classify_connection_status(None) == ConnectionStatus.DISCONNECTED

    def test_heartbeat_within_60s_returns_connected(self):
        """Heartbeat within the last 60 seconds means connected."""
        recent = datetime.now(timezone.utc) - timedelta(seconds=30)
        assert classify_connection_status(recent) == ConnectionStatus.CONNECTED

    def test_heartbeat_at_exactly_0s_returns_connected(self):
        """Heartbeat just now means connected."""
        now = datetime.now(timezone.utc)
        assert classify_connection_status(now) == ConnectionStatus.CONNECTED

    def test_heartbeat_at_59s_returns_connected(self):
        """Heartbeat 59 seconds ago means connected."""
        ts = datetime.now(timezone.utc) - timedelta(seconds=59)
        assert classify_connection_status(ts) == ConnectionStatus.CONNECTED

    def test_heartbeat_at_60s_returns_degraded(self):
        """Heartbeat exactly 60 seconds ago means degraded."""
        ts = datetime.now(timezone.utc) - timedelta(seconds=60)
        assert classify_connection_status(ts) == ConnectionStatus.DEGRADED

    def test_heartbeat_at_180s_returns_degraded(self):
        """Heartbeat 180 seconds ago means degraded."""
        ts = datetime.now(timezone.utc) - timedelta(seconds=180)
        assert classify_connection_status(ts) == ConnectionStatus.DEGRADED

    def test_heartbeat_at_299s_returns_degraded(self):
        """Heartbeat 299 seconds ago means degraded (within 60-300s window)."""
        ts = datetime.now(timezone.utc) - timedelta(seconds=299)
        assert classify_connection_status(ts) == ConnectionStatus.DEGRADED

    def test_heartbeat_at_301s_returns_disconnected(self):
        """Heartbeat 301 seconds ago means disconnected."""
        ts = datetime.now(timezone.utc) - timedelta(seconds=301)
        assert classify_connection_status(ts) == ConnectionStatus.DISCONNECTED

    def test_heartbeat_very_old_returns_disconnected(self):
        """Heartbeat from hours ago means disconnected."""
        ts = datetime.now(timezone.utc) - timedelta(hours=2)
        assert classify_connection_status(ts) == ConnectionStatus.DISCONNECTED

    def test_naive_datetime_treated_as_utc(self):
        """A naive datetime is treated as UTC and classified correctly."""
        # 30 seconds ago, naive (no tzinfo)
        ts = datetime.utcnow() - timedelta(seconds=30)
        assert classify_connection_status(ts) == ConnectionStatus.CONNECTED

    def test_future_heartbeat_returns_connected(self):
        """A future heartbeat (clock skew) is treated as connected."""
        future = datetime.now(timezone.utc) + timedelta(seconds=5)
        assert classify_connection_status(future) == ConnectionStatus.CONNECTED


# ---------------------------------------------------------------------------
# API endpoint tests with mocked database
# ---------------------------------------------------------------------------


_UNSET = object()


def _make_host_record(
    host_id=None,
    hostname="db-primary-1",
    pg_version="16.1",
    server_role="primary",
    health_status="healthy",
    connection_status="connected",
    last_heartbeat=_UNSET,
):
    """Create a mock record dict that behaves like an asyncpg.Record."""
    if host_id is None:
        host_id = uuid.uuid4()
    if last_heartbeat is _UNSET:
        last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=10)

    class MockRecord(dict):
        def __getitem__(self, key):
            return dict.__getitem__(self, key)

    return MockRecord(
        id=host_id,
        hostname=hostname,
        pg_version=pg_version,
        server_role=server_role,
        health_status=health_status,
        connection_status=connection_status,
        last_heartbeat=last_heartbeat,
    )


class MockConnection:
    """Mock asyncpg connection for testing."""

    def __init__(self, records=None, insert_result=None):
        self.records = records or []
        self.insert_result = insert_result

    async def fetch(self, query, *args):
        return self.records

    async def fetchrow(self, query, *args):
        if "INSERT INTO" in query:
            return self.insert_result
        if "WHERE id = $1" in query and args:
            target_id = args[0]
            for r in self.records:
                if r["id"] == target_id:
                    return r
            return None
        if "hostname = $2" in query and len(args) >= 2:
            target = args[1]
            for r in self.records:
                if r["hostname"] == target:
                    return r
            return None
        if "WHERE hostname = $1" in query and args:
            target = args[0]
            for r in self.records:
                if r["hostname"] == target:
                    return r
            return None
        return None

    async def fetchval(self, query, *args):
        return uuid.uuid4()

    async def execute(self, query, *args):
        return "UPDATE 1"


def _override_db(mock_conn):
    """Create a dependency override generator for get_db."""
    from backend.dependencies import get_db

    async def override():
        yield mock_conn

    return get_db, override


@pytest.mark.asyncio
async def test_list_hosts_empty_state():
    """GET /api/v1/fleet/ returns empty list when no hosts are registered."""
    mock_conn = MockConnection(records=[])
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/fleet/")
            assert response.status_code == 200
            data = response.json()
            assert data["hosts"] == []
            assert data["total"] == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_hosts_returns_all_hosts():
    """GET /api/v1/fleet/ returns all registered hosts with derived status."""
    host1 = _make_host_record(
        hostname="db-primary-1",
        pg_version="16.1",
        server_role="primary",
        last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=10),
    )
    host2 = _make_host_record(
        hostname="db-replica-1",
        pg_version="16.1",
        server_role="replica",
        health_status="unhealthy",
        last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=120),
    )
    mock_conn = MockConnection(records=[host1, host2])
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/fleet/")
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 2
            assert len(data["hosts"]) == 2

            # First host should be connected (heartbeat 10s ago)
            h1 = data["hosts"][0]
            assert h1["hostname"] == "db-primary-1"
            assert h1["connection_status"] == "connected"
            assert h1["server_role"] == "primary"

            # Second host should be degraded (heartbeat 120s ago)
            h2 = data["hosts"][1]
            assert h2["hostname"] == "db-replica-1"
            assert h2["connection_status"] == "degraded"
            assert h2["health_status"] == "unhealthy"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_hosts_disconnected_host():
    """GET /api/v1/fleet/ correctly identifies disconnected hosts."""
    host = _make_host_record(
        hostname="db-offline-1",
        last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=600),
    )
    mock_conn = MockConnection(records=[host])
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/fleet/")
            assert response.status_code == 200
            data = response.json()
            assert data["hosts"][0]["connection_status"] == "disconnected"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_host_by_id():
    """GET /api/v1/fleet/{host_id} returns a specific host."""
    host_id = uuid.uuid4()
    host = _make_host_record(
        host_id=host_id,
        hostname="db-primary-1",
        pg_version="16.1",
        server_role="primary",
        last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    mock_conn = MockConnection(records=[host])
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/fleet/{host_id}")
            assert response.status_code == 200
            data = response.json()
            assert data["hostname"] == "db-primary-1"
            assert data["connection_status"] == "connected"
            assert data["pg_version"] == "16.1"
            assert data["server_role"] == "primary"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_host_not_found():
    """GET /api/v1/fleet/{host_id} returns 404 for unknown host."""
    mock_conn = MockConnection(records=[])
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/fleet/{uuid.uuid4()}")
            assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_register_host_success():
    """POST /api/v1/fleet/ registers a new host successfully."""
    host_id = uuid.uuid4()
    new_host = _make_host_record(
        host_id=host_id,
        hostname="new-host.example.com",
        pg_version="15.4",
        server_role="replica",
        health_status="unknown",
        last_heartbeat=None,
    )
    # records=[] means hostname check returns None (no duplicate)
    # insert_result is what the INSERT RETURNING query returns
    mock_conn = MockConnection(records=[], insert_result=new_host)
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/fleet/",
                json={
                    "hostname": "new-host.example.com",
                    "pg_version": "15.4",
                    "server_role": "replica",
                },
            )
            assert response.status_code == 201
            data = response.json()
            assert data["hostname"] == "new-host.example.com"
            assert data["pg_version"] == "15.4"
            assert data["server_role"] == "replica"
            assert data["health_status"] == "unknown"
            # No heartbeat → disconnected
            assert data["connection_status"] == "disconnected"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_register_host_duplicate_hostname():
    """POST /api/v1/fleet/ returns 409 for duplicate hostname."""
    existing = _make_host_record(hostname="existing-host.example.com")
    mock_conn = MockConnection(records=[existing])
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/fleet/",
                json={
                    "hostname": "existing-host.example.com",
                    "pg_version": "16.0",
                    "server_role": "primary",
                },
            )
            assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_register_host_invalid_server_role():
    """POST /api/v1/fleet/ rejects invalid server_role with 422."""
    mock_conn = MockConnection(records=[])
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/fleet/",
                json={
                    "hostname": "test-host.example.com",
                    "pg_version": "16.0",
                    "server_role": "invalid_role",
                },
            )
            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_register_host_minimal_fields():
    """POST /api/v1/fleet/ works with only hostname (pg_version and server_role optional)."""
    host_id = uuid.uuid4()
    new_host = _make_host_record(
        host_id=host_id,
        hostname="minimal-host",
        pg_version=None,
        server_role=None,
        health_status="unknown",
        last_heartbeat=None,
    )
    mock_conn = MockConnection(records=[], insert_result=new_host)
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/fleet/",
                json={"hostname": "minimal-host"},
            )
            assert response.status_code == 201
            data = response.json()
            assert data["hostname"] == "minimal-host"
            assert data["pg_version"] is None
            assert data["server_role"] is None
            assert data["connection_status"] == "disconnected"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_register_host_empty_hostname_rejected():
    """POST /api/v1/fleet/ rejects empty hostname with 422."""
    mock_conn = MockConnection(records=[])
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/fleet/",
                json={"hostname": ""},
            )
            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_receive_pg_stats_evidence_splits_snapshots():
    """POST /fleet/{host_id}/evidence persists pg_stats as two schema-valid snapshots."""
    host_id = uuid.uuid4()

    class EvidenceConnection(MockConnection):
        def __init__(self):
            super().__init__(records=[_make_host_record(host_id=host_id)])
            self.insert_args = []

        async def fetchrow(self, query, *args):
            if "SELECT id FROM hosts" in query:
                return {"id": host_id}
            return await super().fetchrow(query, *args)

        async def fetchval(self, query, *args):
            self.insert_args.append(args)
            return uuid.uuid4()

    mock_conn = EvidenceConnection()
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/fleet/{host_id}/evidence",
                json={
                    "evidence_type": "pg_stats",
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                    "data": {
                        "database_stats": [{"datname": "w2jmon_test"}],
                        "statement_stats": [
                            {
                                "query": (
                                    "SELECT count(*) FROM dba_demo.orders "
                                    "WHERE customer_id = 42 AND status = 'open'"
                                ),
                                "calls": 10,
                                "mean_exec_time": 38.8,
                            }
                        ],
                        "max_query_entries": 100,
                    },
                },
            )

            assert response.status_code == 201
            data = response.json()
            assert data["total"] == 2
            assert data["evidence_types"] == ["pg_stat_database", "pg_stat_statements"]
            inserted_types = [args[2] for args in mock_conn.insert_args]
            assert inserted_types == ["pg_stat_database", "pg_stat_statements"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_receive_capability_report_upserts_agent_snapshot():
    """POST /fleet/{host_id}/capabilities persists independent preflight inputs."""
    host_id = uuid.uuid4()
    organization_id = uuid.uuid4()
    observed_at = datetime.now(timezone.utc)
    insert_result = {
        "host_id": host_id,
        "organization_id": organization_id,
        "connectivity": True,
        "system_information": True,
        "system_metrics": True,
        "pg_stat_statements": True,
        "query_text_collection": False,
        "configuration_read": True,
        "configuration_write": True,
        "reload_permission": True,
        "restart_capability": False,
        "provider_api": False,
        "managed_file_access": False,
        "details": '{"source":"agent-probe"}',
        "observed_at": observed_at,
    }
    mock_conn = MockConnection(insert_result=insert_result)
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/fleet/{host_id}/capabilities",
                json={
                    "database_name": "appdb",
                    "connectivity": True,
                    "system_information": True,
                    "system_metrics": True,
                    "pg_stat_statements": True,
                    "configuration_read": True,
                    "configuration_write": True,
                    "reload_permission": True,
                    "details": {"source": "agent-probe"},
                    "observed_at": observed_at.isoformat(),
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["host_id"] == str(host_id)
        assert data["database_name"] == "appdb"
        assert data["configuration_write"] is True
        assert data["restart_capability"] is False
        assert data["details"] == {"source": "agent-probe"}
    finally:
        app.dependency_overrides.clear()
