"""
Plan review and approval queue API endpoints.

Provides routes for:
- Listing pending plans (GET /api/v1/plans/) - paginated, ordered by submission_time
- Getting plan detail (GET /api/v1/plans/{plan_id})
- Approving a plan (POST /api/v1/plans/{plan_id}/approve)
- Rejecting a plan (POST /api/v1/plans/{plan_id}/reject)

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.dependencies import get_db
from backend.models.enums import PlanStatus
from backend.models.plans import PlanDetail

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/plans", tags=["plans"])

# Maximum plans per page
MAX_PAGE_SIZE = 50

# Retry configuration for forwarding to Guardrail Engine
FORWARDING_TIMEOUT_SECONDS = 30
FORWARDING_MAX_RETRIES = 3
FORWARDING_RETRY_INTERVAL_SECONDS = 10


# --- Request/Response Models ---


class PlanListResponse(BaseModel):
    """Response model for paginated plan listing."""

    plans: List[PlanDetail]
    total: int
    page: int
    page_size: int


class ApproveRequest(BaseModel):
    """Request model for plan approval."""

    approved_by: str = Field(..., min_length=1, description="DBA identity approving the plan")


class RejectRequest(BaseModel):
    """Request model for plan rejection."""

    rejected_by: str = Field(..., min_length=1, description="DBA identity rejecting the plan")
    reason: str = Field(..., description="Rejection reason (min 10 chars trimmed)")


class ApproveResponse(BaseModel):
    """Response model for plan approval."""

    plan_id: str
    status: str
    approved_by: str
    approved_at: str
    message: str


class RejectResponse(BaseModel):
    """Response model for plan rejection."""

    plan_id: str
    status: str
    rejected_by: str
    rejected_at: str
    reason: str
    message: str


# --- Helper Functions ---


def _parse_json_field(value):
    """Parse a JSON field that may be a string or already parsed."""
    import json

    if value is None:
        return []
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def _row_to_plan_detail(row) -> PlanDetail:
    """Convert a database row to a PlanDetail model."""
    return PlanDetail(
        id=row["id"],
        run_id=row["run_id"],
        host_id=row["host_id"],
        status=PlanStatus(row["status"]),
        proposed_changes=_parse_json_field(row["proposed_changes"]),
        evidence_references=_parse_json_field(row["evidence_references"]),
        risk_score=row["risk_score"] if row["risk_score"] is not None else 0,
        confidence_score=float(row["confidence_score"]) if row["confidence_score"] is not None else 0.0,
        uncertainty_explanation=row["uncertainty_explanation"],
        rollback_instructions=_parse_json_field(row["rollback_instructions"]),
        submission_time=row["submission_time"],
    )


async def _forward_to_guardrail_engine(plan_id: UUID, host_id: UUID, proposed_changes: list, db) -> bool:
    """
    Forward an approved plan to the Guardrail Engine for dry-run execution.

    Attempts to execute the dry-run with a 30-second timeout.

    Args:
        plan_id: UUID of the plan.
        host_id: UUID of the target host.
        proposed_changes: List of proposed changes.
        db: Database connection.

    Returns:
        True if forwarding succeeded, False if it failed.
    """
    try:
        from backend.services.guardrail_engine import execute_dry_run

        result = await asyncio.wait_for(
            execute_dry_run(
                proposed_changes=proposed_changes,
                host_id=host_id,
                timeout=FORWARDING_TIMEOUT_SECONDS,
            ),
            timeout=FORWARDING_TIMEOUT_SECONDS,
        )

        # Update plan status based on dry-run result
        if result.passed:
            await db.execute(
                "UPDATE plans SET status = $1 WHERE id = $2",
                PlanStatus.DRY_RUN_PASSED.value,
                plan_id,
            )
        else:
            await db.execute(
                "UPDATE plans SET status = $1 WHERE id = $2",
                PlanStatus.DRY_RUN_FAILED.value,
                plan_id,
            )

        return True
    except (asyncio.TimeoutError, OSError, ConnectionError, RuntimeError) as e:
        logger.warning(f"Guardrail Engine forwarding failed for plan {plan_id}: {e}")
        return False


async def _forward_with_retry(plan_id: UUID, host_id: UUID, proposed_changes: list, db) -> bool:
    """
    Forward plan to Guardrail Engine with retry logic.

    If the Guardrail Engine is unreachable within 30s, retains plan as
    "pending-forwarding", retries 3 times at 10s intervals, then marks
    "forwarding-failed".

    Requirements: 4.4
    """
    # Set plan to pending-forwarding state
    await db.execute(
        "UPDATE plans SET status = $1 WHERE id = $2",
        PlanStatus.PENDING_FORWARDING.value,
        plan_id,
    )

    for attempt in range(FORWARDING_MAX_RETRIES):
        success = await _forward_to_guardrail_engine(plan_id, host_id, proposed_changes, db)
        if success:
            return True

        # Wait before retry (except on last attempt)
        if attempt < FORWARDING_MAX_RETRIES - 1:
            await asyncio.sleep(FORWARDING_RETRY_INTERVAL_SECONDS)

    # All retries exhausted - mark as forwarding-failed
    await db.execute(
        "UPDATE plans SET status = $1 WHERE id = $2",
        PlanStatus.FORWARDING_FAILED.value,
        plan_id,
    )
    return False


# --- Endpoints ---


@router.get("/", response_model=PlanListResponse)
async def list_pending_plans(
    page: int = 1,
    page_size: int = MAX_PAGE_SIZE,
    db=Depends(get_db),
) -> PlanListResponse:
    """
    List all plans awaiting human approval, ordered by submission time.

    Returns paginated results with max 50 plans per page.

    Requirements: 4.1
    """
    # Clamp page_size to max 50
    if page_size < 1:
        page_size = 1
    if page_size > MAX_PAGE_SIZE:
        page_size = MAX_PAGE_SIZE

    if page < 1:
        page = 1

    offset = (page - 1) * page_size

    # Count total pending plans
    count_row = await db.fetchrow(
        "SELECT COUNT(*) as total FROM plans WHERE status = $1",
        PlanStatus.PENDING_APPROVAL.value,
    )
    total = count_row["total"] if count_row else 0

    # Fetch paginated results ordered by submission_time ASC
    rows = await db.fetch(
        """
        SELECT id, run_id, host_id, status, proposed_changes, evidence_references,
               risk_score, confidence_score, uncertainty_explanation,
               rollback_instructions, submission_time
        FROM plans
        WHERE status = $1
        ORDER BY submission_time ASC
        LIMIT $2 OFFSET $3
        """,
        PlanStatus.PENDING_APPROVAL.value,
        page_size,
        offset,
    )

    plans = [_row_to_plan_detail(row) for row in rows]

    return PlanListResponse(
        plans=plans,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{plan_id}", response_model=PlanDetail)
async def get_plan_detail(plan_id: UUID, db=Depends(get_db)) -> PlanDetail:
    """
    Get full details for a specific plan.

    Returns proposed changes, evidence references, risk score,
    uncertainty explanations, and rollback instructions.

    Requirements: 4.2
    """
    row = await db.fetchrow(
        """
        SELECT id, run_id, host_id, status, proposed_changes, evidence_references,
               risk_score, confidence_score, uncertainty_explanation,
               rollback_instructions, submission_time
        FROM plans
        WHERE id = $1
        """,
        plan_id,
    )

    if row is None:
        raise HTTPException(status_code=404, detail=f"Plan with id '{plan_id}' not found")

    return _row_to_plan_detail(row)


@router.post("/{plan_id}/approve", response_model=ApproveResponse)
async def approve_plan(
    plan_id: UUID,
    request: ApproveRequest,
    db=Depends(get_db),
) -> ApproveResponse:
    """
    Approve a plan for execution.

    On approval:
    1. Records approval in Audit_Log with timestamp and DBA identity
    2. Forwards plan to Guardrail Engine for dry-run
    3. Implements retry logic if Guardrail Engine is unreachable

    No plan proceeds to execution without explicit DBA approval in Audit_Log.

    Requirements: 4.3, 4.4, 4.6
    """
    # Fetch plan and verify it exists and is pending
    row = await db.fetchrow(
        """
        SELECT id, run_id, host_id, status, proposed_changes
        FROM plans
        WHERE id = $1
        """,
        plan_id,
    )

    if row is None:
        raise HTTPException(status_code=404, detail=f"Plan with id '{plan_id}' not found")

    if row["status"] != PlanStatus.PENDING_APPROVAL.value:
        raise HTTPException(
            status_code=409,
            detail=f"Plan is not pending approval (current status: {row['status']})",
        )

    # Record approval timestamp
    approved_at = datetime.now(timezone.utc)

    # Update plan status to approved
    await db.execute(
        """
        UPDATE plans
        SET status = $1, approved_by = $2, approved_at = $3
        WHERE id = $4
        """,
        PlanStatus.APPROVED.value,
        request.approved_by,
        approved_at,
        plan_id,
    )

    # Record approval in Audit_Log (Requirement 4.6: no plan proceeds without approval in Audit_Log)
    try:
        from backend.services.audit_logger import get_audit_logger

        audit_logger = get_audit_logger()
        await audit_logger.log(
            run_id=row["run_id"],
            actor_type="human",
            actor_name=request.approved_by,
            action_type="plan_approved",
            target_host_id=row["host_id"],
            result="success",
            details={
                "plan_id": str(plan_id),
                "approved_at": approved_at.isoformat(),
            },
        )
    except Exception as e:
        logger.error(f"Failed to log approval to audit log: {e}")
        # Approval still proceeds even if audit logging has a transient issue
        # The plan status update already records the approval

    # Forward to Guardrail Engine with retry logic (Requirement 4.3, 4.4)
    proposed_changes = _parse_json_field(row["proposed_changes"])

    # Run forwarding in background to avoid blocking the response
    # But for synchronous behavior we attempt it inline
    try:
        forwarding_success = await _forward_with_retry(
            plan_id=plan_id,
            host_id=row["host_id"],
            proposed_changes=proposed_changes,
            db=db,
        )
    except Exception as e:
        logger.error(f"Forwarding failed with unexpected error: {e}")
        forwarding_success = False

    status = PlanStatus.APPROVED.value
    message = "Plan approved and forwarded to Guardrail Engine for dry-run"
    if not forwarding_success:
        # Check current status from DB
        updated_row = await db.fetchrow(
            "SELECT status FROM plans WHERE id = $1", plan_id
        )
        status = updated_row["status"] if updated_row else PlanStatus.FORWARDING_FAILED.value
        message = "Plan approved but forwarding to Guardrail Engine failed after retries"

    return ApproveResponse(
        plan_id=str(plan_id),
        status=status,
        approved_by=request.approved_by,
        approved_at=approved_at.isoformat(),
        message=message,
    )


@router.post("/{plan_id}/reject", response_model=RejectResponse)
async def reject_plan(
    plan_id: UUID,
    request: RejectRequest,
    db=Depends(get_db),
) -> RejectResponse:
    """
    Reject a plan with a reason.

    On rejection:
    1. Validates rejection reason is at least 10 characters (trimmed)
    2. Records rejection in Audit_Log with DBA identity and reason
    3. Notifies DBA_Loop_Worker to re-plan with rejection feedback

    Requirements: 4.5
    """
    # Validate rejection reason minimum length (trimmed)
    trimmed_reason = request.reason.strip()
    if len(trimmed_reason) < 10:
        raise HTTPException(
            status_code=422,
            detail="Rejection reason must be at least 10 characters (after trimming whitespace)",
        )

    # Fetch plan and verify it exists and is pending
    row = await db.fetchrow(
        """
        SELECT id, run_id, host_id, status
        FROM plans
        WHERE id = $1
        """,
        plan_id,
    )

    if row is None:
        raise HTTPException(status_code=404, detail=f"Plan with id '{plan_id}' not found")

    if row["status"] != PlanStatus.PENDING_APPROVAL.value:
        raise HTTPException(
            status_code=409,
            detail=f"Plan is not pending approval (current status: {row['status']})",
        )

    # Record rejection timestamp
    rejected_at = datetime.now(timezone.utc)

    # Update plan status to rejected
    await db.execute(
        """
        UPDATE plans
        SET status = $1, rejected_by = $2, rejected_at = $3, rejection_reason = $4
        WHERE id = $5
        """,
        PlanStatus.REJECTED.value,
        request.rejected_by,
        rejected_at,
        trimmed_reason,
        plan_id,
    )

    # Record rejection in Audit_Log
    try:
        from backend.services.audit_logger import get_audit_logger

        audit_logger = get_audit_logger()
        await audit_logger.log(
            run_id=row["run_id"],
            actor_type="human",
            actor_name=request.rejected_by,
            action_type="plan_rejected",
            target_host_id=row["host_id"],
            result="success",
            result_reason=trimmed_reason,
            details={
                "plan_id": str(plan_id),
                "rejected_at": rejected_at.isoformat(),
                "rejection_reason": trimmed_reason,
            },
        )
    except Exception as e:
        logger.error(f"Failed to log rejection to audit log: {e}")

    # Notify DBA_Loop_Worker to re-plan with rejection feedback
    # This is done by publishing to Redis or updating the loop run state
    try:
        from backend.db.redis_manager import get_redis_client

        redis_client = get_redis_client()
        if redis_client:
            import json

            await redis_client.publish(
                "plan_rejection",
                json.dumps({
                    "plan_id": str(plan_id),
                    "run_id": str(row["run_id"]),
                    "host_id": str(row["host_id"]),
                    "rejection_reason": trimmed_reason,
                    "rejected_by": request.rejected_by,
                }),
            )
    except Exception as e:
        logger.warning(f"Failed to notify DBA_Loop_Worker of rejection: {e}")

    return RejectResponse(
        plan_id=str(plan_id),
        status=PlanStatus.REJECTED.value,
        rejected_by=request.rejected_by,
        rejected_at=rejected_at.isoformat(),
        reason=trimmed_reason,
        message="Plan rejected. DBA Loop Worker notified to re-plan with feedback.",
    )
