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

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.dependencies import get_db
from backend.security import Principal, require_roles

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
    data_size_bytes: int = 0
    data_truncated: bool = False


class EvidenceSnapshotSummaryResponse(BaseModel):
    """Bounded evidence metadata returned by the run listing endpoint."""

    id: UUID
    run_id: UUID
    host_id: UUID
    evidence_type: str
    collected_at: datetime
    quality_score: Optional[float] = None
    freshness_age: str
    data_size_bytes: int = 0


class CategorySummary(BaseModel):
    """Summary of evidence count per category."""

    category: str
    count: int


class EvidenceListResponse(BaseModel):
    """Response model for evidence listing by run."""

    run_id: UUID
    snapshots: List[EvidenceSnapshotSummaryResponse]
    categories: List[CategorySummary]
    total: int
    limit: int
    offset: int


class EvidenceEmptyResponse(BaseModel):
    """Response model for empty evidence state."""

    run_id: UUID
    snapshots: List[EvidenceSnapshotSummaryResponse] = Field(default_factory=list)
    categories: List[CategorySummary] = Field(default_factory=list)
    total: int = 0
    message: str = "No evidence has been collected yet for the selected run"


MAX_INLINE_EVIDENCE_BYTES = 256_000


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


def _bounded_value(value, *, depth: int = 0):
    """Build a small, useful preview without returning an unbounded payload."""
    if depth >= 3:
        if isinstance(value, (list, dict)):
            return f"<{type(value).__name__} omitted>"
        return str(value)[:500]
    if isinstance(value, str):
        return value if len(value) <= 500 else f"{value[:500]}..."
    if isinstance(value, list):
        return {
            "kind": "array",
            "count": len(value),
            "sample": [_bounded_value(item, depth=depth + 1) for item in value[:3]],
        }
    if isinstance(value, dict):
        items = list(value.items())
        return {
            "kind": "object",
            "count": len(items),
            "sample": {
                str(key): _bounded_value(item, depth=depth + 1)
                for key, item in items[:10]
            },
        }
    return value


def _bounded_snapshot_data(data: dict, data_size_bytes: int) -> tuple[dict, bool]:
    """Return full small snapshots and a deterministic preview for large ones."""
    if data_size_bytes <= MAX_INLINE_EVIDENCE_BYTES:
        return data, False
    return (
        {
            "_payload_truncated": True,
            "_payload_size_bytes": data_size_bytes,
            "_preview": _bounded_value(data),
        },
        True,
    )


# --- Endpoints ---


@router.get("/snapshot/{snapshot_id}", response_model=EvidenceSnapshotResponse)
async def get_evidence_snapshot(
    snapshot_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("viewer", "operator", "approver", "admin")),
) -> EvidenceSnapshotResponse:
    """
    Get a specific evidence snapshot by ID.

    Returns full snapshot data with freshness age.
    Returns 404 if the snapshot is not found (unavailable reference).

    Requirements: 3.3, 3.6
    """
    row = await db.fetchrow(
        "SELECT e.id, e.run_id, e.host_id, e.evidence_type, e.collected_at, e.data, "
        "e.quality_score, octet_length(e.data::text) AS data_size_bytes "
        "FROM evidence_snapshots e JOIN hosts h ON h.id = e.host_id "
        "WHERE e.id = $1 AND h.organization_id = $2",
        snapshot_id,
        principal.organization_id,
    )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Evidence snapshot '{snapshot_id}' not found or unavailable",
        )

    data_size_bytes = int(row.get("data_size_bytes") or 0)
    data, truncated = _bounded_snapshot_data(_parse_json_field(row["data"]), data_size_bytes)
    return EvidenceSnapshotResponse(
        id=row["id"],
        run_id=row["run_id"],
        host_id=row["host_id"],
        evidence_type=row["evidence_type"],
        collected_at=row["collected_at"],
        data=data,
        quality_score=float(row["quality_score"]) if row["quality_score"] is not None else None,
        freshness_age=format_freshness_age(row["collected_at"]),
        data_size_bytes=data_size_bytes,
        data_truncated=truncated,
    )


@router.get("/{run_id}", response_model=EvidenceListResponse)
async def list_evidence_by_run(
    run_id: UUID,
    evidence_type: Optional[str] = Query(default=None, min_length=1, max_length=100),
    limit: int = Query(default=100, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("viewer", "operator", "approver", "admin")),
) -> EvidenceListResponse:
    """
    List bounded evidence metadata collected during a loop run.

    Snapshot payloads are intentionally excluded so an active run cannot turn
    this listing into a multi-hundred-megabyte response. Full (or safely
    previewed) data is available from the single-snapshot endpoint.

    Returns evidence grouped by category with counts per category.
    Returns empty-state response when no evidence exists for the run.

    Requirements: 3.1, 3.2, 3.5
    """
    category_rows = await db.fetch(
        "SELECT e.evidence_type, COUNT(*)::integer AS count "
        "FROM evidence_snapshots e JOIN loop_runs r ON r.id = e.run_id "
        "WHERE e.run_id = $1 AND r.organization_id = $2 "
        "AND ($3::text IS NULL OR e.evidence_type = $3) "
        "GROUP BY e.evidence_type",
        run_id,
        principal.organization_id,
        evidence_type,
    )

    total = sum(int(row["count"]) for row in category_rows)

    if total == 0:
        return EvidenceListResponse(
            run_id=run_id,
            snapshots=[],
            categories=[],
            total=0,
            limit=limit,
            offset=offset,
        )

    rows = await db.fetch(
        "SELECT e.id, e.run_id, e.host_id, e.evidence_type, e.collected_at, "
        "e.quality_score, octet_length(e.data::text) AS data_size_bytes "
        "FROM evidence_snapshots e JOIN loop_runs r ON r.id = e.run_id "
        "WHERE e.run_id = $1 AND r.organization_id = $2 "
        "AND ($3::text IS NULL OR e.evidence_type = $3) "
        "ORDER BY e.collected_at DESC, e.id DESC LIMIT $4 OFFSET $5",
        run_id,
        principal.organization_id,
        evidence_type,
        limit,
        offset,
    )

    # Build snapshot list with freshness age
    snapshots = []
    for row in rows:
        freshness = format_freshness_age(row["collected_at"])

        snapshots.append(
            EvidenceSnapshotSummaryResponse(
                id=row["id"],
                run_id=row["run_id"],
                host_id=row["host_id"],
                evidence_type=row["evidence_type"],
                collected_at=row["collected_at"],
                quality_score=(
                    float(row["quality_score"]) if row["quality_score"] is not None else None
                ),
                freshness_age=freshness,
                data_size_bytes=int(row.get("data_size_bytes") or 0),
            )
        )

    category_counts: Dict[str, int] = {}
    for row in category_rows:
        category = categorize_evidence_type(row["evidence_type"])
        category_counts[category] = category_counts.get(category, 0) + int(row["count"])
    categories = [
        CategorySummary(category=cat, count=count)
        for cat, count in sorted(category_counts.items())
    ]

    return EvidenceListResponse(
        run_id=run_id,
        snapshots=snapshots,
        categories=categories,
        total=total,
        limit=limit,
        offset=offset,
    )
