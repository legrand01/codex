"""
Unit tests for DBA Report generation and Reports API.

Tests:
- Report generation logic (report_generator.py)
- Reports API endpoints (api/reports.py)

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.services.report_generator import (
    LABEL_AI_RECOMMENDATION,
    LABEL_INCONCLUSIVE,
    LABEL_VERIFIED_FACT,
    ReportGenerationError,
    ReportGenerator,
)


class AsyncContextManager:
    """Helper to mock async context managers (async with pool.acquire() as conn)."""

    def __init__(self, mock_conn):
        self.mock_conn = mock_conn

    async def __aenter__(self):
        return self.mock_conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


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
# Test ReportGenerator - build methods
# =============================================================================


class TestReportGeneratorBuildMethods:
    """Tests for ReportGenerator internal build methods."""

    def setup_method(self):
        """Set up test fixtures."""
        self.generator = ReportGenerator(pool=MagicMock())

    def test_build_evidence_summaries_verified_fact(self):
        """Evidence above threshold is labeled VERIFIED_FACT."""
        evidence_rows = [
            {
                "id": uuid4(),
                "evidence_type": "pg_settings",
                "collected_at": datetime.now(timezone.utc),
                "quality_score": 0.8,
            }
        ]
        summaries = self.generator._build_evidence_summaries(evidence_rows)
        assert len(summaries) == 1
        assert summaries[0]["provenance"] == LABEL_VERIFIED_FACT
        assert "evidence_gap" not in summaries[0]

    def test_build_evidence_summaries_inconclusive(self):
        """Evidence below threshold is labeled INCONCLUSIVE."""
        evidence_rows = [
            {
                "id": uuid4(),
                "evidence_type": "pg_settings",
                "collected_at": datetime.now(timezone.utc),
                "quality_score": 0.3,
            }
        ]
        summaries = self.generator._build_evidence_summaries(evidence_rows)
        assert len(summaries) == 1
        assert summaries[0]["provenance"] == LABEL_INCONCLUSIVE
        assert "evidence_gap" in summaries[0]

    def test_build_evidence_summaries_null_quality_score(self):
        """Evidence with null quality_score is treated as 0.0 (INCONCLUSIVE)."""
        evidence_rows = [
            {
                "id": uuid4(),
                "evidence_type": "locks",
                "collected_at": datetime.now(timezone.utc),
                "quality_score": None,
            }
        ]
        summaries = self.generator._build_evidence_summaries(evidence_rows)
        assert len(summaries) == 1
        assert summaries[0]["provenance"] == LABEL_INCONCLUSIVE

    def test_build_plans_proposed_ai_recommendation(self):
        """Plans with confidence above threshold labeled AI_RECOMMENDATION."""
        plans = [
            {
                "id": uuid4(),
                "status": "pending_approval",
                "proposed_changes": json.dumps([{"setting": "work_mem"}]),
                "risk_score": 30,
                "confidence_score": 0.85,
                "uncertainty_explanation": None,
                "submission_time": datetime.now(timezone.utc),
            }
        ]
        proposed = self.generator._build_plans_proposed(plans)
        assert len(proposed) == 1
        assert proposed[0]["provenance"] == LABEL_AI_RECOMMENDATION
        assert "evidence_gap" not in proposed[0]

    def test_build_plans_proposed_inconclusive(self):
        """Plans with confidence below threshold marked INCONCLUSIVE."""
        plans = [
            {
                "id": uuid4(),
                "status": "pending_approval",
                "proposed_changes": json.dumps([]),
                "risk_score": 50,
                "confidence_score": 0.4,
                "uncertainty_explanation": "Insufficient evidence",
                "submission_time": datetime.now(timezone.utc),
            }
        ]
        proposed = self.generator._build_plans_proposed(plans)
        assert len(proposed) == 1
        assert proposed[0]["provenance"] == LABEL_INCONCLUSIVE
        assert "evidence_gap" in proposed[0]

    def test_build_approval_decisions_approved(self):
        """Approved plans produce VERIFIED_FACT approval decision."""
        plans = [
            {
                "id": uuid4(),
                "approved_by": "dba_admin",
                "approved_at": datetime.now(timezone.utc),
                "rejected_by": None,
                "rejected_at": None,
                "rejection_reason": None,
            }
        ]
        decisions = self.generator._build_approval_decisions(plans, [])
        assert len(decisions) == 1
        assert decisions[0]["decision"] == "approved"
        assert decisions[0]["provenance"] == LABEL_VERIFIED_FACT

    def test_build_approval_decisions_rejected(self):
        """Rejected plans produce VERIFIED_FACT rejection decision."""
        plans = [
            {
                "id": uuid4(),
                "approved_by": None,
                "approved_at": None,
                "rejected_by": "dba_admin",
                "rejected_at": datetime.now(timezone.utc),
                "rejection_reason": "Too risky for production",
            }
        ]
        decisions = self.generator._build_approval_decisions(plans, [])
        assert len(decisions) == 1
        assert decisions[0]["decision"] == "rejected"
        assert decisions[0]["reason"] == "Too risky for production"
        assert decisions[0]["provenance"] == LABEL_VERIFIED_FACT

    def test_build_applied_changes(self):
        """Applied plans produce VERIFIED_FACT change entries."""
        plans = [
            {
                "id": uuid4(),
                "applied_at": datetime.now(timezone.utc),
                "proposed_changes": json.dumps([{"setting_name": "work_mem"}]),
                "rollback_instructions": json.dumps([{"setting_name": "work_mem"}]),
                "status": "applied",
                "rolled_back_at": None,
            }
        ]
        changes = self.generator._build_applied_changes(plans)
        assert len(changes) == 1
        assert changes[0]["provenance"] == LABEL_VERIFIED_FACT
        assert changes[0]["rolled_back"] is False

    def test_build_applied_changes_rolled_back(self):
        """Rolled back plans are marked as such."""
        now = datetime.now(timezone.utc)
        plans = [
            {
                "id": uuid4(),
                "applied_at": now - timedelta(minutes=5),
                "proposed_changes": [{"setting_name": "shared_buffers"}],
                "rollback_instructions": [{"setting_name": "shared_buffers"}],
                "status": "rolled_back",
                "rolled_back_at": now,
            }
        ]
        changes = self.generator._build_applied_changes(plans)
        assert len(changes) == 1
        assert changes[0]["rolled_back"] is True
        assert changes[0]["rolled_back_at"] is not None

    def test_build_verification_results(self):
        """Verification audit entries produce VERIFIED_FACT results."""
        audit_entries = [
            {
                "action_type": "verification_completed",
                "timestamp": datetime.now(timezone.utc),
                "result": "success",
                "result_reason": "All metrics within threshold",
                "details": json.dumps({"delta_pct": 5.2}),
            }
        ]
        results = self.generator._build_verification_results(audit_entries)
        assert len(results) == 1
        assert results[0]["provenance"] == LABEL_VERIFIED_FACT
        assert results[0]["result"] == "success"

    def test_build_verification_results_no_verification_entries(self):
        """Non-verification entries are excluded."""
        audit_entries = [
            {
                "action_type": "plan_approved",
                "timestamp": datetime.now(timezone.utc),
                "result": "success",
                "result_reason": None,
                "details": None,
            }
        ]
        results = self.generator._build_verification_results(audit_entries)
        assert len(results) == 0


# =============================================================================
# Test outcome status determination
# =============================================================================


class TestOutcomeStatus:
    """Tests for outcome status determination."""

    def setup_method(self):
        self.generator = ReportGenerator(pool=MagicMock())

    def test_completed_run_no_plans_is_success(self):
        """Completed run with no plans is success."""
        run = {"status": "completed"}
        assert self.generator._determine_outcome_status(run, []) == "success"

    def test_completed_run_with_applied_plans_is_success(self):
        """Completed run with successfully applied plans is success."""
        run = {"status": "completed"}
        plans = [{"applied_at": datetime.now(timezone.utc), "status": "applied"}]
        assert self.generator._determine_outcome_status(run, plans) == "success"

    def test_completed_run_all_rolled_back_is_failure(self):
        """Completed run where all applied plans were rolled back is failure."""
        run = {"status": "completed"}
        plans = [
            {"applied_at": datetime.now(timezone.utc), "status": "rolled_back"},
        ]
        assert self.generator._determine_outcome_status(run, plans) == "failure"

    def test_completed_run_partial_rollback_is_partial_success(self):
        """Completed run with some rollbacks is partial_success."""
        now = datetime.now(timezone.utc)
        run = {"status": "completed"}
        plans = [
            {"applied_at": now, "status": "applied"},
            {"applied_at": now, "status": "rolled_back"},
        ]
        assert self.generator._determine_outcome_status(run, plans) == "partial_success"

    def test_failed_run_is_failure(self):
        """Failed run produces failure outcome."""
        run = {"status": "failed"}
        assert self.generator._determine_outcome_status(run, []) == "failure"

    def test_timed_out_run_is_failure(self):
        """Timed out run produces failure outcome."""
        run = {"status": "timed_out"}
        assert self.generator._determine_outcome_status(run, []) == "failure"

    def test_halted_with_applied_is_partial_success(self):
        """Halted run with applied plans is partial_success."""
        run = {"status": "manually_halted"}
        plans = [{"applied_at": datetime.now(timezone.utc), "status": "applied"}]
        assert self.generator._determine_outcome_status(run, plans) == "partial_success"

    def test_halted_without_applied_is_failure(self):
        """Halted run without applied plans is failure."""
        run = {"status": "manually_halted"}
        assert self.generator._determine_outcome_status(run, []) == "failure"


# =============================================================================
# Test Reports API - GET /api/v1/reports/{run_id}
# =============================================================================


class TestGetReportEndpoint:
    """Tests for GET /api/v1/reports/{run_id}."""

    @pytest.mark.asyncio
    async def test_get_existing_report(self, client):
        """Returns existing report from database."""
        run_id = uuid4()
        report_id = uuid4()
        now = datetime.now(timezone.utc)

        report_content = {
            "evidence_summaries": [{"type": "pg_settings", "provenance": "VERIFIED_FACT"}],
            "plans_proposed": [],
            "approval_decisions": [],
            "applied_changes": [],
            "verification_results": [],
        }

        mock_row = {
            "id": report_id,
            "run_id": run_id,
            "goal": "Optimize query performance",
            "host_id": uuid4(),
            "outcome_status": "success",
            "report_content": report_content,
            "generated_at": now,
            "expires_at": now + timedelta(days=90),
        }

        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=mock_row)
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        with patch("backend.api.reports.get_pool", return_value=mock_pool):
            response = await client.get(f"/api/v1/reports/{run_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["run_id"] == str(run_id)
        assert data["goal"] == "Optimize query performance"
        assert data["outcome_status"] == "success"
        assert len(data["evidence_summaries"]) == 1

    @pytest.mark.asyncio
    async def test_get_report_not_found_generation_fails(self, client):
        """Returns 404 when no report exists and generation fails."""
        run_id = uuid4()

        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        mock_gen = AsyncMock()
        mock_gen.generate_report = AsyncMock(side_effect=ReportGenerationError("Run not found"))

        with patch("backend.api.reports.get_pool", return_value=mock_pool):
            with patch("backend.api.reports.get_report_generator", return_value=mock_gen):
                with patch("backend.api.reports.get_audit_logger") as mock_al:
                    mock_logger_instance = AsyncMock()
                    mock_al.return_value = mock_logger_instance
                    response = await client.get(f"/api/v1/reports/{run_id}")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_report_invalid_uuid(self, client):
        """Returns 422 for invalid UUID."""
        response = await client.get("/api/v1/reports/not-a-uuid")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_get_report_db_unavailable(self, client):
        """Returns 503 when database is unavailable."""
        run_id = uuid4()
        with patch("backend.api.reports.get_pool", return_value=None):
            response = await client.get(f"/api/v1/reports/{run_id}")
        assert response.status_code == 503


# =============================================================================
# Test Reports API - GET /api/v1/reports/search
# =============================================================================


class TestSearchReportsEndpoint:
    """Tests for GET /api/v1/reports/search."""

    @pytest.mark.asyncio
    async def test_search_no_filters(self, client):
        """Search with no filters returns all non-expired reports."""
        report_id = uuid4()
        run_id = uuid4()
        now = datetime.now(timezone.utc)

        mock_rows = [
            {
                "id": report_id,
                "run_id": run_id,
                "goal": "Optimize performance",
                "host_id": None,
                "outcome_status": "success",
                "generated_at": now,
                "expires_at": now + timedelta(days=90),
            }
        ]

        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(return_value=mock_rows)
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        with patch("backend.api.reports.get_pool", return_value=mock_pool):
            response = await client.get("/api/v1/reports/search")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["reports"]) == 1
        assert data["reports"][0]["goal"] == "Optimize performance"

    @pytest.mark.asyncio
    async def test_search_with_keywords(self, client):
        """Search with keywords filters by goal text."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        with patch("backend.api.reports.get_pool", return_value=mock_pool):
            response = await client.get("/api/v1/reports/search?keywords=optimize+performance")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["reports"] == []

    @pytest.mark.asyncio
    async def test_search_with_host_id(self, client):
        """Search with host_id filter."""
        host_id = uuid4()
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        with patch("backend.api.reports.get_pool", return_value=mock_pool):
            response = await client.get(f"/api/v1/reports/search?host_id={host_id}")

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_search_db_unavailable(self, client):
        """Returns 503 when database is unavailable."""
        with patch("backend.api.reports.get_pool", return_value=None):
            response = await client.get("/api/v1/reports/search")
        assert response.status_code == 503
