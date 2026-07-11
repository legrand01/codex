"""Idempotent, audited plan application against a target PostgreSQL server."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from backend.models.enums import PlanStatus
from backend.services.audit_logger import AuditLogger, get_audit_logger
from backend.services.target_executor import TargetPostgresExecutor


class PlanExecutionError(RuntimeError):
    """Raised when a plan cannot be safely claimed or applied."""


@dataclass(frozen=True)
class PlanExecutionOutcome:
    plan_id: UUID
    operation_id: UUID
    result: Dict[str, Any]
    already_completed: bool = False


def _parse_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


class PlanExecutionService:
    """Claims a plan once, applies it, verifies it, and records the evidence."""

    def __init__(
        self,
        pool,
        *,
        audit_logger: Optional[AuditLogger] = None,
        target_executor: Optional[TargetPostgresExecutor] = None,
    ) -> None:
        self.pool = pool
        self.audit_logger = audit_logger or get_audit_logger()
        self.target_executor = target_executor or TargetPostgresExecutor(pool)

    async def execute(self, plan_id: UUID) -> PlanExecutionOutcome:
        plan = await self._load_plan(plan_id)
        self._validate_plan(plan)
        operation = await self._claim_operation(plan)

        if operation["status"] == "succeeded":
            return PlanExecutionOutcome(
                plan_id=plan_id,
                operation_id=operation["id"],
                result=_parse_json(operation["result"]) or {},
                already_completed=True,
            )
        if operation["status"] == "in_progress" and operation.get("claimed_at"):
            stale_before = datetime.now(timezone.utc) - timedelta(seconds=120)
            claimed_at = operation["claimed_at"]
            if claimed_at.tzinfo is None:
                claimed_at = claimed_at.replace(tzinfo=timezone.utc)
            if claimed_at >= stale_before:
                raise PlanExecutionError(f"Plan {plan_id} is already being applied")
            recovered = await self._recover_stale_operation(plan, operation)
            if recovered is not None:
                return recovered

        await self._mark_in_progress(operation["id"])
        await self.audit_logger.log(
            run_id=plan["run_id"],
            actor_type="system",
            actor_name="plan_execution_service",
            action_type="target_apply_started",
            target_host_id=plan["host_id"],
            result="success",
            details={
                "plan_id": str(plan_id),
                "operation_id": str(operation["id"]),
                "approved_by": plan["approved_by"],
            },
        )

        try:
            execution = await self.target_executor.apply(
                plan["host_id"],
                _parse_json(plan["proposed_changes"]),
                _parse_json(plan["pre_change_snapshot"]),
            )
            result = execution.to_dict()
            try:
                await self.audit_logger.log(
                    run_id=plan["run_id"],
                    actor_type="system",
                    actor_name="plan_execution_service",
                    action_type="target_apply_verified",
                    target_host_id=plan["host_id"],
                    result="success",
                    details={
                        "plan_id": str(plan_id),
                        "operation_id": str(operation["id"]),
                        "verified_values": execution.verified_values,
                    },
                )
            except Exception:
                await self.target_executor.rollback(
                    plan["host_id"], _parse_json(plan["pre_change_snapshot"])
                )
                raise

            await self._complete(plan_id, operation["id"], result)
            return PlanExecutionOutcome(plan_id, operation["id"], result)
        except Exception as exc:
            await self._fail(plan_id, operation["id"], str(exc))
            try:
                await self.audit_logger.log(
                    run_id=plan["run_id"],
                    actor_type="system",
                    actor_name="plan_execution_service",
                    action_type="target_apply_failed",
                    target_host_id=plan["host_id"],
                    result="failure",
                    result_reason=str(exc),
                    details={
                        "plan_id": str(plan_id),
                        "operation_id": str(operation["id"]),
                    },
                )
            except Exception:
                # The original audit or execution failure remains authoritative.
                pass
            raise PlanExecutionError(f"Plan {plan_id} application failed: {exc}") from exc

    async def _recover_stale_operation(
        self, plan: Dict[str, Any], operation: Dict[str, Any]
    ) -> Optional[PlanExecutionOutcome]:
        """Reconcile a worker crash without replaying an uncertain target write."""
        changes = _parse_json(plan["proposed_changes"])
        snapshot = _parse_json(plan["pre_change_snapshot"])
        expected = {change["setting_name"]: change["proposed_value"] for change in changes}
        original = {name: state["value"] for name, state in snapshot.items()}
        current = await self.target_executor.read_current_values(plan["host_id"], list(expected))

        def matches(values: Dict[str, Any]) -> bool:
            return all(
                str(current[name]).strip().lower() == str(value).strip().lower()
                for name, value in values.items()
            )

        if matches(expected):
            result = {
                "succeeded": True,
                "changed_settings": list(expected),
                "verified_values": current,
                "rolled_back": False,
                "recovered_after_worker_restart": True,
            }
            await self._complete(plan["id"], operation["id"], result)
            await self.audit_logger.log(
                run_id=plan["run_id"],
                actor_type="system",
                actor_name="plan_execution_service",
                action_type="target_apply_recovered",
                target_host_id=plan["host_id"],
                result="success",
                details={"plan_id": str(plan["id"]), "operation_id": str(operation["id"])},
            )
            return PlanExecutionOutcome(plan["id"], operation["id"], result, True)

        if matches(original):
            await self._reset_operation(operation["id"])
            return None

        await self.target_executor.rollback(plan["host_id"], snapshot)
        await self._fail(
            plan["id"],
            operation["id"],
            "Recovered stale operation had a partial target state; rolled back",
        )
        raise PlanExecutionError(
            f"Plan {plan['id']} had a partial stale apply and was rolled back"
        )

    async def _reset_operation(self, operation_id: UUID) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE write_operations
                SET status = 'failed', claimed_at = NULL,
                    error = 'Recovered stale claim before target mutation'
                WHERE id = $1 AND status = 'in_progress'
                """,
                operation_id,
            )

    async def _load_plan(self, plan_id: UUID) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, run_id, host_id, status, proposed_changes,
                       pre_change_snapshot, approved_by, approved_at
                FROM plans
                WHERE id = $1
                """,
                plan_id,
            )
        if row is None:
            raise PlanExecutionError(f"Plan {plan_id} does not exist")
        return dict(row)

    @staticmethod
    def _validate_plan(plan: Dict[str, Any]) -> None:
        if plan["status"] not in {
            PlanStatus.APPROVED.value,
            PlanStatus.DRY_RUN_PASSED.value,
        }:
            raise PlanExecutionError(
                f"Plan {plan['id']} is not approved and dry-run eligible: {plan['status']}"
            )
        if not plan.get("approved_by") or not plan.get("approved_at"):
            raise PlanExecutionError("Plan lacks an authenticated approval record")
        if not plan.get("pre_change_snapshot"):
            raise PlanExecutionError("Plan lacks a pre-change snapshot")

    async def _claim_operation(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        key = f"apply:{plan['id']}"
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO write_operations (
                    plan_id, host_id, operation_type, idempotency_key,
                    status, pre_change_snapshot
                ) VALUES ($1, $2, 'apply', $3, 'pending', $4::jsonb)
                ON CONFLICT (plan_id, operation_type) DO NOTHING
                """,
                plan["id"],
                plan["host_id"],
                key,
                json.dumps(_parse_json(plan["pre_change_snapshot"])),
            )
            row = await conn.fetchrow(
                """
                SELECT id, status, result, claimed_at
                FROM write_operations
                WHERE plan_id = $1 AND operation_type = 'apply'
                """,
                plan["id"],
            )
        if row is None:
            raise PlanExecutionError("Failed to create or load the apply operation")
        return dict(row)

    async def _mark_in_progress(self, operation_id: UUID) -> None:
        async with self.pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE write_operations
                SET status = 'in_progress', claimed_at = $2, error = NULL
                FROM plans p, loop_runs r
                WHERE write_operations.id = $1
                  AND write_operations.status IN ('pending', 'failed')
                  AND p.id = write_operations.plan_id
                  AND r.id = p.run_id
                  AND r.status = 'running'
                RETURNING write_operations.id
                """,
                operation_id,
                datetime.now(timezone.utc),
            )
        if updated is None:
            raise PlanExecutionError("Apply operation could not be claimed atomically")

    async def _complete(
        self, plan_id: UUID, operation_id: UUID, result: Dict[str, Any]
    ) -> None:
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE write_operations
                    SET status = 'succeeded', result = $2::jsonb,
                        completed_at = $3, error = NULL
                    WHERE id = $1 AND status = 'in_progress'
                    """,
                    operation_id,
                    json.dumps(result),
                    now,
                )
                await conn.execute(
                    """
                    UPDATE plans
                    SET status = $2, apply_result = $3::jsonb,
                        applied_at = $4, execution_started_at = COALESCE(execution_started_at, $4)
                    WHERE id = $1
                    """,
                    plan_id,
                    PlanStatus.APPLIED.value,
                    json.dumps(result),
                    now,
                )

    async def _fail(self, plan_id: UUID, operation_id: UUID, error: str) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE write_operations
                    SET status = 'failed', error = $2, completed_at = $3
                    WHERE id = $1
                    """,
                    operation_id,
                    error,
                    datetime.now(timezone.utc),
                )
                await conn.execute(
                    "UPDATE plans SET status = $2 WHERE id = $1",
                    plan_id,
                    PlanStatus.APPLY_FAILED.value,
                )
