"""
Runs API endpoints for DBA loop run management and monitoring.

Provides routes for:
- POST /api/v1/runs/ - Start a new run
- POST /api/v1/runs/{run_id}/halt - Halt an active run
- GET /api/v1/runs/{run_id} - Get run status
- GET /api/v1/runs/ - List active runs
- WebSocket /ws/runs/{run_id} - Real-time step transition updates

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from backend.dependencies import get_db
from backend.models.config import LoopConfig
from backend.models.enums import WorkflowStep
from backend.models.runs import RunSummary

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
    last_step_transition_at: str
    elapsed_seconds: float
    failure_reason: Optional[str] = None
    guardrail_violation: Optional[dict] = None


class RunListResponse(BaseModel):
    """Response model for listing active runs."""

    runs: List[RunSummary]
    total: int


# --- Endpoints ---


@router.post("/api/v1/runs/", response_model=StartRunResponse)
async def start_run(request: StartRunRequest, db=Depends(get_db)) -> StartRunResponse:
    """
    Start a new DBA loop run with a high-level goal.

    Creates a new loop run and begins execution in the background.

    Requirements: 2.1
    """
    from backend.services.loop_worker import DBALoopWorker

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

    worker = DBALoopWorker()

    # Start the run in a background task
    async def _run_in_background():
        try:
            await worker.start_run(goal=request.goal, config=config, host_id=host_id)
        except Exception as e:
            logger.error(f"Background run failed: {e}")

    asyncio.create_task(_run_in_background())

    # Wait briefly for the run_id to be set
    await asyncio.sleep(0.1)
    run_id = str(worker._run_id) if worker._run_id else "pending"

    return StartRunResponse(
        run_id=run_id,
        status="running",
        goal=request.goal,
        message="DBA loop run started successfully",
    )


@router.post("/api/v1/runs/{run_id}/halt", response_model=HaltRunResponse)
async def halt_run(run_id: UUID, db=Depends(get_db)) -> HaltRunResponse:
    """
    Halt an active DBA loop run within 10 seconds.

    Transitions run status to 'manually_halted' and preserves completed step state.
    Returns an error message if the run is no longer active.

    Requirements: 2.4, 2.6
    """
    from backend.services.loop_worker import get_active_runs, get_loop_worker

    active_runs = get_active_runs()
    run_id_str = str(run_id)

    # If there's an active worker for this run, use it
    if run_id_str in active_runs:
        worker = active_runs[run_id_str]
        result = await worker.halt_run(run_id)
    else:
        # Use a new worker instance to check/halt from DB
        worker = get_loop_worker()
        result = await worker.halt_run(run_id)

    if not result["success"]:
        raise HTTPException(
            status_code=409,
            detail=result["message"],
        )

    return HaltRunResponse(
        success=result["success"],
        message=result["message"],
        status=result["status"],
        previous_step=result.get("previous_step"),
    )


@router.get("/api/v1/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(run_id: UUID, db=Depends(get_db)) -> RunStatusResponse:
    """
    Get the current status of a DBA loop run.

    Displays: run ID, goal, current workflow step, elapsed time,
    last step transition, and guardrail violation details if applicable.

    Requirements: 2.1, 2.3
    """
    row = await db.fetchrow(
        """
        SELECT id, goal, status, current_step, current_iteration,
               max_iterations, started_at, last_step_transition_at,
               failure_reason
        FROM loop_runs
        WHERE id = $1
        """,
        run_id,
    )

    if row is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    now = datetime.now(timezone.utc)
    started_at = row["started_at"]
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    elapsed = (now - started_at).total_seconds()

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
        last_step_transition_at=last_step.isoformat() if last_step else started_at.isoformat(),
        elapsed_seconds=round(elapsed, 2),
        failure_reason=row["failure_reason"],
        guardrail_violation=guardrail_violation,
    )


@router.get("/api/v1/runs/", response_model=RunListResponse)
async def list_active_runs(db=Depends(get_db)) -> RunListResponse:
    """
    List all active DBA loop runs.

    Shows: run ID, goal, current step, elapsed time, last step transition.

    Requirements: 2.1, 2.5
    """
    rows = await db.fetch(
        """
        SELECT id, goal, status, current_step, current_iteration,
               started_at, last_step_transition_at
        FROM loop_runs
        WHERE status IN ('running', 'unresponsive')
        ORDER BY started_at DESC
        """
    )

    now = datetime.now(timezone.utc)
    runs = []
    for row in rows:
        started_at = row["started_at"]
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed = (now - started_at).total_seconds()

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
    await websocket.accept()

    channel = f"run:{run_id}:steps"

    try:
        from backend.db.redis_manager import get_redis_client

        redis_client = get_redis_client()

        if redis_client is None:
            # Fallback: poll the database for updates
            await _poll_run_updates(websocket, UUID(run_id))
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


async def _poll_run_updates(websocket: WebSocket, run_id: UUID):
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
                        FROM loop_runs WHERE id = $1
                        """,
                        run_id,
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
