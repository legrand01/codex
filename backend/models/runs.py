"""
Run-related Pydantic models for DBA loop monitoring.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel

from backend.models.enums import WorkflowStep


class RunSummary(BaseModel):
    """Summary representation of a persistent DBA tuning session."""

    id: UUID
    host_id: Optional[UUID] = None
    hostname: Optional[str] = None
    database_name: Optional[str] = None
    goal: str
    current_step: WorkflowStep
    status: str
    tuning_target: str = "system_wide_aqr"
    tuning_mode: str = "reload_only"
    baseline_score: Optional[float] = None
    best_score: Optional[float] = None
    current_iteration: int
    started_at: datetime
    completed_at: Optional[datetime] = None
    last_step_transition_at: datetime
    elapsed_seconds: float
