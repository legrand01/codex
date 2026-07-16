"""Tenant-scoped coded operational event API."""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.dependencies import get_db
from backend.security import Principal, require_roles

router = APIRouter(prefix="/api/v1/events", tags=["events"])


class EventCodeResponse(BaseModel):
    event_code: str
    default_severity: str
    component: str
    description: str


class OperationalEventResponse(BaseModel):
    id: int
    host_id: Optional[UUID] = None
    run_id: Optional[UUID] = None
    configuration_version_id: Optional[UUID] = None
    occurred_at: datetime
    severity: str
    component: str
    event_code: str
    message: str
    details: Dict[str, Any]
    host_name: Optional[str] = None
    run_href: Optional[str] = None
    configuration_href: Optional[str] = None


class OperationalEventListResponse(BaseModel):
    events: List[OperationalEventResponse]
    total: int
    page: int
    page_size: int


@router.get("/catalog", response_model=List[EventCodeResponse])
async def list_event_codes(
    db=Depends(get_db),
    _principal: Principal = Depends(
        require_roles("viewer", "operator", "approver", "admin")
    ),
) -> List[EventCodeResponse]:
    rows = await db.fetch(
        """
        SELECT event_code, default_severity, component, description
        FROM event_code_catalog ORDER BY component, event_code
        """
    )
    return [EventCodeResponse(**dict(row)) for row in rows]


@router.get("/", response_model=OperationalEventListResponse)
async def list_events(
    time_from: Optional[datetime] = None,
    time_to: Optional[datetime] = None,
    severity: List[str] = Query(default=[]),
    code: List[str] = Query(default=[]),
    host_id: Optional[UUID] = None,
    run_id: Optional[UUID] = None,
    component: List[str] = Query(default=[]),
    q: Optional[str] = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    db=Depends(get_db),
    principal: Principal = Depends(
        require_roles("viewer", "operator", "approver", "admin")
    ),
) -> OperationalEventListResponse:
    filters = ["e.organization_id = $1"]
    args: list[object] = [principal.organization_id]

    def add(expression: str, value: object) -> None:
        args.append(value)
        filters.append(expression.replace("?", f"${len(args)}"))

    if time_from:
        add("e.occurred_at >= ?", time_from)
    if time_to:
        add("e.occurred_at <= ?", time_to)
    if severity:
        add("e.severity = ANY(?::text[])", severity)
    if code:
        add("e.event_code = ANY(?::text[])", code)
    if host_id:
        add("e.host_id = ?", host_id)
    if run_id:
        add("e.run_id = ?", run_id)
    if component:
        add("e.component = ANY(?::text[])", component)
    if q and q.strip():
        add(
            "to_tsvector('simple', e.message || ' ' || e.details::text) "
            "@@ plainto_tsquery('simple', ?)",
            q.strip(),
        )
    where = " AND ".join(filters)
    total = await db.fetchval(f"SELECT COUNT(*) FROM host_events e WHERE {where}", *args)
    rows = await db.fetch(
        f"""
        SELECT e.id, e.host_id, e.run_id, e.configuration_version_id,
               e.occurred_at, e.severity, e.component, e.event_code,
               e.message, e.details, h.hostname AS host_name
        FROM host_events e
        LEFT JOIN hosts h ON h.id = e.host_id
        WHERE {where}
        ORDER BY e.occurred_at DESC, e.id DESC
        LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
        """,
        *args,
        page_size,
        (page - 1) * page_size,
    )
    events = []
    for row in rows:
        item = dict(row)
        details = item.get("details") or {}
        if isinstance(details, str):
            details = json.loads(details)
        item["details"] = details
        item["run_href"] = (
            f"/tuning/{item['run_id']}?tab=activity" if item["run_id"] else None
        )
        item["configuration_href"] = (
            f"/tuning/{item['run_id']}?tab=configuration"
            if item["run_id"]
            else f"/configurations/{item['configuration_version_id']}"
            if item["configuration_version_id"]
            else None
        )
        events.append(OperationalEventResponse(**item))
    return OperationalEventListResponse(
        events=events, total=int(total or 0), page=page, page_size=page_size
    )
