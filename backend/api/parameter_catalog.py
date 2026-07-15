"""Versioned parameter catalog and tenant-scoped run disposition API."""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional
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


class ConfigurationVersionResponse(BaseModel):
    id: UUID
    host_id: UUID
    plan_id: Optional[UUID] = None
    configuration_backend: str
    status: str
    managed_conf_path: Optional[str] = None
    parameters: List[Dict[str, Any]]
    backend_snapshot: Dict[str, Any]
    apply_result: Optional[Dict[str, Any]] = None
    rollback_result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: datetime
    applied_at: Optional[datetime] = None
    rolled_back_at: Optional[datetime] = None


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


@router.get(
    "/{run_id}/configuration-versions",
    response_model=List[ConfigurationVersionResponse],
)
async def list_configuration_versions(
    run_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(
        require_roles("viewer", "operator", "approver", "admin")
    ),
) -> List[ConfigurationVersionResponse]:
    owned = await db.fetchval(
        "SELECT EXISTS (SELECT 1 FROM loop_runs WHERE id=$1 AND organization_id=$2)",
        run_id,
        principal.organization_id,
    )
    if not owned:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    rows = await db.fetch(
        """
        SELECT v.id, v.host_id, v.plan_id, v.configuration_backend, v.status,
               v.managed_conf_path, v.parameters,
               (v.backend_snapshot #- '{file,bytes_b64}') AS backend_snapshot,
               (v.apply_result #- '{backend_snapshot,file,bytes_b64}') AS apply_result,
               v.rollback_result, v.error, v.created_at,
               v.applied_at, v.rolled_back_at
        FROM configuration_versions v
        JOIN plans p ON p.id = v.plan_id
        WHERE p.run_id = $1 AND v.organization_id = $2
        ORDER BY v.created_at DESC
        """,
        run_id,
        principal.organization_id,
    )
    result = []
    for row in rows:
        item = dict(row)
        for key, default in (
            ("parameters", []),
            ("backend_snapshot", {}),
            ("apply_result", None),
            ("rollback_result", None),
        ):
            if isinstance(item.get(key), str):
                item[key] = json.loads(item[key])
            elif item.get(key) is None and default is not None:
                item[key] = default
        result.append(ConfigurationVersionResponse(**item))
    return result
