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

# Expose as derive_connection_status for backward compatibility
derive_connection_status = classify_connection_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/fleet", tags=["fleet"])


# --- Request/Response Models ---


class HostRegistration(BaseModel):
    """Request model for registering a new host."""

    hostname: str = Field(..., min_length=1, max_length=255)
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


class AgentTokenResponse(BaseModel):
    host_id: UUID
    agent_token: str


class ExecutionPolicyUpdate(BaseModel):
    environment: str = Field(..., pattern="^(development|staging|production)$")
    target_dsn_env: str = Field(..., min_length=1, max_length=255, pattern="^[A-Z][A-Z0-9_]+$")
    writes_enabled: bool = False


ALLOWED_EVIDENCE_TYPES = {
    "pg_settings",
    "pg_stat_database",
    "pg_stat_statements",
    "locks",
    "replication",
    "wal_checkpoint",
    "os_metrics",
}


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
        "pg_version, server_role, last_heartbeat "
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
                health_status=HealthStatus(row["health_status"]),
                connection_status=current_connection_status,
                pg_version=row["pg_version"],
                server_role=row["server_role"],
                last_heartbeat=row["last_heartbeat"],
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
        "pg_version, server_role, last_heartbeat "
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
        health_status=HealthStatus(row["health_status"]),
        connection_status=current_connection_status,
        pg_version=row["pg_version"],
        server_role=row["server_role"],
        last_heartbeat=row["last_heartbeat"],
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
        "INSERT INTO hosts (organization_id, hostname, pg_version, server_role) "
        "VALUES ($1, $2, $3, $4) RETURNING id, hostname, health_status, "
        "connection_status, pg_version, server_role, last_heartbeat",
        principal.organization_id,
        registration.hostname,
        registration.pg_version,
        registration.server_role,
    )

    # Derive connection status from heartbeat (will be disconnected since no heartbeat)
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


@router.post("/{host_id}/agent-token", response_model=AgentTokenResponse)
async def rotate_agent_token(
    host_id: UUID,
    db=Depends(get_db),
    principal: Principal = Depends(require_roles("admin")),
) -> AgentTokenResponse:
    token = secrets.token_urlsafe(32)
    updated = await db.fetchval(
        """
        UPDATE hosts
        SET agent_token_hash = $3, updated_at = NOW()
        WHERE id = $1 AND organization_id = $2
        RETURNING id
        """,
        host_id,
        principal.organization_id,
        hash_token(token),
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
            updated_at = NOW()
        WHERE id = $1 AND organization_id = $2
        RETURNING id, hostname, health_status, connection_status,
                  pg_version, server_role, last_heartbeat
        """,
        host_id,
        principal.organization_id,
        policy.environment,
        policy.target_dsn_env,
        policy.writes_enabled,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Host with id '{host_id}' not found")
    return HostSummary(
        id=row["id"],
        hostname=row["hostname"],
        health_status=HealthStatus(row["health_status"]),
        connection_status=classify_connection_status(row["last_heartbeat"]),
        pg_version=row["pg_version"],
        server_role=row["server_role"],
        last_heartbeat=row["last_heartbeat"],
    )
