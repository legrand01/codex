"""
Audit Log API endpoints.

Provides routes for:
- GET /api/v1/audit/{run_id} - returns audit entries for a specific run
- GET /api/v1/audit/ - returns recent audit entries with pagination

Requirements: 10.5
"""

import logging
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.dependencies import get_db
from backend.models.audit import AuditEntry
from backend.services.audit_logger import AuditLoggerError, get_audit_logger

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


# --- Response Models ---


class AuditListResponse(BaseModel):
    """Response model for audit log listing."""

    entries: List[AuditEntry]
    total: int
    limit: int
    offset: int


# --- Endpoints ---


@router.get("/", response_model=AuditListResponse)
async def list_audit_entries(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    start_time: Optional[datetime] = Query(default=None),
    end_time: Optional[datetime] = Query(default=None),
) -> AuditListResponse:
    """
    List recent audit log entries with pagination.

    Returns entries in chronological order (oldest first).
    Optionally filter by time range.

    Requirements: 10.5
    """
    audit_logger = get_audit_logger()

    time_range = None
    if start_time and end_time:
        time_range = (start_time, end_time)

    try:
        entries = await audit_logger.query(
            time_range=time_range,
            limit=limit,
            offset=offset,
        )
    except AuditLoggerError as e:
        raise HTTPException(status_code=503, detail=f"Audit log query failed: {e}")

    return AuditListResponse(
        entries=entries,
        total=len(entries),
        limit=limit,
        offset=offset,
    )


@router.get("/{run_id}", response_model=AuditListResponse)
async def get_audit_entries_for_run(
    run_id: UUID,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> AuditListResponse:
    """
    Get all audit log entries for a specific loop run.

    Returns entries in chronological order (oldest first) within 10 seconds.

    Requirements: 10.5
    """
    audit_logger = get_audit_logger()

    try:
        entries = await audit_logger.query(
            run_id=run_id,
            limit=limit,
            offset=offset,
        )
    except AuditLoggerError as e:
        raise HTTPException(status_code=503, detail=f"Audit log query failed: {e}")

    return AuditListResponse(
        entries=entries,
        total=len(entries),
        limit=limit,
        offset=offset,
    )
