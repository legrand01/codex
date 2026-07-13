"""Tenant-scoped measured candidate history APIs."""

import json
from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend.dependencies import get_db
from backend.security import Principal, require_roles

router = APIRouter(prefix="/api/v1/runs", tags=["candidates"])


def _json(value: Any, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


class TuningCandidateResponse(BaseModel):
    id: UUID
    run_id: UUID
    host_id: UUID
    plan_id: UUID
    plan_status: str
    iteration: int
    domain_version: str
    parameter_values: dict[str, Any]
    baseline_score: float
    best_score_before: float
    objective_score: Optional[float] = None
    baseline_delta_pct: Optional[float] = None
    best_delta_pct: Optional[float] = None
    objective_formula: str
    objective_direction: str
    metric_units: dict[str, Any]
    warmup_window_seconds: int
    measurement_window_seconds: int
    observed_measurement_window_seconds: Optional[float] = None
    workload_coverage_pct: Optional[float] = None
    runtime_variance_pct: Optional[float] = None
    safety_metrics: dict[str, Any]
    safety_deltas: dict[str, Any]
    guardrail_violations: List[str] = Field(default_factory=list)
    evidence_references: List[dict[str, Any]] = Field(default_factory=list)
    confidence_score: Optional[float] = None
    decision: str
    decision_reason: Optional[str] = None
    warmup_started_at: Optional[datetime] = None
    warmup_completed_at: Optional[datetime] = None
    measurement_started_at: Optional[datetime] = None
    measured_at: Optional[datetime] = None
    decided_at: Optional[datetime] = None
    created_at: datetime


@router.get("/{run_id}/candidates", response_model=List[TuningCandidateResponse])
async def list_candidates(
    run_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("viewer", "operator", "admin")),
) -> List[TuningCandidateResponse]:
    rows = await db.fetch(
        """
        SELECT c.*, p.status AS plan_status
        FROM tuning_candidates c
        JOIN plans p ON p.id = c.plan_id
        JOIN loop_runs r ON r.id = c.run_id
        WHERE c.run_id = $1 AND r.organization_id = $2
        ORDER BY c.iteration
        """,
        run_id,
        principal.organization_id,
    )
    result = []
    for row in rows:
        payload = dict(row)
        for key, default in (
            ("parameter_values", {}),
            ("metric_units", {}),
            ("safety_metrics", {}),
            ("safety_deltas", {}),
            ("guardrail_violations", []),
            ("evidence_references", []),
        ):
            payload[key] = _json(payload.get(key), default)
        result.append(TuningCandidateResponse(**payload))
    return result
