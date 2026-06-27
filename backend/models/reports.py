"""
Report-related Pydantic models for DBA report generation.
"""

from datetime import datetime
from typing import List
from uuid import UUID

from pydantic import BaseModel


class DBAReport(BaseModel):
    """Final report generated at the end of a DBA loop run."""

    id: UUID
    run_id: UUID
    goal: str
    outcome_status: str  # "success", "partial_success", or "failure"
    evidence_summaries: List[dict]
    plans_proposed: List[dict]
    approval_decisions: List[dict]
    applied_changes: List[dict]
    verification_results: List[dict]
    generated_at: datetime
