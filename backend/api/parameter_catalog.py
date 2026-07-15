"""Versioned parameter catalog and tenant-scoped run disposition API."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.dependencies import get_db
from backend.security import Principal, require_roles
from backend.services.parameter_catalog import refresh_parameter_dispositions

router = APIRouter(prefix="/api/v1/runs", tags=["parameter-catalog"])


class ParameterDispositionResponse(BaseModel):
    id: UUID
    run_id: UUID
    host_id: UUID
    catalog_version: str
    setting_name: str
    display_order: int
    apply_context: str
    bounded_domain_available: bool
    selected: bool
    supported_on_target: bool
    allowlisted: bool
    current_value: Optional[str] = None
    unit: Optional[str] = None
    source: Optional[str] = None
    sourcefile_or_provider: Optional[str] = None
    setting_context: Optional[str] = None
    pending_restart: bool
    baseline_value: Optional[str] = None
    best_verified_value: Optional[str] = None
    pending_candidate_value: Optional[str] = None
    final_disposition: Optional[str] = None
    disposition_reason: Optional[str] = None
    updated_at: datetime


@router.get(
    "/{run_id}/parameter-dispositions",
    response_model=List[ParameterDispositionResponse],
)
async def list_parameter_dispositions(
    run_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(
        require_roles("viewer", "operator", "approver", "admin")
    ),
) -> List[ParameterDispositionResponse]:
    owned = await db.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM loop_runs
            WHERE id = $1 AND organization_id = $2
        )
        """,
        run_id,
        principal.organization_id,
    )
    if not owned:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    await refresh_parameter_dispositions(db, run_id)
    rows = await db.fetch(
        """
        SELECT id, run_id, host_id, catalog_version, setting_name,
               display_order, apply_context, bounded_domain_available,
               selected, supported_on_target, allowlisted, current_value,
               unit, source, sourcefile_or_provider, setting_context,
               pending_restart, baseline_value, best_verified_value,
               pending_candidate_value, final_disposition,
               disposition_reason, updated_at
        FROM run_parameter_dispositions
        WHERE run_id = $1 AND organization_id = $2
        ORDER BY display_order
        """,
        run_id,
        principal.organization_id,
    )
    return [ParameterDispositionResponse(**dict(row)) for row in rows]
