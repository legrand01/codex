"""
Unit tests for backend/services/audit_logger.py.

Tests the AuditLogger class:
- Entry validation (actor_type, actor_name, action_type, result)
- Log method behavior
- Query method behavior with filters

Since the audit logger requires a database, these tests use mocking
for the database connection pool to test the service logic in isolation.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

from backend.services.audit_logger import (
    VALID_ACTOR_TYPES,
    VALID_RESULTS,
    AuditLogger,
    AuditLoggerError,
    AuditValidationError,
)

# =============================================================================
# Fixtures
# =============================================================================


class MockConnection:
    """Mock asyncpg connection that tracks queries."""

    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None, fetchrow_result=None):
        self._rows = rows or []
        self._fetchrow_result = fetchrow_result
        self.queries = []

    async def fetch(self, query: str, *args):
        self.queries.append(("fetch", query, args))
        return self._rows

    async def fetchrow(self, query: str, *args):
        self.queries.append(("fetchrow", query, args))
        return self._fetchrow_result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class MockPool:
    """Mock asyncpg pool."""

    def __init__(self, connection: MockConnection):
        self._connection = connection

    def acquire(self):
        return self._connection


# =============================================================================
# Tests for AuditLogger validation
# =============================================================================


class TestAuditLoggerValidation:
    """Tests for audit entry validation logic."""

    def setup_method(self):
        """Create an AuditLogger instance with a mock pool."""
        self.mock_conn = MockConnection()
        self.mock_pool = MockPool(self.mock_conn)
        self.logger = AuditLogger(pool=self.mock_pool)

    def test_valid_actor_types(self):
        """Valid actor types should not raise."""
        for actor_type in VALID_ACTOR_TYPES:
            # Should not raise
            self.logger._validate_entry(actor_type, "test_actor", "test_action", "success")

    def test_invalid_actor_type_raises(self):
        """Invalid actor_type should raise AuditValidationError."""
        with pytest.raises(AuditValidationError, match="actor_type must be one of"):
            self.logger._validate_entry("robot", "test_actor", "test_action", "success")

    def test_empty_actor_type_raises(self):
        """Empty actor_type should raise AuditValidationError."""
        with pytest.raises(AuditValidationError, match="actor_type must be one of"):
            self.logger._validate_entry("", "test_actor", "test_action", "success")

    def test_valid_results(self):
        """Valid result values should not raise."""
        for result in VALID_RESULTS:
            self.logger._validate_entry("human", "test_actor", "test_action", result)

    def test_invalid_result_raises(self):
        """Invalid result should raise AuditValidationError."""
        with pytest.raises(AuditValidationError, match="result must be one of"):
            self.logger._validate_entry("human", "test_actor", "test_action", "unknown")

    def test_empty_actor_name_raises(self):
        """Empty actor_name should raise AuditValidationError."""
        with pytest.raises(AuditValidationError, match="actor_name must be non-empty"):
            self.logger._validate_entry("human", "", "test_action", "success")

    def test_whitespace_actor_name_raises(self):
        """Whitespace-only actor_name should raise AuditValidationError."""
        with pytest.raises(AuditValidationError, match="actor_name must be non-empty"):
            self.logger._validate_entry("human", "   ", "test_action", "success")

    def test_empty_action_type_raises(self):
        """Empty action_type should raise AuditValidationError."""
        with pytest.raises(AuditValidationError, match="action_type must be non-empty"):
            self.logger._validate_entry("human", "test_actor", "", "success")

    def test_whitespace_action_type_raises(self):
        """Whitespace-only action_type should raise AuditValidationError."""
        with pytest.raises(AuditValidationError, match="action_type must be non-empty"):
            self.logger._validate_entry("human", "test_actor", "   ", "success")


# =============================================================================
# Tests for AuditLogger.log()
# =============================================================================


class TestAuditLoggerLog:
    """Tests for the log() method."""

    def setup_method(self):
        """Create an AuditLogger with a mock pool that returns a valid row."""
        self.run_id = uuid4()
        self.host_id = uuid4()
        self.now = datetime.now(timezone.utc)

        self.mock_row = {
            "id": 1,
            "run_id": self.run_id,
            "timestamp": self.now,
            "actor_type": "system",
            "actor_name": "guardrail_engine",
            "action_type": "risk_assessment",
            "target_host_id": self.host_id,
            "result": "success",
            "result_reason": None,
            "details": json.dumps({"risk_score": 45}),
        }

        self.mock_conn = MockConnection(fetchrow_result=self.mock_row)
        self.mock_pool = MockPool(self.mock_conn)
        self.logger = AuditLogger(pool=self.mock_pool)

    @pytest.mark.asyncio
    async def test_log_returns_audit_entry(self):
        """log() should return a properly constructed AuditEntry."""
        entry = await self.logger.log(
            run_id=self.run_id,
            actor_type="system",
            actor_name="guardrail_engine",
            action_type="risk_assessment",
            target_host_id=self.host_id,
            result="success",
            details={"risk_score": 45},
        )

        assert entry.id == 1
        assert entry.run_id == self.run_id
        assert entry.actor_type == "system"
        assert entry.actor_name == "guardrail_engine"
        assert entry.action_type == "risk_assessment"
        assert entry.target_host_id == self.host_id
        assert entry.result == "success"
        assert entry.details == {"risk_score": 45}

    @pytest.mark.asyncio
    async def test_log_with_minimal_fields(self):
        """log() should work with only required fields."""
        self.mock_row["run_id"] = None
        self.mock_row["target_host_id"] = None
        self.mock_row["details"] = None
        self.mock_row["actor_type"] = "human"
        self.mock_row["actor_name"] = "dba_admin"
        self.mock_row["action_type"] = "plan_approved"

        entry = await self.logger.log(
            actor_type="human",
            actor_name="dba_admin",
            action_type="plan_approved",
            result="success",
        )

        assert entry.id == 1
        assert entry.actor_type == "human"
        assert entry.actor_name == "dba_admin"

    @pytest.mark.asyncio
    async def test_log_validates_before_insert(self):
        """log() should validate fields before attempting database insert."""
        with pytest.raises(AuditValidationError, match="actor_type must be one of"):
            await self.logger.log(
                actor_type="invalid",
                actor_name="test",
                action_type="test",
                result="success",
            )

        # Should not have attempted any database queries
        assert len(self.mock_conn.queries) == 0

    @pytest.mark.asyncio
    async def test_log_with_result_reason(self):
        """log() should persist result_reason for failure/blocked results."""
        self.mock_row["result"] = "blocked"
        self.mock_row["result_reason"] = "Risk score exceeded threshold"

        entry = await self.logger.log(
            actor_type="system",
            actor_name="guardrail_engine",
            action_type="plan_execution",
            result="blocked",
            result_reason="Risk score exceeded threshold",
        )

        assert entry.result == "blocked"
        assert entry.result_reason == "Risk score exceeded threshold"

    @pytest.mark.asyncio
    async def test_log_with_failure_result(self):
        """log() should accept 'failure' as a valid result."""
        self.mock_row["result"] = "failure"
        self.mock_row["result_reason"] = "Dry-run failed"

        entry = await self.logger.log(
            actor_type="system",
            actor_name="guardrail_engine",
            action_type="dry_run",
            result="failure",
            result_reason="Dry-run failed",
        )

        assert entry.result == "failure"

    @pytest.mark.asyncio
    async def test_log_human_actor(self):
        """log() should accept 'human' actor_type for DBA actions."""
        self.mock_row["actor_type"] = "human"
        self.mock_row["actor_name"] = "john_dba"
        self.mock_row["action_type"] = "plan_approved"

        entry = await self.logger.log(
            actor_type="human",
            actor_name="john_dba",
            action_type="plan_approved",
            result="success",
        )

        assert entry.actor_type == "human"
        assert entry.actor_name == "john_dba"

    @pytest.mark.asyncio
    async def test_log_database_error_raises_audit_logger_error(self):
        """log() should wrap database errors in AuditLoggerError."""

        # Create a pool that raises on acquire
        class FailingPool:
            def acquire(self):
                raise Exception("Connection refused")

        self.logger._pool = FailingPool()

        with pytest.raises(AuditLoggerError, match="Failed to persist audit entry"):
            await self.logger.log(
                actor_type="system",
                actor_name="test",
                action_type="test",
                result="success",
            )


# =============================================================================
# Tests for AuditLogger.query()
# =============================================================================


class TestAuditLoggerQuery:
    """Tests for the query() method."""

    def setup_method(self):
        """Create an AuditLogger with mock data."""
        self.run_id = uuid4()
        self.now = datetime.now(timezone.utc)

        self.mock_rows = [
            {
                "id": 1,
                "run_id": self.run_id,
                "timestamp": self.now - timedelta(minutes=5),
                "actor_type": "system",
                "actor_name": "loop_worker",
                "action_type": "run_started",
                "target_host_id": None,
                "result": "success",
                "result_reason": None,
                "details": None,
            },
            {
                "id": 2,
                "run_id": self.run_id,
                "timestamp": self.now - timedelta(minutes=3),
                "actor_type": "system",
                "actor_name": "guardrail_engine",
                "action_type": "risk_assessment",
                "target_host_id": uuid4(),
                "result": "success",
                "result_reason": None,
                "details": json.dumps({"risk_score": 45}),
            },
            {
                "id": 3,
                "run_id": self.run_id,
                "timestamp": self.now - timedelta(minutes=1),
                "actor_type": "human",
                "actor_name": "john_dba",
                "action_type": "plan_approved",
                "target_host_id": uuid4(),
                "result": "success",
                "result_reason": None,
                "details": None,
            },
        ]

        self.mock_conn = MockConnection(rows=self.mock_rows)
        self.mock_pool = MockPool(self.mock_conn)
        self.logger = AuditLogger(pool=self.mock_pool)

    @pytest.mark.asyncio
    async def test_query_returns_entries_in_chronological_order(self):
        """query() should return entries ordered by timestamp ascending."""
        entries = await self.logger.query()

        assert len(entries) == 3
        assert entries[0].id == 1
        assert entries[1].id == 2
        assert entries[2].id == 3
        # Verify chronological order
        for i in range(len(entries) - 1):
            assert entries[i].timestamp <= entries[i + 1].timestamp

    @pytest.mark.asyncio
    async def test_query_with_run_id_filter(self):
        """query() should filter by run_id when provided."""
        await self.logger.query(run_id=self.run_id)

        # Verify the query included a run_id filter
        assert len(self.mock_conn.queries) == 1
        query_str = self.mock_conn.queries[0][1]
        assert "run_id = $1" in query_str

    @pytest.mark.asyncio
    async def test_query_with_time_range_filter(self):
        """query() should filter by time range when provided."""
        start = self.now - timedelta(hours=1)
        end = self.now

        await self.logger.query(time_range=(start, end))

        # Verify the query included time range filters
        assert len(self.mock_conn.queries) == 1
        query_str = self.mock_conn.queries[0][1]
        assert "timestamp >= $1" in query_str
        assert "timestamp <= $2" in query_str

    @pytest.mark.asyncio
    async def test_query_with_run_id_and_time_range(self):
        """query() should combine run_id and time_range filters."""
        start = self.now - timedelta(hours=1)
        end = self.now

        await self.logger.query(run_id=self.run_id, time_range=(start, end))

        # Verify query has all filters
        query_str = self.mock_conn.queries[0][1]
        assert "run_id = $1" in query_str
        assert "timestamp >= $2" in query_str
        assert "timestamp <= $3" in query_str

    @pytest.mark.asyncio
    async def test_query_with_pagination(self):
        """query() should respect limit and offset parameters."""
        await self.logger.query(limit=10, offset=5)

        # Verify pagination params were passed
        query_args = self.mock_conn.queries[0][2]
        assert 10 in query_args  # limit
        assert 5 in query_args  # offset

    @pytest.mark.asyncio
    async def test_query_default_pagination(self):
        """query() should use default limit=100 and offset=0."""
        await self.logger.query()

        query_args = self.mock_conn.queries[0][2]
        assert 100 in query_args  # default limit
        assert 0 in query_args  # default offset

    @pytest.mark.asyncio
    async def test_query_empty_result(self):
        """query() should return empty list when no entries match."""
        self.mock_conn._rows = []

        entries = await self.logger.query(run_id=uuid4())
        assert entries == []

    @pytest.mark.asyncio
    async def test_query_parses_json_details(self):
        """query() should parse JSONB details field correctly."""
        entries = await self.logger.query()

        # Entry 2 has details
        assert entries[1].details == {"risk_score": 45}
        # Entry 1 has no details
        assert entries[0].details is None

    @pytest.mark.asyncio
    async def test_query_database_error_raises_audit_logger_error(self):
        """query() should wrap database errors in AuditLoggerError."""

        class FailingPool:
            def acquire(self):
                raise Exception("Connection refused")

        self.logger._pool = FailingPool()

        with pytest.raises(AuditLoggerError, match="Failed to query audit log"):
            await self.logger.query()


# =============================================================================
# Tests for module-level get_audit_logger
# =============================================================================


class TestGetAuditLogger:
    """Tests for the get_audit_logger singleton."""

    def test_get_audit_logger_returns_instance(self):
        """get_audit_logger() should return an AuditLogger instance."""
        from backend.services.audit_logger import get_audit_logger

        logger = get_audit_logger()
        assert isinstance(logger, AuditLogger)

    def test_get_audit_logger_returns_same_instance(self):
        """get_audit_logger() should return the same singleton instance."""
        from backend.services.audit_logger import get_audit_logger

        logger1 = get_audit_logger()
        logger2 = get_audit_logger()
        assert logger1 is logger2
