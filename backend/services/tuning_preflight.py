"""Fail-closed capability preflight for productized tuning sessions."""

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel

from backend.models.enums import TuningMode, TuningTarget
from backend.services.parameter_catalog import (
    RELOAD_ONLY_PARAMETERS,
    RESTART_PARAMETERS,
)


class CapabilityCheck(BaseModel):
    """One independently reported tuning prerequisite."""

    key: str
    label: str
    status: str
    blocking: bool
    message: str


class TuningModeCapability(BaseModel):
    mode: TuningMode
    available: bool
    reason: str


class ParameterCapability(BaseModel):
    name: str
    context: str
    allowlisted: bool
    available: bool
    reason: str


class TuningPreflightResponse(BaseModel):
    """Capability contract consumed by the Start tuning wizard."""

    host_id: UUID
    hostname: str
    database_name: Optional[str]
    environment: str
    platform_type: str
    configuration_backend: str
    pg_version: Optional[str]
    server_role: Optional[str]
    requested_mode: TuningMode
    ready: bool
    blockers: List[str]
    warnings: List[str]
    checks: List[CapabilityCheck]
    supported_targets: List[TuningTarget]
    supported_modes: List[TuningModeCapability]
    parameters: List[ParameterCapability]
    capability_observed_at: Optional[datetime]


async def build_tuning_preflight(
    db, organization_id: UUID, host_id: UUID, mode: TuningMode
) -> TuningPreflightResponse:
    """Build a tenant-scoped preflight result without optimistic inference."""
    row = await db.fetchrow(
        """
        SELECT h.id, h.hostname, h.database_name, h.environment, h.platform_type,
               h.configuration_backend, h.pg_version, h.server_role,
               h.connection_status, h.target_dsn_env, h.writes_enabled,
               h.agent_write_ambiguous,
               h.restart_required_enabled, h.managed_conf_enrolled,
               COALESCE(c.connectivity, FALSE) AS connectivity,
               COALESCE(c.system_information, FALSE) AS system_information,
               COALESCE(c.system_metrics, FALSE) AS system_metrics,
               COALESCE(c.pg_stat_statements, FALSE) AS pg_stat_statements,
               COALESCE(c.query_text_collection, FALSE) AS query_text_collection,
               COALESCE(c.configuration_read, FALSE) AS configuration_read,
               COALESCE(c.configuration_write, FALSE) AS configuration_write,
               COALESCE(c.reload_permission, FALSE) AS reload_permission,
               COALESCE(c.restart_capability, FALSE) AS restart_capability,
               COALESCE(c.provider_api, FALSE) AS provider_api,
               COALESCE(c.managed_file_access, FALSE) AS managed_file_access,
               c.observed_at AS capability_observed_at
        FROM hosts h
        LEFT JOIN host_capabilities c
          ON c.host_id = h.id AND c.organization_id = h.organization_id
        WHERE h.id = $1 AND h.organization_id = $2
        """,
        host_id,
        organization_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Host '{host_id}' not found")

    allowlist_rows = await db.fetch(
        """
        SELECT setting_name, parameter_context
        FROM guardrail_allowlist
        WHERE host_id = $1
        ORDER BY setting_name
        """,
        host_id,
    )
    allowlist = {item["setting_name"]: item["parameter_context"] for item in allowlist_rows}
    checks: List[CapabilityCheck] = []

    def add_check(key: str, label: str, passed: bool, blocking: bool, message: str) -> None:
        checks.append(
            CapabilityCheck(
                key=key,
                label=label,
                status="passed" if passed else ("blocked" if blocking else "warning"),
                blocking=blocking and not passed,
                message=message,
            )
        )

    observed_at = row["capability_observed_at"]
    if observed_at and observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    capability_fresh = bool(
        observed_at and observed_at >= datetime.now(timezone.utc) - timedelta(minutes=5)
    )
    add_check(
        "capability_freshness",
        "Capability freshness",
        capability_fresh,
        True,
        "The agent capability report must be newer than five minutes.",
    )

    add_check(
        "connectivity",
        "Agent connectivity",
        bool(row["connectivity"]) and row["connection_status"] == "connected",
        True,
        "A connected agent must report current capabilities.",
    )
    add_check(
        "system_information",
        "PostgreSQL identity",
        bool(row["system_information"] and row["pg_version"] and row["server_role"]),
        True,
        "PostgreSQL version and primary/replica role must be known.",
    )
    add_check(
        "system_metrics",
        "System metrics",
        bool(row["system_metrics"]),
        True,
        "CPU, memory, and I/O evidence is required for safety guardrails.",
    )
    add_check(
        "pg_stat_statements",
        "Workload statistics",
        bool(row["pg_stat_statements"]),
        True,
        "pg_stat_statements must be available for a measured workload baseline.",
    )
    add_check(
        "query_text_collection",
        "Query text collection",
        bool(row["query_text_collection"]),
        False,
        "Query text is optional; normalized query identifiers remain required.",
    )
    add_check(
        "configuration_read",
        "Configuration read access",
        bool(row["configuration_read"]),
        True,
        "The agent must read pg_settings values, context, source, and pending restart.",
    )
    add_check(
        "primary_role",
        "Writable primary",
        row["server_role"] == "primary",
        True,
        "Tuning writes are blocked unless the target is a confirmed primary.",
    )
    add_check(
        "agent_lease",
        "Single active Host Agent",
        not bool(row.get("agent_write_ambiguous", False)),
        True,
        "Multiple active Host Agents share this identity; writes remain blocked "
        "until one lease expires.",
    )

    backend = row["configuration_backend"]
    if backend == "provider":
        backend_ready = bool(row["provider_api"])
        backend_message = "The managed provider API capability must be available."
    elif backend == "managed_conf_file":
        backend_ready = bool(
            row["managed_conf_enrolled"]
            and row["managed_file_access"]
            and row["configuration_write"]
            and row["reload_permission"]
        )
        backend_message = (
            "Managed-file enrollment, atomic file access, write permission, and reload "
            "permission are required."
        )
    else:
        backend_ready = bool(
            row["target_dsn_env"]
            and row["writes_enabled"]
            and row["configuration_write"]
            and row["reload_permission"]
        )
        backend_message = (
            "Target DSN, host write interlock, parameter write access, and reload permission "
            "are required."
        )
    add_check(
        "configuration_backend",
        "Configuration apply backend",
        backend_ready,
        True,
        backend_message,
    )
    reload_allowlisted = any(
        name in RELOAD_ONLY_PARAMETERS and context == "reload"
        for name, context in allowlist.items()
    )
    add_check(
        "parameter_allowlist",
        "Parameter allowlist",
        reload_allowlisted,
        True,
        "At least one supported reload parameter must be independently allowlisted.",
    )

    restart_ready = bool(row["restart_required_enabled"] and row["restart_capability"])
    if mode == TuningMode.RESTART_ENABLED:
        add_check(
            "restart_capability",
            "Controlled restart",
            restart_ready,
            True,
            "Restart-enabled tuning requires explicit host enrollment and restart capability.",
        )

    blockers = [check.message for check in checks if check.blocking]
    warnings = [check.message for check in checks if check.status == "warning"]
    reload_ready = not any(
        check.blocking for check in checks if check.key != "restart_capability"
    )
    supported_modes = [
        TuningModeCapability(
            mode=TuningMode.RELOAD_ONLY,
            available=reload_ready,
            reason=(
                "All reload-only prerequisites passed."
                if reload_ready
                else "One or more reload-only prerequisites are blocked."
            ),
        ),
        TuningModeCapability(
            mode=TuningMode.RESTART_ENABLED,
            available=reload_ready and restart_ready,
            reason=(
                "Controlled restart capability is available."
                if reload_ready and restart_ready
                else "Reload prerequisites or controlled restart capability are missing."
            ),
        ),
    ]

    parameters = []
    for name in RELOAD_ONLY_PARAMETERS + RESTART_PARAMETERS:
        context = "restart" if name in RESTART_PARAMETERS else "reload"
        allowlisted = allowlist.get(name) == context
        mode_permits = context == "reload" or mode == TuningMode.RESTART_ENABLED
        available = allowlisted and mode_permits and bool(row["configuration_read"])
        if not allowlisted:
            reason = "Not allowlisted for this host."
        elif not mode_permits:
            reason = "Requires restart-enabled mode."
        elif not row["configuration_read"]:
            reason = "Configuration read capability is unavailable."
        else:
            reason = "Available for this session."
        parameters.append(
            ParameterCapability(
                name=name,
                context=context,
                allowlisted=allowlisted,
                available=available,
                reason=reason,
            )
        )

    return TuningPreflightResponse(
        host_id=row["id"],
        hostname=row["hostname"],
        database_name=row["database_name"],
        environment=row["environment"],
        platform_type=row["platform_type"],
        configuration_backend=backend,
        pg_version=row["pg_version"],
        server_role=row["server_role"],
        requested_mode=mode,
        ready=not blockers,
        blockers=blockers,
        warnings=warnings,
        checks=checks,
        supported_targets=list(TuningTarget),
        supported_modes=supported_modes,
        parameters=parameters,
        capability_observed_at=observed_at,
    )
