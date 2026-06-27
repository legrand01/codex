"""
DBA Report Generator service.

Generates comprehensive reports for completed loop runs containing:
- Original goal
- Evidence summaries with confidence scores
- Plans proposed
- Approval decisions
- Applied changes
- Verification results
- Outcome status

Each item is labeled as "AI_RECOMMENDATION" or "VERIFIED_FACT".
Recommendations with evidence below confidence threshold are marked "INCONCLUSIVE".

Requirements: 13.1, 13.2, 13.3
"""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from backend.db.pool import get_pool
from backend.models.reports import DBAReport

logger = logging.getLogger(__name__)

# Default confidence threshold below which items are marked INCONCLUSIVE
DEFAULT_CONFIDENCE_THRESHOLD = 0.6

# Provenance labels
LABEL_AI_RECOMMENDATION = "AI_RECOMMENDATION"
LABEL_VERIFIED_FACT = "VERIFIED_FACT"
LABEL_INCONCLUSIVE = "INCONCLUSIVE"


class ReportGenerationError(Exception):
    """Base exception for report generation errors."""

    pass


class ReportGenerator:
    """
    Generates DBA reports for completed loop runs.

    Aggregates evidence, plans, approvals, applied changes, and verification
    results into a structured report with provenance labeling.
    """

    def __init__(self, pool=None, confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD):
        """
        Initialize the ReportGenerator.

        Args:
            pool: Optional asyncpg connection pool. If None, uses get_pool().
            confidence_threshold: Threshold below which items are marked INCONCLUSIVE.
        """
        self._pool = pool
        self.confidence_threshold = confidence_threshold

    @property
    def pool(self):
        """Get the connection pool, falling back to the global pool."""
        if self._pool is not None:
            return self._pool
        pool = get_pool()
        if pool is None:
            raise RuntimeError("Database connection pool is not initialized.")
        return pool

    async def _fetch_run(self, run_id: UUID) -> Optional[Dict]:
        """Fetch the loop run record."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, host_id, goal, status, current_step, current_iteration,
                       started_at, completed_at, failure_reason
                FROM loop_runs WHERE id = $1
                """,
                run_id,
            )
        if row is None:
            return None
        return dict(row)

    async def _fetch_evidence(self, run_id: UUID) -> List[Dict]:
        """Fetch all evidence snapshots for a run."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, host_id, evidence_type, collected_at, data, quality_score
                FROM evidence_snapshots
                WHERE run_id = $1
                ORDER BY collected_at ASC
                """,
                run_id,
            )
        return [dict(row) for row in rows]

    async def _fetch_plans(self, run_id: UUID) -> List[Dict]:
        """Fetch all plans for a run."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, host_id, status, proposed_changes, evidence_references,
                       risk_score, confidence_score, uncertainty_explanation,
                       rollback_instructions, rejection_reason,
                       approved_by, approved_at, rejected_by, rejected_at,
                       applied_at, rolled_back_at, submission_time
                FROM plans
                WHERE run_id = $1
                ORDER BY submission_time ASC
                """,
                run_id,
            )
        return [dict(row) for row in rows]

    async def _fetch_audit_entries(self, run_id: UUID) -> List[Dict]:
        """Fetch audit log entries for a run."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, timestamp, actor_type, actor_name, action_type,
                       target_host_id, result, result_reason, details
                FROM audit_log
                WHERE run_id = $1
                ORDER BY timestamp ASC, id ASC
                """,
                run_id,
            )
        return [dict(row) for row in rows]

    def _build_evidence_summaries(self, evidence_rows: List[Dict]) -> List[Dict]:
        """
        Build evidence summaries with confidence scores and provenance labels.

        Evidence snapshots are VERIFIED_FACT since they represent collected data.
        Items with quality_score below threshold are marked INCONCLUSIVE.
        """
        summaries = []
        for row in evidence_rows:
            quality_score = float(row.get("quality_score") or 0.0)

            # Determine provenance label
            if quality_score < self.confidence_threshold:
                provenance = LABEL_INCONCLUSIVE
                evidence_gap = (
                    f"Evidence quality ({quality_score:.2f}) below threshold "
                    f"({self.confidence_threshold:.2f})"
                )
            else:
                provenance = LABEL_VERIFIED_FACT
                evidence_gap = None

            summary = {
                "snapshot_id": str(row["id"]),
                "evidence_type": row["evidence_type"],
                "collected_at": (
                    row["collected_at"].isoformat()
                    if isinstance(row["collected_at"], datetime)
                    else str(row["collected_at"])
                ),
                "quality_score": quality_score,
                "provenance": provenance,
            }
            if evidence_gap:
                summary["evidence_gap"] = evidence_gap

            summaries.append(summary)

        return summaries

    def _build_plans_proposed(self, plans: List[Dict]) -> List[Dict]:
        """
        Build plans proposed section with provenance labels.

        AI-generated plan proposals are labeled as AI_RECOMMENDATION.
        Plans with confidence below threshold are marked INCONCLUSIVE.
        """
        proposed = []
        for plan in plans:
            confidence_score = float(plan.get("confidence_score") or 0.0)

            # Determine provenance
            if confidence_score < self.confidence_threshold:
                provenance = LABEL_INCONCLUSIVE
                evidence_gap = (
                    f"Plan confidence ({confidence_score:.2f}) below threshold "
                    f"({self.confidence_threshold:.2f})"
                )
            else:
                provenance = LABEL_AI_RECOMMENDATION
                evidence_gap = None

            # Parse JSONB fields
            proposed_changes = plan.get("proposed_changes")
            if isinstance(proposed_changes, str):
                proposed_changes = json.loads(proposed_changes)

            entry = {
                "plan_id": str(plan["id"]),
                "status": plan["status"],
                "proposed_changes": proposed_changes or [],
                "risk_score": plan.get("risk_score"),
                "confidence_score": confidence_score,
                "uncertainty_explanation": plan.get("uncertainty_explanation"),
                "submission_time": (
                    plan["submission_time"].isoformat()
                    if isinstance(plan.get("submission_time"), datetime)
                    else str(plan.get("submission_time"))
                ),
                "provenance": provenance,
            }
            if evidence_gap:
                entry["evidence_gap"] = evidence_gap

            proposed.append(entry)

        return proposed

    def _build_approval_decisions(self, plans: List[Dict], audit_entries: List[Dict]) -> List[Dict]:
        """
        Build approval decisions section.

        Approval/rejection decisions are VERIFIED_FACT since they represent
        human actions recorded in the audit log.
        """
        decisions = []
        for plan in plans:
            if plan.get("approved_by"):
                decisions.append({
                    "plan_id": str(plan["id"]),
                    "decision": "approved",
                    "actor": plan["approved_by"],
                    "timestamp": (
                        plan["approved_at"].isoformat()
                        if isinstance(plan.get("approved_at"), datetime)
                        else str(plan.get("approved_at"))
                    ),
                    "provenance": LABEL_VERIFIED_FACT,
                })
            elif plan.get("rejected_by"):
                decisions.append({
                    "plan_id": str(plan["id"]),
                    "decision": "rejected",
                    "actor": plan["rejected_by"],
                    "reason": plan.get("rejection_reason"),
                    "timestamp": (
                        plan["rejected_at"].isoformat()
                        if isinstance(plan.get("rejected_at"), datetime)
                        else str(plan.get("rejected_at"))
                    ),
                    "provenance": LABEL_VERIFIED_FACT,
                })

        return decisions

    def _build_applied_changes(self, plans: List[Dict]) -> List[Dict]:
        """
        Build applied changes section.

        Applied changes are VERIFIED_FACT since the application was confirmed.
        """
        changes = []
        for plan in plans:
            if plan.get("applied_at") is not None:
                proposed_changes = plan.get("proposed_changes")
                if isinstance(proposed_changes, str):
                    proposed_changes = json.loads(proposed_changes)

                rollback_instructions = plan.get("rollback_instructions")
                if isinstance(rollback_instructions, str):
                    rollback_instructions = json.loads(rollback_instructions)

                changes.append({
                    "plan_id": str(plan["id"]),
                    "applied_at": (
                        plan["applied_at"].isoformat()
                        if isinstance(plan["applied_at"], datetime)
                        else str(plan["applied_at"])
                    ),
                    "proposed_changes": proposed_changes or [],
                    "rollback_instructions": rollback_instructions or [],
                    "rolled_back": plan.get("status") == "rolled_back",
                    "rolled_back_at": (
                        plan["rolled_back_at"].isoformat()
                        if isinstance(plan.get("rolled_back_at"), datetime)
                        else None
                    ),
                    "provenance": LABEL_VERIFIED_FACT,
                })

        return changes

    def _build_verification_results(self, audit_entries: List[Dict]) -> List[Dict]:
        """
        Build verification results section from audit entries.

        Verification results are VERIFIED_FACT since they represent measured outcomes.
        """
        results = []
        for entry in audit_entries:
            action_type = entry.get("action_type", "")
            if "verification" in action_type.lower() or "verify" in action_type.lower():
                details = entry.get("details")
                if isinstance(details, str):
                    details = json.loads(details)

                results.append({
                    "action": action_type,
                    "timestamp": (
                        entry["timestamp"].isoformat()
                        if isinstance(entry.get("timestamp"), datetime)
                        else str(entry.get("timestamp"))
                    ),
                    "result": entry.get("result"),
                    "result_reason": entry.get("result_reason"),
                    "details": details,
                    "provenance": LABEL_VERIFIED_FACT,
                })

        return results

    def _determine_outcome_status(self, run: Dict, plans: List[Dict]) -> str:
        """
        Determine the overall outcome status of the run.

        Returns one of: "success", "partial_success", "failure"
        """
        run_status = run.get("status", "")

        if run_status == "completed":
            # Check if all plans were applied successfully
            applied_plans = [p for p in plans if p.get("applied_at") is not None]
            rolled_back = [p for p in plans if p.get("status") == "rolled_back"]

            if not plans:
                return "success"
            elif rolled_back:
                if len(rolled_back) == len(applied_plans):
                    return "failure"
                return "partial_success"
            elif applied_plans:
                return "success"
            return "partial_success"

        elif run_status in ("failed", "timed_out", "unresponsive"):
            return "failure"

        elif run_status == "manually_halted":
            # Halted runs are partial success if some work was done
            applied_plans = [p for p in plans if p.get("applied_at") is not None]
            if applied_plans:
                return "partial_success"
            return "failure"

        return "partial_success"

    async def generate_report(self, run_id: UUID) -> DBAReport:
        """
        Generate a comprehensive DBA report for a completed loop run.

        The report must be generated within 30 seconds and contains:
        - Original goal
        - Evidence summaries with confidence scores
        - Plans proposed
        - Approval decisions
        - Applied changes
        - Verification results
        - Outcome status

        Each item is labeled as AI_RECOMMENDATION or VERIFIED_FACT.
        Recommendations with evidence below confidence threshold are
        marked as INCONCLUSIVE with evidence gap reference.

        Args:
            run_id: UUID of the completed loop run.

        Returns:
            DBAReport with all sections populated.

        Raises:
            ReportGenerationError: If report generation fails.
        """
        try:
            # Fetch run data
            run = await self._fetch_run(run_id)
            if run is None:
                raise ReportGenerationError(f"Loop run {run_id} not found")

            # Fetch all related data
            evidence_rows = await self._fetch_evidence(run_id)
            plans = await self._fetch_plans(run_id)
            audit_entries = await self._fetch_audit_entries(run_id)

            # Build report sections
            evidence_summaries = self._build_evidence_summaries(evidence_rows)
            plans_proposed = self._build_plans_proposed(plans)
            approval_decisions = self._build_approval_decisions(plans, audit_entries)
            applied_changes = self._build_applied_changes(plans)
            verification_results = self._build_verification_results(audit_entries)
            outcome_status = self._determine_outcome_status(run, plans)

            # Build report content JSONB
            report_content = {
                "evidence_summaries": evidence_summaries,
                "plans_proposed": plans_proposed,
                "approval_decisions": approval_decisions,
                "applied_changes": applied_changes,
                "verification_results": verification_results,
            }

            # Generate report ID
            report_id = uuid4()
            generated_at = datetime.now(timezone.utc)

            # Persist the report
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO dba_reports (id, run_id, goal, host_id, outcome_status,
                                           report_content, generated_at, expires_at)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $7 + INTERVAL '90 days')
                    ON CONFLICT (run_id) DO UPDATE SET
                        goal = EXCLUDED.goal,
                        outcome_status = EXCLUDED.outcome_status,
                        report_content = EXCLUDED.report_content,
                        generated_at = EXCLUDED.generated_at,
                        expires_at = EXCLUDED.expires_at
                    """,
                    report_id,
                    run_id,
                    run["goal"],
                    run.get("host_id"),
                    outcome_status,
                    json.dumps(report_content),
                    generated_at,
                )

            # Return the DBAReport model
            return DBAReport(
                id=report_id,
                run_id=run_id,
                goal=run["goal"],
                outcome_status=outcome_status,
                evidence_summaries=evidence_summaries,
                plans_proposed=plans_proposed,
                approval_decisions=approval_decisions,
                applied_changes=applied_changes,
                verification_results=verification_results,
                generated_at=generated_at,
            )

        except ReportGenerationError:
            raise
        except Exception as e:
            logger.error(f"Failed to generate report for run {run_id}: {e}")
            raise ReportGenerationError(
                f"Failed to generate report for run {run_id}: {e}"
            ) from e


# Module-level singleton
_report_generator: Optional[ReportGenerator] = None


def get_report_generator() -> ReportGenerator:
    """
    Get or create the module-level ReportGenerator singleton.

    Returns:
        The ReportGenerator instance.
    """
    global _report_generator
    if _report_generator is None:
        _report_generator = ReportGenerator()
    return _report_generator
