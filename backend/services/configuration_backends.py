"""Configuration backend routing for PostgreSQL target changes.

The workflow depends on this interface rather than assuming ``ALTER SYSTEM``.
Managed-file writes are executed by the authenticated host agent and retain the
exact previous file bytes as their rollback provenance.
"""

import asyncio
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Protocol
from uuid import UUID, uuid4

from backend.config import settings
from backend.services.target_executor import (
    DryRunResult,
    ExecutionResult,
    TargetExecutionError,
    TargetPostgresExecutor,
    TargetValidationError,
)


class ConfigurationBackend(Protocol):
    async def capture_snapshot(
        self, host_id: UUID, setting_names: List[str]
    ) -> Dict[str, Dict[str, Any]]: ...

    async def dry_run(self, host_id: UUID, proposed_changes: List[dict]) -> DryRunResult: ...

    async def apply(
        self,
        host_id: UUID,
        proposed_changes: List[dict],
        expected_snapshot: Mapping[str, Mapping[str, Any]],
        *,
        plan_id: Optional[UUID] = None,
        operation_id: Optional[UUID] = None,
    ) -> ExecutionResult: ...

    async def rollback(
        self,
        host_id: UUID,
        snapshot: Mapping[str, Mapping[str, Any]],
        backend_snapshot: Optional[Mapping[str, Any]] = None,
        *,
        plan_id: Optional[UUID] = None,
        operation_id: Optional[UUID] = None,
    ) -> ExecutionResult: ...

    async def read_current_values(
        self, host_id: UUID, setting_names: List[str]
    ) -> Dict[str, str]: ...

    async def verify_expected_values(
        self, host_id: UUID, expected: Mapping[str, Any]
    ) -> Dict[str, str]: ...

    async def reconcile_applied(
        self,
        host_id: UUID,
        *,
        plan_id: UUID,
        operation_id: UUID,
        verified_values: Mapping[str, str],
    ) -> Optional[ExecutionResult]: ...


class ProviderAdapter(Protocol):
    """Provider-owned parameter-group/flag lifecycle; never filesystem emulation."""

    async def preflight(
        self, host_id: UUID, changes: List[dict], snapshot: Mapping[str, Any]
    ) -> Dict[str, Any]: ...

    async def stage_apply(
        self, host_id: UUID, changes: List[dict], snapshot: Mapping[str, Any]
    ) -> Dict[str, Any]: ...

    async def poll(self, host_id: UUID, operation: Mapping[str, Any]) -> Dict[str, Any]: ...

    async def request_restart(
        self, host_id: UUID, operation: Mapping[str, Any]
    ) -> Dict[str, Any]: ...

    async def verify(
        self, host_id: UUID, operation: Mapping[str, Any], expected: Mapping[str, Any]
    ) -> Dict[str, str]: ...

    async def rollback(
        self,
        host_id: UUID,
        snapshot: Mapping[str, Any],
        backend_snapshot: Mapping[str, Any],
    ) -> Dict[str, Any]: ...


PROVIDER_ADAPTERS: Dict[str, ProviderAdapter] = {}


def register_provider_adapter(platform_type: str, adapter: ProviderAdapter) -> None:
    """Install an explicit provider adapter for one managed platform type."""
    if platform_type not in {"aws_rds", "aurora", "cloud_sql", "aiven", "other_managed"}:
        raise ValueError(f"Unsupported managed platform {platform_type!r}")
    PROVIDER_ADAPTERS[platform_type] = adapter


class AgentCommandError(TargetExecutionError):
    """A managed-file command failed, expired, or timed out."""


class AgentCommandTransport:
    """Durable request/response transport over the outbound agent channel."""

    def __init__(self, pool) -> None:
        self.pool = pool

    async def execute(
        self,
        host_id: UUID,
        action: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
        configuration_version_id: Optional[UUID] = None,
    ) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_commands (
                    organization_id, host_id, configuration_version_id,
                    action, idempotency_key, payload, expires_at
                )
                SELECT organization_id, id, $2, $3, $4, $5::jsonb,
                       NOW() + ($6::text || ' seconds')::interval
                FROM hosts
                WHERE id = $1
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING id, status, result, error
                """,
                host_id,
                configuration_version_id,
                action,
                idempotency_key,
                json.dumps(dict(payload)),
                str(settings.agent_command_timeout_sec),
            )
        if row is None:
            raise AgentCommandError(f"Target host {host_id} does not exist")
        command_id = row["id"]
        deadline = (
            asyncio.get_running_loop().time() + settings.agent_command_timeout_sec
        )
        while True:
            async with self.pool.acquire() as conn:
                state = await conn.fetchrow(
                    "SELECT status, result, error FROM agent_commands WHERE id = $1",
                    command_id,
                )
            if state is None:
                raise AgentCommandError(f"Agent command {command_id} disappeared")
            if state["status"] == "succeeded":
                result = state["result"] or {}
                return json.loads(result) if isinstance(result, str) else dict(result)
            if state["status"] in {"failed", "expired"}:
                raise AgentCommandError(
                    state["error"] or f"Agent command {command_id} {state['status']}"
                )
            if asyncio.get_running_loop().time() >= deadline:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE agent_commands
                        SET status = 'expired', error = 'Control-plane wait timed out',
                            completed_at = NOW()
                        WHERE id = $1 AND status IN ('queued', 'claimed')
                        """,
                        command_id,
                    )
                raise AgentCommandError(f"Agent command {command_id} timed out")
            await asyncio.sleep(settings.agent_command_poll_interval_sec)


@dataclass(frozen=True)
class ManagedHostPolicy:
    organization_id: UUID
    platform_type: str
    configuration_backend: str
    managed_conf_enrolled: bool
    managed_conf_path: Optional[str]
    managed_file_access: bool
    reload_permission: bool


class ManagedConfFileBackend:
    """Apply a deterministic, agent-owned PostgreSQL include file."""

    backend_name = "managed_conf_file"

    def __init__(self, pool, *, transport: Optional[AgentCommandTransport] = None) -> None:
        self.pool = pool
        self.reader = TargetPostgresExecutor(pool)
        self.transport = transport or AgentCommandTransport(pool)

    @staticmethod
    def _mapping(value: Any) -> Dict[str, Any]:
        if not value:
            return {}
        parsed = json.loads(value) if isinstance(value, str) else value
        return dict(parsed) if isinstance(parsed, Mapping) else {}

    @staticmethod
    def _has_exact_file_snapshot(value: Mapping[str, Any]) -> bool:
        file_snapshot = value.get("file")
        return isinstance(file_snapshot, Mapping) and "bytes_b64" in file_snapshot

    async def _policy(self, host_id: UUID, *, for_write: bool) -> ManagedHostPolicy:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT h.organization_id, h.platform_type, h.configuration_backend,
                       h.managed_conf_enrolled, h.managed_conf_path,
                       COALESCE(c.managed_file_access, FALSE) AS managed_file_access,
                       COALESCE(c.reload_permission, FALSE) AS reload_permission
                FROM hosts h
                LEFT JOIN host_capabilities c ON c.host_id = h.id
                WHERE h.id = $1
                """,
                host_id,
            )
        if row is None:
            raise TargetValidationError(f"Target host {host_id} does not exist")
        policy = ManagedHostPolicy(**dict(row))
        path = PurePosixPath(policy.managed_conf_path or "")
        if policy.platform_type != "self_managed":
            raise TargetValidationError(
                "managed_conf_file is restricted to explicitly enrolled self-managed hosts"
            )
        if policy.configuration_backend != self.backend_name:
            raise TargetValidationError("Host is not assigned to managed_conf_file")
        if not policy.managed_conf_enrolled:
            raise TargetValidationError("Managed configuration file enrollment is disabled")
        valid_path = (
            path.is_absolute()
            and path.name == "postgres_tune.conf"
            and path.parent.name == "conf.d"
        )
        if not valid_path:
            raise TargetValidationError(
                "Managed path must be an absolute conf.d/postgres_tune.conf path"
            )
        if not policy.managed_file_access or not policy.reload_permission:
            raise TargetValidationError(
                "Host Agent has not verified managed-file access and pg_reload_conf permission"
            )
        if for_write:
            target_policy = await self.reader.load_policy(host_id)
            self.reader.assert_write_allowed(target_policy)
        return policy

    async def _allowlist(self, host_id: UUID, names: List[str]) -> None:
        validated = self.reader._validate_setting_names(names)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT setting_name FROM guardrail_allowlist
                WHERE host_id = $1 AND setting_name = ANY($2::text[])
                """,
                host_id,
                validated,
            )
        allowed = {row["setting_name"] for row in rows}
        missing = sorted(set(validated) - allowed)
        if missing:
            raise TargetValidationError(
                "Managed file rejected non-allowlisted settings: " + ", ".join(missing)
            )

    async def capture_snapshot(
        self, host_id: UUID, setting_names: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        await self._policy(host_id, for_write=False)
        return await self.reader.capture_snapshot(host_id, setting_names)

    async def dry_run(self, host_id: UUID, proposed_changes: List[dict]) -> DryRunResult:
        try:
            policy = await self._policy(host_id, for_write=False)
            changes = self.reader._validate_changes(proposed_changes)
            names = [item["setting_name"] for item in changes]
            await self._allowlist(host_id, names)
            target_validation = await self.reader.dry_run(host_id, proposed_changes)
            if not target_validation.passed:
                return target_validation
            snapshot = target_validation.snapshot
            result = await self.transport.execute(
                host_id,
                "managed_conf_preflight",
                {
                    "managed_conf_path": policy.managed_conf_path,
                    "changes": changes,
                    "expected_snapshot": snapshot,
                },
                idempotency_key=f"preflight:{host_id}:{uuid4()}",
            )
            return DryRunResult(bool(result.get("passed")), snapshot, result.get("errors", []))
        except TargetExecutionError as exc:
            return DryRunResult(False, {}, [str(exc)])
        except Exception as exc:
            return DryRunResult(False, {}, [f"Managed-file dry-run failed: {exc}"])

    async def apply(
        self,
        host_id: UUID,
        proposed_changes: List[dict],
        expected_snapshot: Mapping[str, Mapping[str, Any]],
        *,
        plan_id: Optional[UUID] = None,
        operation_id: Optional[UUID] = None,
    ) -> ExecutionResult:
        policy = await self._policy(host_id, for_write=True)
        changes = self.reader._validate_changes(proposed_changes)
        names = [item["setting_name"] for item in changes]
        if set(names) != set(expected_snapshot):
            raise TargetValidationError("Expected pre-change snapshot does not match the plan")
        await self._allowlist(host_id, names)
        async with self.pool.acquire() as conn:
            version_id = await conn.fetchval(
                """
                INSERT INTO configuration_versions (
                    organization_id, host_id, plan_id, write_operation_id,
                    configuration_backend, status, managed_conf_path,
                    parameters, pre_change_snapshot
                ) VALUES ($1, $2, $3, $4, $5, 'applying', $6, $7::jsonb, $8::jsonb)
                RETURNING id
                """,
                policy.organization_id,
                host_id,
                plan_id,
                operation_id,
                self.backend_name,
                policy.managed_conf_path,
                json.dumps(changes),
                json.dumps(dict(expected_snapshot)),
            )
        try:
            result = await self.transport.execute(
                host_id,
                "managed_conf_apply",
                {
                    "managed_conf_path": policy.managed_conf_path,
                    "changes": changes,
                    "expected_snapshot": dict(expected_snapshot),
                },
                idempotency_key=f"apply:{version_id}",
                configuration_version_id=version_id,
            )
            backend_snapshot = result.get("backend_snapshot") or {}
            pending = list(result.get("pending_restart") or [])
            status = "pending_restart" if pending else "active"
            durable_result = dict(result)
            durable_result.pop("backend_snapshot", None)
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE configuration_versions
                    SET status = $2, backend_snapshot = $3::jsonb,
                        apply_result = $4::jsonb, applied_at = NOW(), updated_at = NOW()
                    WHERE id = $1
                    """,
                    version_id,
                    status,
                    json.dumps(backend_snapshot),
                    json.dumps(durable_result),
                )
            return ExecutionResult(
                succeeded=True,
                changed_settings=names,
                verified_values=dict(result.get("verified_values") or {}),
                pending_restart=pending,
                backend_snapshot=backend_snapshot,
                configuration_version_id=str(version_id),
            )
        except Exception as exc:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE configuration_versions
                    SET status = 'failed', error = $2, updated_at = NOW()
                    WHERE id = $1
                    """,
                    version_id,
                    str(exc),
                )
            raise

    async def rollback(
        self,
        host_id: UUID,
        snapshot: Mapping[str, Mapping[str, Any]],
        backend_snapshot: Optional[Mapping[str, Any]] = None,
        *,
        plan_id: Optional[UUID] = None,
        operation_id: Optional[UUID] = None,
    ) -> ExecutionResult:
        policy = await self._policy(host_id, for_write=True)
        stored: Dict[str, Any] = dict(backend_snapshot or {})
        version_id = stored.get("configuration_version_id")
        if not self._has_exact_file_snapshot(stored):
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT v.id, v.backend_snapshot, c.result AS command_result
                    FROM configuration_versions v
                    LEFT JOIN LATERAL (
                        SELECT result
                        FROM agent_commands
                        WHERE configuration_version_id = v.id
                          AND action = 'managed_conf_apply'
                          AND status IN ('succeeded', 'expired')
                          AND result ? 'backend_snapshot'
                        ORDER BY completed_at DESC
                        LIMIT 1
                    ) c ON TRUE
                    WHERE v.host_id = $1 AND ($2::uuid IS NULL OR v.plan_id = $2)
                      AND ($3::uuid IS NULL OR v.id = $3)
                      AND v.status IN (
                          'applying', 'active', 'pending_restart', 'failed'
                      )
                    ORDER BY v.created_at DESC LIMIT 1
                    """,
                    host_id,
                    plan_id,
                    UUID(str(version_id)) if version_id else None,
                )
            if row is None:
                raise TargetValidationError("Managed rollback provenance is missing")
            version_id = row["id"]
            stored = self._mapping(row["backend_snapshot"])
            if not self._has_exact_file_snapshot(stored):
                command_result = self._mapping(row["command_result"])
                stored = self._mapping(command_result.get("backend_snapshot"))
            if not self._has_exact_file_snapshot(stored):
                raise TargetValidationError(
                    "Managed rollback provenance is missing from both the "
                    "configuration version and durable agent command"
                )
        if version_id:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE configuration_versions SET status='rolling_back', updated_at=NOW()
                    WHERE id=$1
                    """,
                    UUID(str(version_id)),
                )
        result = await self.transport.execute(
            host_id,
            "managed_conf_rollback",
            {
                "managed_conf_path": policy.managed_conf_path,
                "backend_snapshot": stored,
                "expected_snapshot": dict(snapshot),
            },
            idempotency_key=f"rollback:{version_id or uuid4()}",
            configuration_version_id=UUID(str(version_id)) if version_id else None,
        )
        if version_id:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE configuration_versions
                    SET status='rolled_back', rollback_result=$2::jsonb,
                        rolled_back_at=NOW(), updated_at=NOW()
                    WHERE id=$1
                    """,
                    UUID(str(version_id)),
                    json.dumps(result),
                )
        return ExecutionResult(
            True,
            list(snapshot),
            dict(result.get("verified_values") or {}),
            rolled_back=True,
            pending_restart=list(result.get("pending_restart") or []),
            backend_snapshot=stored,
            configuration_version_id=str(version_id) if version_id else None,
        )

    async def reconcile_applied(
        self,
        host_id: UUID,
        *,
        plan_id: UUID,
        operation_id: UUID,
        verified_values: Mapping[str, str],
    ) -> Optional[ExecutionResult]:
        """Finish a durable agent apply after a control-plane worker crash."""
        await self._policy(host_id, for_write=False)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT v.id, v.parameters, v.backend_snapshot, v.apply_result,
                       c.result AS command_result
                FROM configuration_versions v
                LEFT JOIN LATERAL (
                    SELECT result
                    FROM agent_commands
                    WHERE configuration_version_id = v.id
                      AND action = 'managed_conf_apply'
                      AND status IN ('succeeded', 'expired')
                      AND result ? 'backend_snapshot'
                    ORDER BY completed_at DESC
                    LIMIT 1
                ) c ON TRUE
                WHERE v.host_id = $1 AND v.plan_id = $2
                  AND v.write_operation_id = $3
                  AND v.status IN ('applying', 'active', 'pending_restart')
                ORDER BY v.created_at DESC
                LIMIT 1
                """,
                host_id,
                plan_id,
                operation_id,
            )
        if row is None:
            raise TargetValidationError(
                "Managed apply reached the target but its configuration version is missing"
            )

        command_result = self._mapping(row["command_result"])
        stored = self._mapping(row["backend_snapshot"])
        if not self._has_exact_file_snapshot(stored):
            stored = self._mapping(command_result.get("backend_snapshot"))
        if not self._has_exact_file_snapshot(stored):
            raise TargetValidationError(
                "Managed apply reached the target but exact rollback provenance is missing"
            )

        parameters_value = row["parameters"] or []
        parameters = (
            json.loads(parameters_value)
            if isinstance(parameters_value, str)
            else list(parameters_value)
        )
        names = [str(item["setting_name"]) for item in parameters]
        prior_result = self._mapping(row["apply_result"])
        pending = list(
            command_result.get("pending_restart")
            or prior_result.get("pending_restart")
            or []
        )
        durable_result = command_result or prior_result
        durable_result = dict(durable_result)
        durable_result.pop("backend_snapshot", None)
        durable_result["recovered_after_worker_restart"] = True
        durable_result["verified_values"] = dict(verified_values)
        status = "pending_restart" if pending else "active"
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE configuration_versions
                SET status = $2, backend_snapshot = $3::jsonb,
                    apply_result = $4::jsonb,
                    applied_at = COALESCE(applied_at, NOW()), updated_at = NOW()
                WHERE id = $1
                """,
                row["id"],
                status,
                json.dumps(stored),
                json.dumps(durable_result),
            )
        return ExecutionResult(
            succeeded=True,
            changed_settings=names,
            verified_values=dict(verified_values),
            pending_restart=pending,
            backend_snapshot=stored,
            configuration_version_id=str(row["id"]),
        )

    async def read_current_values(
        self, host_id: UUID, setting_names: List[str]
    ) -> Dict[str, str]:
        await self._policy(host_id, for_write=False)
        return await self.reader.read_current_values(host_id, setting_names)

    async def verify_expected_values(
        self, host_id: UUID, expected: Mapping[str, Any]
    ) -> Dict[str, str]:
        await self._policy(host_id, for_write=False)
        return await self.reader.verify_expected_values(host_id, expected)


class ProviderConfigurationBackend:
    """Provider-owned staged apply/poll/restart/verify path with no file fallback."""

    def __init__(self, pool, platform_type: str, adapter: Optional[ProviderAdapter]) -> None:
        self.pool = pool
        self.platform_type = platform_type
        self.adapter = adapter
        self.reader = TargetPostgresExecutor(pool)

    def _require_adapter(self) -> ProviderAdapter:
        if self.adapter is None:
            raise TargetValidationError(
                f"Provider backend adapter is not configured for {self.platform_type}"
            )
        return self.adapter

    async def capture_snapshot(self, host_id: UUID, setting_names: List[str]):
        return await self.reader.capture_snapshot(host_id, setting_names)

    async def read_current_values(self, host_id: UUID, setting_names: List[str]):
        return await self.reader.read_current_values(host_id, setting_names)

    async def verify_expected_values(self, host_id: UUID, expected: Mapping[str, Any]):
        return await self.reader.verify_expected_values(host_id, expected)

    async def dry_run(self, host_id: UUID, proposed_changes: List[dict]) -> DryRunResult:
        try:
            adapter = self._require_adapter()
            changes = self.reader._validate_changes(proposed_changes)
            snapshot = await self.reader.capture_snapshot(
                host_id, [change["setting_name"] for change in changes]
            )
            result = await adapter.preflight(host_id, changes, snapshot)
            return DryRunResult(
                bool(result.get("passed")), snapshot, list(result.get("errors") or [])
            )
        except TargetExecutionError as exc:
            return DryRunResult(False, {}, [str(exc)])

    async def apply(
        self,
        host_id: UUID,
        proposed_changes: List[dict],
        expected_snapshot: Mapping[str, Mapping[str, Any]],
        **kwargs,
    ) -> ExecutionResult:
        adapter = self._require_adapter()
        target_policy = await self.reader.load_policy(host_id)
        self.reader.assert_write_allowed(target_policy)
        changes = self.reader._validate_changes(proposed_changes)
        operation = await adapter.stage_apply(host_id, changes, expected_snapshot)
        state = await adapter.poll(host_id, operation)
        pending = list(state.get("pending_restart") or [])
        expected = {
            change["setting_name"]: change["proposed_value"]
            for change in changes
            if change["setting_name"] not in pending
        }
        verified = await adapter.verify(host_id, operation, expected) if expected else {}
        return ExecutionResult(
            True,
            [change["setting_name"] for change in changes],
            verified,
            pending_restart=pending,
            backend_snapshot={"provider_operation": operation, "provider_state": state},
        )

    async def rollback(
        self,
        host_id: UUID,
        snapshot: Mapping[str, Mapping[str, Any]],
        backend_snapshot: Optional[Mapping[str, Any]] = None,
        **kwargs,
    ) -> ExecutionResult:
        adapter = self._require_adapter()
        result = await adapter.rollback(host_id, snapshot, backend_snapshot or {})
        operation = result.get("operation") or result
        await adapter.poll(host_id, operation)
        expected = {name: state["value"] for name, state in snapshot.items()}
        verified = await adapter.verify(host_id, operation, expected)
        return ExecutionResult(
            True,
            list(snapshot),
            verified,
            rolled_back=True,
            pending_restart=list(result.get("pending_restart") or []),
            backend_snapshot=dict(backend_snapshot or {}),
        )


class ConfigurationBackendRouter:
    def __init__(self, pool) -> None:
        self.pool = pool

    async def for_host(self, host_id: UUID) -> ConfigurationBackend:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT configuration_backend, platform_type FROM hosts WHERE id = $1",
                host_id,
            )
        if row is None:
            raise TargetValidationError(f"Target host {host_id} does not exist")
        backend = row["configuration_backend"]
        if backend == "alter_system":
            return TargetPostgresExecutor(self.pool)
        if backend == "managed_conf_file":
            return ManagedConfFileBackend(self.pool)
        if backend == "provider":
            platform_type = row["platform_type"]
            return ProviderConfigurationBackend(
                self.pool, platform_type, PROVIDER_ADAPTERS.get(platform_type)
            )
        raise TargetValidationError(f"Unsupported configuration backend {backend!r}")


async def get_configuration_backend(pool, host_id: UUID) -> ConfigurationBackend:
    return await ConfigurationBackendRouter(pool).for_host(host_id)
