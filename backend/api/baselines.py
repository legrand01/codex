"""Baseline measurement and non-executable advisory read APIs."""

import json
from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.dependencies import get_db
from backend.security import Principal, require_roles

router = APIRouter(prefix="/api/v1/runs", tags=["baselines"])


def _json(value: Any, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


class BaselineMeasurementResponse(BaseModel):
    id: UUID
    run_id: UUID
    host_id: UUID
    workload_fingerprint_id: Optional[UUID] = None
    status: str
    objective_type: str
    objective_formula: str
    objective_direction: str
    objective_score: Optional[float] = None
    metric_units: dict[str, Any]
    fingerprint_membership: List[dict[str, Any]]
    warmup_window_seconds: int
    requested_measurement_window_seconds: int
    observed_measurement_window_seconds: float
    workload_coverage_pct: float
    runtime_variance_pct: Optional[float] = None
    safety_metrics: dict[str, Any]
    evidence_references: List[dict[str, Any]]
    root_cause_category: str
    root_cause_confidence: float
    root_cause_summary: str
    root_cause_details: dict[str, Any]
    warnings: List[str]
    captured_at: datetime


class AdvisoryFindingResponse(BaseModel):
    id: UUID
    run_id: UUID
    host_id: UUID
    category: str
    severity: str
    title: str
    summary: str
    recommendations: List[str] = Field(default_factory=list)
    evidence_references: List[dict[str, Any]] = Field(default_factory=list)
    executable: bool
    created_at: datetime


@router.get("/{run_id}/baseline", response_model=BaselineMeasurementResponse)
async def get_baseline(
    run_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("viewer", "operator", "admin")),
) -> BaselineMeasurementResponse:
    row = await db.fetchrow(
        """
        SELECT b.*
        FROM baseline_measurements b
        JOIN loop_runs r ON r.id = b.run_id
        WHERE b.run_id = $1 AND r.organization_id = $2
        """,
        run_id,
        principal.organization_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Baseline measurement not found")
    payload = dict(row)
    for key, default in (
        ("metric_units", {}),
        ("fingerprint_membership", []),
        ("safety_metrics", {}),
        ("evidence_references", []),
        ("root_cause_details", {}),
        ("warnings", []),
    ):
        payload[key] = _json(payload.get(key), default)
    return BaselineMeasurementResponse(**payload)


@router.get("/{run_id}/advisories", response_model=List[AdvisoryFindingResponse])
async def get_advisories(
    run_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("viewer", "operator", "admin")),
) -> List[AdvisoryFindingResponse]:
    rows = await db.fetch(
        """
        SELECT a.id, a.run_id, a.host_id, a.category, a.severity, a.title,
               a.summary, a.recommendations, a.evidence_references,
               a.executable, a.created_at
        FROM advisory_findings a
        JOIN loop_runs r ON r.id = a.run_id
        WHERE a.run_id = $1 AND r.organization_id = $2
        ORDER BY a.created_at
        """,
        run_id,
        principal.organization_id,
    )
    result = []
    for row in rows:
        payload = dict(row)
        payload["recommendations"] = _json(payload.get("recommendations"), [])
        payload["evidence_references"] = _json(payload.get("evidence_references"), [])
        result.append(AdvisoryFindingResponse(**payload))
    return result
