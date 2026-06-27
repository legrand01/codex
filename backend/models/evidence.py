"""
Evidence-related Pydantic models for telemetry snapshots.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class EvidenceSnapshot(BaseModel):
    """A single evidence snapshot collected by a Host Agent."""

    id: UUID
    run_id: UUID
    host_id: UUID
    evidence_type: str
    collected_at: datetime
    data: dict
    quality_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
