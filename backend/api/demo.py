"""
Demo Mode API endpoints for the Autonomous Postgres DBA Agent Platform.

Provides:
- POST /api/v1/demo/activate - Activate demo mode with synthetic data
- POST /api/v1/demo/deactivate - Deactivate demo mode and clear data
- GET /api/v1/demo/status - Get current demo mode status

Requirements: 14.4
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from backend.config import settings
from backend.db.pool import get_pool
from backend.security import require_roles
from backend.services.demo_mode import (
    SYNTHETIC_HOST_ADDRESSES,
    activate_demo_mode,
    deactivate_demo_mode,
    get_demo_data,
    get_demo_status,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/demo",
    tags=["demo"],
    dependencies=[Depends(require_roles("admin"))],
)


def _require_non_production_demo() -> None:
    if settings.environment == "production":
        raise HTTPException(status_code=404, detail="Demo mode is unavailable")


def _jsonb(value: Any) -> str:
    """Serialize demo payloads for explicit JSONB inserts."""
    return json.dumps(value, default=str)


async def _clear_demo_database(db) -> None:
    """Remove previous mutable synthetic demo rows before reseeding."""
    host_rows = await db.fetch(
        "SELECT id FROM hosts WHERE hostname = ANY($1::text[])",
        list(SYNTHETIC_HOST_ADDRESSES),
    )
    host_ids = [row["id"] for row in host_rows]
    if not host_ids:
        return

    await db.execute(
        """
        WITH demo_runs AS (
            SELECT id FROM loop_runs WHERE host_id = ANY($1::uuid[])
        )
        DELETE FROM dba_reports
        WHERE host_id = ANY($1::uuid[])
           OR run_id IN (SELECT id FROM demo_runs)
        """,
        host_ids,
    )
    await db.execute(
        """
        WITH demo_runs AS (
            SELECT id FROM loop_runs WHERE host_id = ANY($1::uuid[])
        )
        DELETE FROM plans
        WHERE host_id = ANY($1::uuid[])
           OR run_id IN (SELECT id FROM demo_runs)
        """,
        host_ids,
    )
    await db.execute(
        """
        WITH demo_runs AS (
            SELECT id FROM loop_runs WHERE host_id = ANY($1::uuid[])
        )
        DELETE FROM evidence_snapshots
        WHERE host_id = ANY($1::uuid[])
           OR run_id IN (SELECT id FROM demo_runs)
        """,
        host_ids,
    )
    await db.execute(
        "DELETE FROM loop_runs WHERE host_id = ANY($1::uuid[])",
        host_ids,
    )
    await db.execute(
        "DELETE FROM guardrail_allowlist WHERE host_id = ANY($1::uuid[])",
        host_ids,
    )
    await db.execute(
        "DELETE FROM agent_config WHERE host_id = ANY($1::uuid[])",
        host_ids,
    )
    await db.execute("DELETE FROM hosts WHERE id = ANY($1::uuid[])", host_ids)


async def _seed_demo_database(db, data: Dict[str, Any]) -> None:
    """Persist synthetic demo data into the normal control-plane tables."""
    hosts = data.get("hosts", [])
    runs = data.get("runs", [])
    evidence = data.get("evidence", [])
    plans = data.get("plans", [])
    audit_entries = data.get("audit_entries", [])

    host_id_map = {}
    for host in hosts:
        row = await db.fetchrow(
            """
            INSERT INTO hosts (
                id, organization_id, hostname, pg_version, server_role, health_status,
                connection_status, last_heartbeat, restart_required_enabled
            )
            VALUES (
                $1, '00000000-0000-0000-0000-000000000001'::uuid,
                $2, $3, $4, $5, $6, $7, $8
            )
            ON CONFLICT (organization_id, hostname) DO UPDATE SET
                pg_version = EXCLUDED.pg_version,
                server_role = EXCLUDED.server_role,
                health_status = EXCLUDED.health_status,
                connection_status = EXCLUDED.connection_status,
                last_heartbeat = EXCLUDED.last_heartbeat,
                restart_required_enabled = EXCLUDED.restart_required_enabled,
                updated_at = NOW()
            RETURNING id
            """,
            host["id"],
            host["hostname"],
            host.get("pg_version"),
            host.get("server_role"),
            host.get("health_status"),
            host.get("connection_status"),
            host.get("last_heartbeat"),
            host.get("restart_required_enabled", False),
        )
        host_id_map[host["id"]] = row["id"]

    run_id_map = {}
    for run in runs:
        host_id = host_id_map.get(run.get("host_id"), run.get("host_id"))
        await db.execute(
            """
            INSERT INTO loop_runs (
                id, host_id, goal, status, current_step, current_iteration,
                max_iterations, max_steps, approval_timeout_hours,
                verification_window_seconds, degradation_threshold_pct,
                started_at, last_step_transition_at, completed_at, failure_reason
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                current_step = EXCLUDED.current_step,
                current_iteration = EXCLUDED.current_iteration,
                last_step_transition_at = EXCLUDED.last_step_transition_at,
                completed_at = EXCLUDED.completed_at,
                failure_reason = EXCLUDED.failure_reason
            """,
            run["id"],
            host_id,
            run["goal"],
            run["status"],
            run.get("current_step"),
            run.get("current_iteration", 1),
            run.get("max_iterations", 10),
            run.get("max_steps", 20),
            run.get("approval_timeout_hours", 24),
            run.get("verification_window_seconds", 60),
            run.get("degradation_threshold_pct", 10.0),
            run.get("started_at"),
            run.get("last_step_transition_at"),
            run.get("completed_at"),
            run.get("failure_reason"),
        )
        run_id_map[run["id"]] = run["id"]

    active_run = next((run for run in runs if run.get("status") == "running"), None)
    evidence_run_id = active_run["id"] if active_run else (runs[0]["id"] if runs else None)
    evidence_ids = []
    for snapshot in evidence:
        host_id = host_id_map.get(snapshot.get("host_id"), snapshot.get("host_id"))
        run_id = run_id_map.get(snapshot.get("run_id")) or evidence_run_id
        await db.execute(
            """
            INSERT INTO evidence_snapshots (
                id, run_id, host_id, evidence_type, collected_at, data, quality_score
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
            ON CONFLICT (id) DO UPDATE SET
                run_id = EXCLUDED.run_id,
                host_id = EXCLUDED.host_id,
                evidence_type = EXCLUDED.evidence_type,
                collected_at = EXCLUDED.collected_at,
                data = EXCLUDED.data,
                quality_score = EXCLUDED.quality_score
            """,
            snapshot["id"],
            run_id,
            host_id,
            snapshot["evidence_type"],
            snapshot["collected_at"],
            _jsonb(snapshot["data"]),
            snapshot.get("quality_score"),
        )
        evidence_ids.append(snapshot["id"])

    for plan in plans:
        host_id = host_id_map.get(plan.get("host_id"), plan.get("host_id"))
        run_id = run_id_map.get(plan.get("run_id"), plan.get("run_id"))
        evidence_refs = plan.get("evidence_references", [])
        if evidence_ids:
            evidence_refs = [
                {"snapshot_id": str(snapshot_id), "timestamp": data["activated_at"]}
                for snapshot_id in evidence_ids[: max(1, min(2, len(evidence_ids)))]
            ]

        await db.execute(
            """
            INSERT INTO plans (
                id, run_id, host_id, status, proposed_changes, evidence_references,
                risk_score, confidence_score, uncertainty_explanation,
                rollback_instructions, rejection_reason, approved_by, approved_at,
                applied_at, submission_time
            )
            VALUES (
                $1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9,
                $10::jsonb, $11, $12, $13, $14, $15
            )
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                proposed_changes = EXCLUDED.proposed_changes,
                evidence_references = EXCLUDED.evidence_references,
                risk_score = EXCLUDED.risk_score,
                confidence_score = EXCLUDED.confidence_score,
                uncertainty_explanation = EXCLUDED.uncertainty_explanation,
                rollback_instructions = EXCLUDED.rollback_instructions,
                rejection_reason = EXCLUDED.rejection_reason,
                approved_by = EXCLUDED.approved_by,
                approved_at = EXCLUDED.approved_at,
                applied_at = EXCLUDED.applied_at,
                submission_time = EXCLUDED.submission_time
            """,
            plan["id"],
            run_id,
            host_id,
            plan["status"],
            _jsonb(plan.get("proposed_changes", [])),
            _jsonb(evidence_refs),
            plan.get("risk_score"),
            plan.get("confidence_score"),
            plan.get("uncertainty_explanation"),
            _jsonb(plan.get("rollback_instructions", [])),
            plan.get("rejection_reason"),
            plan.get("approved_by"),
            plan.get("approved_at"),
            plan.get("applied_at"),
            plan.get("submission_time"),
        )

    for entry in audit_entries:
        await db.execute(
            """
            INSERT INTO audit_log (
                run_id, timestamp, actor_type, actor_name, action_type,
                target_host_id, result, result_reason, details
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            """,
            run_id_map.get(entry.get("run_id"), entry.get("run_id")),
            entry.get("timestamp"),
            entry.get("actor_type"),
            entry.get("actor_name"),
            entry.get("action_type"),
            host_id_map.get(entry.get("target_host_id"), entry.get("target_host_id")),
            entry.get("result"),
            entry.get("result_reason"),
            _jsonb(entry.get("details")),
        )

    if runs and plans:
        successful_run = runs[0]
        applied_plan = plans[0]
        report_content = {
            "evidence_summaries": [
                {
                    "evidence_type": item["evidence_type"],
                    "quality_score": item.get("quality_score"),
                    "summary": "Synthetic evidence generated by demo mode.",
                }
                for item in evidence
            ],
            "plans_proposed": [applied_plan],
            "approval_decisions": [
                {
                    "plan_id": str(applied_plan["id"]),
                    "approved_by": applied_plan.get("approved_by"),
                    "approved_at": (
                        applied_plan.get("approved_at").isoformat()
                        if hasattr(applied_plan.get("approved_at"), "isoformat")
                        else applied_plan.get("approved_at")
                    ),
                }
            ],
            "applied_changes": applied_plan.get("proposed_changes", []),
            "verification_results": [
                {
                    "metric": "mean query latency",
                    "before": "245.8 ms",
                    "after": "81.2 ms",
                    "status": "improved",
                }
            ],
        }
        await db.execute(
            """
            INSERT INTO dba_reports (
                run_id, goal, host_id, outcome_status, report_content,
                generated_at, expires_at
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
            ON CONFLICT (run_id) DO UPDATE SET
                outcome_status = EXCLUDED.outcome_status,
                report_content = EXCLUDED.report_content,
                generated_at = EXCLUDED.generated_at,
                expires_at = EXCLUDED.expires_at
            """,
            successful_run["id"],
            successful_run["goal"],
            host_id_map.get(successful_run.get("host_id"), successful_run.get("host_id")),
            "success",
            _jsonb(report_content),
            datetime.now(timezone.utc),
            datetime.now(timezone.utc) + timedelta(days=90),
        )


async def _seed_demo_database_if_available(data: Dict[str, Any]) -> None:
    """Seed the control plane when the app has an initialized database pool."""
    pool = get_pool()
    if pool is None:
        logger.info("Skipping demo database seed because the DB pool is not initialized.")
        return

    async with pool.acquire() as db:
        async with db.transaction():
            await _clear_demo_database(db)
            await _seed_demo_database(db, data)


async def _clear_demo_database_if_available() -> None:
    """Clear persisted synthetic rows when the app has an initialized DB pool."""
    pool = get_pool()
    if pool is None:
        logger.info("Skipping demo database cleanup because the DB pool is not initialized.")
        return

    async with pool.acquire() as db:
        async with db.transaction():
            await _clear_demo_database(db)


@router.post("/activate")
async def activate_demo():
    """
    Activate demo mode and seed synthetic data.

    Seeds the platform with realistic fleet data, evidence, loop runs,
    and plans to demonstrate full functionality without production databases.

    Returns:
        Activation summary with counts of seeded data.

    Raises:
        HTTPException 409: If demo mode is already active.
    """
    _require_non_production_demo()
    try:
        result = activate_demo_mode()
        await _seed_demo_database_if_available(get_demo_data())
        logger.info("Demo mode activated via API.")
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/deactivate")
async def deactivate_demo():
    """
    Deactivate demo mode and clear all synthetic data.

    Returns:
        Deactivation confirmation.

    Raises:
        HTTPException 409: If demo mode is not currently active.
    """
    _require_non_production_demo()
    try:
        result = deactivate_demo_mode()
        await _clear_demo_database_if_available()
        logger.info("Demo mode deactivated via API.")
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/status")
async def demo_status():
    """
    Get current demo mode status.

    Returns:
        Current demo mode state and summary if active.
    """
    _require_non_production_demo()
    return get_demo_status()
