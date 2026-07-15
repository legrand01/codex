"""
Reports API endpoints.

Provides routes for:
- GET /api/v1/reports/{run_id} - get or generate a report for a run
- GET /api/v1/reports/search - search reports by date range, host, and keywords

Requirements: 13.4, 13.5, 13.6
"""

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.db.pool import get_pool
from backend.security import Principal, require_roles
from backend.services.audit_logger import get_audit_logger
from backend.services.report_generator import (
    ReportGenerationError,
    get_report_generator,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


# --- Response Models ---


class ReportSummary(BaseModel):
    """Summary representation of a DBA report for search results."""

    id: UUID
    run_id: UUID
    goal: str
    host_id: Optional[UUID] = None
    outcome_status: str
    generated_at: datetime
    expires_at: Optional[datetime] = None


class ReportSearchResponse(BaseModel):
    """Response model for report search."""

    reports: List[ReportSummary]
    total: int


class ReportResponse(BaseModel):
    """Full report response model."""

    id: UUID
    run_id: UUID
    goal: str
    host_id: Optional[UUID] = None
    outcome_status: str
    evidence_summaries: List[dict]
    plans_proposed: List[dict]
    approval_decisions: List[dict]
    applied_changes: List[dict]
    verification_results: List[dict]
    parameter_dispositions: List[dict]
    generated_at: datetime
    expires_at: Optional[datetime] = None


# --- Endpoints ---


@router.get("/search", response_model=ReportSearchResponse)
async def search_reports(
    start_date: Optional[datetime] = Query(default=None, description="Start of date range filter"),
    end_date: Optional[datetime] = Query(default=None, description="End of date range filter"),
    host_id: Optional[UUID] = Query(default=None, description="Filter by host identifier"),
    keywords: Optional[str] = Query(
        default=None, description="Search goal text by keywords (space-separated)"
    ),
    limit: int = Query(default=50, ge=1, le=200, description="Max results to return"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    principal: Principal = Depends(require_roles("viewer", "operator", "approver", "admin")),
) -> ReportSearchResponse:
    """
    Search DBA reports by date range, host identifier, and goal keywords.

    Returns matching results within 5 seconds. Reports are retained for
    a minimum of 90 days (enforced via expires_at column).

    Requirements: 13.4, 13.6
    """
    pool = get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database connection unavailable")

    try:
        # Build dynamic query
        conditions = ["r.organization_id = $1"]
        params = [principal.organization_id]
        param_idx = 2

        if start_date is not None:
            conditions.append(f"d.generated_at >= ${param_idx}")
            params.append(start_date)
            param_idx += 1

        if end_date is not None:
            conditions.append(f"d.generated_at <= ${param_idx}")
            params.append(end_date)
            param_idx += 1

        if host_id is not None:
            conditions.append(f"d.host_id = ${param_idx}")
            params.append(host_id)
            param_idx += 1

        if keywords is not None and keywords.strip():
            # Search goal text using ILIKE for each keyword
            keyword_list = keywords.strip().split()
            keyword_conditions = []
            for keyword in keyword_list:
                keyword_conditions.append(f"d.goal ILIKE ${param_idx}")
                params.append(f"%{keyword}%")
                param_idx += 1
            # All keywords must match (AND logic)
            conditions.append(f"({' AND '.join(keyword_conditions)})")

        # Only return non-expired reports (retention >= 90 days)
        conditions.append(f"(d.expires_at IS NULL OR d.expires_at > ${param_idx})")
        params.append(datetime.now(timezone.utc))
        param_idx += 1

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        # Count query
        count_query = (
            "SELECT COUNT(*) FROM dba_reports d "
            "JOIN loop_runs r ON r.id = d.run_id " + where_clause
        )
        async with pool.acquire() as conn:
            total = await conn.fetchval(count_query, *params)

        # Data query with pagination
        params.append(limit)
        limit_ph = f"${param_idx}"
        param_idx += 1

        params.append(offset)
        offset_ph = f"${param_idx}"

        data_query = f"""
            SELECT d.id, d.run_id, d.goal, d.host_id, d.outcome_status,
                   d.generated_at, d.expires_at
            FROM dba_reports d
            JOIN loop_runs r ON r.id = d.run_id
            {where_clause}
            ORDER BY generated_at DESC
            LIMIT {limit_ph} OFFSET {offset_ph}
        """

        async with pool.acquire() as conn:
            rows = await conn.fetch(data_query, *params)

        reports = [
            ReportSummary(
                id=row["id"],
                run_id=row["run_id"],
                goal=row["goal"],
                host_id=row["host_id"],
                outcome_status=row["outcome_status"],
                generated_at=row["generated_at"],
                expires_at=row["expires_at"],
            )
            for row in rows
        ]

        return ReportSearchResponse(reports=reports, total=total or 0)

    except Exception as e:
        logger.error(f"Failed to search reports: {e}")
        raise HTTPException(status_code=500, detail=f"Report search failed: {e}")


@router.get("/{run_id}", response_model=ReportResponse)
async def get_report(
    run_id: UUID,
    principal: Principal = Depends(require_roles("viewer", "operator", "approver", "admin")),
) -> ReportResponse:
    """
    Get or generate a DBA report for a specific run.

    If a report already exists for the run, returns it.
    If no report exists, attempts to generate one.

    Handles report generation failure by logging the failure and
    persisting raw run data for regeneration.

    Requirements: 13.4, 13.5
    """
    pool = get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database connection unavailable")

    try:
        # Check if report already exists
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT d.id, d.run_id, d.goal, d.host_id, d.outcome_status,
                       d.report_content, d.generated_at, d.expires_at
                FROM dba_reports d
                JOIN loop_runs r ON r.id = d.run_id
                WHERE d.run_id = $1 AND r.organization_id = $2
                """,
                run_id,
                principal.organization_id,
            )

        if row is not None:
            # Parse report_content from JSONB
            report_content = row["report_content"]
            if isinstance(report_content, str):
                report_content = json.loads(report_content)

            return ReportResponse(
                id=row["id"],
                run_id=row["run_id"],
                goal=row["goal"],
                host_id=row["host_id"],
                outcome_status=row["outcome_status"],
                evidence_summaries=report_content.get("evidence_summaries", []),
                plans_proposed=report_content.get("plans_proposed", []),
                approval_decisions=report_content.get("approval_decisions", []),
                applied_changes=report_content.get("applied_changes", []),
                verification_results=report_content.get("verification_results", []),
                parameter_dispositions=report_content.get(
                    "parameter_dispositions", []
                ),
                generated_at=row["generated_at"],
                expires_at=row["expires_at"],
            )

        # A missing report must not become a cross-tenant run existence oracle.
        async with pool.acquire() as conn:
            owned_run = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM loop_runs WHERE id = $1 AND organization_id = $2)",
                run_id,
                principal.organization_id,
            )
        if not owned_run:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        # No existing report - attempt to generate one
        try:
            report_generator = get_report_generator()
            report = await report_generator.generate_report(run_id)

            return ReportResponse(
                id=report.id,
                run_id=report.run_id,
                goal=report.goal,
                host_id=None,
                outcome_status=report.outcome_status,
                evidence_summaries=report.evidence_summaries,
                plans_proposed=report.plans_proposed,
                approval_decisions=report.approval_decisions,
                applied_changes=report.applied_changes,
                verification_results=report.verification_results,
                parameter_dispositions=report.parameter_dispositions,
                generated_at=report.generated_at,
                expires_at=None,
            )

        except ReportGenerationError as gen_err:
            # Log failure and persist raw run data for regeneration
            logger.error(f"Report generation failed for run {run_id}: {gen_err}")

            # Try to log the failure in audit log
            try:
                audit_logger = get_audit_logger()
                await audit_logger.log(
                    run_id=run_id,
                    actor_type="system",
                    actor_name="report_generator",
                    action_type="report_generation_failed",
                    result="failure",
                    result_reason=str(gen_err),
                    details={"run_id": str(run_id)},
                )
            except Exception as audit_err:
                logger.warning(f"Failed to log report generation failure: {audit_err}")

            raise HTTPException(
                status_code=404,
                detail=f"Report not found and generation failed for run {run_id}: {gen_err}",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get report for run {run_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve report: {e}")
