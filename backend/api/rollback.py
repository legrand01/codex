"""
Rollback API endpoints for the Control Plane.

Provides:
- POST /api/v1/rollback/{plan_id} - Initiate rollback for an applied plan
- GET /api/v1/rollback/{plan_id}/status - Get rollback status for a plan

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.config import settings
from backend.db.pool import get_pool
from backend.models.enums import PlanStatus
from backend.security import Principal, require_roles
from backend.services.audit_logger import get_audit_logger
from backend.services.rollback_service import (
    RollbackEligibilityError,
    RollbackInstructionsError,
    RollbackStatus,
    RollbackTimeoutError,
    execute_rollback,
    validate_rollback_eligibility,
    validate_rollback_instructions,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/rollback", tags=["rollback"])


class RollbackResponse(BaseModel):
    """Response model for rollback initiation."""

    plan_id: str
    status: str
    message: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    failed_step: Optional[int] = None
    error: Optional[str] = None


class RollbackStatusResponse(BaseModel):
    """Response model for rollback status."""

    plan_id: str
    plan_status: str
    rollback_status: str
    applied_at: Optional[str] = None
    rolled_back_at: Optional[str] = None


async def _get_plan(plan_id: UUID, organization_id: Optional[UUID] = None) -> Optional[dict]:
    """Fetch a plan from the database by ID."""
    pool = get_pool()
    if pool is None:
        return None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, run_id, host_id, status, rollback_instructions,
                   pre_change_snapshot, apply_result, applied_at, rolled_back_at
            FROM plans
            WHERE id = $1
              AND ($2::uuid IS NULL OR organization_id = $2)
            """,
            plan_id,
            organization_id,
        )
    if row is None:
        return None
    return dict(row)


async def _update_plan_status(plan_id: UUID, new_status: PlanStatus) -> None:
    """Update the plan status in the database."""
    pool = get_pool()
    if pool is None:
        raise RuntimeError("Database pool not available")

    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        if new_status == PlanStatus.ROLLED_BACK:
            await conn.execute(
                """
                UPDATE plans SET status = $1, rolled_back_at = $2
                WHERE id = $3
                """,
                new_status.value,
                now,
                plan_id,
            )
        else:
            await conn.execute(
                """
                UPDATE plans SET status = $1 WHERE id = $2
                """,
                new_status.value,
                plan_id,
            )


@router.post("/{plan_id}", response_model=RollbackResponse)
async def initiate_rollback(
    plan_id: UUID,
    principal: Principal = Depends(require_roles("operator", "approver", "admin")),
) -> RollbackResponse:
    """
    Initiate rollback for an applied plan.

    Executes the rollback instructions stored with the original plan.
    The operation must complete or fail within 300 seconds.

    - Validates plan status is "applied" or "rollback_failed" (400 otherwise)
    - Validates rollback instructions exist and are parsable (400 otherwise)
    - On success: updates plan status to "rolled_back", records in Audit_Log
    - On failure: updates plan status to "rollback_failed", records failure in Audit_Log

    Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6
    """
    audit_logger = get_audit_logger()

    # Fetch the plan
    plan = await _get_plan(plan_id, principal.organization_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found.")

    plan_status = PlanStatus(plan["status"])
    host_id = plan.get("host_id")

    # Validate rollback eligibility (Req 5.4)
    try:
        validate_rollback_eligibility(plan_status)
    except RollbackEligibilityError as e:
        # Log rejection in audit log
        await audit_logger.log(
            run_id=plan.get("run_id"),
            actor_type="system",
            actor_name="rollback_service",
            action_type="rollback_rejected",
            target_host_id=host_id,
            result="blocked",
            result_reason=str(e),
            details={"plan_id": str(plan_id), "current_status": plan_status.value},
        )
        raise HTTPException(status_code=400, detail=str(e))

    # Validate rollback instructions (Req 5.5)
    try:
        instructions = validate_rollback_instructions(plan.get("rollback_instructions"))
    except RollbackInstructionsError as e:
        # Alert DBA and log rejection in audit log
        await audit_logger.log(
            run_id=plan.get("run_id"),
            actor_type="system",
            actor_name="rollback_service",
            action_type="rollback_rejected_invalid_instructions",
            target_host_id=host_id,
            result="failure",
            result_reason=str(e),
            details={"plan_id": str(plan_id)},
        )
        raise HTTPException(status_code=400, detail=str(e))

    # Execute rollback (Req 5.1, 5.2)
    try:
        pool = get_pool()
        if settings.require_live_target_rollback and pool is None:
            raise RuntimeError("Database pool not available")
        result = await execute_rollback(
            plan_id,
            instructions,
            host_id=host_id,
            pre_change_snapshot=plan.get("pre_change_snapshot"),
            control_pool=pool,
            backend_snapshot=(plan.get("apply_result") or {}).get("backend_snapshot")
            if isinstance(plan.get("apply_result"), dict)
            else None,
        )

        # Success: update plan status to "rolled_back" (Req 5.6)
        await _update_plan_status(plan_id, PlanStatus.ROLLED_BACK)

        # Record success in Audit_Log (Req 5.6)
        await audit_logger.log(
            run_id=plan.get("run_id"),
            actor_type="system",
            actor_name="rollback_service",
            action_type="rollback_completed",
            target_host_id=host_id,
            result="success",
            details={
                "plan_id": str(plan_id),
                "instructions_executed": len(instructions),
                "started_at": result.started_at.isoformat() if result.started_at else None,
                "completed_at": result.completed_at.isoformat() if result.completed_at else None,
            },
        )

        return RollbackResponse(
            plan_id=str(plan_id),
            status=RollbackStatus.COMPLETED.value,
            message="Rollback completed successfully.",
            started_at=result.started_at.isoformat() if result.started_at else None,
            completed_at=result.completed_at.isoformat() if result.completed_at else None,
        )

    except RollbackTimeoutError as e:
        # Timeout: mark as rollback_failed (Req 5.3)
        await _update_plan_status(plan_id, PlanStatus.ROLLBACK_FAILED)

        await audit_logger.log(
            run_id=plan.get("run_id"),
            actor_type="system",
            actor_name="rollback_service",
            action_type="rollback_failed",
            target_host_id=host_id,
            result="failure",
            result_reason=f"Rollback timed out: {str(e)}",
            details={"plan_id": str(plan_id), "error": "timeout"},
        )

        return RollbackResponse(
            plan_id=str(plan_id),
            status=RollbackStatus.FAILED.value,
            message="Rollback failed due to timeout.",
            error=str(e),
        )

    except Exception as e:
        # General failure: mark plan as rollback_failed, alert DBA (Req 5.3)
        await _update_plan_status(plan_id, PlanStatus.ROLLBACK_FAILED)

        await audit_logger.log(
            run_id=plan.get("run_id"),
            actor_type="system",
            actor_name="rollback_service",
            action_type="rollback_failed",
            target_host_id=host_id,
            result="failure",
            result_reason=f"Rollback execution error: {str(e)}",
            details={"plan_id": str(plan_id), "error": str(e)},
        )

        return RollbackResponse(
            plan_id=str(plan_id),
            status=RollbackStatus.FAILED.value,
            message="Rollback failed during execution.",
            error=str(e),
        )


@router.get("/{plan_id}/status", response_model=RollbackStatusResponse)
async def get_rollback_status(
    plan_id: UUID,
    principal: Principal = Depends(require_roles("viewer", "operator", "approver", "admin")),
) -> RollbackStatusResponse:
    """
    Get the rollback status for a plan.

    Returns the current plan status and related timestamps.
    The status is updated within 5 seconds of state transitions.

    Requirements: 5.2
    """
    plan = await _get_plan(plan_id, principal.organization_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found.")

    plan_status = PlanStatus(plan["status"])

    # Derive rollback status from plan status
    if plan_status == PlanStatus.ROLLED_BACK:
        rollback_status = RollbackStatus.COMPLETED.value
    elif plan_status == PlanStatus.ROLLBACK_FAILED:
        rollback_status = RollbackStatus.FAILED.value
    elif plan_status == PlanStatus.APPLIED:
        rollback_status = RollbackStatus.PENDING.value
    else:
        rollback_status = "not_applicable"

    return RollbackStatusResponse(
        plan_id=str(plan_id),
        plan_status=plan_status.value,
        rollback_status=rollback_status,
        applied_at=plan["applied_at"].isoformat() if plan.get("applied_at") else None,
        rolled_back_at=plan["rolled_back_at"].isoformat() if plan.get("rolled_back_at") else None,
    )
