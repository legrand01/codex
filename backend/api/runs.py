"""
Runs API endpoints for DBA loop run management and monitoring.

Provides routes for:
- POST /api/v1/runs/ - Start a new run
- POST /api/v1/runs/{run_id}/halt - Halt an active run
- GET /api/v1/runs/{run_id} - Get run status
- GET /api/v1/runs/ - List persistent tuning sessions
- WebSocket /ws/runs/{run_id} - Real-time step transition updates

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from backend.dependencies import get_db
from backend.models.config import LoopConfig
from backend.models.enums import WorkflowStep
from backend.models.runs import RunSummary
from backend.security import Principal, require_roles

logger = logging.getLogger(__name__)

router = APIRouter(tags=["runs"])


# --- Request/Response Models ---


class StartRunRequest(BaseModel):
    """Request model for starting a new DBA loop run."""

    goal: str = Field(..., min_length=1, description="High-level DBA goal")
    host_id: Optional[str] = Field(None, description="Target host UUID")
    max_iterations: int = Field(default=10, ge=1)
    max_steps: int = Field(default=20, ge=1)
    approval_timeout_hours: int = Field(default=24, ge=1)
    verification_window_seconds: int = Field(default=60, ge=10, le=600)
    degradation_threshold_pct: float = Field(default=10.0, ge=0.0)


class StartRunResponse(BaseModel):
    """Response model for starting a run."""

    run_id: str
    status: str
    goal: str
    message: str


class HaltRunResponse(BaseModel):
    """Response model for halting a run."""

    success: bool
    message: str
    status: str
    previous_step: Optional[str] = None


class RunStatusResponse(BaseModel):
    """Response model for run status."""

    id: str
    goal: str
    status: str
    current_step: Optional[str] = None
    current_iteration: int
    max_iterations: int
    started_at: str
    completed_at: Optional[str] = None
    last_step_transition_at: str
    elapsed_seconds: float
    failure_reason: Optional[str] = None
    guardrail_violation: Optional[dict] = None


class RunListResponse(BaseModel):
    """Response model for listing persistent tuning sessions."""

    runs: List[RunSummary]
    total: int


# --- Endpoints ---


@router.post("/api/v1/runs/", response_model=StartRunResponse)
async def start_run(
    request: StartRunRequest,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("operator", "admin")),
) -> StartRunResponse:
    """
    Start a new DBA loop run with a high-level goal.

    Creates a new loop run and begins execution in the background.

    Requirements: 2.1
    """
    config = LoopConfig(
        max_iterations=request.max_iterations,
        max_steps=request.max_steps,
        approval_timeout_hours=request.approval_timeout_hours,
        verification_window_seconds=request.verification_window_seconds,
        degradation_threshold_pct=request.degradation_threshold_pct,
    )

    host_id = None
    if request.host_id:
        try:
            host_id = UUID(request.host_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid host_id format")

    if host_id is None:
        host = await db.fetchrow(
            """
            SELECT id, organization_id FROM hosts
            WHERE organization_id = $1
            ORDER BY created_at LIMIT 1
            """,
            principal.organization_id,
        )
    else:
        host = await db.fetchrow(
            "SELECT id, organization_id FROM hosts WHERE id = $1 AND organization_id = $2",
            host_id,
            principal.organization_id,
        )
    if host is None:
        raise HTTPException(status_code=409, detail="A registered target host is required")

    run_id = uuid4()
    now = datetime.now(timezone.utc)
    async with db.transaction():
        await db.execute(
            """
            INSERT INTO loop_runs (
                id, organization_id, host_id, goal, status, current_step,
                current_iteration, max_iterations, max_steps,
                approval_timeout_hours, verification_window_seconds,
                degradation_threshold_pct, started_at, last_step_transition_at
            ) VALUES (
                $1, $2, $3, $4, 'queued', 'observe', 1, $5, $6, $7, $8, $9, $10, $10
            )
            """,
            run_id,
            host["organization_id"],
            host["id"],
            request.goal,
            config.max_iterations,
            config.max_steps,
            config.approval_timeout_hours,
            config.verification_window_seconds,
            config.degradation_threshold_pct,
            now,
        )
        await db.execute(
            """
            INSERT INTO run_jobs (run_id, organization_id, status)
            VALUES ($1, $2, 'queued')
            """,
            run_id,
            host["organization_id"],
        )

    return StartRunResponse(
        run_id=str(run_id),
        status="queued",
        goal=request.goal,
        message="DBA loop run queued durably",
    )


@router.post("/api/v1/runs/{run_id}/halt", response_model=HaltRunResponse)
async def halt_run(
    run_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("operator", "admin")),
) -> HaltRunResponse:
    """
    Halt an active DBA loop run within 10 seconds.

    Transitions run status to 'manually_halted' and preserves completed step state.
    Returns an error message if the run is no longer active.

    Requirements: 2.4, 2.6
    """
    async with db.transaction():
        row = await db.fetchrow(
            """
            SELECT id, status, current_step
            FROM loop_runs
            WHERE id = $1 AND organization_id = $2
            FOR UPDATE
            """,
            run_id,
            principal.organization_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        if row["status"] not in {"queued", "running", "waiting_approval", "unresponsive"}:
            raise HTTPException(
                status_code=409,
                detail=f"Run is no longer active (current status: {row['status']})",
            )
        write_in_progress = await db.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM write_operations w
                JOIN plans p ON p.id = w.plan_id
                WHERE p.run_id = $1 AND w.status = 'in_progress'
            )
            """,
            run_id,
        )
        if write_in_progress:
            raise HTTPException(
                status_code=409,
                detail="A target write is in progress; wait for verified completion or rollback",
            )
        verification_required = await db.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM plans
                WHERE run_id = $1 AND status = 'applied'
                  AND verification_completed_at IS NULL
            )
            """,
            run_id,
        )
        if verification_required:
            raise HTTPException(
                status_code=409,
                detail="An applied change still requires verification or rollback",
            )
        await db.execute(
            """
            UPDATE run_jobs
            SET status = CASE WHEN status = 'claimed' THEN 'cancel_requested' ELSE 'cancelled' END,
                updated_at = NOW(),
                completed_at = CASE WHEN status = 'claimed' THEN NULL ELSE NOW() END
            WHERE run_id = $1 AND status IN ('queued', 'claimed', 'waiting_approval')
            """,
            run_id,
        )
        await db.execute(
            """
            UPDATE loop_runs
            SET status = 'manually_halted', failure_reason = 'Run manually halted by operator',
                completed_at = NOW(), last_step_transition_at = NOW()
            WHERE id = $1
            """,
            run_id,
        )

    return HaltRunResponse(
        success=True,
        message=f"Run '{run_id}' halted successfully",
        status="manually_halted",
        previous_step=row["current_step"],
    )


@router.get("/api/v1/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(
    run_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("viewer", "operator", "approver", "admin")),
) -> RunStatusResponse:
    """
    Get the current status of a DBA loop run.

    Displays: run ID, goal, current workflow step, elapsed time,
    last step transition, and guardrail violation details if applicable.

    Requirements: 2.1, 2.3
    """
    row = await db.fetchrow(
        """
        SELECT id, goal, status, current_step, current_iteration,
               max_iterations, started_at, completed_at, last_step_transition_at,
               failure_reason
        FROM loop_runs
        WHERE id = $1 AND organization_id = $2
        """,
        run_id,
        principal.organization_id,
    )

    if row is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    started_at = row["started_at"]
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    completed_at = row.get("completed_at")
    if completed_at and completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    elapsed_end = completed_at or datetime.now(timezone.utc)
    elapsed = (elapsed_end - started_at).total_seconds()

    # Check for guardrail violation details
    guardrail_violation = None
    if row["status"] == "failed" and row["failure_reason"]:
        if "guardrail" in row["failure_reason"].lower():
            guardrail_violation = {
                "reason": row["failure_reason"],
                "step": row["current_step"],
            }

    last_step = row["last_step_transition_at"]
    if last_step and last_step.tzinfo is None:
        last_step = last_step.replace(tzinfo=timezone.utc)

    return RunStatusResponse(
        id=str(row["id"]),
        goal=row["goal"],
        status=row["status"],
        current_step=row["current_step"],
        current_iteration=row["current_iteration"],
        max_iterations=row["max_iterations"],
        started_at=started_at.isoformat(),
        completed_at=completed_at.isoformat() if completed_at else None,
        last_step_transition_at=last_step.isoformat() if last_step else started_at.isoformat(),
        elapsed_seconds=round(elapsed, 2),
        failure_reason=row["failure_reason"],
        guardrail_violation=guardrail_violation,
    )


@router.get("/api/v1/runs/", response_model=RunListResponse)
async def list_runs(
    active_only: bool = False,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("viewer", "operator", "approver", "admin")),
) -> RunListResponse:
    """
    List persistent DBA tuning sessions, including terminal runs by default.

    ``active_only=true`` retains the operational queue view for callers that
    need it. Terminal durations are frozen at ``completed_at``.

    Requirements: 2.1, 2.5
    """
    active_clause = (
        "AND status IN ('queued', 'running', 'waiting_approval', 'unresponsive')"
        if active_only
        else ""
    )
    rows = await db.fetch(
        f"""
        SELECT id, goal, status, current_step, current_iteration,
               started_at, completed_at, last_step_transition_at
        FROM loop_runs
        WHERE organization_id = $1
          {active_clause}
        ORDER BY started_at DESC
        """,
        principal.organization_id,
    )

    now = datetime.now(timezone.utc)
    runs = []
    for row in rows:
        started_at = row["started_at"]
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        completed_at = row.get("completed_at")
        if completed_at and completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)
        elapsed = ((completed_at or now) - started_at).total_seconds()

        last_step = row["last_step_transition_at"]
        if last_step and last_step.tzinfo is None:
            last_step = last_step.replace(tzinfo=timezone.utc)

        runs.append(
            RunSummary(
                id=row["id"],
                goal=row["goal"],
                current_step=WorkflowStep(row["current_step"])
                if row["current_step"]
                else WorkflowStep.OBSERVE,
                status=row["status"],
                current_iteration=row["current_iteration"],
                started_at=started_at,
                completed_at=completed_at,
                last_step_transition_at=last_step if last_step else started_at,
                elapsed_seconds=round(elapsed, 2),
            )
        )

    return RunListResponse(runs=runs, total=len(runs))


@router.websocket("/ws/runs/{run_id}")
async def ws_run_updates(websocket: WebSocket, run_id: str):
    """
    WebSocket endpoint for real-time step transition updates.

    Publishes step transition events within 5 seconds of occurrence.
    Subscribes to the Redis pub/sub channel for the specific run.

    Requirements: 2.2
    """
    from backend.db.pool import get_pool
    from backend.security import authenticate_websocket

    principal = await authenticate_websocket(websocket)
    if principal is None:
        await websocket.close(code=4401, reason="Authentication is required")
        return
    try:
        parsed_run_id = UUID(run_id)
    except ValueError:
        await websocket.close(code=4400, reason="Invalid run ID")
        return
    pool = get_pool()
    if pool is None:
        await websocket.close(code=1013, reason="Database unavailable")
        return
    async with pool.acquire() as conn:
        owned = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM loop_runs WHERE id = $1 AND organization_id = $2)",
            parsed_run_id,
            principal.organization_id,
        )
    if not owned:
        await websocket.close(code=4404, reason="Run not found")
        return
    await websocket.accept(subprotocol="dbtune-auth")

    channel = f"run:{run_id}:steps"

    try:
        from backend.db.redis_manager import get_redis_client

        redis_client = get_redis_client()

        if redis_client is None:
            # Fallback: poll the database for updates
            await _poll_run_updates(websocket, parsed_run_id, principal.organization_id)
            return

        # Subscribe to the run's step transition channel
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(channel)

        try:
            while True:
                # Check for messages with a 5-second timeout
                message = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0),
                    timeout=10.0,
                )
                if message and message["type"] == "message":
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    await websocket.send_text(data)
                else:
                    # Send a heartbeat ping
                    await websocket.send_text(json.dumps({"type": "heartbeat", "run_id": run_id}))
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    except WebSocketDisconnect:
        logger.debug(f"WebSocket disconnected for run {run_id}")
    except Exception as e:
        logger.warning(f"WebSocket error for run {run_id}: {e}")
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


async def _poll_run_updates(websocket: WebSocket, run_id: UUID, organization_id: UUID):
    """Fallback polling for run status when Redis is unavailable."""
    from backend.db.pool import get_pool

    last_step = None
    try:
        while True:
            pool = get_pool()
            if pool:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        SELECT current_step, status, last_step_transition_at
                        FROM loop_runs WHERE id = $1 AND organization_id = $2
                        """,
                        run_id,
                        organization_id,
                    )
                if row and row["current_step"] != last_step:
                    last_step = row["current_step"]
                    await websocket.send_text(
                        json.dumps(
                            {
                                "run_id": str(run_id),
                                "step": row["current_step"],
                                "status": row["status"],
                                "timestamp": row["last_step_transition_at"].isoformat()
                                if row["last_step_transition_at"]
                                else None,
                            }
                        )
                    )
                if row and row["status"] not in ("running", "unresponsive"):
                    await websocket.send_text(
                        json.dumps(
                            {
                                "run_id": str(run_id),
                                "type": "completed",
                                "status": row["status"],
                            }
                        )
                    )
                    break
            await asyncio.sleep(2)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
