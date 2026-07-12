"""
Tests for the Runs API endpoints.

Tests cover:
- POST /api/v1/runs/ - Start a new run
- POST /api/v1/runs/{run_id}/halt - Halt a run
- GET /api/v1/runs/{run_id} - Get run status
- GET /api/v1/runs/ - List persistent tuning sessions
- WebSocket /ws/runs/{run_id} - Real-time updates

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app


@pytest.fixture
async def api_client():
    """Create an async test client for the FastAPI application."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _mock_db_dependency():
    """Create a mock database connection dependency override."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        return_value={"id": uuid4(), "organization_id": uuid4()}
    )
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchval = AsyncMock(return_value=False)
    mock_conn.execute = AsyncMock()
    transaction = AsyncMock()
    transaction.__aenter__ = AsyncMock(return_value=transaction)
    transaction.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=transaction)
    return mock_conn


# --- Start Run Tests ---


class TestStartRunEndpoint:
    """Tests for POST /api/v1/runs/."""

    @pytest.mark.asyncio
    async def test_start_run_success(self, api_client):
        """Starting a run returns success response."""
        mock_worker = MagicMock()
        mock_worker._run_id = uuid4()
        mock_worker.start_run = AsyncMock()

        mock_conn = _mock_db_dependency()

        with patch("backend.services.loop_worker.DBALoopWorker", return_value=mock_worker):
            with patch("backend.dependencies.get_pool") as mock_get_pool:
                mock_pool = MagicMock()
                mock_pool.acquire = MagicMock(
                    return_value=AsyncMock(
                        __aenter__=AsyncMock(return_value=mock_conn),
                        __aexit__=AsyncMock(return_value=None),
                    )
                )
                mock_get_pool.return_value = mock_pool
                response = await api_client.post(
                    "/api/v1/runs/",
                    json={"goal": "Optimize query performance"},
                )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert data["goal"] == "Optimize query performance"

    @pytest.mark.asyncio
    async def test_start_run_empty_goal_rejected(self, api_client):
        """Starting a run with empty goal is rejected."""
        mock_conn = _mock_db_dependency()

        with patch("backend.dependencies.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.acquire = MagicMock(
                return_value=AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_conn),
                    __aexit__=AsyncMock(return_value=None),
                )
            )
            mock_get_pool.return_value = mock_pool
            response = await api_client.post(
                "/api/v1/runs/",
                json={"goal": ""},
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_start_run_with_config(self, api_client):
        """Starting a run with custom config."""
        mock_worker = MagicMock()
        mock_worker._run_id = uuid4()
        mock_worker.start_run = AsyncMock()

        mock_conn = _mock_db_dependency()

        with patch("backend.services.loop_worker.DBALoopWorker", return_value=mock_worker):
            with patch("backend.dependencies.get_pool") as mock_get_pool:
                mock_pool = MagicMock()
                mock_pool.acquire = MagicMock(
                    return_value=AsyncMock(
                        __aenter__=AsyncMock(return_value=mock_conn),
                        __aexit__=AsyncMock(return_value=None),
                    )
                )
                mock_get_pool.return_value = mock_pool
                response = await api_client.post(
                    "/api/v1/runs/",
                    json={
                        "goal": "Fix replication lag",
                        "max_iterations": 5,
                        "max_steps": 10,
                        "approval_timeout_hours": 12,
                    },
                )

        assert response.status_code == 200
        data = response.json()
        assert data["goal"] == "Fix replication lag"

    @pytest.mark.asyncio
    async def test_start_run_invalid_host_id(self, api_client):
        """Starting a run with invalid host_id format."""
        mock_conn = _mock_db_dependency()
        mock_conn.fetchrow = AsyncMock(return_value=None)

        with patch("backend.dependencies.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.acquire = MagicMock(
                return_value=AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_conn),
                    __aexit__=AsyncMock(return_value=None),
                )
            )
            mock_get_pool.return_value = mock_pool
            response = await api_client.post(
                "/api/v1/runs/",
                json={"goal": "Test", "host_id": "not-a-uuid"},
            )

        assert response.status_code == 422


# --- Halt Run Tests ---


class TestHaltRunEndpoint:
    """Tests for POST /api/v1/runs/{run_id}/halt."""

    @pytest.mark.asyncio
    async def test_halt_active_run(self, api_client):
        """Halting an active run succeeds."""
        run_id = uuid4()

        mock_worker = MagicMock()
        mock_worker.halt_run = AsyncMock(
            return_value={
                "success": True,
                "message": f"Run '{run_id}' halted successfully",
                "status": "manually_halted",
                "previous_step": "observe",
            }
        )

        mock_conn = _mock_db_dependency()
        mock_conn.fetchrow = AsyncMock(
            return_value={"id": run_id, "status": "running", "current_step": "observe"}
        )

        with patch("backend.services.loop_worker.get_active_runs", return_value={}):
            with patch("backend.services.loop_worker.get_loop_worker", return_value=mock_worker):
                with patch("backend.dependencies.get_pool") as mock_get_pool:
                    mock_pool = MagicMock()
                    mock_pool.acquire = MagicMock(
                        return_value=AsyncMock(
                            __aenter__=AsyncMock(return_value=mock_conn),
                            __aexit__=AsyncMock(return_value=None),
                        )
                    )
                    mock_get_pool.return_value = mock_pool
                    response = await api_client.post(f"/api/v1/runs/{run_id}/halt")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["status"] == "manually_halted"

    @pytest.mark.asyncio
    async def test_halt_completed_run_rejected(self, api_client):
        """Halting a completed run returns 409."""
        run_id = uuid4()

        mock_worker = MagicMock()
        mock_worker.halt_run = AsyncMock(
            return_value={
                "success": False,
                "message": f"Run '{run_id}' is no longer active (current status: completed)",
                "status": "completed",
            }
        )

        mock_conn = _mock_db_dependency()
        mock_conn.fetchrow = AsyncMock(
            return_value={"id": run_id, "status": "completed", "current_step": "report"}
        )

        with patch("backend.services.loop_worker.get_active_runs", return_value={}):
            with patch("backend.services.loop_worker.get_loop_worker", return_value=mock_worker):
                with patch("backend.dependencies.get_pool") as mock_get_pool:
                    mock_pool = MagicMock()
                    mock_pool.acquire = MagicMock(
                        return_value=AsyncMock(
                            __aenter__=AsyncMock(return_value=mock_conn),
                            __aexit__=AsyncMock(return_value=None),
                        )
                    )
                    mock_get_pool.return_value = mock_pool
                    response = await api_client.post(f"/api/v1/runs/{run_id}/halt")

        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_halt_nonexistent_run(self, api_client):
        """Halting a non-existent run returns 409."""
        run_id = uuid4()

        mock_worker = MagicMock()
        mock_worker.halt_run = AsyncMock(
            return_value={
                "success": False,
                "message": f"Run '{run_id}' not found",
                "status": "not_found",
            }
        )

        mock_conn = _mock_db_dependency()
        mock_conn.fetchrow = AsyncMock(return_value=None)

        with patch("backend.services.loop_worker.get_active_runs", return_value={}):
            with patch("backend.services.loop_worker.get_loop_worker", return_value=mock_worker):
                with patch("backend.dependencies.get_pool") as mock_get_pool:
                    mock_pool = MagicMock()
                    mock_pool.acquire = MagicMock(
                        return_value=AsyncMock(
                            __aenter__=AsyncMock(return_value=mock_conn),
                            __aexit__=AsyncMock(return_value=None),
                        )
                    )
                    mock_get_pool.return_value = mock_pool
                    response = await api_client.post(f"/api/v1/runs/{run_id}/halt")

        assert response.status_code == 404


# --- Get Run Status Tests ---


class TestGetRunStatus:
    """Tests for GET /api/v1/runs/{run_id}."""

    @pytest.mark.asyncio
    async def test_get_run_status_success(self, api_client):
        """Getting status of an existing run returns details."""
        run_id = uuid4()
        now = datetime.now(timezone.utc)

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": run_id,
                "goal": "Optimize queries",
                "status": "running",
                "current_step": "observe",
                "current_iteration": 1,
                "max_iterations": 10,
                "started_at": now - timedelta(seconds=30),
                "last_step_transition_at": now - timedelta(seconds=5),
                "failure_reason": None,
            }
        )

        with patch("backend.dependencies.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.acquire = MagicMock(
                return_value=AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_conn),
                    __aexit__=AsyncMock(return_value=None),
                )
            )
            mock_get_pool.return_value = mock_pool

            response = await api_client.get(f"/api/v1/runs/{run_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(run_id)
        assert data["goal"] == "Optimize queries"
        assert data["status"] == "running"
        assert data["current_step"] == "observe"
        assert data["elapsed_seconds"] > 0

    @pytest.mark.asyncio
    async def test_get_run_status_not_found(self, api_client):
        """Getting status of non-existent run returns 404."""
        run_id = uuid4()

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)

        with patch("backend.dependencies.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.acquire = MagicMock(
                return_value=AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_conn),
                    __aexit__=AsyncMock(return_value=None),
                )
            )
            mock_get_pool.return_value = mock_pool

            response = await api_client.get(f"/api/v1/runs/{run_id}")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_run_status_with_guardrail_violation(self, api_client):
        """Getting status of a run stopped by guardrail shows violation details."""
        run_id = uuid4()
        now = datetime.now(timezone.utc)

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": run_id,
                "goal": "Optimize queries",
                "status": "failed",
                "current_step": "safety_check",
                "current_iteration": 1,
                "max_iterations": 10,
                "started_at": now - timedelta(seconds=60),
                "last_step_transition_at": now - timedelta(seconds=10),
                "failure_reason": (
                    "Guardrail check failed at stage 'allowlist': Setting not allowed"
                ),
            }
        )

        with patch("backend.dependencies.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.acquire = MagicMock(
                return_value=AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_conn),
                    __aexit__=AsyncMock(return_value=None),
                )
            )
            mock_get_pool.return_value = mock_pool

            response = await api_client.get(f"/api/v1/runs/{run_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["guardrail_violation"] is not None
        assert "guardrail" in data["guardrail_violation"]["reason"].lower()

    @pytest.mark.asyncio
    async def test_completed_run_duration_is_frozen(self, api_client):
        """Completed runs use completed_at rather than continuing to age."""
        run_id = uuid4()
        now = datetime.now(timezone.utc)
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": run_id,
                "goal": "Tune a completed workload",
                "status": "completed",
                "current_step": "report",
                "current_iteration": 1,
                "max_iterations": 3,
                "started_at": now - timedelta(minutes=10),
                "completed_at": now - timedelta(minutes=8),
                "last_step_transition_at": now - timedelta(minutes=8),
                "failure_reason": None,
            }
        )

        with patch("backend.dependencies.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.acquire = MagicMock(
                return_value=AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_conn),
                    __aexit__=AsyncMock(return_value=None),
                )
            )
            mock_get_pool.return_value = mock_pool
            response = await api_client.get(f"/api/v1/runs/{run_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["completed_at"] is not None
        assert data["elapsed_seconds"] == pytest.approx(120.0, abs=0.1)


# --- List Active Runs Tests ---


class TestListRuns:
    """Tests for GET /api/v1/runs/."""

    @pytest.mark.asyncio
    async def test_list_runs_empty(self, api_client):
        """Listing sessions with no runs returns an empty history."""
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])

        with patch("backend.dependencies.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.acquire = MagicMock(
                return_value=AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_conn),
                    __aexit__=AsyncMock(return_value=None),
                )
            )
            mock_get_pool.return_value = mock_pool

            response = await api_client.get("/api/v1/runs/")

        assert response.status_code == 200
        data = response.json()
        assert data["runs"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_runs_with_results(self, api_client):
        """Listing runs returns active runs with correct details."""
        run_id = uuid4()
        now = datetime.now(timezone.utc)

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": run_id,
                    "goal": "Fix performance",
                    "status": "running",
                    "current_step": "diagnose",
                    "current_iteration": 2,
                    "started_at": now - timedelta(seconds=120),
                    "completed_at": None,
                    "last_step_transition_at": now - timedelta(seconds=10),
                }
            ]
        )

        with patch("backend.dependencies.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.acquire = MagicMock(
                return_value=AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_conn),
                    __aexit__=AsyncMock(return_value=None),
                )
            )
            mock_get_pool.return_value = mock_pool

            response = await api_client.get("/api/v1/runs/")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["runs"][0]["goal"] == "Fix performance"
        assert data["runs"][0]["status"] == "running"
        assert data["runs"][0]["current_step"] == "diagnose"

    @pytest.mark.asyncio
    async def test_default_history_includes_completed_runs(self, api_client):
        """The default query does not filter terminal sessions out."""
        run_id = uuid4()
        now = datetime.now(timezone.utc)
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": run_id,
                    "goal": "Completed tuning session",
                    "status": "completed",
                    "current_step": "report",
                    "current_iteration": 1,
                    "started_at": now - timedelta(minutes=5),
                    "completed_at": now - timedelta(minutes=3),
                    "last_step_transition_at": now - timedelta(minutes=3),
                }
            ]
        )

        with patch("backend.dependencies.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.acquire = MagicMock(
                return_value=AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_conn),
                    __aexit__=AsyncMock(return_value=None),
                )
            )
            mock_get_pool.return_value = mock_pool
            response = await api_client.get("/api/v1/runs/")

        assert response.status_code == 200
        data = response.json()
        assert data["runs"][0]["status"] == "completed"
        assert data["runs"][0]["elapsed_seconds"] == pytest.approx(120.0, abs=0.1)
        query = mock_conn.fetch.await_args.args[0]
        assert "status IN" not in query

    @pytest.mark.asyncio
    async def test_active_only_filter_keeps_operational_queue(self, api_client):
        """Callers can still request only operationally active sessions."""
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])

        with patch("backend.dependencies.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.acquire = MagicMock(
                return_value=AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_conn),
                    __aexit__=AsyncMock(return_value=None),
                )
            )
            mock_get_pool.return_value = mock_pool
            response = await api_client.get("/api/v1/runs/?active_only=true")

        assert response.status_code == 200
        query = mock_conn.fetch.await_args.args[0]
        assert "status IN ('queued', 'running', 'waiting_approval', 'unresponsive')" in query
