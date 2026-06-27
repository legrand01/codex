"""
Audit-related Pydantic models for the append-only audit log.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class AuditEntry(BaseModel):
    """A single entry in the append-only audit log."""

    id: int
    run_id: Optional[UUID] = None
    timestamp: datetime
    actor_type: str  # "human" or "system"
    actor_name: str
    action_type: str
    target_host_id: Optional[UUID] = None
    result: str  # "success", "failure", or "blocked"
    result_reason: Optional[str] = None
    details: Optional[dict] = None
