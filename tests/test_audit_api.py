"""
Unit tests for backend/api/audit.py endpoints.

Tests the audit API routes:
- GET /api/v1/audit/ - list recent audit entries
- GET /api/v1/audit/{run_id} - list audit entries for a specific run

Uses mocking to isolate the API layer from the database.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.models.audit import AuditEntry
from backend.services.audit_logger import AuditLoggerError

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def client():
    """Create an async test client for the FastAPI application."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# =============================================================================
# Test GET /api/v1/audit/
# =============================================================================


class TestListAuditEntries:
    """Tests for listing recent audit entries."""

    @pytest.mark.asyncio
    async def test_list_audit_entries_returns_entries(self, client):
        """GET /api/v1/audit/ should return audit entries."""
        run_id = uuid4()
        now = datetime.now(timezone.utc)
        mock_entries = [
            AuditEntry(
                id=1,
                run_id=run_id,
                timestamp=now - timedelta(minutes=5),
                actor_type="system",
                actor_name="loop_worker",
                action_type="run_started",
                target_host_id=None,
                result="success",
                result_reason=None,
                details=None,
            ),
            AuditEntry(
                id=2,
                run_id=run_id,
                timestamp=now - timedelta(minutes=1),
                actor_type="human",
                actor_name="dba_admin",
                action_type="plan_approved",
                target_host_id=uuid4(),
                result="success",
                result_reason=None,
                details={"plan_id": str(uuid4())},
            ),
        ]

        with patch("backend.api.audit.get_audit_logger") as mock_get_logger:
            mock_logger = AsyncMock()
            mock_logger.query = AsyncMock(return_value=mock_entries)
            mock_get_logger.return_value = mock_logger

            response = await client.get("/api/v1/audit/")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert len(data["entries"]) == 2
        assert data["entries"][0]["actor_type"] == "system"
        assert data["entries"][1]["actor_type"] == "human"

    @pytest.mark.asyncio
    async def test_list_audit_entries_empty(self, client):
        """GET /api/v1/audit/ should return empty list when no entries."""
        with patch("backend.api.audit.get_audit_logger") as mock_get_logger:
            mock_logger = AsyncMock()
            mock_logger.query = AsyncMock(return_value=[])
            mock_get_logger.return_value = mock_logger

            response = await client.get("/api/v1/audit/")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["entries"] == []

    @pytest.mark.asyncio
    async def test_list_audit_entries_with_pagination(self, client):
        """GET /api/v1/audit/ should accept limit and offset params."""
        with patch("backend.api.audit.get_audit_logger") as mock_get_logger:
            mock_logger = AsyncMock()
            mock_logger.query = AsyncMock(return_value=[])
            mock_get_logger.return_value = mock_logger

            response = await client.get("/api/v1/audit/?limit=10&offset=20")

        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 10
        assert data["offset"] == 20

    @pytest.mark.asyncio
    async def test_list_audit_entries_service_error(self, client):
        """GET /api/v1/audit/ should return 503 on service error."""
        with patch("backend.api.audit.get_audit_logger") as mock_get_logger:
            mock_logger = AsyncMock()
            mock_logger.query = AsyncMock(side_effect=AuditLoggerError("Database unavailable"))
            mock_get_logger.return_value = mock_logger

            response = await client.get("/api/v1/audit/")

        assert response.status_code == 503


# =============================================================================
# Test GET /api/v1/audit/{run_id}
# =============================================================================


class TestGetAuditEntriesForRun:
    """Tests for getting audit entries for a specific run."""

    @pytest.mark.asyncio
    async def test_get_run_entries_returns_entries(self, client):
        """GET /api/v1/audit/{run_id} should return entries for that run."""
        run_id = uuid4()
        now = datetime.now(timezone.utc)
        mock_entries = [
            AuditEntry(
                id=1,
                run_id=run_id,
                timestamp=now - timedelta(minutes=5),
                actor_type="system",
                actor_name="loop_worker",
                action_type="run_started",
                target_host_id=None,
                result="success",
                result_reason=None,
                details=None,
            ),
        ]

        with patch("backend.api.audit.get_audit_logger") as mock_get_logger:
            mock_logger = AsyncMock()
            mock_logger.query = AsyncMock(return_value=mock_entries)
            mock_get_logger.return_value = mock_logger

            response = await client.get(f"/api/v1/audit/{run_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["entries"][0]["run_id"] == str(run_id)

    @pytest.mark.asyncio
    async def test_get_run_entries_empty(self, client):
        """GET /api/v1/audit/{run_id} should return empty for unknown run."""
        run_id = uuid4()

        with patch("backend.api.audit.get_audit_logger") as mock_get_logger:
            mock_logger = AsyncMock()
            mock_logger.query = AsyncMock(return_value=[])
            mock_get_logger.return_value = mock_logger

            response = await client.get(f"/api/v1/audit/{run_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["entries"] == []

    @pytest.mark.asyncio
    async def test_get_run_entries_invalid_uuid(self, client):
        """GET /api/v1/audit/{run_id} should return 422 for invalid UUID."""
        response = await client.get("/api/v1/audit/not-a-uuid")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_get_run_entries_service_error(self, client):
        """GET /api/v1/audit/{run_id} should return 503 on service error."""
        run_id = uuid4()

        with patch("backend.api.audit.get_audit_logger") as mock_get_logger:
            mock_logger = AsyncMock()
            mock_logger.query = AsyncMock(side_effect=AuditLoggerError("Database unavailable"))
            mock_get_logger.return_value = mock_logger

            response = await client.get(f"/api/v1/audit/{run_id}")

        assert response.status_code == 503
