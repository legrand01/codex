"""Configuration history, comparison, export, and guarded reapply requests."""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from backend.db.pool import get_pool
from backend.dependencies import get_db
from backend.security import Principal, require_roles
from backend.services.baseline_measurement import capture_baseline
from backend.services.configuration_backends import get_configuration_backend
from backend.services.operational_events import OperationalEventRecorder

router = APIRouter(prefix="/api/v1/configurations", tags=["configurations"])


class ConfigurationVersionSummary(BaseModel):
    id: UUID
    host_id: UUID
    run_id: Optional[UUID] = None
    plan_id: Optional[UUID] = None
    database_name: Optional[str] = None
    configuration_backend: str
    status: str
    parameters: List[Dict[str, Any]]
    source_provenance: Dict[str, Any]
    verification_result: Optional[Dict[str, Any]] = None
    created_at: datetime
    applied_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    superseded_at: Optional[datetime] = None
    rolled_back_at: Optional[datetime] = None
    origin_configuration_version_id: Optional[UUID] = None
    active: bool


class ConfigurationHistoryResponse(BaseModel):
    versions: List[ConfigurationVersionSummary]
    total: int


class ConfigurationDifference(BaseModel):
    setting_name: str
    left_value: Optional[str] = None
    right_value: Optional[str] = None
    changed: bool


class ConfigurationCompareResponse(BaseModel):
    left: ConfigurationVersionSummary
    right: ConfigurationVersionSummary
    differences: List[ConfigurationDifference]


class ReapplyResponse(BaseModel):
    configuration_version_id: UUID
    run_id: UUID
    plan_id: UUID
    status: str
    run_href: str
    approval_href: str


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def _summary(row: Any) -> ConfigurationVersionSummary:
    item = dict(row)
    item["parameters"] = _json(item.get("parameters"), [])
    item["source_provenance"] = _json(item.get("source_provenance"), {})
    item["verification_result"] = _json(item.get("verification_result"), None)
    item["active"] = item["status"] in {"active", "pending_restart"}
    return ConfigurationVersionSummary(**item)


async def _owned_version(db, version_id: UUID, organization_id: UUID):
    return await db.fetchrow(
        """
        SELECT id, host_id, run_id, plan_id, database_name,
               configuration_backend, status, parameters, source_provenance,
               verification_result, created_at, applied_at, verified_at,
               superseded_at, rolled_back_at, origin_configuration_version_id
        FROM configuration_versions
        WHERE id = $1 AND organization_id = $2
        """,
        version_id,
        organization_id,
    )


@router.get("/", response_model=ConfigurationHistoryResponse)
async def list_configuration_history(
    host_id: UUID,
    database_name: Optional[str] = None,
    db=Depends(get_db),
    principal: Principal = Depends(
        require_roles("viewer", "operator", "approver", "admin")
    ),
) -> ConfigurationHistoryResponse:
    args: list[object] = [principal.organization_id, host_id]
    database_filter = ""
    if database_name is not None:
        args.append(database_name)
        database_filter = f"AND database_name = ${len(args)}"
    rows = await db.fetch(
        f"""
        SELECT id, host_id, run_id, plan_id, database_name,
               configuration_backend, status, parameters, source_provenance,
               verification_result, created_at, applied_at, verified_at,
               superseded_at, rolled_back_at, origin_configuration_version_id
        FROM configuration_versions
        WHERE organization_id = $1 AND host_id = $2 {database_filter}
        ORDER BY created_at DESC
        """,
        *args,
    )
    versions = [_summary(row) for row in rows]
    return ConfigurationHistoryResponse(versions=versions, total=len(versions))


@router.get("/compare", response_model=ConfigurationCompareResponse)
async def compare_configurations(
    left_id: UUID,
    right_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(
        require_roles("viewer", "operator", "approver", "admin")
    ),
) -> ConfigurationCompareResponse:
    left_row = await _owned_version(db, left_id, principal.organization_id)
    right_row = await _owned_version(db, right_id, principal.organization_id)
    if left_row is None or right_row is None:
        raise HTTPException(status_code=404, detail="Configuration version not found")
    left, right = _summary(left_row), _summary(right_row)
    if left.host_id != right.host_id or left.database_name != right.database_name:
        raise HTTPException(
            status_code=409,
            detail="Only versions for the same host and database can be compared",
        )

    def values(version: ConfigurationVersionSummary) -> Dict[str, str]:
        result = {}
        for item in version.parameters:
            name = item.get("setting_name") or item.get("name")
            value = item.get("proposed_value", item.get("value"))
            if name is not None and value is not None:
                result[str(name)] = str(value)
        return result

    left_values, right_values = values(left), values(right)
    differences = [
        ConfigurationDifference(
            setting_name=name,
            left_value=left_values.get(name),
            right_value=right_values.get(name),
            changed=left_values.get(name) != right_values.get(name),
        )
        for name in sorted(set(left_values) | set(right_values))
    ]
    return ConfigurationCompareResponse(left=left, right=right, differences=differences)


@router.get("/{version_id}/download", response_class=PlainTextResponse)
async def download_configuration(
    version_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(
        require_roles("viewer", "operator", "approver", "admin")
    ),
) -> PlainTextResponse:
    row = await _owned_version(db, version_id, principal.organization_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Configuration version not found")
    version = _summary(row)
    lines = [
        "# dbtune configuration export (secrets and target connection data omitted)",
        f"# version: {version.id}",
        f"# backend: {version.configuration_backend}",
        f"# status: {version.status}",
    ]
    for item in sorted(version.parameters, key=lambda value: str(value.get("setting_name"))):
        name = item.get("setting_name") or item.get("name")
        value = item.get("proposed_value", item.get("value"))
        if name is not None and value is not None:
            escaped = str(value).replace("'", "''")
            lines.append(f"{name} = '{escaped}'")
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        headers={"Content-Disposition": f'attachment; filename="dbtune-{version.id}.conf"'},
    )


@router.post("/{version_id}/reapply", response_model=ReapplyResponse)
async def request_configuration_reapply(
    version_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("operator", "admin")),
) -> ReapplyResponse:
    row = await _owned_version(db, version_id, principal.organization_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Configuration version not found")
    version = _summary(row)
    if version.status not in {"superseded", "rolled_back"}:
        raise HTTPException(
            status_code=409,
            detail="Only superseded or rolled-back verified versions are eligible",
        )
    if not version.verification_result or not version.verification_result.get("succeeded"):
        raise HTTPException(
            status_code=409,
            detail="Only versions with successful verification provenance are eligible",
        )
    if version.run_id is None or not version.parameters:
        raise HTTPException(status_code=409, detail="Version lacks reusable run provenance")
    pool = get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database pool is unavailable")
    changes = []
    for item in version.parameters:
        name = item.get("setting_name") or item.get("name")
        value = item.get("proposed_value", item.get("value"))
        if name is not None and value is not None:
            changes.append(
                {
                    "change_type": "setting",
                    "setting_name": str(name),
                    "proposed_value": str(value),
                    "reason": f"Reapply verified configuration {version.id}",
                }
            )
    backend = await get_configuration_backend(pool, version.host_id)
    try:
        snapshot = await backend.capture_snapshot(
            version.host_id, [item["setting_name"] for item in changes]
        )
        dry_run = await backend.dry_run(version.host_id, changes)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Live preflight failed: {exc}") from exc
    if not dry_run.passed:
        raise HTTPException(status_code=409, detail={"dry_run_errors": dry_run.errors})
    allowlisted = await db.fetchval(
        """
        SELECT COUNT(*) = $2 FROM guardrail_allowlist
        WHERE host_id = $1 AND setting_name = ANY($3::text[])
        """,
        version.host_id,
        len(changes),
        [item["setting_name"] for item in changes],
    )
    if not allowlisted:
        raise HTTPException(status_code=409, detail="Version parameters are no longer allowlisted")
    rollback = [
        {
            "setting_name": name,
            "restore_value": state["value"],
            "reason": "Restore live state captured before guarded reapply",
        }
        for name, state in snapshot.items()
    ]
    evidence = [{"configuration_version_id": str(version.id), "kind": "verified_history"}]
    async with db.transaction():
        new_run_id = await db.fetchval(
            """
            INSERT INTO loop_runs (
                host_id, organization_id, goal, status, current_step,
                current_iteration, max_iterations, max_steps,
                approval_timeout_hours, verification_window_seconds,
                degradation_threshold_pct, database_name, tuning_target,
                tuning_mode, workload_fingerprint_id, selected_parameters,
                approval_policy, warmup_window_seconds,
                measurement_window_seconds, objective_guardrails,
                configuration_backend, baseline_score, best_score,
                parameter_catalog_version
            )
            SELECT host_id, organization_id,
                   'Reapply verified configuration ' || $2::text,
                   'running', 'snapshot', 1, 1, max_steps,
                   approval_timeout_hours, verification_window_seconds,
                   degradation_threshold_pct, database_name, tuning_target,
                   tuning_mode, workload_fingerprint_id, $3::jsonb,
                   'per_candidate', warmup_window_seconds,
                   measurement_window_seconds, objective_guardrails,
                   configuration_backend, baseline_score, best_score,
                   parameter_catalog_version
            FROM loop_runs WHERE id = $1 AND organization_id = $4
            RETURNING id
            """,
            version.run_id,
            str(version.id),
            json.dumps([item["setting_name"] for item in changes]),
            principal.organization_id,
        )
        if new_run_id is None:
            raise HTTPException(status_code=409, detail="Originating run is unavailable")
    try:
        baseline = await capture_baseline(new_run_id, pool)
    except Exception as exc:
        await db.execute(
            """
            UPDATE loop_runs SET status='failed', completed_at=NOW(),
                failure_reason=$2 WHERE id=$1
            """,
            new_run_id, f"Fresh reapply baseline failed: {exc}",
        )
        raise HTTPException(status_code=409, detail=f"Fresh baseline failed: {exc}") from exc
    if baseline["status"] != "ready" or baseline["objective_score"] is None:
        await db.execute(
            """
            UPDATE loop_runs SET status='failed', completed_at=NOW(),
                failure_reason=$2 WHERE id=$1
            """,
            new_run_id, "Fresh workload baseline is not ready for guarded reapply",
        )
        raise HTTPException(
            status_code=409,
            detail="Fresh workload baseline is not ready; collect stable evidence first",
        )
    evidence.append({"baseline_id": str(baseline["id"]), "kind": "fresh_baseline"})
    await db.execute(
        """
        UPDATE loop_runs SET status='waiting_approval', current_step='approval_gate',
            baseline_score=$2, best_score=$2 WHERE id=$1
        """,
        new_run_id, float(baseline["objective_score"]),
    )
    async with db.transaction():
        plan_id = await db.fetchval(
            """
            INSERT INTO plans (
                run_id, host_id, organization_id, status, proposed_changes,
                evidence_references, risk_score, confidence_score,
                uncertainty_explanation, rollback_instructions,
                pre_change_snapshot, planning_policy_version, planner_kind,
                configuration_backend, source_configuration_version_id
            ) VALUES (
                $1, $2, $3, 'pending_approval', $4::jsonb, $5::jsonb,
                30, 0.95, 'Historical success does not guarantee current workload benefit',
                $6::jsonb, $7::jsonb, 'configuration-reapply-v1',
                'configuration_history', $8, $9
            ) RETURNING id
            """,
            new_run_id, version.host_id, principal.organization_id,
            json.dumps(changes), json.dumps(evidence), json.dumps(rollback),
            json.dumps(snapshot), version.configuration_backend, version.id,
        )
        await db.execute(
            """
            INSERT INTO tuning_candidates (
                organization_id, run_id, host_id, plan_id, iteration,
                domain_version, parameter_values, pre_change_snapshot,
                baseline_score, best_score_before, objective_formula,
                objective_direction, metric_units, warmup_window_seconds,
                measurement_window_seconds, evidence_references,
                confidence_score, decision
            ) VALUES (
                $1, $2, $3, $4, 1, 'configuration-reapply-v1', $5::jsonb,
                $6::jsonb, $7, $7, $8, $9, $10::jsonb, $11, $12,
                $13::jsonb, 0.95, 'pending_approval'
            )
            """,
            principal.organization_id, new_run_id, version.host_id, plan_id,
            json.dumps({item["setting_name"]: item["proposed_value"] for item in changes}),
            json.dumps(snapshot), float(baseline["objective_score"]),
            baseline["objective_formula"], baseline["objective_direction"],
            json.dumps(_json(baseline["metric_units"], {})),
            baseline["warmup_window_seconds"],
            baseline["requested_measurement_window_seconds"], json.dumps(evidence),
        )
        await db.execute(
            """
            INSERT INTO run_jobs (run_id, organization_id, status)
            VALUES ($1, $2, 'waiting_approval')
            """,
            new_run_id, principal.organization_id,
        )
        await OperationalEventRecorder(pool).record(
            "CONFIG_REAPPLY_REQUESTED",
            "A prior verified configuration was requested for reapply and awaits approval",
            organization_id=principal.organization_id, host_id=version.host_id,
            run_id=new_run_id, configuration_version_id=version.id,
            details={"plan_id": str(plan_id), "source_version_id": str(version.id)},
            connection=db,
        )
    return ReapplyResponse(
        configuration_version_id=version.id, run_id=new_run_id, plan_id=plan_id,
        status="pending_approval", run_href=f"/tuning/{new_run_id}?tab=plans",
        approval_href=f"/plans?run_id={new_run_id}",
    )
