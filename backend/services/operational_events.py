"""Durable coded operational events alongside the append-only audit log."""

import json
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional
from uuid import UUID

from backend.db.pool import get_pool


@dataclass(frozen=True)
class EventDefinition:
    severity: str
    component: str


EVENT_DEFINITIONS: Dict[str, EventDefinition] = {
    "AGENT_DUPLICATE_DETECTED": EventDefinition("critical", "host_agent"),
    "AGENT_DUPLICATE_RESOLVED": EventDefinition("info", "host_agent"),
    "AGENT_COMMAND_FAILED": EventDefinition("error", "host_agent"),
    "AGENT_CAPABILITY_DEGRADED": EventDefinition("warning", "host_agent"),
    "CANDIDATE_BLOCKED": EventDefinition("warning", "optimizer"),
    "CANDIDATE_KEPT": EventDefinition("info", "optimizer"),
    "CANDIDATE_ROLLED_BACK": EventDefinition("warning", "optimizer"),
    "CANDIDATE_INCONCLUSIVE": EventDefinition("warning", "optimizer"),
    "PLAN_APPROVED": EventDefinition("info", "approval"),
    "PLAN_REJECTED": EventDefinition("warning", "approval"),
    "CONFIG_APPLY_STARTED": EventDefinition("info", "configuration"),
    "CONFIG_APPLY_SUCCEEDED": EventDefinition("info", "configuration"),
    "CONFIG_APPLY_FAILED": EventDefinition("error", "configuration"),
    "CONFIG_REAPPLY_REQUESTED": EventDefinition("info", "configuration"),
    "CONFIG_RELOAD_SUCCEEDED": EventDefinition("info", "configuration"),
    "CONFIG_RELOAD_FAILED": EventDefinition("error", "configuration"),
    "CONFIG_RESTART_PENDING": EventDefinition("warning", "configuration"),
    "CONFIG_RESTART_VERIFIED": EventDefinition("info", "configuration"),
    "CONFIG_ROLLBACK_STARTED": EventDefinition("warning", "configuration"),
    "CONFIG_ROLLBACK_SUCCEEDED": EventDefinition("info", "configuration"),
    "CONFIG_ROLLBACK_FAILED": EventDefinition("critical", "configuration"),
    "CONFIG_PRECEDENCE_CONFLICT": EventDefinition("error", "configuration"),
    "WORKLOAD_COVERAGE_WARNING": EventDefinition("warning", "measurement"),
    "REPORT_GENERATED": EventDefinition("info", "reporting"),
    "REPORT_GENERATION_FAILED": EventDefinition("error", "reporting"),
}


class OperationalEventError(RuntimeError):
    """An event code or persistence request was invalid."""


class OperationalEventRecorder:
    def __init__(self, pool=None) -> None:
        self.pool = pool

    async def record(
        self,
        event_code: str,
        message: str,
        *,
        organization_id: Optional[UUID] = None,
        host_id: Optional[UUID] = None,
        run_id: Optional[UUID] = None,
        configuration_version_id: Optional[UUID] = None,
        severity: Optional[str] = None,
        component: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
        connection=None,
    ) -> int:
        definition = EVENT_DEFINITIONS.get(event_code)
        if definition is None:
            raise OperationalEventError(f"Unknown operational event code {event_code!r}")
        if not message.strip():
            raise OperationalEventError("Operational event message must not be empty")

        async def insert(conn) -> int:
            resolved_org = organization_id
            if resolved_org is None and host_id is not None:
                resolved_org = await conn.fetchval(
                    "SELECT organization_id FROM hosts WHERE id = $1", host_id
                )
            if resolved_org is None and run_id is not None:
                resolved_org = await conn.fetchval(
                    "SELECT organization_id FROM loop_runs WHERE id = $1", run_id
                )
            if resolved_org is None:
                raise OperationalEventError("Operational event organization is missing")
            statement = """
                INSERT INTO host_events (
                    organization_id, host_id, run_id, configuration_version_id,
                    severity, component, event_code, message, details
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                RETURNING id
                """
            args = (
                resolved_org, host_id, run_id, configuration_version_id,
                severity or definition.severity,
                component or definition.component, event_code, message.strip(),
                json.dumps(dict(details or {})),
            )
            if hasattr(conn, "fetchval"):
                event_id = await conn.fetchval(statement, *args)
                return int(event_id)
            # Lightweight endpoint test doubles may only model execute().
            await conn.execute(statement, *args)
            return 0

        if connection is not None:
            return await insert(connection)
        pool = self.pool or get_pool()
        if pool is None:
            raise OperationalEventError("Database pool is unavailable")
        async with pool.acquire() as conn:
            return await insert(conn)


_recorder: Optional[OperationalEventRecorder] = None


def get_operational_event_recorder() -> OperationalEventRecorder:
    global _recorder
    if _recorder is None:
        _recorder = OperationalEventRecorder()
    return _recorder
