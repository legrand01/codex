"""
Host-related Pydantic models for fleet management.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel

from backend.models.enums import ConnectionStatus, HealthStatus


class HostSummary(BaseModel):
    """Summary representation of a PostgreSQL host in the fleet overview."""

    id: UUID
    hostname: str
    health_status: HealthStatus
    connection_status: ConnectionStatus
    pg_version: Optional[str] = None
    server_role: Optional[str] = None
    last_heartbeat: Optional[datetime] = None
