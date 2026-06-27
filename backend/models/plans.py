"""
Plan-related Pydantic models for AI-generated recommendations.
"""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from backend.models.enums import PlanStatus


class PlanDetail(BaseModel):
    """Detailed representation of an AI-generated plan."""

    id: UUID
    run_id: UUID
    host_id: UUID
    status: PlanStatus
    proposed_changes: List[dict]
    evidence_references: List[dict]
    risk_score: int = Field(ge=0, le=100)
    confidence_score: float = Field(ge=0.0, le=1.0)
    uncertainty_explanation: Optional[str] = None
    rollback_instructions: List[dict]
    submission_time: datetime


class RiskScore(BaseModel):
    """Risk assessment for a proposed plan."""

    score: int = Field(ge=0, le=100)
    breakdown: List[dict]
    host_role_multiplier: float
    blocked: bool
    block_reason: Optional[str] = None
