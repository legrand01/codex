"""Restart-safe state machine for one durable DBA run job."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from backend.models.config import LoopConfig
from backend.models.enums import PlanStatus, WorkflowStep
from backend.services.audit_logger import AuditLogger, get_audit_logger
from backend.services.loop_worker import DBALoopWorker
from backend.services.plan_execution import PlanExecutionService
from backend.services.report_generator import ReportGenerator
from backend.services.target_executor import TargetPostgresExecutor
from backend.services.verification import METRIC_CATEGORIES, verify_and_decide


@dataclass(frozen=True)
class RunProcessResult:
    disposition: str
    message: str


def _json(value: Any, default):
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


class DurableRunOrchestrator:
    """Advance a persisted run to its next durable boundary."""

    def __init__(self, pool, *, audit_logger: Optional[AuditLogger] = None) -> None:
        self.pool = pool
        self.audit_logger = audit_logger or get_audit_logger()

    async def process(self, run_id: UUID) -> RunProcessResult:
        run = await self._load_run(run_id)
        plan = await self._load_latest_plan(run_id)
        config = LoopConfig(
            max_iterations=run["max_iterations"],
            max_steps=run["max_steps"],
            approval_timeout_hours=run["approval_timeout_hours"],
            verification_window_seconds=run["verification_window_seconds"],
            degradation_threshold_pct=float(run["degradation_threshold_pct"]),
        )

        if run["status"] == "manually_halted":
            return RunProcessResult("cancelled", "Run was cancelled")

        if plan is None:
            return await self._prepare_plan(run, config)
        return await self._advance_plan(run, plan, config)

    async def _prepare_plan(self, run: Dict[str, Any], config: LoopConfig) -> RunProcessResult:
        run_id = run["id"]
        host_id = run["host_id"]
        worker = DBALoopWorker(pool=self.pool, audit_logger=self.audit_logger)

        await self._set_run(run_id, "running", WorkflowStep.OBSERVE)
        evidence = await worker._collect_evidence(run_id, host_id)
        if not evidence.success:
            return await self._fail_run(run_id, evidence.error or "Evidence collection failed")

        await self._set_run(run_id, "running", WorkflowStep.SNAPSHOT)
        snapshot = await worker._capture_target_snapshot(run_id, host_id)
        if not snapshot.success:
            return await self._fail_run(run_id, snapshot.error or "Target snapshot failed")
        pre_snapshot = snapshot.data["pre_change_snapshot"]

        # The planner performs the rule-based diagnosis while constructing the
        # candidate plan; persist the real state transition before invoking it.
        await self._set_run(run_id, "running", WorkflowStep.DIAGNOSE)
        await self.audit_logger.log(
            run_id=run_id,
            actor_type="system",
            actor_name="durable_run_orchestrator",
            action_type="diagnosis_started",
            target_host_id=host_id,
            result="success",
        )
        await self._set_run(run_id, "running", WorkflowStep.PROPOSE_PLAN)
        proposed = await worker._propose_plan(
            run_id,
            host_id,
            run["goal"],
            pre_change_snapshot=pre_snapshot,
        )
        if not proposed.success:
            return await self._fail_run(run_id, proposed.error or "Plan generation failed")
        if not proposed.data.get("is_actionable", True):
            await self._complete_run(run_id, host_id)
            return RunProcessResult("completed", "Diagnostic completed without a write plan")

        await self._set_run(run_id, "running", WorkflowStep.SAFETY_CHECK)
        safety = await worker._submit_to_guardrail(
            run_id=run_id,
            host_id=host_id,
            proposed_changes=proposed.data["proposed_changes"],
            rollback_instructions=proposed.data["rollback_instructions"],
            pre_snapshot=pre_snapshot,
            config=config,
        )
        if not safety.success:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE plans SET status = $2 WHERE id = $1",
                    UUID(proposed.data["plan_id"]),
                    PlanStatus.BLOCKED.value,
                )
            return await self._fail_run(run_id, safety.error or "Safety check failed")

        await self._set_run(run_id, "waiting_approval", WorkflowStep.APPROVAL_GATE)
        return RunProcessResult("waiting_approval", "Plan is waiting for authenticated approval")

    async def _advance_plan(
        self, run: Dict[str, Any], plan: Dict[str, Any], config: LoopConfig
    ) -> RunProcessResult:
        run_id = run["id"]
        host_id = run["host_id"]
        status = plan["status"]
        if status == PlanStatus.PENDING_APPROVAL.value:
            await self._set_run(run_id, "waiting_approval", WorkflowStep.APPROVAL_GATE)
            return RunProcessResult("waiting_approval", "Plan is waiting for approval")
        if status == PlanStatus.REJECTED.value:
            return await self._fail_run(run_id, "Plan was rejected")
        if status in {
            PlanStatus.BLOCKED.value,
            PlanStatus.DRY_RUN_FAILED.value,
            PlanStatus.APPLY_FAILED.value,
            PlanStatus.ROLLBACK_FAILED.value,
        }:
            return await self._fail_run(run_id, f"Plan stopped in terminal state {status}")
        if status == PlanStatus.ROLLED_BACK.value:
            return await self._fail_run(run_id, "Applied change was rolled back")

        proposed_changes = _json(plan["proposed_changes"], [])
        pre_snapshot = _json(plan["pre_change_snapshot"], {})
        executor = TargetPostgresExecutor(self.pool)

        if status in {PlanStatus.APPROVED.value, PlanStatus.DRY_RUN_PASSED.value}:
            if await self._cancelled(run_id):
                return RunProcessResult("cancelled", "Run was cancelled before target write")
            await self._set_run(run_id, "running", WorkflowStep.DRY_RUN)
            dry_run = await executor.dry_run(host_id, proposed_changes)
            if not dry_run.passed:
                await self._set_plan_status(plan["id"], PlanStatus.DRY_RUN_FAILED)
                return await self._fail_run(
                    run_id, "Post-approval live dry-run failed: " + "; ".join(dry_run.errors)
                )
            if dry_run.snapshot != pre_snapshot:
                await self._set_plan_status(plan["id"], PlanStatus.BLOCKED)
                return await self._fail_run(run_id, "Target configuration drifted after approval")
            await self._set_plan_status(plan["id"], PlanStatus.DRY_RUN_PASSED)
            baseline = await self._load_metric_evidence(host_id)
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE plans SET pre_metric_evidence = $2::jsonb WHERE id = $1",
                    plan["id"],
                    json.dumps(baseline),
                )
            await self._set_run(run_id, "running", WorkflowStep.APPLY)
            if await self._cancelled(run_id):
                return RunProcessResult("cancelled", "Run was cancelled before target write")
            await PlanExecutionService(
                self.pool,
                audit_logger=self.audit_logger,
                target_executor=executor,
            ).execute(plan["id"])
            plan = await self._load_latest_plan(run_id)
            status = plan["status"]

        if status == PlanStatus.APPLIED.value:
            if await self._cancelled(run_id):
                # An applied change must still be verified or rolled back.  The
                # halt endpoint rejects cancellation once a write is in progress,
                # so reaching this state means recovery must continue.
                await self.audit_logger.log(
                    run_id=run_id,
                    actor_type="system",
                    actor_name="durable_run_orchestrator",
                    action_type="cancellation_deferred_for_verification",
                    target_host_id=host_id,
                    result="blocked",
                    result_reason="Applied target state requires verification",
                )
            await self._set_run(run_id, "running", WorkflowStep.VERIFY)
            expected = {
                change["setting_name"]: change["proposed_value"]
                for change in proposed_changes
            }
            verified_values = await executor.verify_expected_values(host_id, expected)
            await self.audit_logger.log(
                run_id=run_id,
                actor_type="system",
                actor_name="durable_run_orchestrator",
                action_type="target_configuration_verified",
                target_host_id=host_id,
                result="success",
                details={"plan_id": str(plan["id"]), "verified_values": verified_values},
            )

            await self._set_run(run_id, "running", WorkflowStep.MEASURE)
            decision = await verify_and_decide(
                run_id=run_id,
                host_id=host_id,
                plan_id=plan["id"],
                pre_evidence=_json(plan.get("pre_metric_evidence"), {}),
                config=config,
                pool=self.pool,
                audit_logger=self.audit_logger,
                applied_at=plan.get("applied_at"),
            )
            verification = {
                "configuration_verified": True,
                "verified_values": verified_values,
                "performance": decision,
                "verified_at": datetime.now(timezone.utc).isoformat(),
            }
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE plans
                    SET verification_result = $2::jsonb,
                        verification_completed_at = NOW()
                    WHERE id = $1
                    """,
                    plan["id"],
                    json.dumps(verification),
                )
            await self._set_run(run_id, "running", WorkflowStep.KEEP_ROLLBACK)
            if decision["decision"] != "kept":
                return await self._fail_run(run_id, "Performance verification triggered rollback")

            await self._complete_run(run_id, host_id)
            return RunProcessResult("completed", "Plan applied and verified")

        return await self._fail_run(run_id, f"Unsupported recovery state {status}")

    async def _load_run(self, run_id: UUID) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM loop_runs WHERE id = $1", run_id)
        if row is None:
            raise RuntimeError(f"Run {run_id} does not exist")
        return dict(row)

    async def _load_latest_plan(self, run_id: UUID) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM plans
                WHERE run_id = $1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                run_id,
            )
        return dict(row) if row else None

    async def _load_metric_evidence(self, host_id: UUID) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        async with self.pool.acquire() as conn:
            for category in METRIC_CATEGORIES:
                row = await conn.fetchrow(
                    """
                    SELECT data
                    FROM evidence_snapshots
                    WHERE host_id = $1 AND evidence_type = $2
                    ORDER BY collected_at DESC
                    LIMIT 1
                    """,
                    host_id,
                    category,
                )
                if row and row["data"]:
                    result[category] = _json(row["data"], {})
        if not result:
            raise RuntimeError("No pre-apply performance evidence is available")
        return result

    async def _set_run(self, run_id: UUID, status: str, step: WorkflowStep) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE loop_runs
                SET status = $2, current_step = $3, last_step_transition_at = NOW()
                WHERE id = $1
                """,
                run_id,
                status,
                step.value,
            )

    async def _set_plan_status(self, plan_id: UUID, status: PlanStatus) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE plans SET status = $2 WHERE id = $1",
                plan_id,
                status.value,
            )

    async def _cancelled(self, run_id: UUID) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT r.status AS run_status, j.status AS job_status
                FROM loop_runs r
                JOIN run_jobs j ON j.run_id = r.id
                WHERE r.id = $1
                """,
                run_id,
            )
        return bool(
            row
            and (
                row["run_status"] == "manually_halted"
                or row["job_status"] in {"cancel_requested", "cancelled"}
            )
        )

    async def _complete_run(self, run_id: UUID, host_id: UUID) -> None:
        await self._set_run(run_id, "completed", WorkflowStep.REPORT)
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE loop_runs SET completed_at = NOW(), failure_reason = NULL WHERE id = $1",
                run_id,
            )
        await self.audit_logger.log(
            run_id=run_id,
            actor_type="system",
            actor_name="durable_run_orchestrator",
            action_type="run_completed",
            target_host_id=host_id,
            result="success",
        )
        await ReportGenerator(pool=self.pool).generate_report(run_id)

    async def _fail_run(self, run_id: UUID, reason: str) -> RunProcessResult:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE loop_runs
                SET status = 'failed', failure_reason = $2,
                    completed_at = NOW(), last_step_transition_at = NOW()
                WHERE id = $1
                """,
                run_id,
                reason,
            )
        return RunProcessResult("failed", reason)
