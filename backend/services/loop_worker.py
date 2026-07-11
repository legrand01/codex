"""
DBA Loop Worker service for autonomous PostgreSQL investigation and tuning loops.

Provides:
- start_run() to execute iterative observe/diagnose/plan/verify cycles
- halt_run() to stop active runs within 10 seconds
- Goal decomposition into workflow steps
- Approval gate and guardrail integration
- Error handling with retry logic

Requirements: 2.4, 2.5, 2.6, 11.1, 11.2, 11.3, 11.4, 11.7, 11.8, 11.9
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from backend.db.pool import get_pool
from backend.models.config import LoopConfig
from backend.models.enums import PlanStatus, WorkflowStep
from backend.services.audit_logger import AuditLogger, get_audit_logger

logger = logging.getLogger(__name__)


# Timeout for unresponsive detection (seconds)
UNRESPONSIVE_TIMEOUT_SECONDS = 60

# Evidence collection retry delay (seconds)
EVIDENCE_RETRY_DELAY_SECONDS = 10


@dataclass
class RunResult:
    """Result of a DBA loop run execution."""

    run_id: UUID
    status: str
    goal: str
    iterations_completed: int
    steps_executed: int
    failure_reason: Optional[str] = None
    report: Optional[dict] = None


@dataclass
class StepResult:
    """Result of executing a single workflow step."""

    step: WorkflowStep
    success: bool
    data: dict = field(default_factory=dict)
    error: Optional[str] = None


# Track active runs for halt support
_active_runs: Dict[str, "DBALoopWorker"] = {}


def get_active_runs() -> Dict[str, "DBALoopWorker"]:
    """Get all currently active loop worker instances."""
    return _active_runs


class DBALoopWorker:
    """
    Orchestrator for iterative observe/diagnose/plan/verify cycles.

    Executes a goal-driven loop with configurable iteration and step limits,
    guardrail integration, approval gates, and comprehensive error handling.
    """

    def __init__(
        self,
        pool=None,
        audit_logger: Optional[AuditLogger] = None,
    ):
        self._pool = pool
        self._audit_logger = audit_logger
        self._halted = False
        self._run_id: Optional[UUID] = None
        self._host_id: Optional[UUID] = None
        self._last_step_transition: float = time.monotonic()
        self._heartbeat_time: float = time.monotonic()

    @property
    def pool(self):
        if self._pool is not None:
            return self._pool
        pool = get_pool()
        if pool is None:
            raise RuntimeError("Database connection pool is not initialized.")
        return pool

    @property
    def audit_logger(self) -> AuditLogger:
        if self._audit_logger is not None:
            return self._audit_logger
        return get_audit_logger()

    @property
    def is_halted(self) -> bool:
        return self._halted

    def _update_heartbeat(self):
        """Update the heartbeat timestamp."""
        self._heartbeat_time = time.monotonic()
        self._last_step_transition = time.monotonic()

    def is_unresponsive(self) -> bool:
        """Check if the worker has been unresponsive (no activity > 60s)."""
        elapsed = time.monotonic() - self._last_step_transition
        return elapsed > UNRESPONSIVE_TIMEOUT_SECONDS

    async def _transition_step(self, run_id: UUID, step: WorkflowStep) -> None:
        """Update the current workflow step in the database and notify."""
        self._update_heartbeat()
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE loop_runs
                SET current_step = $1, last_step_transition_at = $2
                WHERE id = $3
                """,
                step.value,
                now,
                run_id,
            )
        # Publish step transition via Redis for WebSocket updates
        try:
            from backend.db.redis_manager import get_redis_client

            redis_client = get_redis_client()
            if redis_client:
                await redis_client.publish(
                    f"run:{run_id}:steps",
                    json.dumps(
                        {
                            "run_id": str(run_id),
                            "step": step.value,
                            "timestamp": now.isoformat(),
                        }
                    ),
                )
        except Exception as e:
            logger.warning(f"Failed to publish step transition: {e}")

    async def _update_run_status(
        self, run_id: UUID, status: str, failure_reason: Optional[str] = None
    ) -> None:
        """Update the run status in the database."""
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            if status in ("completed", "failed", "manually_halted", "timed_out"):
                await conn.execute(
                    """
                    UPDATE loop_runs
                    SET status = $1, failure_reason = $2, completed_at = $3
                    WHERE id = $4
                    """,
                    status,
                    failure_reason,
                    now,
                    run_id,
                )
            else:
                await conn.execute(
                    """
                    UPDATE loop_runs
                    SET status = $1, failure_reason = $2
                    WHERE id = $3
                    """,
                    status,
                    failure_reason,
                    run_id,
                )

    async def _increment_iteration(self, run_id: UUID) -> None:
        """Increment the current iteration counter."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE loop_runs SET current_iteration = current_iteration + 1 WHERE id = $1",
                run_id,
            )

    def decompose_goal(self, goal: str, max_steps: int = 20) -> List[WorkflowStep]:
        """
        Decompose a high-level goal into a sequence of workflow steps.

        The decomposition follows the standard workflow:
        observe -> snapshot -> diagnose -> propose_plan -> safety_check ->
        approval_gate -> dry_run -> apply -> verify -> measure -> keep_rollback -> report

        Returns at most max_steps steps, always > 0 for non-empty goals.

        Requirements: 11.1
        """
        if not goal or not goal.strip():
            return [WorkflowStep.REPORT]

        # Standard full workflow cycle
        full_cycle = [
            WorkflowStep.OBSERVE,
            WorkflowStep.SNAPSHOT,
            WorkflowStep.DIAGNOSE,
            WorkflowStep.PROPOSE_PLAN,
            WorkflowStep.SAFETY_CHECK,
            WorkflowStep.APPROVAL_GATE,
            WorkflowStep.DRY_RUN,
            WorkflowStep.APPLY,
            WorkflowStep.VERIFY,
            WorkflowStep.MEASURE,
            WorkflowStep.KEEP_ROLLBACK,
            WorkflowStep.REPORT,
        ]

        # Truncate to max_steps
        steps = full_cycle[:max_steps]
        return steps if steps else [WorkflowStep.REPORT]

    async def _collect_evidence(self, run_id: UUID, host_id: UUID) -> StepResult:
        """
        Collect evidence from the host agent.

        On failure: retry once after 10 seconds, halt if retry fails.
        Requirements: 11.8
        """

        async def _do_collect() -> dict:
            """Attempt evidence collection from the database/host."""
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE evidence_snapshots
                    SET run_id = $1
                    WHERE host_id = $2 AND run_id IS NULL
                    """,
                    run_id,
                    host_id,
                )
                rows = await conn.fetch(
                    """
                    SELECT id, run_id, evidence_type, collected_at, data, quality_score
                    FROM evidence_snapshots
                    WHERE host_id = $1
                    ORDER BY collected_at DESC
                    LIMIT 20
                    """,
                    host_id,
                )
            return {
                "snapshots_collected": len(rows),
                "evidence_types": list(set(r["evidence_type"] for r in rows)),
            }

        try:
            data = await _do_collect()
            return StepResult(step=WorkflowStep.OBSERVE, success=True, data=data)
        except Exception as first_error:
            logger.warning(
                f"Evidence collection failed for run {run_id}, "
                f"retrying in {EVIDENCE_RETRY_DELAY_SECONDS}s: {first_error}"
            )
            # Retry once after delay
            await asyncio.sleep(EVIDENCE_RETRY_DELAY_SECONDS)
            try:
                data = await _do_collect()
                return StepResult(step=WorkflowStep.OBSERVE, success=True, data=data)
            except Exception as retry_error:
                error_msg = f"Evidence collection failed after retry: {retry_error}"
                return StepResult(step=WorkflowStep.OBSERVE, success=False, error=error_msg)

    def _extract_current_settings(self, evidence: List[dict]) -> dict:
        """Extract current pg_settings values from evidence snapshots."""
        settings = {}
        for item in evidence:
            if item.get("evidence_type") != "pg_settings":
                continue
            data = item.get("data") or {}
            raw_settings = data.get("settings") if isinstance(data, dict) else None
            if isinstance(raw_settings, dict):
                settings.update(raw_settings)
            elif isinstance(raw_settings, list):
                for entry in raw_settings:
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("name")
                    if name:
                        settings[name] = entry.get("setting")
        return settings

    async def _capture_target_snapshot(self, run_id: UUID, host_id: UUID) -> StepResult:
        """Capture authoritative target values for every allowlisted setting."""
        from backend.services.target_executor import TargetPostgresExecutor

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT setting_name
                FROM guardrail_allowlist
                WHERE host_id = $1
                ORDER BY setting_name
                """,
                host_id,
            )
        setting_names = [row["setting_name"] for row in rows]
        if not setting_names:
            return StepResult(
                step=WorkflowStep.SNAPSHOT,
                success=False,
                error="Cannot snapshot target: guardrail allowlist is empty",
            )

        try:
            executor = TargetPostgresExecutor(self.pool)
            snapshot = await executor.capture_snapshot(host_id, setting_names)
            collected_at = datetime.now(timezone.utc)
            evidence_data = {
                "settings": [
                    {
                        "name": name,
                        "setting": state["value"],
                        "unit": state.get("unit"),
                        "context": state.get("context"),
                        "source": state.get("source"),
                        "sourcefile": state.get("sourcefile"),
                        "pending_restart": state.get("pending_restart", False),
                        "in_auto_conf": state.get("in_auto_conf", False),
                    }
                    for name, state in snapshot.items()
                ],
                "total_count": len(snapshot),
                "authoritative_target_snapshot": True,
            }
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO evidence_snapshots (
                        run_id, host_id, evidence_type, collected_at, data, quality_score
                    ) VALUES ($1, $2, 'pg_settings', $3, $4::jsonb, 1.0)
                    """,
                    run_id,
                    host_id,
                    collected_at,
                    json.dumps(evidence_data),
                )
            await self.audit_logger.log(
                run_id=run_id,
                actor_type="system",
                actor_name="loop_worker",
                action_type="target_snapshot_captured",
                target_host_id=host_id,
                result="success",
                details={"settings": sorted(snapshot), "collected_at": collected_at.isoformat()},
            )
            return StepResult(
                step=WorkflowStep.SNAPSHOT,
                success=True,
                data={"pre_change_snapshot": snapshot},
            )
        except Exception as exc:
            return StepResult(
                step=WorkflowStep.SNAPSHOT,
                success=False,
                error=f"Authoritative target snapshot failed: {exc}",
            )

    async def _propose_plan(
        self,
        run_id: UUID,
        host_id: UUID,
        goal: str,
        pre_change_snapshot: Optional[dict] = None,
    ) -> StepResult:
        """Generate and persist a pending approval plan from recent evidence."""
        from backend.services.ai_planning import (
            PLANNER_KIND,
            PLANNING_POLICY_VERSION,
            diagnose,
            generate_plan,
        )

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, evidence_type, collected_at, data, quality_score
                FROM evidence_snapshots
                WHERE host_id = $1 AND (run_id = $2 OR run_id IS NULL)
                ORDER BY collected_at DESC
                LIMIT 50
                """,
                host_id,
                run_id,
            )

        evidence = [
            {
                "id": row["id"],
                "evidence_type": row["evidence_type"],
                "collected_at": row["collected_at"],
                "data": (
                    json.loads(row["data"])
                    if isinstance(row["data"], str)
                    else row["data"]
                ),
                "quality_score": row["quality_score"],
            }
            for row in rows
        ]
        diagnosis = await diagnose(evidence, goal)
        plan = await generate_plan(
            diagnosis=diagnosis,
            evidence=evidence,
            current_settings=self._extract_current_settings(evidence),
        )

        # The P0 executor only permits online settings captured in the
        # authoritative allowlisted snapshot.  Keep broader planner ideas out
        # of an executable plan instead of relying on a later all-or-nothing
        # guardrail rejection.
        if pre_change_snapshot is not None:
            allowed_settings = set(pre_change_snapshot)
            plan.proposed_changes = [
                change
                for change in plan.proposed_changes
                if change.get("change_type", "setting") == "setting"
                and change.get("setting_name") in allowed_settings
            ]
            plan.rollback_instructions = [
                instruction
                for instruction in plan.rollback_instructions
                if instruction.get("setting_name") in allowed_settings
            ]
            for change in plan.proposed_changes:
                state = pre_change_snapshot[change["setting_name"]]
                change["current_value"] = state["value"]
            for instruction in plan.rollback_instructions:
                state = pre_change_snapshot[instruction["setting_name"]]
                instruction["restore_value"] = state["value"]
            plan.is_actionable = bool(plan.proposed_changes)

        if not plan.is_actionable:
            await self.audit_logger.log(
                run_id=run_id,
                actor_type="system",
                actor_name="loop_worker",
                action_type="plan_not_actionable",
                target_host_id=host_id,
                result="success",
                result_reason=plan.uncertainty_explanation,
                details={"diagnostic_summary": plan.diagnostic_summary},
            )
            return StepResult(
                step=WorkflowStep.PROPOSE_PLAN,
                success=True,
                data={
                    "is_actionable": False,
                    "diagnostic_summary": plan.diagnostic_summary,
                    "uncertainty_explanation": plan.uncertainty_explanation,
                },
            )

        pre_change_snapshot = pre_change_snapshot or {}
        async with self.pool.acquire() as conn:
            plan_id = await conn.fetchval(
                """
                INSERT INTO plans (
                    run_id, host_id, organization_id, status,
                    proposed_changes, evidence_references,
                    risk_score, confidence_score, uncertainty_explanation,
                    rollback_instructions, pre_change_snapshot,
                    planning_policy_version, planner_kind
                )
                VALUES (
                    $1, $2, (SELECT organization_id FROM hosts WHERE id = $2),
                    $3, $4::jsonb, $5::jsonb, $6, $7, $8, $9::jsonb, $10::jsonb,
                    $11, $12
                )
                RETURNING id
                """,
                run_id,
                host_id,
                PlanStatus.PENDING_APPROVAL.value,
                json.dumps(plan.proposed_changes),
                json.dumps(plan.evidence_references),
                35,
                plan.confidence_score,
                plan.uncertainty_explanation,
                json.dumps(plan.rollback_instructions),
                json.dumps(pre_change_snapshot),
                PLANNING_POLICY_VERSION,
                PLANNER_KIND,
            )

        await self.audit_logger.log(
            run_id=run_id,
            actor_type="system",
            actor_name="loop_worker",
            action_type="plan_created",
            target_host_id=host_id,
            result="success",
            details={
                "plan_id": str(plan_id),
                "proposed_changes_count": len(plan.proposed_changes),
                "confidence_score": plan.confidence_score,
                "pre_change_snapshot": pre_change_snapshot,
                "planning_policy_version": PLANNING_POLICY_VERSION,
                "planner_kind": PLANNER_KIND,
            },
        )
        return StepResult(
            step=WorkflowStep.PROPOSE_PLAN,
            success=True,
            data={
                "is_actionable": True,
                "plan_id": str(plan_id),
                "proposed_changes": plan.proposed_changes,
                "rollback_instructions": plan.rollback_instructions,
                "evidence_references": plan.evidence_references,
                "confidence_score": plan.confidence_score,
                "proposed_changes_count": len(plan.proposed_changes),
                "pre_change_snapshot": pre_change_snapshot,
            },
        )

    async def _submit_to_guardrail(
        self,
        run_id: UUID,
        host_id: UUID,
        proposed_changes: List[dict],
        rollback_instructions: List[dict],
        pre_snapshot: dict,
        config: LoopConfig,
    ) -> StepResult:
        """
        Submit plan to Guardrail Engine and wait for approval gate resolution.

        Pauses execution until approval is resolved or timeout elapses.
        On rejection: stop execution, record failure reason.
        On timeout: halt execution, record timeout.

        Requirements: 11.3, 11.4, 11.7
        """
        from backend.services.guardrail_engine import full_safety_check

        try:
            safety_result = await full_safety_check(
                proposed_changes=proposed_changes,
                host_id=host_id,
                rollback_instructions=rollback_instructions,
                pre_snapshot=pre_snapshot,
                pool=self.pool,
                audit_logger=self.audit_logger,
            )

            if not safety_result.passed:
                # Guardrail rejected the plan
                failure_reason = (
                    f"Guardrail check failed at stage '{safety_result.blocked_at_stage}': "
                    f"{'; '.join(safety_result.errors)}"
                )
                await self.audit_logger.log(
                    run_id=run_id,
                    actor_type="system",
                    actor_name="loop_worker",
                    action_type="guardrail_rejection",
                    target_host_id=host_id,
                    result="blocked",
                    result_reason=failure_reason,
                    details={
                        "blocked_at_stage": safety_result.blocked_at_stage,
                        "errors": safety_result.errors,
                    },
                )
                return StepResult(
                    step=WorkflowStep.SAFETY_CHECK,
                    success=False,
                    error=failure_reason,
                    data={"blocked_at_stage": safety_result.blocked_at_stage},
                )

            return StepResult(
                step=WorkflowStep.SAFETY_CHECK,
                success=True,
                data={"safety_check": "passed"},
            )

        except asyncio.TimeoutError:
            timeout_msg = f"Approval timeout ({config.approval_timeout_hours}h) elapsed"
            await self.audit_logger.log(
                run_id=run_id,
                actor_type="system",
                actor_name="loop_worker",
                action_type="approval_timeout",
                target_host_id=host_id,
                result="failure",
                result_reason=timeout_msg,
            )
            return StepResult(
                step=WorkflowStep.APPROVAL_GATE,
                success=False,
                error=timeout_msg,
            )
        except Exception as e:
            error_msg = f"Guardrail submission error: {str(e)}"
            await self.audit_logger.log(
                run_id=run_id,
                actor_type="system",
                actor_name="loop_worker",
                action_type="guardrail_error",
                target_host_id=host_id,
                result="failure",
                result_reason=error_msg,
            )
            return StepResult(
                step=WorkflowStep.SAFETY_CHECK,
                success=False,
                error=error_msg,
            )

    async def _wait_for_approval(
        self, run_id: UUID, host_id: UUID, config: LoopConfig
    ) -> StepResult:
        """
        Wait for the approval gate to be resolved or timeout.

        Checks plan status periodically. If approval_timeout elapses,
        halts execution.

        Requirements: 11.3, 11.7
        """
        timeout_seconds = config.approval_timeout_hours * 3600
        start = time.monotonic()
        check_interval = 5  # Check every 5 seconds

        while not self._halted:
            elapsed = time.monotonic() - start
            if elapsed >= timeout_seconds:
                timeout_msg = (
                    f"Approval timeout ({config.approval_timeout_hours}h) elapsed "
                    f"without resolution"
                )
                await self.audit_logger.log(
                    run_id=run_id,
                    actor_type="system",
                    actor_name="loop_worker",
                    action_type="approval_timeout",
                    target_host_id=host_id,
                    result="failure",
                    result_reason=timeout_msg,
                )
                return StepResult(
                    step=WorkflowStep.APPROVAL_GATE,
                    success=False,
                    error=timeout_msg,
                )

            # Check if any plan for this run has been approved or rejected
            async with self.pool.acquire() as conn:
                plan_row = await conn.fetchrow(
                    """
                    SELECT id, status FROM plans
                    WHERE run_id = $1
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    run_id,
                )

            if plan_row is None:
                error_msg = "Approval gate reached without a plan awaiting approval"
                await self.audit_logger.log(
                    run_id=run_id,
                    actor_type="system",
                    actor_name="loop_worker",
                    action_type="approval_gate_without_plan",
                    target_host_id=host_id,
                    result="blocked",
                    result_reason=error_msg,
                )
                return StepResult(
                    step=WorkflowStep.APPROVAL_GATE,
                    success=False,
                    error=error_msg,
                )

            if plan_row:
                status = plan_row["status"]
                if status in (
                    PlanStatus.APPROVED.value,
                    PlanStatus.PENDING_FORWARDING.value,
                    PlanStatus.DRY_RUN_PASSED.value,
                ):
                    return StepResult(
                        step=WorkflowStep.APPROVAL_GATE,
                        success=True,
                        data={"plan_id": str(plan_row["id"]), "status": status},
                    )
                elif status == PlanStatus.REJECTED.value:
                    return StepResult(
                        step=WorkflowStep.APPROVAL_GATE,
                        success=False,
                        error="Plan was rejected by DBA",
                        data={"plan_id": str(plan_row["id"]), "status": "rejected"},
                    )
                elif status in (
                    PlanStatus.BLOCKED.value,
                    PlanStatus.DRY_RUN_FAILED.value,
                ):
                    return StepResult(
                        step=WorkflowStep.APPROVAL_GATE,
                        success=False,
                        error=f"Plan blocked with status: {status}",
                        data={"plan_id": str(plan_row["id"]), "status": status},
                    )

            self._update_heartbeat()
            await asyncio.sleep(check_interval)

        return StepResult(
            step=WorkflowStep.APPROVAL_GATE,
            success=False,
            error="Run was halted during approval wait",
        )

    async def start_run(
        self,
        goal: str,
        config: LoopConfig,
        host_id: Optional[UUID] = None,
    ) -> RunResult:
        """
        Start an autonomous DBA loop run.

        Decomposes the goal into steps, executes iterative loops up to
        max_iterations, collecting evidence at each observation step.

        Requirements: 11.1, 11.2
        """
        # Create the run record in the database
        run_id = uuid4()
        self._run_id = run_id
        self._host_id = host_id
        self._halted = False
        now = datetime.now(timezone.utc)

        # Determine host_id if not provided (use first available host)
        if host_id is None:
            async with self.pool.acquire() as conn:
                host_row = await conn.fetchrow("SELECT id FROM hosts LIMIT 1")
            if host_row:
                host_id = host_row["id"]
                self._host_id = host_id
        if host_id is None:
            raise RuntimeError("A registered target host is required to start a P0 run")

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO loop_runs (
                    id, host_id, goal, status, current_step,
                    current_iteration, max_iterations, max_steps,
                    approval_timeout_hours, verification_window_seconds,
                    degradation_threshold_pct, started_at, last_step_transition_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """,
                run_id,
                host_id,
                goal,
                "running",
                WorkflowStep.OBSERVE.value,
                1,
                config.max_iterations,
                config.max_steps,
                config.approval_timeout_hours,
                config.verification_window_seconds,
                config.degradation_threshold_pct,
                now,
                now,
            )

        # Register as active
        _active_runs[str(run_id)] = self

        # Log run start
        await self.audit_logger.log(
            run_id=run_id,
            actor_type="system",
            actor_name="loop_worker",
            action_type="run_started",
            target_host_id=host_id,
            result="success",
            details={"goal": goal, "config": config.model_dump()},
        )

        steps = self.decompose_goal(goal, config.max_steps)
        iterations_completed = 0
        steps_executed = 0
        failure_reason = None
        active_plan: Optional[dict] = None
        diagnostic_report: Optional[dict] = None
        completed_diagnostic_only = False
        completed_goal = False
        pre_change_snapshot: dict = {}

        try:
            for iteration in range(1, config.max_iterations + 1):
                if self._halted:
                    break
                active_plan = None

                for step in steps:
                    if self._halted:
                        break

                    await self._transition_step(run_id, step)
                    steps_executed += 1

                    # Execute the step based on type
                    if step == WorkflowStep.OBSERVE:
                        result = await self._collect_evidence(run_id, host_id)
                        if not result.success:
                            failure_reason = result.error
                            await self._update_run_status(run_id, "failed", failure_reason)
                            await self.audit_logger.log(
                                run_id=run_id,
                                actor_type="system",
                                actor_name="loop_worker",
                                action_type="evidence_collection_failed",
                                target_host_id=host_id,
                                result="failure",
                                result_reason=failure_reason,
                            )
                            _active_runs.pop(str(run_id), None)
                            return RunResult(
                                run_id=run_id,
                                status="failed",
                                goal=goal,
                                iterations_completed=iterations_completed,
                                steps_executed=steps_executed,
                                failure_reason=failure_reason,
                            )

                    elif step == WorkflowStep.SNAPSHOT:
                        result = await self._capture_target_snapshot(run_id, host_id)
                        if not result.success:
                            failure_reason = result.error
                            await self._update_run_status(run_id, "failed", failure_reason)
                            await self.audit_logger.log(
                                run_id=run_id,
                                actor_type="system",
                                actor_name="loop_worker",
                                action_type="target_snapshot_failed",
                                target_host_id=host_id,
                                result="failure",
                                result_reason=failure_reason,
                            )
                            _active_runs.pop(str(run_id), None)
                            return RunResult(
                                run_id=run_id,
                                status="failed",
                                goal=goal,
                                iterations_completed=iterations_completed,
                                steps_executed=steps_executed,
                                failure_reason=failure_reason,
                            )
                        pre_change_snapshot = result.data["pre_change_snapshot"]

                    elif step == WorkflowStep.PROPOSE_PLAN:
                        result = await self._propose_plan(
                            run_id,
                            host_id,
                            goal,
                            pre_change_snapshot=pre_change_snapshot,
                        )
                        if not result.success:
                            failure_reason = result.error
                            await self._update_run_status(run_id, "failed", failure_reason)
                            _active_runs.pop(str(run_id), None)
                            return RunResult(
                                run_id=run_id,
                                status="failed",
                                goal=goal,
                                iterations_completed=iterations_completed,
                                steps_executed=steps_executed,
                                failure_reason=failure_reason,
                            )

                        if not result.data.get("is_actionable", True):
                            diagnostic_report = {
                                "diagnostic_summary": result.data.get("diagnostic_summary"),
                                "uncertainty_explanation": result.data.get(
                                    "uncertainty_explanation"
                                ),
                            }
                            completed_diagnostic_only = True
                            iterations_completed = iteration
                            await self.audit_logger.log(
                                run_id=run_id,
                                actor_type="system",
                                actor_name="loop_worker",
                                action_type="diagnostic_only_completed",
                                target_host_id=host_id,
                                result="success",
                                result_reason=result.data.get("uncertainty_explanation"),
                                details=diagnostic_report,
                            )
                            break

                        active_plan = result.data

                    elif step == WorkflowStep.APPROVAL_GATE:
                        gate_result = await self._wait_for_approval(run_id, host_id, config)
                        if not gate_result.success:
                            if "timeout" in (gate_result.error or "").lower():
                                status = "timed_out"
                            else:
                                status = "failed"
                            failure_reason = gate_result.error
                            await self._update_run_status(run_id, status, failure_reason)
                            _active_runs.pop(str(run_id), None)
                            return RunResult(
                                run_id=run_id,
                                status=status,
                                goal=goal,
                                iterations_completed=iterations_completed,
                                steps_executed=steps_executed,
                                failure_reason=failure_reason,
                            )

                    elif step == WorkflowStep.SAFETY_CHECK:
                        if not active_plan:
                            failure_reason = "Safety check reached without an actionable plan"
                            await self.audit_logger.log(
                                run_id=run_id,
                                actor_type="system",
                                actor_name="loop_worker",
                                action_type="safety_check_without_plan",
                                target_host_id=host_id,
                                result="blocked",
                                result_reason=failure_reason,
                            )
                            await self._update_run_status(run_id, "failed", failure_reason)
                            _active_runs.pop(str(run_id), None)
                            return RunResult(
                                run_id=run_id,
                                status="failed",
                                goal=goal,
                                iterations_completed=iterations_completed,
                                steps_executed=steps_executed,
                                failure_reason=failure_reason,
                            )

                        proposed_changes = active_plan.get("proposed_changes") or []
                        rollback_instructions = active_plan.get("rollback_instructions") or []
                        if not proposed_changes:
                            failure_reason = "Safety check reached with no proposed changes"
                            await self.audit_logger.log(
                                run_id=run_id,
                                actor_type="system",
                                actor_name="loop_worker",
                                action_type="safety_check_without_changes",
                                target_host_id=host_id,
                                result="blocked",
                                result_reason=failure_reason,
                                details={"plan_id": active_plan.get("plan_id")},
                            )
                            await self._update_run_status(run_id, "failed", failure_reason)
                            _active_runs.pop(str(run_id), None)
                            return RunResult(
                                run_id=run_id,
                                status="failed",
                                goal=goal,
                                iterations_completed=iterations_completed,
                                steps_executed=steps_executed,
                                failure_reason=failure_reason,
                            )

                        safety_result = await self._submit_to_guardrail(
                            run_id=run_id,
                            host_id=host_id,
                            proposed_changes=proposed_changes,
                            rollback_instructions=rollback_instructions,
                            pre_snapshot=active_plan.get("pre_change_snapshot") or {},
                            config=config,
                        )
                        if not safety_result.success:
                            failure_reason = safety_result.error
                            await self._update_run_status(run_id, "failed", failure_reason)
                            _active_runs.pop(str(run_id), None)
                            return RunResult(
                                run_id=run_id,
                                status="failed",
                                goal=goal,
                                iterations_completed=iterations_completed,
                                steps_executed=steps_executed,
                                failure_reason=failure_reason,
                            )

                    elif step == WorkflowStep.DRY_RUN:
                        if not active_plan:
                            raise RuntimeError("Dry-run reached without an active plan")
                        from backend.services.guardrail_engine import execute_dry_run

                        dry_run = await execute_dry_run(
                            proposed_changes=active_plan["proposed_changes"],
                            host_id=host_id,
                            pool=self.pool,
                            audit_logger=self.audit_logger,
                        )
                        if not dry_run.passed:
                            failure_reason = (
                                "Post-approval dry-run failed: " + "; ".join(dry_run.errors)
                            )
                            async with self.pool.acquire() as conn:
                                await conn.execute(
                                    "UPDATE plans SET status = $1 WHERE id = $2",
                                    PlanStatus.DRY_RUN_FAILED.value,
                                    UUID(active_plan["plan_id"]),
                                )
                            await self._update_run_status(run_id, "failed", failure_reason)
                            _active_runs.pop(str(run_id), None)
                            return RunResult(
                                run_id=run_id,
                                status="failed",
                                goal=goal,
                                iterations_completed=iterations_completed,
                                steps_executed=steps_executed,
                                failure_reason=failure_reason,
                            )
                        approved_snapshot = active_plan.get("pre_change_snapshot") or {}
                        if (
                            dry_run.pre_change_snapshot
                            and dry_run.pre_change_snapshot != approved_snapshot
                        ):
                            failure_reason = "Target configuration drifted after plan creation"
                            await self._update_run_status(run_id, "failed", failure_reason)
                            _active_runs.pop(str(run_id), None)
                            return RunResult(
                                run_id=run_id,
                                status="failed",
                                goal=goal,
                                iterations_completed=iterations_completed,
                                steps_executed=steps_executed,
                                failure_reason=failure_reason,
                            )
                        async with self.pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE plans SET status = $1 WHERE id = $2",
                                PlanStatus.DRY_RUN_PASSED.value,
                                UUID(active_plan["plan_id"]),
                            )

                    elif step == WorkflowStep.APPLY:
                        if not active_plan:
                            raise RuntimeError("Apply reached without an active plan")
                        from backend.services.plan_execution import PlanExecutionService

                        outcome = await PlanExecutionService(
                            self.pool,
                            audit_logger=self.audit_logger,
                        ).execute(UUID(active_plan["plan_id"]))
                        active_plan["apply_result"] = outcome.result

                    elif step == WorkflowStep.VERIFY:
                        if not active_plan or not active_plan.get("apply_result"):
                            raise RuntimeError("Verify reached without a completed target apply")
                        from backend.services.target_executor import TargetPostgresExecutor

                        expected_values = {
                            change["setting_name"]: change["proposed_value"]
                            for change in active_plan["proposed_changes"]
                        }
                        verified_values = await TargetPostgresExecutor(
                            self.pool
                        ).verify_expected_values(host_id, expected_values)
                        verification_result = {
                            "configuration_verified": True,
                            "verified_values": verified_values,
                            "verified_at": datetime.now(timezone.utc).isoformat(),
                        }
                        active_plan["verification_result"] = verification_result
                        async with self.pool.acquire() as conn:
                            await conn.execute(
                                """
                                UPDATE plans
                                SET verification_result = $2::jsonb,
                                    verification_completed_at = NOW()
                                WHERE id = $1
                                """,
                                UUID(active_plan["plan_id"]),
                                json.dumps(verification_result),
                            )
                        await self.audit_logger.log(
                            run_id=run_id,
                            actor_type="system",
                            actor_name="loop_worker",
                            action_type="target_configuration_verified",
                            target_host_id=host_id,
                            result="success",
                            details={
                                "plan_id": active_plan["plan_id"],
                                "verified_values": verified_values,
                            },
                        )

                    elif step == WorkflowStep.REPORT:
                        completed_goal = True
                        iterations_completed = iteration

                    else:
                        # Other steps: log transition, update heartbeat
                        self._update_heartbeat()
                        await asyncio.sleep(0)  # yield control

                if completed_diagnostic_only or completed_goal:
                    break

                iterations_completed = iteration
                if iteration < config.max_iterations:
                    await self._increment_iteration(run_id)

        except asyncio.CancelledError:
            failure_reason = "Run was cancelled"
            await self._update_run_status(run_id, "manually_halted", failure_reason)
            _active_runs.pop(str(run_id), None)
            return RunResult(
                run_id=run_id,
                status="manually_halted",
                goal=goal,
                iterations_completed=iterations_completed,
                steps_executed=steps_executed,
                failure_reason=failure_reason,
            )
        except Exception as exc:
            failure_reason = f"Run execution failed: {exc}"
            await self._update_run_status(run_id, "failed", failure_reason)
            try:
                await self.audit_logger.log(
                    run_id=run_id,
                    actor_type="system",
                    actor_name="loop_worker",
                    action_type="run_failed",
                    target_host_id=host_id,
                    result="failure",
                    result_reason=failure_reason,
                    details={"current_step": step.value if "step" in locals() else None},
                )
            finally:
                _active_runs.pop(str(run_id), None)
            return RunResult(
                run_id=run_id,
                status="failed",
                goal=goal,
                iterations_completed=iterations_completed,
                steps_executed=steps_executed,
                failure_reason=failure_reason,
            )

        # Determine final status
        if self._halted:
            final_status = "manually_halted"
            failure_reason = "Run manually halted by DBA"
        elif completed_diagnostic_only:
            final_status = "completed"
        elif completed_goal:
            final_status = "completed"
        elif iterations_completed >= config.max_iterations:
            # Max iterations reached without achieving goal
            final_status = "completed"
            failure_reason = f"Maximum iteration limit ({config.max_iterations}) reached"
            await self.audit_logger.log(
                run_id=run_id,
                actor_type="system",
                actor_name="loop_worker",
                action_type="max_iterations_reached",
                target_host_id=host_id,
                result="success",
                result_reason=failure_reason,
                details={
                    "iterations_completed": iterations_completed,
                    "max_iterations": config.max_iterations,
                },
            )
        else:
            final_status = "completed"

        await self._update_run_status(run_id, final_status, failure_reason)

        if completed_goal:
            try:
                from backend.services.report_generator import ReportGenerator

                report = await ReportGenerator(pool=self.pool).generate_report(run_id)
                diagnostic_report = report.model_dump(mode="json")
            except Exception as exc:
                final_status = "failed"
                failure_reason = f"Final report generation failed: {exc}"
                await self._update_run_status(run_id, final_status, failure_reason)

        # Log run completion
        await self.audit_logger.log(
            run_id=run_id,
            actor_type="system",
            actor_name="loop_worker",
            action_type="run_completed",
            target_host_id=host_id,
            result="success" if final_status == "completed" else "failure",
            result_reason=failure_reason,
            details={
                "status": final_status,
                "iterations_completed": iterations_completed,
                "steps_executed": steps_executed,
                "diagnostic_report": diagnostic_report,
            },
        )

        _active_runs.pop(str(run_id), None)
        return RunResult(
            run_id=run_id,
            status=final_status,
            goal=goal,
            iterations_completed=iterations_completed,
            steps_executed=steps_executed,
            failure_reason=failure_reason,
        )

    async def halt_run(self, run_id: UUID) -> dict:
        """
        Halt an active run within 10 seconds.

        Transitions status to 'manually_halted', preserves completed step state.
        Rejects halt on completed/stopped runs with appropriate message.

        Requirements: 2.4, 2.6
        """
        run_id_str = str(run_id)

        # Check if the run exists in the database
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, status, current_step FROM loop_runs WHERE id = $1",
                run_id,
            )

        if row is None:
            return {
                "success": False,
                "message": f"Run '{run_id}' not found",
                "status": "not_found",
            }

        current_status = row["status"]

        # Reject halt on completed/stopped runs
        if current_status in ("completed", "failed", "manually_halted", "timed_out"):
            return {
                "success": False,
                "message": (
                    f"Run '{run_id}' is no longer active and cannot be halted "
                    f"(current status: {current_status})"
                ),
                "status": current_status,
            }

        # Signal the worker to halt
        self._halted = True

        # If this run is tracked as active, signal it
        if run_id_str in _active_runs:
            worker = _active_runs[run_id_str]
            worker._halted = True

        # Update status directly in the database
        await self._update_run_status(run_id, "manually_halted", "Halted by DBA")

        # Log the halt
        await self.audit_logger.log(
            run_id=run_id,
            actor_type="human",
            actor_name="dba",
            action_type="run_halted",
            target_host_id=self._host_id,
            result="success",
            result_reason="Manual halt requested",
            details={"previous_step": row["current_step"]},
        )

        _active_runs.pop(run_id_str, None)

        return {
            "success": True,
            "message": f"Run '{run_id}' halted successfully",
            "status": "manually_halted",
            "previous_step": row["current_step"],
        }


async def mark_unresponsive_runs(pool=None) -> List[UUID]:
    """
    Check all active runs and mark as unresponsive if no step transition
    or heartbeat within 60 seconds.

    Requirements: 2.5
    """
    marked = []
    for run_id_str, worker in list(_active_runs.items()):
        if worker.is_unresponsive():
            run_id = UUID(run_id_str)
            if pool is None:
                pool = get_pool()
            if pool:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE loop_runs
                        SET status = 'unresponsive'
                        WHERE id = $1 AND status = 'running'
                        """,
                        run_id,
                    )
                marked.append(run_id)
                _active_runs.pop(run_id_str, None)
    return marked


# Module-level singleton
_loop_worker: Optional[DBALoopWorker] = None


def get_loop_worker() -> DBALoopWorker:
    """Get or create the module-level DBALoopWorker singleton."""
    global _loop_worker
    if _loop_worker is None:
        _loop_worker = DBALoopWorker()
    return _loop_worker
