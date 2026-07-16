"""
Fleet management API endpoints.

Provides routes for:
- Listing all hosts (GET /api/v1/fleet/)
- Getting a specific host (GET /api/v1/fleet/{host_id})
- Registering a new host (POST /api/v1/fleet/)
- Receiving heartbeats from host agents (POST /api/v1/fleet/{host_id}/heartbeat)

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5
"""

import json
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.dependencies import get_db
from backend.models.enums import HealthStatus
from backend.models.hosts import HostSummary
from backend.security import Principal, hash_token, require_agent, require_roles
from backend.services.demo_mode import is_demo_active, is_synthetic_address
from backend.services.fleet_service import (
    check_health_thresholds,
    classify_connection_status,
    process_heartbeat,
)
from backend.services.operational_events import OperationalEventRecorder

# Expose as derive_connection_status for backward compatibility
derive_connection_status = classify_connection_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/fleet", tags=["fleet"])


# --- Request/Response Models ---


class HostRegistration(BaseModel):
    """Request model for registering a new host."""

    hostname: str = Field(..., min_length=1, max_length=255)
    database_name: Optional[str] = Field(default=None, min_length=1, max_length=63)
    pg_version: Optional[str] = None
    server_role: Optional[str] = Field(None, pattern="^(primary|replica)$")


class HeartbeatRequest(BaseModel):
    """Request model for host agent heartbeat."""

    pg_version: Optional[str] = None
    server_role: Optional[str] = Field(None, pattern="^(primary|replica)$")
    metrics: Optional[Dict[str, float]] = None


class RoleReportRequest(BaseModel):
    """Request model for host agent role/version reports."""

    pg_version: str
    server_role: str = Field(..., pattern="^(primary|replica)$")
    timestamp: Optional[datetime] = None


class EvidenceIngestRequest(BaseModel):
    """Evidence snapshot posted by a host agent."""

    host_id: Optional[UUID] = None
    run_id: Optional[UUID] = None
    evidence_type: str
    collected_at: datetime
    data: Any
    quality_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class HeartbeatResponse(BaseModel):
    """Response model for heartbeat processing."""

    host_id: str
    hostname: str
    connection_status: str
    pg_version: Optional[str] = None
    server_role: Optional[str] = None
    last_heartbeat: str
    health_check_result: Optional[Dict] = None


class FleetListResponse(BaseModel):
    """Response model for fleet listing."""

    hosts: List[HostSummary]
    total: int


class EvidenceIngestResponse(BaseModel):
    """Response model for host-agent evidence ingestion."""

    host_id: str
    inserted_snapshot_ids: List[str]
    evidence_types: List[str]
    total: int


class CapabilityReportRequest(BaseModel):
    """Independent capabilities observed by the authenticated Host Agent."""

    database_name: Optional[str] = Field(default=None, min_length=1, max_length=63)
    connectivity: bool
    system_information: bool
    system_metrics: bool
    pg_stat_statements: bool
    query_text_collection: bool = False
    configuration_read: bool
    configuration_write: bool
    reload_permission: bool
    restart_capability: bool = False
    provider_api: bool = False
    managed_file_access: bool = False
    details: Dict[str, Any] = Field(default_factory=dict)
    observed_at: Optional[datetime] = None


class CapabilityReportResponse(CapabilityReportRequest):
    host_id: UUID
    organization_id: UUID
    observed_at: datetime


class AgentTokenResponse(BaseModel):
    host_id: UUID
    agent_token: str


class ExecutionPolicyUpdate(BaseModel):
    environment: str = Field(..., pattern="^(development|staging|production)$")
    target_dsn_env: str = Field(..., min_length=1, max_length=255, pattern="^[A-Z][A-Z0-9_]+$")
    writes_enabled: bool = False
    database_name: Optional[str] = Field(default=None, min_length=1, max_length=63)
    platform_type: Optional[str] = Field(
        default=None,
        pattern="^(self_managed|aws_rds|aurora|cloud_sql|aiven|other_managed)$",
    )
    configuration_backend: Optional[str] = Field(
        default=None, pattern="^(alter_system|managed_conf_file|provider)$"
    )
    managed_conf_enrolled: Optional[bool] = None
    managed_conf_path: Optional[str] = Field(default=None, min_length=1, max_length=1024)
    restart_required_enabled: Optional[bool] = None


class AgentCommandResponse(BaseModel):
    id: UUID
    action: str
    payload: Dict[str, Any]
    expires_at: datetime


class AgentCommandResultRequest(BaseModel):
    succeeded: bool
    result: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class CapabilityDiagnosticResponse(BaseModel):
    host_id: UUID
    hostname: str
    database_name: Optional[str] = None
    pg_version: Optional[str] = None
    platform_type: str
    configuration_backend: str
    observed_at: Optional[datetime] = None
    capabilities: Dict[str, bool]
    agent_write_ambiguous: bool
    lease_holder_id: Optional[UUID] = None
    lease_expires_at: Optional[datetime] = None
    active_agents: List[Dict[str, Any]]


class SetupGuideResponse(BaseModel):
    host_id: UUID
    pg_major: Optional[int] = None
    configuration_backend: str
    mode: str
    prerequisites: List[str]
    sql: List[str]
    agent_environment: Dict[str, str]
    file_instructions: List[str]
    provider_instructions: List[str]
    cautions: List[str]


ALLOWED_EVIDENCE_TYPES = {
    "pg_settings",
    "pg_stat_database",
    "pg_stat_statements",
    "locks",
    "replication",
    "wal_checkpoint",
    "os_metrics",
}


@router.get("/{host_id}/diagnostics", response_model=CapabilityDiagnosticResponse)
async def get_capability_diagnostics(
    host_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(
        require_roles("viewer", "operator", "approver", "admin")
    ),
) -> CapabilityDiagnosticResponse:
    row = await db.fetchrow(
        """
        SELECT h.id, h.hostname, h.database_name, h.pg_version, h.platform_type,
               h.configuration_backend, h.agent_write_ambiguous,
               h.agent_lease_holder_id, h.agent_lease_expires_at,
               c.connectivity, c.system_information, c.system_metrics,
               c.pg_stat_statements, c.query_text_collection,
               c.configuration_read, c.configuration_write,
               c.reload_permission, c.restart_capability, c.provider_api,
               c.managed_file_access, c.observed_at
        FROM hosts h LEFT JOIN host_capabilities c ON c.host_id = h.id
        WHERE h.id = $1 AND h.organization_id = $2
        """,
        host_id,
        principal.organization_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Host '{host_id}' not found")
    agents = await db.fetch(
        """
        SELECT instance_id, first_seen_at, last_seen_at, lease_expires_at, details
        FROM host_agent_instances
        WHERE host_id = $1 AND lease_expires_at > NOW()
        ORDER BY last_seen_at DESC
        """,
        host_id,
    )
    capability_names = (
        "connectivity", "system_information", "system_metrics",
        "pg_stat_statements", "query_text_collection", "configuration_read",
        "configuration_write", "reload_permission", "restart_capability",
        "provider_api", "managed_file_access",
    )
    return CapabilityDiagnosticResponse(
        host_id=row["id"], hostname=row["hostname"],
        database_name=row["database_name"], pg_version=row["pg_version"],
        platform_type=row["platform_type"],
        configuration_backend=row["configuration_backend"],
        observed_at=row["observed_at"],
        capabilities={name: bool(row[name]) for name in capability_names},
        agent_write_ambiguous=bool(row["agent_write_ambiguous"]),
        lease_holder_id=row["agent_lease_holder_id"],
        lease_expires_at=row["agent_lease_expires_at"],
        active_agents=[dict(agent) for agent in agents],
    )


@router.get("/{host_id}/setup", response_model=SetupGuideResponse)
async def get_setup_guide(
    host_id: UUID,
    mode: str = "reload_only",
    db=Depends(get_db),
    principal: Principal = Depends(
        require_roles("viewer", "operator", "approver", "admin")
    ),
) -> SetupGuideResponse:
    if mode not in {"reload_only", "restart_enabled"}:
        raise HTTPException(status_code=422, detail="Unsupported tuning mode")
    host = await db.fetchrow(
        """
        SELECT id, pg_version, platform_type, configuration_backend,
               managed_conf_path, target_dsn_env
        FROM hosts WHERE id = $1 AND organization_id = $2
        """,
        host_id,
        principal.organization_id,
    )
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host '{host_id}' not found")
    allowlist = await db.fetch(
        """
        SELECT setting_name, parameter_context FROM guardrail_allowlist
        WHERE host_id = $1 ORDER BY setting_name
        """,
        host_id,
    )
    version_match = re.search(r"(?<!\d)(\d{1,2})(?:\.\d+)", str(host["pg_version"] or ""))
    pg_major = int(version_match.group(1)) if version_match else None
    settings_list = [str(item["setting_name"]) for item in allowlist]
    sql = [
        "GRANT CONNECT ON DATABASE <database> TO dbtune_agent;",
        "GRANT pg_monitor TO dbtune_agent;",
        "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;",
    ]
    if host["configuration_backend"] == "alter_system":
        if pg_major is not None and pg_major >= 15:
            sql.extend(
                f'GRANT ALTER SYSTEM ON PARAMETER "{name}" TO dbtune_agent;'
                for name in settings_list
            )
        else:
            sql.append(
                "Use a narrowly scoped SECURITY DEFINER function for allowlisted settings; "
                "PostgreSQL 15+ parameter grants are preferred."
            )
        sql.append("GRANT EXECUTE ON FUNCTION pg_reload_conf() TO dbtune_agent;")
    file_instructions: List[str] = []
    provider_instructions: List[str] = []
    if host["configuration_backend"] == "managed_conf_file":
        path = host["managed_conf_path"] or "<PGDATA>/conf.d/postgres_tune.conf"
        file_instructions = [
            "Add include_dir = 'conf.d' to postgresql.conf once, then verify it is loaded.",
            f"Create {path} with owner postgres and mode 0600.",
            "Grant the agent write access only to that file and its parent directory.",
            "Keep postgresql.auto.conf untouched; every apply uses atomic replace "
            "and pg_reload_conf().",
        ]
    if host["configuration_backend"] == "provider":
        provider_instructions = [
            f"Create a least-privilege {host['platform_type']} API identity for parameter changes.",
            "Limit it to the enrolled instance or parameter group and the allowlisted settings.",
            "Grant read/status polling separately from write/apply permission.",
        ]
    cautions = [
        "Use one unique AGENT_INSTANCE_ID and one private state volume per installation.",
        "Query text collection is optional and should be disabled where SQL text is sensitive.",
    ]
    if mode == "restart_enabled":
        cautions.append("Restart changes remain pending until a controlled restart is verified.")
    return SetupGuideResponse(
        host_id=host_id, pg_major=pg_major,
        configuration_backend=host["configuration_backend"], mode=mode,
        prerequisites=[
            "Outbound HTTPS from agent to control plane",
            "Read-only PostgreSQL monitoring connection",
            "pg_stat_statements in shared_preload_libraries when required by this PG version",
        ],
        sql=sql,
        agent_environment={
            "AGENT_HOST_ID": str(host_id),
            "AGENT_INSTANCE_ID": "<unique UUID for this installation>",
            "AGENT_TOKEN": "<rotate from Fleet after installation>",
            "TARGET_DSN_ENV": str(host["target_dsn_env"] or "<secret environment variable>"),
        },
        file_instructions=file_instructions,
        provider_instructions=provider_instructions,
        cautions=cautions,
    )


# --- Endpoints ---


@router.get("/", response_model=FleetListResponse)
async def list_hosts(
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("viewer", "operator", "approver", "admin")),
) -> FleetListResponse:
    """
    List all registered PostgreSQL hosts with their current status.

    Returns fleet overview with health status, connection status,
    pg_version, and server role for each host.

    If no hosts are registered, returns an empty list with total=0.

    Requirements: 1.1, 1.5
    """
    rows = await db.fetch(
        "SELECT id, hostname, health_status, connection_status, "
        "pg_version, server_role, database_name, last_heartbeat, "
        "agent_write_ambiguous, agent_lease_expires_at "
        "FROM hosts WHERE organization_id = $1 ORDER BY hostname",
        principal.organization_id,
    )

    hosts = []
    for row in rows:
        # Derive connection status based on current heartbeat age
        if is_demo_active() and is_synthetic_address(row["hostname"]):
            current_connection_status = row["connection_status"]
        else:
            current_connection_status = classify_connection_status(row["last_heartbeat"])
        hosts.append(
            HostSummary(
                id=row["id"],
                hostname=row["hostname"],
                database_name=row.get("database_name"),
                health_status=HealthStatus(row["health_status"]),
                connection_status=current_connection_status,
                pg_version=row["pg_version"],
                server_role=row["server_role"],
                last_heartbeat=row["last_heartbeat"],
                agent_write_ambiguous=bool(row.get("agent_write_ambiguous", False)),
                agent_lease_expires_at=row.get("agent_lease_expires_at"),
            )
        )

    return FleetListResponse(hosts=hosts, total=len(hosts))


@router.get("/{host_id}", response_model=HostSummary)
async def get_host(
    host_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("viewer", "operator", "approver", "admin")),
) -> HostSummary:
    """
    Get details for a specific host.

    Returns host details with connection status derived from last_heartbeat.

    Requirements: 1.1, 1.4
    """
    row = await db.fetchrow(
        "SELECT id, hostname, health_status, connection_status, "
        "pg_version, server_role, database_name, last_heartbeat, "
        "agent_write_ambiguous, agent_lease_expires_at "
        "FROM hosts WHERE id = $1 AND organization_id = $2",
        host_id,
        principal.organization_id,
    )

    if row is None:
        raise HTTPException(status_code=404, detail=f"Host with id '{host_id}' not found")

    if is_demo_active() and is_synthetic_address(row["hostname"]):
        current_connection_status = row["connection_status"]
    else:
        current_connection_status = classify_connection_status(row["last_heartbeat"])

    return HostSummary(
        id=row["id"],
        hostname=row["hostname"],
        database_name=row.get("database_name"),
        health_status=HealthStatus(row["health_status"]),
        connection_status=current_connection_status,
        pg_version=row["pg_version"],
        server_role=row["server_role"],
        last_heartbeat=row["last_heartbeat"],
        agent_write_ambiguous=bool(row.get("agent_write_ambiguous", False)),
        agent_lease_expires_at=row.get("agent_lease_expires_at"),
    )


@router.post("/", response_model=HostSummary, status_code=201)
async def register_host(
    registration: HostRegistration,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("operator", "admin")),
) -> HostSummary:
    """
    Register a new PostgreSQL host in the fleet.

    Creates a new host with the given hostname, pg_version, and server_role.
    Connection status defaults to 'disconnected' (no heartbeat yet).
    Health status defaults to 'unknown'.

    Requirements: 1.1, 1.4
    """
    # Check for duplicate hostname
    existing = await db.fetchrow(
        "SELECT id FROM hosts WHERE organization_id = $1 AND hostname = $2",
        principal.organization_id,
        registration.hostname,
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Host with hostname '{registration.hostname}' already exists",
        )

    row = await db.fetchrow(
        "INSERT INTO hosts (organization_id, hostname, database_name, pg_version, server_role) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id, hostname, health_status, "
        "connection_status, pg_version, server_role, database_name, last_heartbeat",
        principal.organization_id,
        registration.hostname,
        registration.database_name,
        registration.pg_version,
        registration.server_role,
    )

    # Derive connection status from heartbeat (will be disconnected since no heartbeat)
    current_connection_status = classify_connection_status(row["last_heartbeat"])

    return HostSummary(
        id=row["id"],
        hostname=row["hostname"],
        database_name=row.get("database_name"),
        health_status=HealthStatus(row["health_status"]),
        connection_status=current_connection_status,
        pg_version=row["pg_version"],
        server_role=row["server_role"],
        last_heartbeat=row["last_heartbeat"],
    )


@router.post("/{host_id}/heartbeat")
async def receive_heartbeat(
    host_id: UUID,
    heartbeat: HeartbeatRequest,
    _agent_id: UUID = Depends(require_agent),
):
    """
    Receive a heartbeat from a host agent.

    Updates the host's last_heartbeat timestamp, recalculates connection status,
    and optionally updates pg_version and server_role.

    If metrics are provided, also evaluates health thresholds.

    Requirements: 1.2, 1.3
    """
    try:
        result = await process_heartbeat(
            host_id=host_id,
            pg_version=heartbeat.pg_version,
            server_role=heartbeat.server_role,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # If metrics provided, evaluate health thresholds
    health_result = None
    if heartbeat.metrics:
        try:
            health_result = await check_health_thresholds(
                host_id=host_id,
                metrics=heartbeat.metrics,
            )
        except Exception as e:
            logger.warning(f"Health threshold check failed for host {host_id}: {e}")

    return HeartbeatResponse(
        host_id=result["host_id"],
        hostname=result["hostname"],
        connection_status=result["connection_status"],
        pg_version=result.get("pg_version"),
        server_role=result.get("server_role"),
        last_heartbeat=result["last_heartbeat"],
        health_check_result=health_result,
    )


@router.post("/{host_id}/role", response_model=HostSummary)
async def receive_role_report(
    host_id: UUID,
    report: RoleReportRequest,
    db=Depends(get_db),
    _agent_id: UUID = Depends(require_agent),
) -> HostSummary:
    """
    Receive role/version reports from a host agent.

    Updates PostgreSQL version and primary/replica role as soon as the agent
    detects them.
    """
    now = datetime.now(timezone.utc)
    row = await db.fetchrow(
        """
        UPDATE hosts
        SET pg_version = $1, server_role = $2, updated_at = $3
        WHERE id = $4
        RETURNING id, hostname, health_status, connection_status,
                  pg_version, server_role, last_heartbeat
        """,
        report.pg_version,
        report.server_role,
        now,
        host_id,
    )

    if row is None:
        raise HTTPException(status_code=404, detail=f"Host with id '{host_id}' not found")

    current_connection_status = classify_connection_status(row["last_heartbeat"])
    return HostSummary(
        id=row["id"],
        hostname=row["hostname"],
        health_status=HealthStatus(row["health_status"]),
        connection_status=current_connection_status,
        pg_version=row["pg_version"],
        server_role=row["server_role"],
        last_heartbeat=row["last_heartbeat"],
    )


@router.post(
    "/{host_id}/capabilities",
    response_model=CapabilityReportResponse,
    status_code=201,
)
async def receive_capability_report(
    host_id: UUID,
    report: CapabilityReportRequest,
    db=Depends(get_db),
    _agent_id: UUID = Depends(require_agent),
) -> CapabilityReportResponse:
    """Upsert the independent capability snapshot consumed by preflight."""
    observed_at = report.observed_at or datetime.now(timezone.utc)
    previous = await db.fetchrow(
        "SELECT * FROM host_capabilities WHERE host_id = $1", host_id
    )
    row = await db.fetchrow(
        """
        INSERT INTO host_capabilities (
            host_id, organization_id, connectivity, system_information,
            system_metrics, pg_stat_statements, query_text_collection,
            configuration_read, configuration_write, reload_permission,
            restart_capability, provider_api, managed_file_access, details,
            observed_at, updated_at
        )
        SELECT h.id, h.organization_id, $2, $3, $4, $5, $6, $7, $8, $9,
               $10, $11, $12, $13::jsonb, $14, NOW()
        FROM hosts h
        WHERE h.id = $1
        ON CONFLICT (host_id) DO UPDATE SET
            organization_id = EXCLUDED.organization_id,
            connectivity = EXCLUDED.connectivity,
            system_information = EXCLUDED.system_information,
            system_metrics = EXCLUDED.system_metrics,
            pg_stat_statements = EXCLUDED.pg_stat_statements,
            query_text_collection = EXCLUDED.query_text_collection,
            configuration_read = EXCLUDED.configuration_read,
            configuration_write = EXCLUDED.configuration_write,
            reload_permission = EXCLUDED.reload_permission,
            restart_capability = EXCLUDED.restart_capability,
            provider_api = EXCLUDED.provider_api,
            managed_file_access = EXCLUDED.managed_file_access,
            details = EXCLUDED.details,
            observed_at = EXCLUDED.observed_at,
            updated_at = NOW()
        RETURNING host_id, organization_id, connectivity, system_information,
                  system_metrics, pg_stat_statements, query_text_collection,
                  configuration_read, configuration_write, reload_permission,
                  restart_capability, provider_api, managed_file_access, details,
                  observed_at
        """,
        host_id,
        report.connectivity,
        report.system_information,
        report.system_metrics,
        report.pg_stat_statements,
        report.query_text_collection,
        report.configuration_read,
        report.configuration_write,
        report.reload_permission,
        report.restart_capability,
        report.provider_api,
        report.managed_file_access,
        json.dumps(report.details),
        observed_at,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Host with id '{host_id}' not found")
    if report.database_name:
        await db.execute(
            "UPDATE hosts SET database_name = $2, updated_at = NOW() WHERE id = $1",
            host_id,
            report.database_name,
        )
    capability_values = {
        "connectivity": report.connectivity,
        "system_information": report.system_information,
        "system_metrics": report.system_metrics,
        "pg_stat_statements": report.pg_stat_statements,
        "query_text_collection": report.query_text_collection,
        "configuration_read": report.configuration_read,
        "configuration_write": report.configuration_write,
        "reload_permission": report.reload_permission,
        "restart_capability": report.restart_capability,
        "provider_api": report.provider_api,
        "managed_file_access": report.managed_file_access,
    }
    degraded = [
        name for name, value in capability_values.items()
        if not value and (previous is None or bool(previous.get(name)))
    ]
    if degraded:
        await OperationalEventRecorder().record(
            "AGENT_CAPABILITY_DEGRADED",
            "Host Agent reported unavailable capabilities",
            organization_id=row["organization_id"], host_id=host_id,
            details={"capabilities": degraded}, connection=db,
        )
    response_data = dict(row)
    response_data["database_name"] = report.database_name
    if isinstance(response_data.get("details"), str):
        response_data["details"] = json.loads(response_data["details"])
    return CapabilityReportResponse(**response_data)


def _quality_score_for_payload(payload: Any) -> float:
    """Return a conservative quality score for ingested host-agent payloads."""
    if payload is None:
        return 0.0
    if isinstance(payload, dict):
        return 0.9 if payload else 0.2
    if isinstance(payload, list):
        return 0.9 if payload else 0.2
    return 0.5


def _normalize_evidence(snapshot: EvidenceIngestRequest) -> List[Dict[str, Any]]:
    """Normalize host-agent payloads into evidence_snapshots rows."""
    data = snapshot.data
    if snapshot.evidence_type == "pg_stats":
        if not isinstance(data, dict):
            raise HTTPException(status_code=422, detail="pg_stats evidence data must be an object")

        normalized = []
        database_stats = data.get("database_stats", [])
        normalized.append(
            {
                "evidence_type": "pg_stat_database",
                "data": {
                    "database_stats": database_stats,
                    "total_databases_collected": len(database_stats),
                },
            }
        )

        statement_stats = data.get("statement_stats", [])
        normalized.append(
            {
                "evidence_type": "pg_stat_statements",
                "data": {
                    "queries": statement_stats,
                    "total_queries_collected": len(statement_stats),
                    "max_query_entries": data.get("max_query_entries"),
                },
            }
        )
        return normalized

    if snapshot.evidence_type not in ALLOWED_EVIDENCE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported evidence_type '{snapshot.evidence_type}'",
        )

    return [{"evidence_type": snapshot.evidence_type, "data": data}]


@router.post("/{host_id}/evidence", response_model=EvidenceIngestResponse, status_code=201)
async def receive_evidence(
    host_id: UUID,
    snapshot: EvidenceIngestRequest,
    db=Depends(get_db),
    _agent_id: UUID = Depends(require_agent),
) -> EvidenceIngestResponse:
    """
    Receive evidence snapshots from host agents.

    Host-agent pg_stats payloads are split into pg_stat_database and
    pg_stat_statements records so they match the persisted schema and planner
    expectations.
    """
    host = await db.fetchrow("SELECT id FROM hosts WHERE id = $1", host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host with id '{host_id}' not found")

    inserted_snapshot_ids: List[str] = []
    evidence_types: List[str] = []
    for normalized in _normalize_evidence(snapshot):
        evidence_type = normalized["evidence_type"]
        data = normalized["data"]
        quality_score = (
            snapshot.quality_score
            if snapshot.quality_score is not None
            else _quality_score_for_payload(data)
        )
        snapshot_id = await db.fetchval(
            """
            INSERT INTO evidence_snapshots (
                run_id, host_id, evidence_type, collected_at, data, quality_score
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6)
            RETURNING id
            """,
            snapshot.run_id,
            host_id,
            evidence_type,
            snapshot.collected_at,
            json.dumps(data),
            quality_score,
        )
        inserted_snapshot_ids.append(str(snapshot_id))
        evidence_types.append(evidence_type)

    return EvidenceIngestResponse(
        host_id=str(host_id),
        inserted_snapshot_ids=inserted_snapshot_ids,
        evidence_types=evidence_types,
        total=len(inserted_snapshot_ids),
    )


@router.get("/{host_id}/commands", response_model=Optional[AgentCommandResponse])
async def claim_agent_command(
    host_id: UUID,
    db=Depends(get_db),
    _agent_id: UUID = Depends(require_agent),
) -> Optional[AgentCommandResponse]:
    """Atomically claim the oldest unexpired command for this authenticated agent."""
    async with db.transaction():
        await db.execute(
            """
            UPDATE agent_commands
            SET status = 'expired', completed_at = NOW(), error = 'Command expired'
            WHERE host_id = $1 AND status IN ('queued', 'claimed') AND expires_at <= NOW()
            """,
            host_id,
        )
        row = await db.fetchrow(
            """
            WITH candidate AS (
                SELECT id
                FROM agent_commands
                WHERE host_id = $1 AND status = 'queued' AND expires_at > NOW()
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE agent_commands c
            SET status = 'claimed', claimed_at = NOW()
            FROM candidate
            WHERE c.id = candidate.id
            RETURNING c.id, c.action, c.payload, c.expires_at
            """,
            host_id,
        )
    if row is None:
        return None
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return AgentCommandResponse(
        id=row["id"], action=row["action"], payload=payload, expires_at=row["expires_at"]
    )


@router.post("/{host_id}/commands/{command_id}/result")
async def complete_agent_command(
    host_id: UUID,
    command_id: UUID,
    report: AgentCommandResultRequest,
    db=Depends(get_db),
    _agent_id: UUID = Depends(require_agent),
) -> Dict[str, Any]:
    """Persist a terminal, immutable result for one claimed agent command."""
    row = await db.fetchrow(
        """
        UPDATE agent_commands
        SET status = $3, result = $4::jsonb, error = $5, completed_at = NOW()
        WHERE id = $1 AND host_id = $2 AND status = 'claimed'
        RETURNING id, status
        """,
        command_id,
        host_id,
        "succeeded" if report.succeeded else "failed",
        json.dumps(report.result),
        report.error,
    )
    if row is None:
        existing = await db.fetchrow(
            "SELECT id, status FROM agent_commands WHERE id = $1 AND host_id = $2",
            command_id,
            host_id,
        )
        if existing is None:
            raise HTTPException(status_code=409, detail="Command is missing")
        matching_terminal = (
            report.succeeded and existing["status"] == "succeeded"
        ) or (not report.succeeded and existing["status"] == "failed")
        if matching_terminal:
            return {"id": str(existing["id"]), "status": existing["status"]}
        if existing["status"] == "expired":
            # Preserve late apply provenance for safe recovery without pretending
            # that the timed-out command completed within its execution lease.
            await db.execute(
                """
                UPDATE agent_commands
                SET result = $3::jsonb, error = COALESCE($4, error),
                    completed_at = NOW()
                WHERE id = $1 AND host_id = $2 AND status = 'expired'
                """,
                command_id,
                host_id,
                json.dumps(report.result),
                report.error or "Agent result arrived after command expiry",
            )
            return {
                "id": str(existing["id"]),
                "status": "expired",
                "result_recorded": True,
            }
        raise HTTPException(status_code=409, detail="Command is already terminal")
    if not report.succeeded:
        await OperationalEventRecorder().record(
            "AGENT_COMMAND_FAILED", "A Host Agent command failed",
            host_id=host_id,
            details={"command_id": str(command_id), "error": report.error},
            connection=db,
        )
    return {"id": str(row["id"]), "status": row["status"]}


@router.post("/{host_id}/agent-token", response_model=AgentTokenResponse)
async def rotate_agent_token(
    host_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("admin")),
) -> AgentTokenResponse:
    token = secrets.token_urlsafe(32)
    async with db.transaction():
        updated = await db.fetchval(
            """
            UPDATE hosts
            SET agent_token_hash = $3, agent_write_ambiguous = FALSE,
                agent_lease_holder_id = NULL, agent_lease_expires_at = NULL,
                updated_at = NOW()
            WHERE id = $1 AND organization_id = $2
            RETURNING id
            """,
            host_id,
            principal.organization_id,
            hash_token(token),
        )
        if updated is not None:
            await db.execute(
                "DELETE FROM host_agent_instances WHERE host_id = $1", host_id
            )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Host with id '{host_id}' not found")
    return AgentTokenResponse(host_id=host_id, agent_token=token)


@router.put("/{host_id}/execution-policy", response_model=HostSummary)
async def update_execution_policy(
    host_id: UUID,
    policy: ExecutionPolicyUpdate,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("admin")),
) -> HostSummary:
    row = await db.fetchrow(
        """
        UPDATE hosts
        SET environment = $3, target_dsn_env = $4, writes_enabled = $5,
            database_name = COALESCE($6, database_name),
            platform_type = COALESCE($7, platform_type),
            configuration_backend = COALESCE($8, configuration_backend),
            managed_conf_enrolled = COALESCE($9, managed_conf_enrolled),
            restart_required_enabled = COALESCE($10, restart_required_enabled),
            managed_conf_path = COALESCE($11, managed_conf_path),
            updated_at = NOW()
        WHERE id = $1 AND organization_id = $2
        RETURNING id, hostname, health_status, connection_status,
                  pg_version, server_role, database_name, last_heartbeat,
                  agent_write_ambiguous, agent_lease_expires_at
        """,
        host_id,
        principal.organization_id,
        policy.environment,
        policy.target_dsn_env,
        policy.writes_enabled,
        policy.database_name,
        policy.platform_type,
        policy.configuration_backend,
        policy.managed_conf_enrolled,
        policy.restart_required_enabled,
        policy.managed_conf_path,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Host with id '{host_id}' not found")
    return HostSummary(
        id=row["id"],
        hostname=row["hostname"],
        database_name=row.get("database_name"),
        health_status=HealthStatus(row["health_status"]),
        connection_status=classify_connection_status(row["last_heartbeat"]),
        pg_version=row["pg_version"],
        server_role=row["server_role"],
        last_heartbeat=row["last_heartbeat"],
        agent_write_ambiguous=bool(row["agent_write_ambiguous"]),
        agent_lease_expires_at=row["agent_lease_expires_at"],
    )
