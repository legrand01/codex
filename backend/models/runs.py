"""
Run-related Pydantic models for DBA loop monitoring.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from backend.models.enums import WorkflowStep


class RunSummary(BaseModel):
    """Summary representation of an active DBA loop run."""

    id: UUID
    goal: str
    current_step: WorkflowStep
    status: str
    current_iteration: int
    started_at: datetime
    last_step_transition_at: datetime
    elapsed_seconds: float
