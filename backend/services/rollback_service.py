"""
Rollback Service for executing plan rollback operations.

Provides:
- execute_rollback() to run stored rollback instructions for a plan
- validate_rollback_eligibility() to check plan status allows rollback
- validate_rollback_instructions() to check instructions are present and parsable

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6
"""

import asyncio
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional
from uuid import UUID

from backend.config import settings
from backend.models.enums import PlanStatus

logger = logging.getLogger(__name__)

# Timeout for rollback execution (300 seconds)
ROLLBACK_TIMEOUT_SECONDS = 300

# Statuses that allow rollback
ROLLBACK_ELIGIBLE_STATUSES = {PlanStatus.APPLIED, PlanStatus.ROLLBACK_FAILED}

# Statuses that explicitly prevent rollback
ROLLBACK_PREVENTED_STATUSES = {PlanStatus.ROLLED_BACK}


class RollbackStatus(str, Enum):
    """Status of a rollback operation."""

    PENDING = "pending"
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"
    FAILED = "failed"


class RollbackError(Exception):
    """Base exception for rollback operations."""

    pass


class RollbackEligibilityError(RollbackError):
    """Raised when a plan is not eligible for rollback."""

    pass


class RollbackInstructionsError(RollbackError):
    """Raised when rollback instructions are missing or unparsable."""

    pass


class RollbackTimeoutError(RollbackError):
    """Raised when rollback exceeds the 300-second timeout."""

    pass


class RollbackResult:
    """Result of a rollback execution."""

    def __init__(
        self,
        plan_id: UUID,
        status: RollbackStatus,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        failed_step: Optional[int] = None,
        error: Optional[str] = None,
    ):
        self.plan_id = plan_id
        self.status = status
        self.started_at = started_at
        self.completed_at = completed_at
        self.failed_step = failed_step
        self.error = error

    def to_dict(self) -> Dict:
        return {
            "plan_id": str(self.plan_id),
            "status": self.status.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "failed_step": self.failed_step,
            "error": self.error,
        }


def validate_rollback_eligibility(plan_status: PlanStatus) -> None:
    """
    Validate that a plan's current status allows rollback initiation.

    Rollback is only permitted for plans with status "applied" or "rollback_failed".
    All other statuses (including "rolled_back") are rejected.

    Args:
        plan_status: The current status of the plan.

    Raises:
        RollbackEligibilityError: If the plan is not eligible for rollback.
    """
    if plan_status in ROLLBACK_PREVENTED_STATUSES:
        raise RollbackEligibilityError(
            f"Plan has already been rolled back (status: {plan_status.value}). "
            "Cannot rollback again."
        )

    if plan_status not in ROLLBACK_ELIGIBLE_STATUSES:
        raise RollbackEligibilityError(
            f"Plan status '{plan_status.value}' is not eligible for rollback. "
            f"Rollback is only allowed for plans with status: "
            f"{', '.join(s.value for s in ROLLBACK_ELIGIBLE_STATUSES)}."
        )


def validate_rollback_instructions(rollback_instructions: Optional[List]) -> List[dict]:
    """
    Validate that rollback instructions are present and parsable.

    Args:
        rollback_instructions: The rollback instructions from the plan.

    Returns:
        The validated list of rollback instruction dicts.

    Raises:
        RollbackInstructionsError: If instructions are missing or unparsable.
    """
    if rollback_instructions is None:
        raise RollbackInstructionsError(
            "Rollback instructions are missing. Cannot execute rollback."
        )

    if not isinstance(rollback_instructions, list):
        raise RollbackInstructionsError(
            "Rollback instructions are not in expected format (must be a list)."
        )

    if len(rollback_instructions) == 0:
        raise RollbackInstructionsError("Rollback instructions are empty. Cannot execute rollback.")

    # Validate each instruction is a dict with required fields
    for i, instruction in enumerate(rollback_instructions):
        if not isinstance(instruction, dict):
            raise RollbackInstructionsError(
                f"Rollback instruction at index {i} is not a valid object. "
                "Cannot parse rollback instructions."
            )

    return rollback_instructions


async def execute_rollback(
    plan_id: UUID,
    rollback_instructions: List[dict],
    timeout: int = ROLLBACK_TIMEOUT_SECONDS,
    *,
    host_id: Optional[UUID] = None,
    pre_change_snapshot: Optional[dict] = None,
    control_pool=None,
    target_executor=None,
) -> RollbackResult:
    """
    Execute rollback instructions for a plan.

    Simulates execution of rollback instructions with a timeout.
    In a real deployment, this would execute SQL statements against
    the target PostgreSQL host.

    The operation must complete or fail within 300 seconds.

    Args:
        plan_id: UUID of the plan being rolled back.
        rollback_instructions: List of rollback instruction dicts to execute.
        timeout: Maximum seconds allowed for rollback (default: 300).

    Returns:
        RollbackResult indicating success or failure.

    Raises:
        RollbackTimeoutError: If rollback exceeds the timeout.
    """
    started_at = datetime.now(timezone.utc)

    if settings.require_live_target_rollback or target_executor is not None:
        if host_id is None:
            raise RollbackInstructionsError("Live rollback requires a target host_id")
        if not pre_change_snapshot:
            raise RollbackInstructionsError("Live rollback requires a pre-change snapshot")
        if target_executor is None:
            if control_pool is None:
                raise RollbackInstructionsError("Live rollback requires the control-plane pool")
            from backend.services.target_executor import TargetPostgresExecutor

            target_executor = TargetPostgresExecutor(control_pool)
        try:
            execution = await asyncio.wait_for(
                target_executor.rollback(host_id, pre_change_snapshot),
                timeout=timeout,
            )
            if not execution.succeeded or not execution.rolled_back:
                raise RollbackError("Target executor did not verify rollback completion")
            return RollbackResult(
                plan_id=plan_id,
                status=RollbackStatus.COMPLETED,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )
        except asyncio.TimeoutError as exc:
            raise RollbackTimeoutError(
                f"Rollback for plan {plan_id} exceeded {timeout} second timeout."
            ) from exc

    try:
        result = await asyncio.wait_for(
            _execute_instructions(plan_id, rollback_instructions, started_at),
            timeout=timeout,
        )
        return result
    except asyncio.TimeoutError:
        raise RollbackTimeoutError(
            f"Rollback for plan {plan_id} exceeded {timeout} second timeout."
        )


async def _execute_instructions(
    plan_id: UUID,
    instructions: List[dict],
    started_at: datetime,
) -> RollbackResult:
    """
    Internal: Execute each rollback instruction sequentially.

    In a production system, this would:
    1. Connect to the target PostgreSQL host
    2. Execute each rollback SQL statement
    3. Verify the setting was restored

    For now, we simulate successful execution of each step.

    Args:
        plan_id: UUID of the plan.
        instructions: List of instruction dicts.
        started_at: When execution started.

    Returns:
        RollbackResult with completion status.
    """
    for i, instruction in enumerate(instructions):
        # Simulate executing each rollback step
        # In production, this would run the actual SQL/commands
        # e.g., ALTER SYSTEM SET setting_name = restore_value;
        # followed by SELECT pg_reload_conf();
        logger.info(
            f"Executing rollback step {i + 1}/{len(instructions)} for plan {plan_id}: {instruction}"
        )
        # Small delay to simulate real execution
        await asyncio.sleep(0.01)

    completed_at = datetime.now(timezone.utc)
    return RollbackResult(
        plan_id=plan_id,
        status=RollbackStatus.COMPLETED,
        started_at=started_at,
        completed_at=completed_at,
    )
