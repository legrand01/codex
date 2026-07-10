"""
Tests for the Host Agent core service.

Covers:
- Each collector returns properly structured data
- Heartbeat format is correct
- Configuration validation works
- Role/version detection works
"""

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

# Adjust import path for the host agent module.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "host_agent"))

from agent import HostAgent
from config import AgentConfig, ConfigValidationError

# ============================================================================
# Mock DB Connection Helpers
# ============================================================================


class MockRecord(dict):
    """Mock asyncpg Record that supports both attribute and key access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class MockConnection:
    """Mock asyncpg connection that returns predefined query results."""

    def __init__(self, results: Optional[Dict[str, Any]] = None):
        self._results = results or {}
        self._default_settings = [
            MockRecord(
                name="work_mem",
                setting="4096",
                unit="kB",
                category="Resource Usage / Memory",
                short_desc="Sets the maximum memory for query workspaces.",
                context="user",
                vartype="integer",
                source="default",
            ),
            MockRecord(
                name="shared_buffers",
                setting="128MB",
                unit=None,
                category="Resource Usage / Memory",
                short_desc="Sets the number of shared memory buffers.",
                context="postmaster",
                vartype="string",
                source="configuration file",
            ),
        ]

        self._default_db_stats = [
            MockRecord(
                datname="postgres",
                numbackends=5,
                xact_commit=1000,
                xact_rollback=10,
                blks_read=500,
                blks_hit=9500,
                tup_returned=10000,
                tup_fetched=5000,
                tup_inserted=200,
                tup_updated=100,
                tup_deleted=50,
                conflicts=0,
                temp_files=2,
                temp_bytes=1024,
                deadlocks=0,
            ),
        ]
        self._default_version_role = MockRecord(
            pg_version="PostgreSQL 16.1 on x86_64-linux",
            is_replica=False,
        )

    async def fetch(self, query, *args):
        if "pg_settings" in query:
            return self._results.get("pg_settings", self._default_settings)
        elif "pg_stat_statements" in query:
            return self._results.get("pg_stat_statements", [])
        elif "pg_stat_database" in query:
            return self._results.get("pg_stat_database", self._default_db_stats)
        elif "pg_locks" in query:
            return self._results.get("pg_locks", [])
        elif "pg_stat_replication" in query:
            return self._results.get("pg_stat_replication", [])
        return []

    async def fetchrow(self, query, *args):
        if "version()" in query:
            return self._results.get("version_role", self._default_version_role)
        elif "pg_stat_bgwriter" in query and "EXTRACT" in query:
            return self._results.get(
                "checkpoint_age",
                MockRecord(uptime_seconds=3600.0, stats_age_seconds=1800.0),
            )
        elif "pg_stat_bgwriter" in query:
            return self._results.get(
                "bgwriter",
                MockRecord(
                    checkpoints_timed=100,
                    checkpoints_req=5,
                    checkpoint_write_time=500.0,
                    checkpoint_sync_time=50.0,
                    buffers_checkpoint=1000,
                    buffers_clean=200,
                    buffers_backend=300,
                    maxwritten_clean=10,
                    buffers_alloc=5000,
                ),
            )
        elif "pg_wal_lsn_diff" in query:
            return self._results.get(
                "wal",
                MockRecord(wal_bytes_total=1073741824, wal_segment_size=16777216),
            )
        elif "pg_is_in_recovery" in query:
            return self._results.get(
                "replication_lag",
                MockRecord(replay_lag_seconds=0.0, is_replica=False),
            )
        return None


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def valid_config():
    """Create a valid AgentConfig for testing."""
    return AgentConfig(
        host_id="test-host-001",
        control_plane_url="http://localhost:8000",
        pg_connection_string="postgresql://user:pass@localhost:5432/db",
        pg_settings_interval=60,
        pg_stats_interval=30,
        locks_replication_interval=15,
        os_metrics_interval=15,
        max_query_entries=100,
        buffer_max_bytes=512 * 1024 * 1024,
        heartbeat_interval=30,
    )


@pytest.fixture
def mock_conn():
    """Create a mock database connection."""
    return MockConnection()


@pytest.fixture
def agent(valid_config, mock_conn):
    """Create a HostAgent with valid config and mock connection."""
    return HostAgent(config=valid_config, conn=mock_conn)


# ============================================================================
# Configuration Validation Tests
# ============================================================================


class TestAgentConfig:
    """Tests for AgentConfig validation."""

    def test_valid_config_passes_validation(self, valid_config):
        """Valid configuration should pass validation without errors."""
        valid_config.validate()  # Should not raise

    def test_empty_host_id_raises_error(self, valid_config):
        """Empty host_id should raise ConfigValidationError."""
        valid_config.host_id = ""
        with pytest.raises(ConfigValidationError, match="host_id"):
            valid_config.validate()

    def test_pg_settings_interval_below_range(self, valid_config):
        """pg_settings_interval below 10 should raise error."""
        valid_config.pg_settings_interval = 9
        with pytest.raises(ConfigValidationError, match="pg_settings_interval"):
            valid_config.validate()

    def test_pg_settings_interval_above_range(self, valid_config):
        """pg_settings_interval above 3600 should raise error."""
        valid_config.pg_settings_interval = 3601
        with pytest.raises(ConfigValidationError, match="pg_settings_interval"):
            valid_config.validate()

    def test_pg_stats_interval_below_range(self, valid_config):
        """pg_stats_interval below 5 should raise error."""
        valid_config.pg_stats_interval = 4
        with pytest.raises(ConfigValidationError, match="pg_stats_interval"):
            valid_config.validate()

    def test_pg_stats_interval_above_range(self, valid_config):
        """pg_stats_interval above 600 should raise error."""
        valid_config.pg_stats_interval = 601
        with pytest.raises(ConfigValidationError, match="pg_stats_interval"):
            valid_config.validate()

    def test_locks_replication_interval_below_range(self, valid_config):
        """locks_replication_interval below 5 should raise error."""
        valid_config.locks_replication_interval = 4
        with pytest.raises(ConfigValidationError, match="locks_replication_interval"):
            valid_config.validate()

    def test_locks_replication_interval_above_range(self, valid_config):
        """locks_replication_interval above 300 should raise error."""
        valid_config.locks_replication_interval = 301
        with pytest.raises(ConfigValidationError, match="locks_replication_interval"):
            valid_config.validate()

    def test_os_metrics_interval_below_range(self, valid_config):
        """os_metrics_interval below 5 should raise error."""
        valid_config.os_metrics_interval = 4
        with pytest.raises(ConfigValidationError, match="os_metrics_interval"):
            valid_config.validate()

    def test_os_metrics_interval_above_range(self, valid_config):
        """os_metrics_interval above 300 should raise error."""
        valid_config.os_metrics_interval = 301
        with pytest.raises(ConfigValidationError, match="os_metrics_interval"):
            valid_config.validate()

    def test_boundary_values_accepted(self):
        """Boundary values at edges of ranges should be accepted."""
        config = AgentConfig(
            host_id="test",
            pg_connection_string="postgresql://user:pass@localhost:5432/db",
            pg_settings_interval=10,  # min
            pg_stats_interval=5,  # min
            locks_replication_interval=5,  # min
            os_metrics_interval=5,  # min
        )
        config.validate()  # Should not raise

        config2 = AgentConfig(
            host_id="test",
            pg_connection_string="postgresql://user:pass@localhost:5432/db",
            pg_settings_interval=3600,  # max
            pg_stats_interval=600,  # max
            locks_replication_interval=300,  # max
            os_metrics_interval=300,  # max
        )
        config2.validate()  # Should not raise

    def test_max_query_entries_zero_raises_error(self, valid_config):
        """max_query_entries less than 1 should raise error."""
        valid_config.max_query_entries = 0
        with pytest.raises(ConfigValidationError, match="max_query_entries"):
            valid_config.validate()

    def test_from_env(self, monkeypatch):
        """Configuration should be loadable from environment variables."""
        monkeypatch.setenv("AGENT_HOST_ID", "env-host-123")
        monkeypatch.setenv("AGENT_HOSTNAME", "env-hostname")
        monkeypatch.setenv("CONTROL_PLANE_URL", "http://cp:9000")
        monkeypatch.setenv("PG_CONNECTION_STRING", "postgresql://a@b/c")
        monkeypatch.setenv("PG_SETTINGS_INTERVAL", "120")
        monkeypatch.setenv("PG_STATS_INTERVAL", "60")
        monkeypatch.setenv("LOCKS_REPLICATION_INTERVAL", "30")
        monkeypatch.setenv("OS_METRICS_INTERVAL", "20")

        config = AgentConfig.from_env()
        assert config.host_id == "env-host-123"
        assert config.hostname == "env-hostname"
        assert config.control_plane_url == "http://cp:9000"
        assert config.pg_connection_string == "postgresql://a@b/c"
        assert config.pg_settings_interval == 120
        assert config.pg_stats_interval == 60
        assert config.locks_replication_interval == 30
        assert config.os_metrics_interval == 20

    def test_hostname_without_host_id_passes_validation(self, valid_config):
        """hostname can be used for control-plane auto-registration."""
        valid_config.host_id = ""
        valid_config.hostname = "w2j"
        valid_config.validate()


# ============================================================================
# Collector Tests
# ============================================================================


class TestPgSettingsCollector:
    """Tests for pg_settings evidence collector."""

    @pytest.mark.asyncio
    async def test_returns_structured_data(self, agent):
        """collect_pg_settings should return properly structured evidence."""
        result = await agent.collect_pg_settings()

        assert result is not None
        assert result["host_id"] == "test-host-001"
        assert result["evidence_type"] == "pg_settings"
        assert "collected_at" in result
        assert "data" in result
        assert "settings" in result["data"]
        assert "total_count" in result["data"]

    @pytest.mark.asyncio
    async def test_includes_utc_timestamp(self, agent):
        """Collected evidence must include a UTC timestamp."""
        result = await agent.collect_pg_settings()

        assert result is not None
        collected_at = datetime.fromisoformat(result["collected_at"])
        assert collected_at.tzinfo is not None
        # Should be very recent
        now = datetime.now(timezone.utc)
        diff = (now - collected_at).total_seconds()
        assert diff < 5  # collected within last 5 seconds

    @pytest.mark.asyncio
    async def test_includes_host_id(self, agent):
        """Collected evidence must include the host identifier."""
        result = await agent.collect_pg_settings()
        assert result is not None
        assert result["host_id"] == "test-host-001"

    @pytest.mark.asyncio
    async def test_handles_db_failure_gracefully(self, valid_config):
        """Should return None and not crash on DB error."""
        failing_conn = MagicMock()
        failing_conn.fetch = AsyncMock(side_effect=Exception("connection lost"))
        agent = HostAgent(config=valid_config, conn=failing_conn)

        result = await agent.collect_pg_settings()
        assert result is None


class TestPgStatsCollector:
    """Tests for pg_stats evidence collector."""

    @pytest.mark.asyncio
    async def test_returns_structured_data(self, agent):
        """collect_pg_stats should return properly structured evidence."""
        result = await agent.collect_pg_stats()

        assert result is not None
        assert result["host_id"] == "test-host-001"
        assert result["evidence_type"] == "pg_stats"
        assert "collected_at" in result
        assert "data" in result
        assert "database_stats" in result["data"]
        assert "statement_stats" in result["data"]
        assert "max_query_entries" in result["data"]

    @pytest.mark.asyncio
    async def test_includes_utc_timestamp(self, agent):
        """Collected evidence must include a UTC timestamp."""
        result = await agent.collect_pg_stats()
        assert result is not None
        collected_at = datetime.fromisoformat(result["collected_at"])
        assert collected_at.tzinfo is not None

    @pytest.mark.asyncio
    async def test_respects_max_query_entries(self, valid_config, mock_conn):
        """Should pass max_query_entries to the statement query."""
        valid_config.max_query_entries = 50
        agent = HostAgent(config=valid_config, conn=mock_conn)
        result = await agent.collect_pg_stats()
        assert result is not None
        assert result["data"]["max_query_entries"] == 50


class TestLocksCollector:
    """Tests for locks evidence collector."""

    @pytest.mark.asyncio
    async def test_returns_structured_data(self, agent):
        """collect_locks should return properly structured evidence."""
        result = await agent.collect_locks()

        assert result is not None
        assert result["host_id"] == "test-host-001"
        assert result["evidence_type"] == "locks"
        assert "collected_at" in result
        assert "data" in result
        assert "locks" in result["data"]
        assert "total_locks" in result["data"]
        assert "granted_count" in result["data"]
        assert "waiting_count" in result["data"]

    @pytest.mark.asyncio
    async def test_includes_utc_timestamp(self, agent):
        """Collected evidence must include a UTC timestamp."""
        result = await agent.collect_locks()
        assert result is not None
        collected_at = datetime.fromisoformat(result["collected_at"])
        assert collected_at.tzinfo is not None


class TestReplicationCollector:
    """Tests for replication evidence collector."""

    @pytest.mark.asyncio
    async def test_returns_structured_data(self, agent):
        """collect_replication should return properly structured evidence."""
        result = await agent.collect_replication()

        assert result is not None
        assert result["host_id"] == "test-host-001"
        assert result["evidence_type"] == "replication"
        assert "collected_at" in result
        assert "data" in result
        assert "is_replica" in result["data"]
        assert "replay_lag_seconds" in result["data"]
        assert "replication_connections" in result["data"]

    @pytest.mark.asyncio
    async def test_includes_utc_timestamp(self, agent):
        """Collected evidence must include a UTC timestamp."""
        result = await agent.collect_replication()
        assert result is not None
        collected_at = datetime.fromisoformat(result["collected_at"])
        assert collected_at.tzinfo is not None

    @pytest.mark.asyncio
    async def test_detects_primary_role(self, agent):
        """On a primary, is_replica should be False."""
        result = await agent.collect_replication()
        assert result is not None
        assert result["data"]["is_replica"] is False


class TestWalCheckpointCollector:
    """Tests for WAL/checkpoint evidence collector."""

    @pytest.mark.asyncio
    async def test_returns_structured_data(self, agent):
        """collect_wal_checkpoint should return properly structured evidence."""
        result = await agent.collect_wal_checkpoint()

        assert result is not None
        assert result["host_id"] == "test-host-001"
        assert result["evidence_type"] == "wal_checkpoint"
        assert "collected_at" in result
        assert "data" in result
        assert "checkpoint" in result["data"]
        assert "wal" in result["data"]

    @pytest.mark.asyncio
    async def test_includes_utc_timestamp(self, agent):
        """Collected evidence must include a UTC timestamp."""
        result = await agent.collect_wal_checkpoint()
        assert result is not None
        collected_at = datetime.fromisoformat(result["collected_at"])
        assert collected_at.tzinfo is not None


class TestOsMetricsCollector:
    """Tests for OS metrics evidence collector."""

    @pytest.mark.asyncio
    async def test_returns_structured_data(self, agent):
        """collect_os_metrics should return properly structured evidence."""
        result = await agent.collect_os_metrics()

        assert result is not None
        assert result["host_id"] == "test-host-001"
        assert result["evidence_type"] == "os_metrics"
        assert "collected_at" in result
        assert "data" in result
        assert "cpu_percent" in result["data"]
        assert "memory_percent" in result["data"]
        assert "memory_total_bytes" in result["data"]
        assert "disk_io" in result["data"]

    @pytest.mark.asyncio
    async def test_includes_utc_timestamp(self, agent):
        """Collected evidence must include a UTC timestamp."""
        result = await agent.collect_os_metrics()
        assert result is not None
        collected_at = datetime.fromisoformat(result["collected_at"])
        assert collected_at.tzinfo is not None

    @pytest.mark.asyncio
    async def test_cpu_percent_is_numeric(self, agent):
        """CPU percent should be a numeric value."""
        result = await agent.collect_os_metrics()
        assert result is not None
        assert isinstance(result["data"]["cpu_percent"], (int, float))
        assert 0 <= result["data"]["cpu_percent"] <= 100

    @pytest.mark.asyncio
    async def test_memory_percent_is_numeric(self, agent):
        """Memory percent should be a numeric value."""
        result = await agent.collect_os_metrics()
        assert result is not None
        assert isinstance(result["data"]["memory_percent"], (int, float))
        assert 0 <= result["data"]["memory_percent"] <= 100


# ============================================================================
# Heartbeat Tests
# ============================================================================


class TestHeartbeat:
    """Tests for heartbeat reporting."""

    @pytest.mark.asyncio
    async def test_heartbeat_format(self, agent):
        """Heartbeat should POST correct payload to control plane."""
        import httpx

        # Track the request made
        captured_request = {}

        async def mock_post(url, json=None, **kwargs):
            captured_request["url"] = url
            captured_request["json"] = json
            return httpx.Response(200)

        agent._http_client = MagicMock()
        agent._http_client.post = mock_post
        agent.pg_version = "PostgreSQL 16.1"
        agent.server_role = "primary"

        await agent.report_heartbeat()

        assert "url" in captured_request
        assert "/api/v1/fleet/test-host-001/heartbeat" in captured_request["url"]
        payload = captured_request["json"]
        assert payload["host_id"] == "test-host-001"
        assert "timestamp" in payload
        assert payload["pg_version"] == "PostgreSQL 16.1"
        assert payload["server_role"] == "primary"

        # Verify timestamp is valid ISO format
        ts = datetime.fromisoformat(payload["timestamp"])
        assert ts.tzinfo is not None

    @pytest.mark.asyncio
    async def test_heartbeat_no_client(self, agent):
        """Heartbeat should do nothing if HTTP client is None."""
        agent._http_client = None
        await agent.report_heartbeat()  # Should not raise


# ============================================================================
# Role/Version Detection Tests
# ============================================================================


class TestRoleVersionDetection:
    """Tests for role and version detection."""

    @pytest.mark.asyncio
    async def test_detects_primary_role(self, agent):
        """Should detect primary role when pg_is_in_recovery() returns False."""
        # Mock the HTTP client for role reporting
        agent._http_client = MagicMock()
        agent._http_client.post = AsyncMock(return_value=MagicMock(status_code=200))

        result = await agent.detect_role_version()

        assert result is not None
        assert result["server_role"] == "primary"
        assert "PostgreSQL" in result["pg_version"]
        assert agent.server_role == "primary"
        assert agent.pg_version is not None

    @pytest.mark.asyncio
    async def test_detects_replica_role(self, valid_config):
        """Should detect replica role when pg_is_in_recovery() returns True."""
        replica_conn = MockConnection(
            results={
                "version_role": MockRecord(
                    pg_version="PostgreSQL 16.1 on x86_64-linux",
                    is_replica=True,
                ),
            }
        )
        agent = HostAgent(config=valid_config, conn=replica_conn)
        agent._http_client = MagicMock()
        agent._http_client.post = AsyncMock(return_value=MagicMock(status_code=200))

        result = await agent.detect_role_version()

        assert result is not None
        assert result["server_role"] == "replica"
        assert agent.server_role == "replica"

    @pytest.mark.asyncio
    async def test_handles_no_connection(self, valid_config):
        """Should return None when no DB connection available."""
        agent = HostAgent(config=valid_config, conn=None)
        result = await agent.detect_role_version()
        assert result is None

    @pytest.mark.asyncio
    async def test_handles_query_failure(self, valid_config):
        """Should return None and not crash on query failure."""
        failing_conn = MagicMock()
        failing_conn.fetchrow = AsyncMock(side_effect=Exception("db error"))
        agent = HostAgent(config=valid_config, conn=failing_conn)

        result = await agent.detect_role_version()
        assert result is None


# ============================================================================
# Evidence Snapshot Structure Tests
# ============================================================================


class TestEvidenceSnapshotStructure:
    """Tests verifying all evidence snapshots have required fields."""

    @pytest.mark.asyncio
    async def test_all_collectors_include_host_id(self, agent):
        """Every collector must include host_id in the snapshot."""
        collectors = [
            agent.collect_pg_settings,
            agent.collect_pg_stats,
            agent.collect_locks,
            agent.collect_replication,
            agent.collect_wal_checkpoint,
            agent.collect_os_metrics,
        ]
        for collector in collectors:
            result = await collector()
            assert result is not None, f"{collector.__name__} returned None"
            assert "host_id" in result, f"{collector.__name__} missing host_id"
            assert result["host_id"] == "test-host-001"

    @pytest.mark.asyncio
    async def test_all_collectors_include_utc_timestamp(self, agent):
        """Every collector must include a UTC collected_at timestamp."""
        collectors = [
            agent.collect_pg_settings,
            agent.collect_pg_stats,
            agent.collect_locks,
            agent.collect_replication,
            agent.collect_wal_checkpoint,
            agent.collect_os_metrics,
        ]
        for collector in collectors:
            result = await collector()
            assert result is not None, f"{collector.__name__} returned None"
            assert "collected_at" in result, f"{collector.__name__} missing collected_at"
            ts = datetime.fromisoformat(result["collected_at"])
            assert ts.tzinfo is not None, f"{collector.__name__} timestamp not UTC"

    @pytest.mark.asyncio
    async def test_all_collectors_include_evidence_type(self, agent):
        """Every collector must include evidence_type."""
        expected_types = {
            "collect_pg_settings": "pg_settings",
            "collect_pg_stats": "pg_stats",
            "collect_locks": "locks",
            "collect_replication": "replication",
            "collect_wal_checkpoint": "wal_checkpoint",
            "collect_os_metrics": "os_metrics",
        }
        for method_name, expected_type in expected_types.items():
            collector = getattr(agent, method_name)
            result = await collector()
            assert result is not None
            assert result["evidence_type"] == expected_type

    @pytest.mark.asyncio
    async def test_all_collectors_include_data(self, agent):
        """Every collector must include a data field with dict content."""
        collectors = [
            agent.collect_pg_settings,
            agent.collect_pg_stats,
            agent.collect_locks,
            agent.collect_replication,
            agent.collect_wal_checkpoint,
            agent.collect_os_metrics,
        ]
        for collector in collectors:
            result = await collector()
            assert result is not None
            assert "data" in result
            assert isinstance(result["data"], dict)
