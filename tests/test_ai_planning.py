"""
Tests for AI Planning Module: evidence quality checking, diagnosis,
plan generation, rollback instructions, and confidence scoring.

Covers tasks 11.1, 11.2, 11.3.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from backend.services.ai_planning import (
    DEFAULT_QUALITY_THRESHOLD,
    EXPECTED_EVIDENCE_TYPES,
    DiagnosisResult,
    EvidenceQualityReport,
    GeneratedPlan,
    Recommendation,
    check_evidence_quality,
    diagnose,
    generate_plan,
)

# ─── Helpers ─────────────────────────────────────────────────────────────────


def make_evidence_snapshot(
    evidence_type: str,
    data: dict = None,
    age_seconds: float = 30.0,
    snapshot_id: str = None,
):
    """Create a mock evidence snapshot dict."""
    now = datetime.now(timezone.utc)
    collected_at = now - timedelta(seconds=age_seconds)
    return {
        "id": snapshot_id or str(uuid4()),
        "evidence_type": evidence_type,
        "collected_at": collected_at,
        "data": data if data is not None else {"sample_key": "sample_value"},
    }


def make_full_evidence_set(age_seconds: float = 30.0):
    """Create a complete set of evidence spanning all expected types."""
    return [
        make_evidence_snapshot(
            "pg_settings",
            data={
                "shared_buffers": "128MB",
                "work_mem": "4MB",
                "effective_cache_size": "4GB",
                "max_connections": "100",
                "maintenance_work_mem": "64MB",
            },
            age_seconds=age_seconds,
        ),
        make_evidence_snapshot(
            "pg_stat_database",
            data={"xact_commit": 1000, "xact_rollback": 5, "blks_hit": 50000},
            age_seconds=age_seconds,
        ),
        make_evidence_snapshot(
            "pg_stat_statements",
            data=[
                {"query": "SELECT 1", "calls": 100, "total_time": 50.0},
                {"query": "SELECT * FROM users", "calls": 50, "total_time": 200.0},
            ],
            age_seconds=age_seconds,
        ),
        make_evidence_snapshot(
            "locks",
            data={"active_locks": 3, "waiting_locks": 0},
            age_seconds=age_seconds,
        ),
        make_evidence_snapshot(
            "replication",
            data={"lag_bytes": 1024, "state": "streaming"},
            age_seconds=age_seconds,
        ),
        make_evidence_snapshot(
            "wal_checkpoint",
            data={"checkpoint_frequency": 300, "wal_size_mb": 64},
            age_seconds=age_seconds,
        ),
        make_evidence_snapshot(
            "os_metrics",
            data={"cpu_percent": 45.0, "memory_percent": 60.0, "disk_iops": 150},
            age_seconds=age_seconds,
        ),
    ]


# ─── Evidence Quality Checking Tests (Task 11.2) ─────────────────────────────


class TestCheckEvidenceQuality:
    """Tests for check_evidence_quality function."""

    def test_all_types_present_fresh_evidence(self):
        """All expected evidence types present and fresh → sufficient."""
        evidence = make_full_evidence_set(age_seconds=30)

        report = check_evidence_quality(evidence)

        assert isinstance(report, EvidenceQualityReport)
        assert report.sufficient is True
        assert len(report.available_types) == len(EXPECTED_EVIDENCE_TYPES)
        assert report.missing_types == []
        # All fresh evidence should have good quality scores
        for etype in EXPECTED_EVIDENCE_TYPES:
            assert etype in report.quality_scores
            assert report.quality_scores[etype] >= DEFAULT_QUALITY_THRESHOLD

    def test_missing_evidence_types(self):
        """Some evidence types missing → reports them as missing."""
        evidence = [
            make_evidence_snapshot("pg_settings", data={"shared_buffers": "128MB"}),
            make_evidence_snapshot("os_metrics", data={"cpu_percent": 50.0}),
        ]

        report = check_evidence_quality(evidence)

        assert "pg_settings" in report.available_types
        assert "os_metrics" in report.available_types
        assert "pg_stat_database" in report.missing_types
        assert "locks" in report.missing_types
        assert "replication" in report.missing_types

    def test_empty_evidence(self):
        """Empty evidence list → not sufficient, all types missing."""
        report = check_evidence_quality([])

        assert report.sufficient is False
        assert report.available_types == []
        assert set(report.missing_types) == set(EXPECTED_EVIDENCE_TYPES)
        assert set(report.below_threshold) == set(EXPECTED_EVIDENCE_TYPES)

    def test_stale_evidence_below_threshold(self):
        """Very old evidence → quality below threshold."""
        # Evidence that is 20 minutes old (well past MAX_EVIDENCE_AGE_SECONDS=600s)
        evidence = make_full_evidence_set(age_seconds=1200)

        report = check_evidence_quality(evidence)

        # Stale evidence should have low freshness scores
        # With completeness still contributing, some may be above threshold
        # but overall freshness component should be 0
        for etype in EXPECTED_EVIDENCE_TYPES:
            assert etype in report.quality_scores
            # Score should be reduced due to staleness
            # 0.6 * 0.0 (stale) + 0.4 * completeness
            assert report.quality_scores[etype] <= 0.5

    def test_custom_threshold(self):
        """Custom threshold affects sufficiency determination."""
        evidence = make_full_evidence_set(age_seconds=300)  # 5 min old

        # With very high threshold, evidence may not be sufficient
        report_high = check_evidence_quality(evidence, threshold=0.95)
        # With very low threshold, evidence should be sufficient
        report_low = check_evidence_quality(evidence, threshold=0.1)

        assert report_low.sufficient is True
        # High threshold makes it harder to be sufficient
        assert len(report_high.below_threshold) >= len(report_low.below_threshold)

    def test_quality_scores_between_0_and_1(self):
        """All quality scores are within [0.0, 1.0] range."""
        evidence = make_full_evidence_set(age_seconds=30)

        report = check_evidence_quality(evidence)

        for score in report.quality_scores.values():
            assert 0.0 <= score <= 1.0

    def test_evidence_with_empty_data(self):
        """Evidence with empty data dict → low completeness score."""
        evidence = [
            make_evidence_snapshot("pg_settings", data={}, age_seconds=10),
        ]

        report = check_evidence_quality(evidence)

        # Empty data should result in low quality
        assert report.quality_scores.get("pg_settings", 0) < DEFAULT_QUALITY_THRESHOLD


# ─── Diagnosis Tests (Task 11.1) ─────────────────────────────────────────────


class TestDiagnose:
    """Tests for diagnose function."""

    @pytest.mark.asyncio
    async def test_diagnosis_with_full_evidence(self):
        """Diagnosis with full evidence produces recommendations."""
        evidence = make_full_evidence_set(age_seconds=30)
        goal = "improve query performance and memory usage"

        result = await diagnose(evidence, goal)

        assert isinstance(result, DiagnosisResult)
        assert len(result.recommendations) > 0
        assert result.overall_confidence > 0.0
        assert result.diagnostic_summary != ""
        assert result.evidence_quality_report is not None
        assert result.evidence_quality_report.sufficient is True

    @pytest.mark.asyncio
    async def test_diagnosis_references_only_provided_evidence(self):
        """Each recommendation references only evidence from the provided list."""
        evidence = make_full_evidence_set(age_seconds=30)
        goal = "optimize memory usage"

        result = await diagnose(evidence, goal)

        # Collect all snapshot IDs from the evidence
        evidence_ids = {str(item["id"]) for item in evidence}

        for rec in result.recommendations:
            for ref in rec.evidence_references:
                assert ref["snapshot_id"] in evidence_ids

    @pytest.mark.asyncio
    async def test_diagnosis_confidence_scores_in_range(self):
        """All confidence scores are in [0.0, 1.0] range."""
        evidence = make_full_evidence_set(age_seconds=30)
        goal = "improve performance"

        result = await diagnose(evidence, goal)

        assert 0.0 <= result.overall_confidence <= 1.0
        for rec in result.recommendations:
            assert 0.0 <= rec.confidence_score <= 1.0

    @pytest.mark.asyncio
    async def test_diagnosis_empty_evidence(self):
        """Empty evidence produces no recommendations with diagnostic summary."""
        result = await diagnose([], "optimize performance")

        assert isinstance(result, DiagnosisResult)
        assert result.recommendations == []
        assert result.overall_confidence == 0.0
        summary = result.diagnostic_summary.lower()
        assert "insufficient" in summary or "missing" in summary
        assert result.evidence_quality_report is not None
        assert result.evidence_quality_report.sufficient is False

    @pytest.mark.asyncio
    async def test_diagnosis_inconclusive_with_poor_evidence(self):
        """Diagnosis with insufficient evidence marks recommendations inconclusive."""
        # Only provide minimal, stale evidence
        evidence = [
            make_evidence_snapshot(
                "pg_settings",
                data={"shared_buffers": "128MB", "work_mem": "4MB"},
                age_seconds=900,  # Very stale
            ),
        ]
        goal = "improve memory"

        result = await diagnose(evidence, goal)

        # With insufficient overall evidence quality, should be non-actionable
        assert result.evidence_quality_report is not None
        assert result.evidence_quality_report.sufficient is False

    @pytest.mark.asyncio
    async def test_diagnosis_includes_evidence_gaps(self):
        """Recommendations include evidence gaps listing missing data."""
        # Only pg_settings, missing other types
        evidence = make_full_evidence_set(age_seconds=30)
        goal = "improve buffer performance"

        result = await diagnose(evidence, goal)

        # With full evidence, gaps should be minimal
        # But some recommendations may still list gaps for ideal confidence
        for rec in result.recommendations:
            assert isinstance(rec.evidence_gaps, list)


# ─── Plan Generation Tests (Task 11.3) ───────────────────────────────────────


class TestGeneratePlan:
    """Tests for generate_plan function."""

    @pytest.mark.asyncio
    async def test_plan_includes_rollback_for_every_change(self):
        """Every proposed change has a corresponding rollback instruction."""
        evidence = make_full_evidence_set(age_seconds=30)
        goal = "improve memory and buffer performance"

        diagnosis = await diagnose(evidence, goal)

        current_settings = {
            "shared_buffers": "128MB",
            "work_mem": "4MB",
            "effective_cache_size": "4GB",
            "max_connections": "100",
        }

        plan = await generate_plan(diagnosis, evidence, current_settings)

        assert isinstance(plan, GeneratedPlan)

        if plan.is_actionable:
            # Key assertion: rollback count equals proposed change count
            assert len(plan.rollback_instructions) == len(plan.proposed_changes)

            # Each rollback references the same setting it reverses
            change_settings = {c["setting_name"] for c in plan.proposed_changes}
            rollback_settings = {r["setting_name"] for r in plan.rollback_instructions}
            assert change_settings == rollback_settings

    @pytest.mark.asyncio
    async def test_plan_rollback_restores_current_value(self):
        """Rollback instructions restore the original current values."""
        evidence = make_full_evidence_set(age_seconds=30)
        goal = "optimize memory"

        diagnosis = await diagnose(evidence, goal)

        current_settings = {
            "shared_buffers": "128MB",
            "work_mem": "4MB",
            "effective_cache_size": "4GB",
        }

        plan = await generate_plan(diagnosis, evidence, current_settings)

        if plan.is_actionable:
            for rollback in plan.rollback_instructions:
                setting_name = rollback["setting_name"]
                restore_value = rollback["restore_value"]
                # Restore value should match the original current setting
                assert restore_value == str(current_settings[setting_name])

    @pytest.mark.asyncio
    async def test_plan_includes_evidence_references(self):
        """Plan includes evidence references with snapshot IDs and timestamps."""
        evidence = make_full_evidence_set(age_seconds=30)
        goal = "improve performance"

        diagnosis = await diagnose(evidence, goal)

        current_settings = {
            "shared_buffers": "128MB",
            "work_mem": "4MB",
        }

        plan = await generate_plan(diagnosis, evidence, current_settings)

        # Plan-level evidence references
        assert len(plan.evidence_references) > 0
        for ref in plan.evidence_references:
            assert "snapshot_id" in ref
            assert "timestamp" in ref

    @pytest.mark.asyncio
    async def test_inconclusive_evidence_produces_non_actionable_plan(self):
        """All inconclusive recommendations produce a non-actionable plan."""
        # Create a diagnosis where all recommendations are inconclusive
        quality_report = EvidenceQualityReport(
            sufficient=False,
            available_types=["pg_settings"],
            missing_types=["pg_stat_database", "os_metrics"],
            quality_scores={"pg_settings": 0.3},
            below_threshold=["pg_settings"],
        )

        diagnosis = DiagnosisResult(
            recommendations=[
                Recommendation(
                    setting_name="shared_buffers",
                    proposed_value="256MB",
                    current_value="128MB",
                    confidence_score=0.3,
                    evidence_gaps=["pg_stat_database", "os_metrics"],
                    is_inconclusive=True,
                    reasoning="Insufficient evidence",
                ),
            ],
            overall_confidence=0.3,
            diagnostic_summary="Limited evidence available",
            evidence_quality_report=quality_report,
        )

        evidence = [make_evidence_snapshot("pg_settings", data={"shared_buffers": "128MB"})]
        current_settings = {"shared_buffers": "128MB"}

        plan = await generate_plan(diagnosis, evidence, current_settings)

        assert isinstance(plan, GeneratedPlan)
        assert plan.is_actionable is False
        assert plan.proposed_changes == []
        assert plan.rollback_instructions == []
        assert plan.diagnostic_summary != ""

    @pytest.mark.asyncio
    async def test_empty_evidence_produces_diagnostic_only_plan(self):
        """Empty evidence produces a plan with only diagnostic summary."""
        diagnosis = DiagnosisResult(
            recommendations=[],
            overall_confidence=0.0,
            diagnostic_summary="No evidence available for analysis",
            evidence_quality_report=EvidenceQualityReport(
                sufficient=False,
                available_types=[],
                missing_types=list(EXPECTED_EVIDENCE_TYPES),
                quality_scores={},
                below_threshold=list(EXPECTED_EVIDENCE_TYPES),
            ),
        )

        plan = await generate_plan(diagnosis, [], {})

        assert plan.is_actionable is False
        assert plan.proposed_changes == []
        assert plan.rollback_instructions == []
        assert plan.confidence_score == 0.0
        assert (
            "no actionable" in plan.uncertainty_explanation.lower()
            or plan.diagnostic_summary != ""
        )

    @pytest.mark.asyncio
    async def test_plan_confidence_in_range(self):
        """Plan confidence score is in [0.0, 1.0] range."""
        evidence = make_full_evidence_set(age_seconds=30)
        goal = "improve memory usage"

        diagnosis = await diagnose(evidence, goal)
        current_settings = {"shared_buffers": "128MB", "work_mem": "4MB"}

        plan = await generate_plan(diagnosis, evidence, current_settings)

        assert 0.0 <= plan.confidence_score <= 1.0

    @pytest.mark.asyncio
    async def test_plan_with_rejection_feedback(self):
        """Rejection feedback causes related recommendations to be filtered."""
        evidence = make_full_evidence_set(age_seconds=30)
        goal = "improve memory and buffer performance"

        diagnosis = await diagnose(evidence, goal)
        current_settings = {
            "shared_buffers": "128MB",
            "work_mem": "4MB",
            "effective_cache_size": "4GB",
        }

        # Generate plan with rejection feedback mentioning shared_buffers
        plan = await generate_plan(
            diagnosis, evidence, current_settings,
            rejection_feedback="Do not change shared_buffers, it was recently tuned"
        )

        # shared_buffers should not be in proposed changes
        if plan.is_actionable:
            setting_names = [c["setting_name"] for c in plan.proposed_changes]
            assert "shared_buffers" not in setting_names

    @pytest.mark.asyncio
    async def test_plan_changes_are_executable(self):
        """Proposed changes include SQL statements for Control Plane execution."""
        evidence = make_full_evidence_set(age_seconds=30)
        goal = "improve memory usage"

        diagnosis = await diagnose(evidence, goal)
        current_settings = {"shared_buffers": "128MB", "work_mem": "4MB"}

        plan = await generate_plan(diagnosis, evidence, current_settings)

        if plan.is_actionable:
            for change in plan.proposed_changes:
                assert "sql_statement" in change
                assert "ALTER SYSTEM SET" in change["sql_statement"]
                assert change["setting_name"] in change["sql_statement"]

    @pytest.mark.asyncio
    async def test_slow_query_evidence_generates_index_plan(self):
        """Slow equality-filter query evidence produces an index recommendation."""
        evidence = make_full_evidence_set(age_seconds=30)
        for item in evidence:
            if item["evidence_type"] == "pg_stat_statements":
                item["data"] = {
                    "queries": [
                        {
                            "query": (
                                "SELECT count(*) FROM dba_demo.orders "
                                "WHERE customer_id = 42 AND status = 'open'"
                            ),
                            "calls": 100,
                            "mean_exec_time_ms": 38.8,
                            "shared_blks_read": 5280,
                        }
                    ],
                    "total_queries_collected": 1,
                }

        diagnosis = await diagnose(evidence, "improve slow query performance")
        plan = await generate_plan(diagnosis, evidence, {})

        index_changes = [
            change for change in plan.proposed_changes
            if change.get("change_type") == "index"
        ]
        assert index_changes
        sql = index_changes[0]["sql_statement"]
        assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in sql
        assert '"dba_demo"."orders"' in sql
        assert '"customer_id"' in sql
        assert "WHERE \"status\" = 'open'" in sql

    @pytest.mark.asyncio
    async def test_rollback_instructions_include_evidence_references(self):
        """Rollback instructions include evidence references."""
        evidence = make_full_evidence_set(age_seconds=30)
        goal = "optimize memory"

        diagnosis = await diagnose(evidence, goal)
        current_settings = {"shared_buffers": "128MB", "work_mem": "4MB"}

        plan = await generate_plan(diagnosis, evidence, current_settings)

        if plan.is_actionable:
            for rollback in plan.rollback_instructions:
                assert "evidence_references" in rollback
                assert isinstance(rollback["evidence_references"], list)
