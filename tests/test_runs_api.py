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
from types import SimpleNamespace
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


def _ready_preflight(*parameters):
    return MagicMock(
        ready=True,
        blockers=[],
        parameters=[SimpleNamespace(name=name, available=True) for name in parameters],
    )


# --- Start Run Tests ---


class TestStartRunEndpoint:
    """Tests for POST /api/v1/runs/."""

    @pytest.mark.asyncio
    async def test_start_run_success(self, api_client):
        """Starting a run returns success response."""
        mock_conn = _mock_db_dependency()

        with patch(
            "backend.api.runs.build_tuning_preflight",
            AsyncMock(return_value=_ready_preflight()),
        ):
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
        mock_conn = _mock_db_dependency()

        with patch(
            "backend.api.runs.build_tuning_preflight",
            AsyncMock(return_value=_ready_preflight()),
        ):
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

    @pytest.mark.asyncio
    async def test_start_run_persists_wizard_contract(self, api_client):
        """Session creation persists fields supplied by the future wizard."""
        host_id = uuid4()
        organization_id = uuid4()
        mock_conn = _mock_db_dependency()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": host_id,
                "organization_id": organization_id,
                "database_name": "defaultdb",
                "configuration_backend": "alter_system",
            }
        )

        with patch(
            "backend.api.runs.build_tuning_preflight",
            AsyncMock(
                return_value=_ready_preflight("work_mem", "random_page_cost")
            ),
        ):
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
                        "host_id": str(host_id),
                        "database_name": "appdb",
                        "goal": "Improve checkout AQR",
                        "tuning_target": "system_wide_aqr",
                        "tuning_mode": "reload_only",
                        "selected_parameters": ["work_mem", "random_page_cost"],
                        "approval_policy": "per_candidate",
                        "warmup_window_seconds": 90,
                        "measurement_window_seconds": 600,
                        "objective_guardrails": {"aqr_regression_pct": 5.0},
                    },
                )

        assert response.status_code == 200
        loop_insert = next(
            call
            for call in mock_conn.execute.await_args_list
            if "INSERT INTO loop_runs" in call.args[0]
        )
        assert "database_name, tuning_target, tuning_mode" in loop_insert.args[0]
        assert "appdb" in loop_insert.args
        assert "system_wide_aqr" in loop_insert.args
        assert "reload_only" in loop_insert.args
        assert 600 in loop_insert.args

    @pytest.mark.asyncio
    async def test_start_run_rechecks_preflight_server_side(self, api_client):
        mock_conn = _mock_db_dependency()
        blocked = MagicMock(
            ready=False,
            blockers=["The agent capability report must be newer than five minutes."],
            parameters=[],
        )
        with patch("backend.api.runs.build_tuning_preflight", AsyncMock(return_value=blocked)):
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
                    "/api/v1/runs/", json={"goal": "Unsafe bypass attempt"}
                )

        assert response.status_code == 409
        assert "preflight is blocked" in response.text

    @pytest.mark.asyncio
    async def test_reload_only_rejects_restart_parameters(self, api_client):
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
                json={
                    "goal": "Tune memory",
                    "tuning_mode": "reload_only",
                    "selected_parameters": ["shared_buffers"],
                },
            )

        assert response.status_code == 422
        assert "restart parameters" in response.text


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
        mock_conn.fetchval = AsyncMock(return_value=0)

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
        mock_conn.fetchval = AsyncMock(return_value=1)
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
        mock_conn.fetchval = AsyncMock(return_value=1)
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
        mock_conn.fetchval = AsyncMock(return_value=0)

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

    @pytest.mark.asyncio
    async def test_session_filters_are_composable_and_paginated(self, api_client):
        """History filters bind values and preserve an accurate total."""
        host_id = uuid4()
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=57)
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
            response = await api_client.get(
                "/api/v1/runs/",
                params=[
                    ("page", "2"),
                    ("page_size", "20"),
                    ("host_id", str(host_id)),
                    ("database", "appdb"),
                    ("status", "completed"),
                    ("status", "failed"),
                    ("tuning_target", "system_wide_aqr"),
                    ("tuning_mode", "reload_only"),
                    ("objective", "checkout"),
                    ("date_from", "2026-07-01T00:00:00Z"),
                    ("date_to", "2026-07-31T23:59:59Z"),
                ],
            )

        assert response.status_code == 200
        data = response.json()
        assert data == {
            "runs": [],
            "total": 57,
            "page": 2,
            "page_size": 20,
            "total_pages": 3,
        }
        count_query = mock_conn.fetchval.await_args.args[0]
        assert "r.host_id" in count_query
        assert "r.database_name" in count_query
        assert "r.status = ANY" in count_query
        assert "r.tuning_target" in count_query
        assert "r.tuning_mode" in count_query
        assert "plainto_tsquery" in count_query
        assert "r.started_at >=" in count_query
        assert "r.started_at <=" in count_query
        assert "checkout" not in count_query
        fetch_args = mock_conn.fetch.await_args.args
        assert fetch_args[-2:] == (20, 20)

    @pytest.mark.asyncio
    async def test_session_filter_rejects_reversed_date_range(self, api_client):
        mock_conn = AsyncMock()
        with patch("backend.dependencies.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.acquire = MagicMock(
                return_value=AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_conn),
                    __aexit__=AsyncMock(return_value=None),
                )
            )
            mock_get_pool.return_value = mock_pool
            response = await api_client.get(
                "/api/v1/runs/",
                params={
                    "date_from": "2026-08-01T00:00:00Z",
                    "date_to": "2026-07-01T00:00:00Z",
                },
            )
        assert response.status_code == 422


def _capability_row(**overrides):
    row = {
        "id": uuid4(),
        "hostname": "postgres-lab",
        "database_name": "appdb",
        "environment": "staging",
        "platform_type": "self_managed",
        "configuration_backend": "alter_system",
        "pg_version": "PostgreSQL 17.5",
        "server_role": "primary",
        "connection_status": "connected",
        "target_dsn_env": "LAB_TARGET_DSN",
        "writes_enabled": True,
        "restart_required_enabled": True,
        "managed_conf_enrolled": False,
        "connectivity": True,
        "system_information": True,
        "system_metrics": True,
        "pg_stat_statements": True,
        "query_text_collection": False,
        "configuration_read": True,
        "configuration_write": True,
        "reload_permission": True,
        "restart_capability": True,
        "provider_api": False,
        "managed_file_access": False,
        "capability_observed_at": datetime.now(timezone.utc),
    }
    row.update(overrides)
    return row


class TestTuningPreflight:
    """Tests for the Start tuning capability contract."""

    @pytest.mark.asyncio
    async def test_reload_preflight_returns_modes_and_parameter_catalog(self, api_client):
        host = _capability_row()
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=host)
        mock_conn.fetch = AsyncMock(
            return_value=[
                {"setting_name": "work_mem", "parameter_context": "reload"},
                {"setting_name": "shared_buffers", "parameter_context": "restart"},
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
            response = await api_client.get(
                "/api/v1/runs/preflight",
                params={"host_id": str(host["id"]), "mode": "reload_only"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["ready"] is True
        assert data["database_name"] == "appdb"
        assert data["blockers"] == []
        assert data["warnings"]
        assert len(data["parameters"]) == 19
        work_mem = next(item for item in data["parameters"] if item["name"] == "work_mem")
        shared = next(
            item for item in data["parameters"] if item["name"] == "shared_buffers"
        )
        assert work_mem["available"] is True
        assert shared["available"] is False
        assert "restart-enabled" in shared["reason"]
        assert data["supported_modes"][1]["available"] is True

    @pytest.mark.asyncio
    async def test_restart_preflight_fails_closed_without_restart_capability(
        self, api_client
    ):
        host = _capability_row(restart_capability=False)
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=host)
        mock_conn.fetch = AsyncMock(
            return_value=[
                {"setting_name": "work_mem", "parameter_context": "reload"}
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
            response = await api_client.get(
                "/api/v1/runs/preflight",
                params={"host_id": str(host["id"]), "mode": "restart_enabled"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["ready"] is False
        restart_check = next(
            item for item in data["checks"] if item["key"] == "restart_capability"
        )
        assert restart_check["status"] == "blocked"
        assert data["supported_modes"][1]["available"] is False

    @pytest.mark.asyncio
    async def test_preflight_blocks_stale_capability_snapshot(self, api_client):
        host = _capability_row(
            capability_observed_at=datetime.now(timezone.utc) - timedelta(minutes=6)
        )
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=host)
        mock_conn.fetch = AsyncMock(
            return_value=[
                {"setting_name": "work_mem", "parameter_context": "reload"}
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
            response = await api_client.get(
                "/api/v1/runs/preflight", params={"host_id": str(host["id"])}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["ready"] is False
        freshness = next(
            item for item in data["checks"] if item["key"] == "capability_freshness"
        )
        assert freshness["status"] == "blocked"

    @pytest.mark.asyncio
    async def test_preflight_unknown_host_returns_404(self, api_client):
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
            response = await api_client.get(
                "/api/v1/runs/preflight", params={"host_id": str(uuid4())}
            )

        assert response.status_code == 404
