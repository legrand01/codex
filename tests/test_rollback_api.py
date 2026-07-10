"""
Tests for the Rollback API endpoints and rollback service logic.

Tests cover:
- Rollback eligibility validation (status checks)
- Rollback instructions validation
- Rollback initiation endpoint (success and failure paths)
- Rollback status endpoint
- Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from backend.models.enums import PlanStatus
from backend.services.rollback_service import (
    ROLLBACK_ELIGIBLE_STATUSES,
    RollbackEligibilityError,
    RollbackInstructionsError,
    RollbackStatus,
    RollbackTimeoutError,
    execute_rollback,
    validate_rollback_eligibility,
    validate_rollback_instructions,
)

# ============================================================
# Unit tests for validate_rollback_eligibility
# ============================================================


class TestValidateRollbackEligibility:
    """Tests for rollback eligibility validation (Req 5.4)."""

    def test_applied_status_is_eligible(self):
        """Plans with 'applied' status should be eligible for rollback."""
        # Should not raise
        validate_rollback_eligibility(PlanStatus.APPLIED)

    def test_rollback_failed_status_is_eligible(self):
        """Plans with 'rollback_failed' status should be eligible for retry."""
        # Should not raise
        validate_rollback_eligibility(PlanStatus.ROLLBACK_FAILED)

    def test_rolled_back_status_is_prevented(self):
        """Plans with 'rolled_back' status cannot be rolled back again."""
        with pytest.raises(RollbackEligibilityError) as exc_info:
            validate_rollback_eligibility(PlanStatus.ROLLED_BACK)
        assert "already been rolled back" in str(exc_info.value)

    def test_pending_approval_status_is_rejected(self):
        """Plans with 'pending_approval' status are not eligible."""
        with pytest.raises(RollbackEligibilityError) as exc_info:
            validate_rollback_eligibility(PlanStatus.PENDING_APPROVAL)
        assert "not eligible" in str(exc_info.value)

    def test_approved_status_is_rejected(self):
        """Plans with 'approved' status are not eligible."""
        with pytest.raises(RollbackEligibilityError):
            validate_rollback_eligibility(PlanStatus.APPROVED)

    def test_rejected_status_is_rejected(self):
        """Plans with 'rejected' status are not eligible."""
        with pytest.raises(RollbackEligibilityError):
            validate_rollback_eligibility(PlanStatus.REJECTED)

    def test_blocked_status_is_rejected(self):
        """Plans with 'blocked' status are not eligible."""
        with pytest.raises(RollbackEligibilityError):
            validate_rollback_eligibility(PlanStatus.BLOCKED)

    def test_dry_run_passed_status_is_rejected(self):
        """Plans with 'dry_run_passed' status are not eligible."""
        with pytest.raises(RollbackEligibilityError):
            validate_rollback_eligibility(PlanStatus.DRY_RUN_PASSED)

    def test_all_ineligible_statuses_are_rejected(self):
        """All statuses not in ROLLBACK_ELIGIBLE_STATUSES should be rejected."""
        for status in PlanStatus:
            if status in ROLLBACK_ELIGIBLE_STATUSES:
                # These should pass
                validate_rollback_eligibility(status)
            else:
                # These should raise
                with pytest.raises(RollbackEligibilityError):
                    validate_rollback_eligibility(status)


# ============================================================
# Unit tests for validate_rollback_instructions
# ============================================================


class TestValidateRollbackInstructions:
    """Tests for rollback instructions validation (Req 5.5)."""

    def test_valid_instructions(self):
        """Valid instructions list should be returned as-is."""
        instructions = [
            {"setting": "work_mem", "restore_value": "4MB"},
            {"setting": "shared_buffers", "restore_value": "128MB"},
        ]
        result = validate_rollback_instructions(instructions)
        assert result == instructions

    def test_none_instructions_raises(self):
        """None instructions should raise RollbackInstructionsError."""
        with pytest.raises(RollbackInstructionsError) as exc_info:
            validate_rollback_instructions(None)
        assert "missing" in str(exc_info.value)

    def test_empty_list_raises(self):
        """Empty list should raise RollbackInstructionsError."""
        with pytest.raises(RollbackInstructionsError) as exc_info:
            validate_rollback_instructions([])
        assert "empty" in str(exc_info.value)

    def test_non_list_raises(self):
        """Non-list input should raise RollbackInstructionsError."""
        with pytest.raises(RollbackInstructionsError) as exc_info:
            validate_rollback_instructions("not a list")
        assert "not in expected format" in str(exc_info.value)

    def test_non_dict_items_raises(self):
        """List containing non-dict items should raise."""
        with pytest.raises(RollbackInstructionsError) as exc_info:
            validate_rollback_instructions(["step1", "step2"])
        assert "not a valid object" in str(exc_info.value)

    def test_single_instruction_valid(self):
        """Single instruction is valid."""
        instructions = [{"setting": "work_mem", "restore_value": "4MB"}]
        result = validate_rollback_instructions(instructions)
        assert result == instructions


# ============================================================
# Unit tests for execute_rollback
# ============================================================


class TestExecuteRollback:
    """Tests for rollback execution (Req 5.1, 5.2)."""

    @pytest.mark.asyncio
    async def test_successful_rollback(self):
        """Rollback should complete successfully with valid instructions."""
        plan_id = uuid4()
        instructions = [
            {"setting": "work_mem", "restore_value": "4MB"},
            {"setting": "shared_buffers", "restore_value": "128MB"},
        ]
        result = await execute_rollback(plan_id, instructions)
        assert result.status == RollbackStatus.COMPLETED
        assert result.plan_id == plan_id
        assert result.started_at is not None
        assert result.completed_at is not None
        assert result.error is None

    @pytest.mark.asyncio
    async def test_rollback_timeout(self):
        """Rollback should raise timeout error if it exceeds timeout."""
        plan_id = uuid4()
        # Use very short timeout to trigger timeout
        instructions = [{"setting": "work_mem", "restore_value": "4MB"}]

        async def slow_execute(*args, **kwargs):
            await asyncio.sleep(10)

        # Patch _execute_instructions to simulate slow execution
        with patch(
            "backend.services.rollback_service._execute_instructions",
            side_effect=slow_execute,
        ):
            with pytest.raises(RollbackTimeoutError):
                await execute_rollback(plan_id, instructions, timeout=0.01)

    @pytest.mark.asyncio
    async def test_rollback_result_has_timestamps(self):
        """Rollback result should have valid start and end timestamps."""
        plan_id = uuid4()
        instructions = [{"setting": "work_mem", "restore_value": "4MB"}]
        result = await execute_rollback(plan_id, instructions)
        assert result.started_at <= result.completed_at

    @pytest.mark.asyncio
    async def test_rollback_result_to_dict(self):
        """RollbackResult.to_dict() should produce serializable dict."""
        plan_id = uuid4()
        instructions = [{"setting": "work_mem", "restore_value": "4MB"}]
        result = await execute_rollback(plan_id, instructions)
        result_dict = result.to_dict()
        assert result_dict["plan_id"] == str(plan_id)
        assert result_dict["status"] == "completed"
        assert result_dict["started_at"] is not None
        assert result_dict["completed_at"] is not None


# ============================================================
# Integration tests for Rollback API endpoints
# ============================================================


class TestRollbackAPIEndpoints:
    """Tests for rollback API endpoints (Req 5.1-5.6)."""

    @pytest.mark.asyncio
    async def test_initiate_rollback_plan_not_found(self, client):
        """POST /api/v1/rollback/{plan_id} returns 404 for non-existent plan."""
        plan_id = uuid4()
        with patch("backend.api.rollback._get_plan", return_value=None):
            response = await client.post(f"/api/v1/rollback/{plan_id}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_initiate_rollback_ineligible_status(self, client):
        """POST /api/v1/rollback/{plan_id} returns 400 for ineligible status."""
        plan_id = uuid4()
        mock_plan = {
            "id": plan_id,
            "run_id": uuid4(),
            "host_id": uuid4(),
            "status": PlanStatus.PENDING_APPROVAL.value,
            "rollback_instructions": [{"setting": "work_mem", "restore_value": "4MB"}],
            "applied_at": None,
            "rolled_back_at": None,
        }
        with patch("backend.api.rollback._get_plan", return_value=mock_plan):
            with patch("backend.api.rollback.get_audit_logger") as mock_logger:
                mock_logger.return_value = AsyncMock()
                mock_logger.return_value.log = AsyncMock()
                response = await client.post(f"/api/v1/rollback/{plan_id}")
        assert response.status_code == 400
        assert "not eligible" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_initiate_rollback_rolled_back_status(self, client):
        """POST /api/v1/rollback/{plan_id} returns 400 for already rolled-back plan."""
        plan_id = uuid4()
        mock_plan = {
            "id": plan_id,
            "run_id": uuid4(),
            "host_id": uuid4(),
            "status": PlanStatus.ROLLED_BACK.value,
            "rollback_instructions": [{"setting": "work_mem", "restore_value": "4MB"}],
            "applied_at": None,
            "rolled_back_at": datetime.now(timezone.utc),
        }
        with patch("backend.api.rollback._get_plan", return_value=mock_plan):
            with patch("backend.api.rollback.get_audit_logger") as mock_logger:
                mock_logger.return_value = AsyncMock()
                mock_logger.return_value.log = AsyncMock()
                response = await client.post(f"/api/v1/rollback/{plan_id}")
        assert response.status_code == 400
        assert "already been rolled back" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_initiate_rollback_missing_instructions(self, client):
        """POST /api/v1/rollback/{plan_id} returns 400 for missing instructions."""
        plan_id = uuid4()
        mock_plan = {
            "id": plan_id,
            "run_id": uuid4(),
            "host_id": uuid4(),
            "status": PlanStatus.APPLIED.value,
            "rollback_instructions": None,
            "applied_at": datetime.now(timezone.utc),
            "rolled_back_at": None,
        }
        with patch("backend.api.rollback._get_plan", return_value=mock_plan):
            with patch("backend.api.rollback.get_audit_logger") as mock_logger:
                mock_logger.return_value = AsyncMock()
                mock_logger.return_value.log = AsyncMock()
                response = await client.post(f"/api/v1/rollback/{plan_id}")
        assert response.status_code == 400
        assert "missing" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_initiate_rollback_empty_instructions(self, client):
        """POST /api/v1/rollback/{plan_id} returns 400 for empty instructions."""
        plan_id = uuid4()
        mock_plan = {
            "id": plan_id,
            "run_id": uuid4(),
            "host_id": uuid4(),
            "status": PlanStatus.APPLIED.value,
            "rollback_instructions": [],
            "applied_at": datetime.now(timezone.utc),
            "rolled_back_at": None,
        }
        with patch("backend.api.rollback._get_plan", return_value=mock_plan):
            with patch("backend.api.rollback.get_audit_logger") as mock_logger:
                mock_logger.return_value = AsyncMock()
                mock_logger.return_value.log = AsyncMock()
                response = await client.post(f"/api/v1/rollback/{plan_id}")
        assert response.status_code == 400
        assert "empty" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_initiate_rollback_success(self, client):
        """POST /api/v1/rollback/{plan_id} returns success for valid rollback."""
        plan_id = uuid4()
        host_id = uuid4()
        mock_plan = {
            "id": plan_id,
            "run_id": uuid4(),
            "host_id": host_id,
            "status": PlanStatus.APPLIED.value,
            "rollback_instructions": [
                {"setting": "work_mem", "restore_value": "4MB"},
            ],
            "applied_at": datetime.now(timezone.utc),
            "rolled_back_at": None,
        }
        with patch("backend.api.rollback._get_plan", return_value=mock_plan):
            with patch("backend.api.rollback.get_audit_logger") as mock_logger:
                mock_audit = AsyncMock()
                mock_logger.return_value = mock_audit
                with patch("backend.api.rollback._update_plan_status") as mock_update:
                    mock_update.return_value = None
                    response = await client.post(f"/api/v1/rollback/{plan_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["plan_id"] == str(plan_id)
        assert "successfully" in data["message"]

    @pytest.mark.asyncio
    async def test_initiate_rollback_from_rollback_failed_status(self, client):
        """POST /api/v1/rollback/{plan_id} allows retry from rollback_failed status."""
        plan_id = uuid4()
        mock_plan = {
            "id": plan_id,
            "run_id": uuid4(),
            "host_id": uuid4(),
            "status": PlanStatus.ROLLBACK_FAILED.value,
            "rollback_instructions": [
                {"setting": "work_mem", "restore_value": "4MB"},
            ],
            "applied_at": datetime.now(timezone.utc),
            "rolled_back_at": None,
        }
        with patch("backend.api.rollback._get_plan", return_value=mock_plan):
            with patch("backend.api.rollback.get_audit_logger") as mock_logger:
                mock_logger.return_value = AsyncMock()
                with patch("backend.api.rollback._update_plan_status") as mock_update:
                    mock_update.return_value = None
                    response = await client.post(f"/api/v1/rollback/{plan_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_get_rollback_status_not_found(self, client):
        """GET /api/v1/rollback/{plan_id}/status returns 404 for missing plan."""
        plan_id = uuid4()
        with patch("backend.api.rollback._get_plan", return_value=None):
            response = await client.get(f"/api/v1/rollback/{plan_id}/status")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_rollback_status_applied(self, client):
        """GET /api/v1/rollback/{plan_id}/status returns pending for applied plans."""
        plan_id = uuid4()
        applied_at = datetime.now(timezone.utc)
        mock_plan = {
            "id": plan_id,
            "run_id": uuid4(),
            "host_id": uuid4(),
            "status": PlanStatus.APPLIED.value,
            "rollback_instructions": [{"setting": "work_mem", "restore_value": "4MB"}],
            "applied_at": applied_at,
            "rolled_back_at": None,
        }
        with patch("backend.api.rollback._get_plan", return_value=mock_plan):
            response = await client.get(f"/api/v1/rollback/{plan_id}/status")

        assert response.status_code == 200
        data = response.json()
        assert data["plan_status"] == "applied"
        assert data["rollback_status"] == "pending"

    @pytest.mark.asyncio
    async def test_get_rollback_status_completed(self, client):
        """GET /api/v1/rollback/{plan_id}/status returns completed for rolled-back."""
        plan_id = uuid4()
        rolled_back_at = datetime.now(timezone.utc)
        mock_plan = {
            "id": plan_id,
            "run_id": uuid4(),
            "host_id": uuid4(),
            "status": PlanStatus.ROLLED_BACK.value,
            "rollback_instructions": [{"setting": "work_mem", "restore_value": "4MB"}],
            "applied_at": datetime.now(timezone.utc),
            "rolled_back_at": rolled_back_at,
        }
        with patch("backend.api.rollback._get_plan", return_value=mock_plan):
            response = await client.get(f"/api/v1/rollback/{plan_id}/status")

        assert response.status_code == 200
        data = response.json()
        assert data["plan_status"] == "rolled_back"
        assert data["rollback_status"] == "completed"

    @pytest.mark.asyncio
    async def test_get_rollback_status_failed(self, client):
        """GET /api/v1/rollback/{plan_id}/status returns failed for rollback_failed."""
        plan_id = uuid4()
        mock_plan = {
            "id": plan_id,
            "run_id": uuid4(),
            "host_id": uuid4(),
            "status": PlanStatus.ROLLBACK_FAILED.value,
            "rollback_instructions": [{"setting": "work_mem", "restore_value": "4MB"}],
            "applied_at": datetime.now(timezone.utc),
            "rolled_back_at": None,
        }
        with patch("backend.api.rollback._get_plan", return_value=mock_plan):
            response = await client.get(f"/api/v1/rollback/{plan_id}/status")

        assert response.status_code == 200
        data = response.json()
        assert data["plan_status"] == "rollback_failed"
        assert data["rollback_status"] == "failed"

    @pytest.mark.asyncio
    async def test_initiate_rollback_execution_failure(self, client):
        """POST /api/v1/rollback/{plan_id} handles execution failure gracefully."""
        plan_id = uuid4()
        mock_plan = {
            "id": plan_id,
            "run_id": uuid4(),
            "host_id": uuid4(),
            "status": PlanStatus.APPLIED.value,
            "rollback_instructions": [
                {"setting": "work_mem", "restore_value": "4MB"},
            ],
            "applied_at": datetime.now(timezone.utc),
            "rolled_back_at": None,
        }
        with patch("backend.api.rollback._get_plan", return_value=mock_plan):
            with patch("backend.api.rollback.get_audit_logger") as mock_logger:
                mock_logger.return_value = AsyncMock()
                with patch("backend.api.rollback._update_plan_status") as mock_update:
                    mock_update.return_value = None
                    with patch(
                        "backend.api.rollback.execute_rollback",
                        side_effect=RuntimeError("Connection refused"),
                    ):
                        response = await client.post(f"/api/v1/rollback/{plan_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        assert "Connection refused" in data["error"]
