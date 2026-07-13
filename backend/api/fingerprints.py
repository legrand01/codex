"""Workload fingerprint catalog and evidence-backed recommendation API."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.dependencies import get_db
from backend.security import Principal, require_roles
from backend.services.workload_fingerprints import analyze_workload_snapshots

router = APIRouter(prefix="/api/v1/fingerprints", tags=["fingerprints"])


class FingerprintCandidate(BaseModel):
    query_id: str
    query_text: Optional[str] = None
    calls: int
    average_query_runtime_ms: float
    total_runtime_ms: float
    runtime_coverage_pct: float
    impact_score: float
    recommended: bool
    selected: bool
    last_seen_at: Optional[datetime] = None


class FingerprintDiagnostics(BaseModel):
    host_id: UUID
    database_name: Optional[str] = None
    status: str
    ready: bool
    candidates: List[FingerprintCandidate]
    selected_query_ids: List[str]
    coverage_pct: float
    membership_stability_pct: Optional[float] = None
    runtime_variance_pct: Optional[float] = None
    source_snapshot_id: Optional[UUID] = None
    source_collected_at: Optional[datetime] = None
    snapshot_count: int
    collector_truncated: bool
    warnings: List[str]


class FingerprintMember(BaseModel):
    query_id: str
    query_text: Optional[str] = None
    calls: int
    average_query_runtime_ms: float
    total_runtime_ms: float
    runtime_coverage_pct: float
    impact_score: float
    last_seen_at: Optional[datetime] = None
    ordinal: int


class WorkloadFingerprint(BaseModel):
    id: UUID
    host_id: UUID
    database_name: Optional[str] = None
    name: str
    kind: str
    status: str
    ready: bool
    selection_criteria: dict[str, Any]
    diagnostics: dict[str, Any]
    observed_coverage_pct: float
    membership_stability_pct: Optional[float] = None
    runtime_variance_pct: Optional[float] = None
    source_snapshot_id: Optional[UUID] = None
    source_collected_at: Optional[datetime] = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    members: List[FingerprintMember] = Field(default_factory=list)


class FingerprintList(BaseModel):
    fingerprints: List[WorkloadFingerprint]
    total: int


class RecommendFingerprintRequest(BaseModel):
    host_id: UUID
    database_name: Optional[str] = Field(default=None, min_length=1, max_length=63)
    name: str = Field(default="Recommended workload", min_length=1, max_length=120)
    include_query_text: bool = False


class CreateFingerprintRequest(BaseModel):
    host_id: UUID
    database_name: Optional[str] = Field(default=None, min_length=1, max_length=63)
    name: str = Field(..., min_length=1, max_length=120)
    query_ids: List[str]
    include_query_text: bool = False


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def _require_host(db, organization_id: UUID, host_id: UUID):
    host = await db.fetchrow(
        """
        SELECT id, database_name FROM hosts
        WHERE id = $1 AND organization_id = $2
        """,
        host_id,
        organization_id,
    )
    if host is None:
        raise HTTPException(status_code=404, detail="Target host not found")
    return host


async def _recent_snapshots(db, host_id: UUID):
    return await db.fetch(
        """
        SELECT id, collected_at, data
        FROM evidence_snapshots
        WHERE host_id = $1 AND evidence_type = 'pg_stat_statements'
        ORDER BY collected_at DESC
        LIMIT 6
        """,
        host_id,
    )


def _diagnostics_response(
    host_id: UUID, database_name: Optional[str], analysis: dict[str, Any]
) -> FingerprintDiagnostics:
    return FingerprintDiagnostics(host_id=host_id, database_name=database_name, **analysis)


async def _read_fingerprint(db, organization_id: UUID, fingerprint_id: UUID):
    row = await db.fetchrow(
        """
        SELECT id, host_id, database_name, name, kind, status,
               selection_criteria, diagnostics, observed_coverage_pct,
               membership_stability_pct, runtime_variance_pct,
               source_snapshot_id, source_collected_at, created_by,
               created_at, updated_at
        FROM workload_fingerprints
        WHERE id = $1 AND organization_id = $2
        """,
        fingerprint_id,
        organization_id,
    )
    if row is None:
        return None
    members = await db.fetch(
        """
        SELECT query_id, query_text, calls, average_query_runtime_ms,
               total_runtime_ms, runtime_coverage_pct, impact_score,
               last_seen_at, ordinal
        FROM workload_fingerprint_members
        WHERE fingerprint_id = $1
        ORDER BY ordinal
        """,
        fingerprint_id,
    )
    payload = dict(row)
    payload.update(
        ready=row["status"] == "ready",
        selection_criteria=_json_dict(row["selection_criteria"]),
        diagnostics=_json_dict(row["diagnostics"]),
        members=[FingerprintMember(**dict(member)) for member in members],
    )
    return WorkloadFingerprint(**payload)


async def _persist_fingerprint(
    db,
    principal: Principal,
    host_id: UUID,
    database_name: Optional[str],
    name: str,
    kind: str,
    include_query_text: bool,
    analysis: dict[str, Any],
) -> WorkloadFingerprint:
    fingerprint_id = uuid4()
    diagnostics = {
        "warnings": analysis["warnings"],
        "snapshot_count": analysis["snapshot_count"],
        "collector_truncated": analysis["collector_truncated"],
        "minimum_coverage_pct": 70,
        "minimum_stability_pct": 60,
        "maximum_runtime_variance_pct": 50,
    }
    criteria = {
        "algorithm": (
            "average_query_runtime_ms_x_log1p_calls"
            if kind == "recommended"
            else "explicit_membership"
        ),
        "query_text_persisted": include_query_text,
        "membership_is_immutable_for_runs": True,
    }
    async with db.transaction():
        await db.execute(
            """
            INSERT INTO workload_fingerprints (
                id, organization_id, host_id, database_name, name, kind,
                status, selection_criteria, diagnostics, observed_coverage_pct,
                membership_stability_pct, runtime_variance_pct,
                source_snapshot_id, source_collected_at, created_by
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10,
                $11, $12, $13, $14, $15
            )
            """,
            fingerprint_id,
            principal.organization_id,
            host_id,
            database_name,
            name,
            kind,
            analysis["status"],
            json.dumps(criteria),
            json.dumps(diagnostics),
            analysis["coverage_pct"],
            analysis["membership_stability_pct"],
            analysis["runtime_variance_pct"],
            analysis["source_snapshot_id"],
            analysis["source_collected_at"],
            principal.subject,
        )

        selected = set(analysis["selected_query_ids"])
        ordinal = 0
        for candidate in analysis["candidates"]:
            if candidate["query_id"] not in selected:
                continue
            ordinal += 1
            await db.execute(
                """
                INSERT INTO workload_fingerprint_members (
                    fingerprint_id, query_id, query_text, calls,
                    average_query_runtime_ms, total_runtime_ms,
                    runtime_coverage_pct, impact_score, last_seen_at, ordinal
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                fingerprint_id,
                candidate["query_id"],
                candidate["query_text"] if include_query_text else None,
                candidate["calls"],
                candidate["average_query_runtime_ms"],
                candidate["total_runtime_ms"],
                candidate["runtime_coverage_pct"],
                candidate["impact_score"],
                candidate["last_seen_at"],
                ordinal,
            )
    result = await _read_fingerprint(db, principal.organization_id, fingerprint_id)
    if result is None:  # pragma: no cover - protects against an unexpected lost write
        raise HTTPException(status_code=500, detail="Fingerprint was not persisted")
    return result


@router.get("/candidates", response_model=FingerprintDiagnostics)
async def get_candidates(
    host_id: UUID,
    database_name: Optional[str] = Query(default=None, min_length=1, max_length=63),
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("viewer", "operator", "admin")),
) -> FingerprintDiagnostics:
    host = await _require_host(db, principal.organization_id, host_id)
    resolved_database = database_name or host.get("database_name")
    analysis = analyze_workload_snapshots(await _recent_snapshots(db, host_id))
    return _diagnostics_response(host_id, resolved_database, analysis)


@router.post("/recommend", response_model=WorkloadFingerprint, status_code=201)
async def recommend_fingerprint(
    request: RecommendFingerprintRequest,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("operator", "admin")),
) -> WorkloadFingerprint:
    host = await _require_host(db, principal.organization_id, request.host_id)
    analysis = analyze_workload_snapshots(await _recent_snapshots(db, request.host_id))
    if not analysis["selected_query_ids"]:
        raise HTTPException(status_code=409, detail=analysis["warnings"])
    existing = await db.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM workload_fingerprints
            WHERE organization_id = $1 AND host_id = $2 AND kind = 'recommended'
              AND lower(name) = lower($3)
        )
        """,
        principal.organization_id,
        request.host_id,
        request.name.strip(),
    )
    name = request.name.strip()
    if existing:
        suffix = datetime.now(timezone.utc).strftime(" · %Y-%m-%d %H:%M:%S UTC")
        name = f"{name[: 120 - len(suffix)]}{suffix}"
    return await _persist_fingerprint(
        db,
        principal,
        request.host_id,
        request.database_name or host.get("database_name"),
        name,
        "recommended",
        request.include_query_text,
        analysis,
    )


@router.post("/", response_model=WorkloadFingerprint, status_code=201)
async def create_fingerprint(
    request: CreateFingerprintRequest,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("operator", "admin")),
) -> WorkloadFingerprint:
    if not 1 <= len(request.query_ids) <= 25:
        raise HTTPException(status_code=422, detail="Select between 1 and 25 query IDs")
    host = await _require_host(db, principal.organization_id, request.host_id)
    duplicate = await db.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM workload_fingerprints
            WHERE organization_id = $1 AND host_id = $2 AND lower(name) = lower($3)
        )
        """,
        principal.organization_id,
        request.host_id,
        request.name.strip(),
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="A fingerprint with this name already exists")
    requested_ids = list(dict.fromkeys(str(query_id) for query_id in request.query_ids))
    analysis = analyze_workload_snapshots(
        await _recent_snapshots(db, request.host_id), requested_ids
    )
    missing = sorted(set(requested_ids) - set(analysis["selected_query_ids"]))
    if missing:
        raise HTTPException(
            status_code=409,
            detail={"message": "Some query IDs are no longer visible", "query_ids": missing},
        )
    return await _persist_fingerprint(
        db,
        principal,
        request.host_id,
        request.database_name or host.get("database_name"),
        request.name.strip(),
        "custom",
        request.include_query_text,
        analysis,
    )


@router.get("/", response_model=FingerprintList)
async def list_fingerprints(
    host_id: Optional[UUID] = None,
    database_name: Optional[str] = Query(default=None, min_length=1, max_length=63),
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("viewer", "operator", "admin")),
) -> FingerprintList:
    rows = await db.fetch(
        """
        SELECT id FROM workload_fingerprints
        WHERE organization_id = $1
          AND ($2::uuid IS NULL OR host_id = $2)
          AND ($3::text IS NULL OR database_name = $3)
        ORDER BY updated_at DESC
        """,
        principal.organization_id,
        host_id,
        database_name,
    )
    items = []
    for row in rows:
        item = await _read_fingerprint(db, principal.organization_id, row["id"])
        if item is not None:
            items.append(item)
    return FingerprintList(fingerprints=items, total=len(items))


@router.get("/{fingerprint_id}", response_model=WorkloadFingerprint)
async def get_fingerprint(
    fingerprint_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("viewer", "operator", "admin")),
) -> WorkloadFingerprint:
    result = await _read_fingerprint(db, principal.organization_id, fingerprint_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Workload fingerprint not found")
    return result
