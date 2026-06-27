"""
Tests for the Plan Review and Approval Queue API endpoints.

Tests cover:
- GET /api/v1/plans/ (list pending plans, pagination, ordering, empty state)
- GET /api/v1/plans/{plan_id} (get plan detail, 404)
- POST /api/v1/plans/{plan_id}/approve (approve plan, audit logging, forwarding)
- POST /api/v1/plans/{plan_id}/reject (reject plan, reason validation, audit logging)

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.api.plans import (
    MAX_PAGE_SIZE,
    _parse_json_field,
    _row_to_plan_detail,
)
from backend.main import app
from backend.models.enums import PlanStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockRecord(dict):
    """Mock asyncpg record that supports dict-like access."""

    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def _make_plan_record(
    plan_id=None,
    run_id=None,
    host_id=None,
    status="pending_approval",
    proposed_changes=None,
    evidence_references=None,
    risk_score=35,
    confidence_score=0.85,
    uncertainty_explanation="Moderate confidence based on available evidence",
    rollback_instructions=None,
    submission_time=None,
    rejection_reason=None,
    approved_by=None,
    approved_at=None,
    rejected_by=None,
    rejected_at=None,
):
    """Create a mock plan database row."""
    if plan_id is None:
        plan_id = uuid.uuid4()
    if run_id is None:
        run_id = uuid.uuid4()
    if host_id is None:
        host_id = uuid.uuid4()
    if proposed_changes is None:
        proposed_changes = [{"setting_name": "shared_buffers", "proposed_value": "2GB"}]
    if evidence_references is None:
        evidence_references = [{"snapshot_id": str(uuid.uuid4()), "type": "pg_settings"}]
    if rollback_instructions is None:
        rollback_instructions = [{"setting_name": "shared_buffers", "restore_value": "1GB"}]
    if submission_time is None:
        submission_time = datetime.now(timezone.utc)

    return MockRecord(
        id=plan_id,
        run_id=run_id,
        host_id=host_id,
        status=status,
        proposed_changes=proposed_changes,
        evidence_references=evidence_references,
        risk_score=risk_score,
        confidence_score=confidence_score,
        uncertainty_explanation=uncertainty_explanation,
        rollback_instructions=rollback_instructions,
        submission_time=submission_time,
        rejection_reason=rejection_reason,
        approved_by=approved_by,
        approved_at=approved_at,
        rejected_by=rejected_by,
        rejected_at=rejected_at,
    )


class MockConnection:
    """Mock asyncpg connection for testing plan API endpoints."""

    def __init__(self, records=None, fetchrow_responses=None):
        self.records = records or []
        self.fetchrow_responses = fetchrow_responses or []
        self._fetchrow_call_count = 0
        self.executed_queries = []

    async def fetch(self, query, *args):
        return self.records

    async def fetchrow(self, query, *args):
        # Use pre-configured responses if available
        if self.fetchrow_responses:
            if self._fetchrow_call_count < len(self.fetchrow_responses):
                result = self.fetchrow_responses[self._fetchrow_call_count]
                self._fetchrow_call_count += 1
                return result
            return None

        # Default: look up by ID
        if args:
            target_id = args[0]
            for r in self.records:
                if r["id"] == target_id:
                    return r
        return None

    async def execute(self, query, *args):
        self.executed_queries.append((query, args))


def _override_db(mock_conn):
    """Create a dependency override for get_db."""
    from backend.dependencies import get_db

    async def override():
        yield mock_conn

    return get_db, override


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


class TestParseJsonField:
    """Tests for _parse_json_field helper."""

    def test_none_returns_empty_list(self):
        assert _parse_json_field(None) == []

    def test_list_passes_through(self):
        data = [{"key": "value"}]
        assert _parse_json_field(data) == data

    def test_dict_passes_through(self):
        data = {"key": "value"}
        assert _parse_json_field(data) == data

    def test_json_string_parsed(self):
        data = '[{"setting_name": "shared_buffers"}]'
        assert _parse_json_field(data) == [{"setting_name": "shared_buffers"}]

    def test_invalid_json_string_returns_empty(self):
        assert _parse_json_field("not valid json{{{") == []


class TestRowToPlanDetail:
    """Tests for _row_to_plan_detail helper."""

    def test_converts_valid_row(self):
        plan_id = uuid.uuid4()
        run_id = uuid.uuid4()
        host_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        row = _make_plan_record(
            plan_id=plan_id,
            run_id=run_id,
            host_id=host_id,
            submission_time=now,
        )
        result = _row_to_plan_detail(row)

        assert result.id == plan_id
        assert result.run_id == run_id
        assert result.host_id == host_id
        assert result.status == PlanStatus.PENDING_APPROVAL
        assert result.risk_score == 35
        assert result.confidence_score == 0.85
        assert result.submission_time == now

    def test_handles_null_risk_score(self):
        row = _make_plan_record(risk_score=None, confidence_score=None)
        result = _row_to_plan_detail(row)
        assert result.risk_score == 0
        assert result.confidence_score == 0.0


# ---------------------------------------------------------------------------
# API endpoint tests: GET /api/v1/plans/ (list pending plans)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_plans_empty():
    """GET /api/v1/plans/ returns empty list when no pending plans exist."""
    mock_conn = MockConnection(records=[])
    # Override fetchrow for COUNT query
    mock_conn.fetchrow_responses = [MockRecord(total=0)]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/plans/")
            assert response.status_code == 200
            data = response.json()
            assert data["plans"] == []
            assert data["total"] == 0
            assert data["page"] == 1
            assert data["page_size"] == MAX_PAGE_SIZE
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_plans_returns_pending_plans():
    """GET /api/v1/plans/ returns pending plans ordered by submission_time."""
    time1 = datetime.now(timezone.utc) - timedelta(minutes=10)
    time2 = datetime.now(timezone.utc) - timedelta(minutes=5)

    plan1 = _make_plan_record(submission_time=time1)
    plan2 = _make_plan_record(submission_time=time2)

    mock_conn = MockConnection(records=[plan1, plan2])
    mock_conn.fetchrow_responses = [MockRecord(total=2)]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/plans/")
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 2
            assert len(data["plans"]) == 2
            # Verify plan structure
            plan = data["plans"][0]
            assert "id" in plan
            assert "proposed_changes" in plan
            assert "evidence_references" in plan
            assert "risk_score" in plan
            assert "uncertainty_explanation" in plan
            assert "rollback_instructions" in plan
            assert "submission_time" in plan
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_plans_pagination_clamped():
    """GET /api/v1/plans/?page_size=100 clamps to max 50."""
    mock_conn = MockConnection(records=[])
    mock_conn.fetchrow_responses = [MockRecord(total=0)]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/plans/?page_size=100")
            assert response.status_code == 200
            data = response.json()
            assert data["page_size"] == MAX_PAGE_SIZE
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_plans_custom_page():
    """GET /api/v1/plans/?page=2 returns second page."""
    mock_conn = MockConnection(records=[])
    mock_conn.fetchrow_responses = [MockRecord(total=55)]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/plans/?page=2")
            assert response.status_code == 200
            data = response.json()
            assert data["page"] == 2
            assert data["total"] == 55
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# API endpoint tests: GET /api/v1/plans/{plan_id} (get plan detail)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_plan_detail():
    """GET /api/v1/plans/{plan_id} returns full plan details."""
    plan_id = uuid.uuid4()
    plan = _make_plan_record(plan_id=plan_id, risk_score=42)

    mock_conn = MockConnection(records=[plan])
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/plans/{plan_id}")
            assert response.status_code == 200
            data = response.json()
            assert data["id"] == str(plan_id)
            assert data["risk_score"] == 42
            assert data["status"] == "pending_approval"
            assert "proposed_changes" in data
            assert "evidence_references" in data
            assert "rollback_instructions" in data
            assert "uncertainty_explanation" in data
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_plan_not_found():
    """GET /api/v1/plans/{plan_id} returns 404 for unknown plan."""
    mock_conn = MockConnection(records=[])
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/plans/{uuid.uuid4()}")
            assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# API endpoint tests: POST /api/v1/plans/{plan_id}/approve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_plan_success():
    """POST /api/v1/plans/{plan_id}/approve approves a pending plan."""
    plan_id = uuid.uuid4()
    plan = _make_plan_record(plan_id=plan_id, status="pending_approval")

    mock_conn = MockConnection(records=[plan])
    # First fetchrow: fetch plan for approval check
    # Second fetchrow: get updated status
    mock_conn.fetchrow_responses = [plan, MockRecord(status="approved")]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch(
                "backend.api.plans._forward_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ):
                response = await client.post(
                    f"/api/v1/plans/{plan_id}/approve",
                    json={"approved_by": "dba_admin"},
                )
                assert response.status_code == 200
                data = response.json()
                assert data["plan_id"] == str(plan_id)
                assert data["approved_by"] == "dba_admin"
                assert "approved_at" in data
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_approve_plan_not_found():
    """POST /api/v1/plans/{plan_id}/approve returns 404 for unknown plan."""
    mock_conn = MockConnection(records=[])
    mock_conn.fetchrow_responses = [None]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/plans/{uuid.uuid4()}/approve",
                json={"approved_by": "dba_admin"},
            )
            assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_approve_plan_not_pending():
    """POST /api/v1/plans/{plan_id}/approve returns 409 for non-pending plan."""
    plan_id = uuid.uuid4()
    plan = _make_plan_record(plan_id=plan_id, status="approved")

    mock_conn = MockConnection(records=[plan])
    mock_conn.fetchrow_responses = [plan]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/plans/{plan_id}/approve",
                json={"approved_by": "dba_admin"},
            )
            assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_approve_plan_missing_identity():
    """POST /api/v1/plans/{plan_id}/approve returns 422 when approved_by is missing."""
    mock_conn = MockConnection(records=[])
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/plans/{uuid.uuid4()}/approve",
                json={},
            )
            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_approve_plan_records_audit_log():
    """POST /api/v1/plans/{plan_id}/approve records approval in audit log."""
    plan_id = uuid.uuid4()
    plan = _make_plan_record(plan_id=plan_id, status="pending_approval")

    mock_conn = MockConnection(records=[plan])
    mock_conn.fetchrow_responses = [plan, MockRecord(status="approved")]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            mock_audit_logger = MagicMock()
            mock_audit_logger.log = AsyncMock()

            with patch(
                "backend.api.plans._forward_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ), patch(
                "backend.services.audit_logger.get_audit_logger",
                return_value=mock_audit_logger,
            ):
                response = await client.post(
                    f"/api/v1/plans/{plan_id}/approve",
                    json={"approved_by": "senior_dba"},
                )
                assert response.status_code == 200

                # Verify audit logger was called
                mock_audit_logger.log.assert_called_once()
                call_kwargs = mock_audit_logger.log.call_args[1]
                assert call_kwargs["actor_type"] == "human"
                assert call_kwargs["actor_name"] == "senior_dba"
                assert call_kwargs["action_type"] == "plan_approved"
                assert call_kwargs["result"] == "success"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_approve_plan_forwarding_failure():
    """POST /api/v1/plans/{plan_id}/approve handles forwarding failure gracefully."""
    plan_id = uuid.uuid4()
    plan = _make_plan_record(plan_id=plan_id, status="pending_approval")

    mock_conn = MockConnection(records=[plan])
    mock_conn.fetchrow_responses = [
        plan,  # First: get plan for approval
        MockRecord(status="forwarding_failed"),  # Second: get updated status
    ]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            mock_audit_logger = MagicMock()
            mock_audit_logger.log = AsyncMock()

            with patch(
                "backend.api.plans._forward_with_retry",
                new_callable=AsyncMock,
                return_value=False,
            ), patch(
                "backend.services.audit_logger.get_audit_logger",
                return_value=mock_audit_logger,
            ):
                response = await client.post(
                    f"/api/v1/plans/{plan_id}/approve",
                    json={"approved_by": "dba_admin"},
                )
                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "forwarding_failed"
                assert "failed" in data["message"].lower()
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# API endpoint tests: POST /api/v1/plans/{plan_id}/reject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_plan_success():
    """POST /api/v1/plans/{plan_id}/reject rejects a plan with valid reason."""
    plan_id = uuid.uuid4()
    plan = _make_plan_record(plan_id=plan_id, status="pending_approval")

    mock_conn = MockConnection(records=[plan])
    mock_conn.fetchrow_responses = [plan]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            mock_audit_logger = MagicMock()
            mock_audit_logger.log = AsyncMock()

            with patch(
                "backend.services.audit_logger.get_audit_logger",
                return_value=mock_audit_logger,
            ), patch(
                "backend.db.redis_manager.get_redis_client",
                return_value=None,
            ):
                response = await client.post(
                    f"/api/v1/plans/{plan_id}/reject",
                    json={
                        "rejected_by": "dba_admin",
                        "reason": "Risk is too high for this workload pattern",
                    },
                )
                assert response.status_code == 200
                data = response.json()
                assert data["plan_id"] == str(plan_id)
                assert data["status"] == "rejected"
                assert data["rejected_by"] == "dba_admin"
                assert data["reason"] == "Risk is too high for this workload pattern"
                assert "rejected_at" in data
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_reject_plan_reason_too_short():
    """POST /api/v1/plans/{plan_id}/reject returns 422 for reason < 10 chars."""
    plan_id = uuid.uuid4()
    plan = _make_plan_record(plan_id=plan_id, status="pending_approval")

    mock_conn = MockConnection(records=[plan])
    mock_conn.fetchrow_responses = [plan]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/plans/{plan_id}/reject",
                json={
                    "rejected_by": "dba_admin",
                    "reason": "too risky",  # 9 chars
                },
            )
            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_reject_plan_reason_whitespace_trimmed():
    """POST /api/v1/plans/{plan_id}/reject trims whitespace before checking length."""
    plan_id = uuid.uuid4()
    plan = _make_plan_record(plan_id=plan_id, status="pending_approval")

    mock_conn = MockConnection(records=[plan])
    mock_conn.fetchrow_responses = [plan]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/plans/{plan_id}/reject",
                json={
                    "rejected_by": "dba_admin",
                    "reason": "   short   ",  # "short" is 5 chars trimmed
                },
            )
            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_reject_plan_reason_exactly_10_chars():
    """POST /api/v1/plans/{plan_id}/reject accepts reason of exactly 10 chars trimmed."""
    plan_id = uuid.uuid4()
    plan = _make_plan_record(plan_id=plan_id, status="pending_approval")

    mock_conn = MockConnection(records=[plan])
    mock_conn.fetchrow_responses = [plan]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            mock_audit_logger = MagicMock()
            mock_audit_logger.log = AsyncMock()

            with patch(
                "backend.services.audit_logger.get_audit_logger",
                return_value=mock_audit_logger,
            ), patch(
                "backend.db.redis_manager.get_redis_client",
                return_value=None,
            ):
                response = await client.post(
                    f"/api/v1/plans/{plan_id}/reject",
                    json={
                        "rejected_by": "dba_admin",
                        "reason": "1234567890",  # Exactly 10 chars
                    },
                )
                assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_reject_plan_not_found():
    """POST /api/v1/plans/{plan_id}/reject returns 404 for unknown plan."""
    mock_conn = MockConnection(records=[])
    mock_conn.fetchrow_responses = [None]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/plans/{uuid.uuid4()}/reject",
                json={
                    "rejected_by": "dba_admin",
                    "reason": "This plan is not suitable for production",
                },
            )
            assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_reject_plan_not_pending():
    """POST /api/v1/plans/{plan_id}/reject returns 409 for non-pending plan."""
    plan_id = uuid.uuid4()
    plan = _make_plan_record(plan_id=plan_id, status="rejected")

    mock_conn = MockConnection(records=[plan])
    mock_conn.fetchrow_responses = [plan]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/plans/{plan_id}/reject",
                json={
                    "rejected_by": "dba_admin",
                    "reason": "This plan is not suitable for production",
                },
            )
            assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_reject_plan_records_audit_log():
    """POST /api/v1/plans/{plan_id}/reject records rejection in audit log."""
    plan_id = uuid.uuid4()
    plan = _make_plan_record(plan_id=plan_id, status="pending_approval")

    mock_conn = MockConnection(records=[plan])
    mock_conn.fetchrow_responses = [plan]
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            mock_audit_logger = MagicMock()
            mock_audit_logger.log = AsyncMock()

            with patch(
                "backend.services.audit_logger.get_audit_logger",
                return_value=mock_audit_logger,
            ), patch(
                "backend.db.redis_manager.get_redis_client",
                return_value=None,
            ):
                response = await client.post(
                    f"/api/v1/plans/{plan_id}/reject",
                    json={
                        "rejected_by": "senior_dba",
                        "reason": "Insufficient evidence for this change",
                    },
                )
                assert response.status_code == 200

                # Verify audit logger was called
                mock_audit_logger.log.assert_called_once()
                call_kwargs = mock_audit_logger.log.call_args[1]
                assert call_kwargs["actor_type"] == "human"
                assert call_kwargs["actor_name"] == "senior_dba"
                assert call_kwargs["action_type"] == "plan_rejected"
                assert call_kwargs["result"] == "success"
                assert call_kwargs["result_reason"] == "Insufficient evidence for this change"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_reject_plan_missing_fields():
    """POST /api/v1/plans/{plan_id}/reject returns 422 when fields are missing."""
    mock_conn = MockConnection(records=[])
    dep, override = _override_db(mock_conn)

    app.dependency_overrides[dep] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Missing rejected_by
            response = await client.post(
                f"/api/v1/plans/{uuid.uuid4()}/reject",
                json={"reason": "This is a valid reason"},
            )
            assert response.status_code == 422

            # Missing reason
            response = await client.post(
                f"/api/v1/plans/{uuid.uuid4()}/reject",
                json={"rejected_by": "dba_admin"},
            )
            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()
