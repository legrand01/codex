"""
Evidence viewing API endpoints.

Provides routes for:
- Listing evidence by loop run (GET /api/v1/evidence/{run_id})
- Getting a specific snapshot (GET /api/v1/evidence/snapshot/{snapshot_id})

Includes utility for formatting evidence freshness age.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
"""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.dependencies import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/evidence", tags=["evidence"])


# --- Evidence Type to Category Mapping ---

EVIDENCE_TYPE_CATEGORY_MAP: Dict[str, str] = {
    "pg_settings": "configuration",
    "pg_stat_database": "performance",
    "pg_stat_statements": "performance",
    "locks": "locks",
    "replication": "replication",
    "wal_checkpoint": "wal_checkpoint",
    "os_metrics": "os_metrics",
}


# --- Response Models ---


class EvidenceSnapshotResponse(BaseModel):
    """Response model for a single evidence snapshot with freshness."""

    id: UUID
    run_id: UUID
    host_id: UUID
    evidence_type: str
    collected_at: datetime
    data: dict
    quality_score: Optional[float] = None
    freshness_age: str


class CategorySummary(BaseModel):
    """Summary of evidence count per category."""

    category: str
    count: int


class EvidenceListResponse(BaseModel):
    """Response model for evidence listing by run."""

    run_id: UUID
    snapshots: List[EvidenceSnapshotResponse]
    categories: List[CategorySummary]
    total: int


class EvidenceEmptyResponse(BaseModel):
    """Response model for empty evidence state."""

    run_id: UUID
    snapshots: List[EvidenceSnapshotResponse] = Field(default_factory=list)
    categories: List[CategorySummary] = Field(default_factory=list)
    total: int = 0
    message: str = "No evidence has been collected yet for the selected run"


# --- Utility Functions ---


def format_freshness_age(collected_at: datetime) -> str:
    """
    Format evidence freshness age relative to the current time.

    Returns:
        - "Xs ago" for ages < 60 seconds
        - "Xm ago" for ages < 3600 seconds (60 minutes)
        - "Xh ago" for ages >= 3600 seconds

    Uses floor division for the numeric value.

    Requirements: 3.4
    """
    now = datetime.now(timezone.utc)

    # Handle naive datetimes by assuming UTC
    if collected_at.tzinfo is None:
        collected_at = collected_at.replace(tzinfo=timezone.utc)

    age_seconds = int((now - collected_at).total_seconds())

    # Ensure non-negative (handle clock skew)
    if age_seconds < 0:
        age_seconds = 0

    if age_seconds < 60:
        return f"{age_seconds}s ago"
    elif age_seconds < 3600:
        minutes = age_seconds // 60
        return f"{minutes}m ago"
    else:
        hours = age_seconds // 3600
        return f"{hours}h ago"


def categorize_evidence_type(evidence_type: str) -> str:
    """Map an evidence_type to its display category."""
    return EVIDENCE_TYPE_CATEGORY_MAP.get(evidence_type, evidence_type)


def _parse_json_field(value) -> dict:
    """Parse a JSON/JSONB field that may already be decoded or may be a string."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


# --- Endpoints ---


@router.get("/snapshot/{snapshot_id}", response_model=EvidenceSnapshotResponse)
async def get_evidence_snapshot(snapshot_id: UUID, db=Depends(get_db)) -> EvidenceSnapshotResponse:
    """
    Get a specific evidence snapshot by ID.

    Returns full snapshot data with freshness age.
    Returns 404 if the snapshot is not found (unavailable reference).

    Requirements: 3.3, 3.6
    """
    row = await db.fetchrow(
        "SELECT id, run_id, host_id, evidence_type, collected_at, data, quality_score "
        "FROM evidence_snapshots WHERE id = $1",
        snapshot_id,
    )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Evidence snapshot '{snapshot_id}' not found or unavailable",
        )

    return EvidenceSnapshotResponse(
        id=row["id"],
        run_id=row["run_id"],
        host_id=row["host_id"],
        evidence_type=row["evidence_type"],
        collected_at=row["collected_at"],
        data=_parse_json_field(row["data"]),
        quality_score=float(row["quality_score"]) if row["quality_score"] is not None else None,
        freshness_age=format_freshness_age(row["collected_at"]),
    )


@router.get("/{run_id}", response_model=EvidenceListResponse)
async def list_evidence_by_run(run_id: UUID, db=Depends(get_db)) -> EvidenceListResponse:
    """
    List all evidence snapshots collected during a loop run.

    Returns evidence grouped by category with counts per category.
    Returns empty-state response when no evidence exists for the run.

    Requirements: 3.1, 3.2, 3.5
    """
    rows = await db.fetch(
        "SELECT id, run_id, host_id, evidence_type, collected_at, data, quality_score "
        "FROM evidence_snapshots WHERE run_id = $1 "
        "ORDER BY collected_at ASC",
        run_id,
    )

    if not rows:
        return EvidenceListResponse(
            run_id=run_id,
            snapshots=[],
            categories=[],
            total=0,
        )

    # Build snapshot list with freshness age
    snapshots = []
    category_counts: Dict[str, int] = {}

    for row in rows:
        freshness = format_freshness_age(row["collected_at"])
        category = categorize_evidence_type(row["evidence_type"])

        # Count by category
        category_counts[category] = category_counts.get(category, 0) + 1

        snapshots.append(
            EvidenceSnapshotResponse(
                id=row["id"],
                run_id=row["run_id"],
                host_id=row["host_id"],
                evidence_type=row["evidence_type"],
                collected_at=row["collected_at"],
                data=_parse_json_field(row["data"]),
                quality_score=(
                    float(row["quality_score"]) if row["quality_score"] is not None else None
                ),
                freshness_age=freshness,
            )
        )

    # Build category summaries
    categories = [
        CategorySummary(category=cat, count=cnt) for cat, cnt in sorted(category_counts.items())
    ]

    return EvidenceListResponse(
        run_id=run_id,
        snapshots=snapshots,
        categories=categories,
        total=len(snapshots),
    )
