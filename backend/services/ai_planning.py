"""
AI Planning Module for evidence-based PostgreSQL diagnostic recommendations.

Provides:
- check_evidence_quality() to assess evidence sufficiency
- diagnose() to analyze evidence and produce recommendations
- generate_plan() to convert recommendations into executable plans with rollback

Key constraints:
- References only evidence from the current loop run (never fabricates metrics)
- Includes confidence scores [0.0, 1.0] and evidence gaps for each recommendation
- Generates rollback instructions for every proposed change
- Marks recommendations as inconclusive when evidence quality is insufficient
- Returns diagnostic-only plans when all evidence is below threshold or empty

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Expected evidence types for a comprehensive diagnosis
EXPECTED_EVIDENCE_TYPES = [
    "pg_settings",
    "pg_stat_database",
    "pg_stat_statements",
    "locks",
    "replication",
    "wal_checkpoint",
    "os_metrics",
]

# Default evidence quality threshold
DEFAULT_QUALITY_THRESHOLD = 0.6

# Maximum age in seconds before evidence is considered stale
MAX_EVIDENCE_AGE_SECONDS = 600  # 10 minutes


@dataclass
class EvidenceQualityReport:
    """Report on the quality and sufficiency of collected evidence.

    Attributes:
        sufficient: Whether the evidence meets the overall quality threshold.
        available_types: List of evidence types that are present.
        missing_types: List of expected evidence types that are absent.
        quality_scores: Dict mapping evidence type to its quality score [0.0, 1.0].
        below_threshold: List of evidence types with quality below the threshold.
    """

    sufficient: bool
    available_types: List[str] = field(default_factory=list)
    missing_types: List[str] = field(default_factory=list)
    quality_scores: Dict[str, float] = field(default_factory=dict)
    below_threshold: List[str] = field(default_factory=list)


@dataclass
class Recommendation:
    """A single diagnostic recommendation.

    Attributes:
        setting_name: The PostgreSQL setting to modify.
        proposed_value: The recommended new value.
        current_value: The current value of the setting.
        confidence_score: Confidence in the recommendation [0.0, 1.0].
        evidence_gaps: List of specific evidence types reducing confidence.
        evidence_references: List of dicts with snapshot_id and timestamp.
        is_inconclusive: Whether the recommendation lacks sufficient evidence.
        reasoning: Human-readable explanation of the recommendation.
    """

    setting_name: str
    proposed_value: str
    current_value: str
    confidence_score: float
    evidence_gaps: List[str] = field(default_factory=list)
    evidence_references: List[dict] = field(default_factory=list)
    is_inconclusive: bool = False
    reasoning: str = ""
    change_type: str = "setting"
    sql_statement: str = ""
    rollback_sql_statement: str = ""


@dataclass
class DiagnosisResult:
    """Result of evidence diagnosis.

    Attributes:
        recommendations: List of recommendations produced from the evidence.
        overall_confidence: Average confidence across all recommendations.
        diagnostic_summary: Human-readable summary of the diagnosis.
        evidence_quality_report: Report on the quality of input evidence.
    """

    recommendations: List[Recommendation] = field(default_factory=list)
    overall_confidence: float = 0.0
    diagnostic_summary: str = ""
    evidence_quality_report: Optional[EvidenceQualityReport] = None


@dataclass
class GeneratedPlan:
    """A generated plan with proposed changes and rollback instructions.

    Attributes:
        proposed_changes: List of dicts describing setting changes.
        rollback_instructions: List of dicts with reversal actions.
        evidence_references: List of dicts with snapshot_id and timestamp.
        confidence_score: Overall confidence in the plan [0.0, 1.0].
        uncertainty_explanation: Explanation of why confidence is below 1.0.
        is_actionable: Whether the plan contains actionable changes.
        diagnostic_summary: Summary of the diagnosis that produced this plan.
    """

    proposed_changes: List[dict] = field(default_factory=list)
    rollback_instructions: List[dict] = field(default_factory=list)
    evidence_references: List[dict] = field(default_factory=list)
    confidence_score: float = 0.0
    uncertainty_explanation: str = ""
    is_actionable: bool = False
    diagnostic_summary: str = ""


def _calculate_freshness_score(collected_at: datetime, now: Optional[datetime] = None) -> float:
    """Calculate a freshness score for an evidence snapshot.

    Returns 1.0 for very fresh evidence, decaying to 0.0 for stale evidence.

    Args:
        collected_at: When the evidence was collected.
        now: Current time (defaults to UTC now).

    Returns:
        Freshness score between 0.0 and 1.0.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Ensure both timestamps are offset-aware for comparison
    if collected_at.tzinfo is None:
        collected_at = collected_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    age_seconds = (now - collected_at).total_seconds()

    if age_seconds <= 0:
        return 1.0
    if age_seconds >= MAX_EVIDENCE_AGE_SECONDS:
        return 0.0

    # Linear decay from 1.0 to 0.0 over MAX_EVIDENCE_AGE_SECONDS
    return max(0.0, min(1.0, 1.0 - (age_seconds / MAX_EVIDENCE_AGE_SECONDS)))


def _calculate_completeness_score(data: dict) -> float:
    """Calculate a completeness score for evidence data.

    Returns 1.0 for data with many fields, lower for sparse data.

    Args:
        data: The evidence data dictionary.

    Returns:
        Completeness score between 0.0 and 1.0.
    """
    if not data:
        return 0.0

    if isinstance(data, dict):
        # Score based on how many non-null fields are present
        total_fields = len(data)
        if total_fields == 0:
            return 0.0
        non_null_fields = sum(1 for v in data.values() if v is not None and v != "")
        return min(1.0, non_null_fields / max(total_fields, 1))
    elif isinstance(data, list):
        # For list data, score based on having entries
        return min(1.0, len(data) / 5.0)  # Normalize: 5+ entries = 1.0

    return 0.5  # Default for unknown formats


def check_evidence_quality(
    evidence: List[dict],
    threshold: float = DEFAULT_QUALITY_THRESHOLD,
) -> EvidenceQualityReport:
    """Check the quality and sufficiency of collected evidence.

    Evaluates which evidence types are present, calculates quality scores
    based on freshness and completeness, and identifies types below threshold.

    Args:
        evidence: List of evidence snapshot dicts. Each should have at minimum:
                 - "evidence_type": str identifying the type
                 - "collected_at": datetime or ISO string of collection time
                 - "data": dict with the evidence payload
                 Optional:
                 - "id" or "snapshot_id": UUID of the snapshot
        threshold: Minimum quality score to consider evidence sufficient (default: 0.6).

    Returns:
        EvidenceQualityReport indicating overall sufficiency and per-type scores.
    """
    if not evidence:
        return EvidenceQualityReport(
            sufficient=False,
            available_types=[],
            missing_types=list(EXPECTED_EVIDENCE_TYPES),
            quality_scores={},
            below_threshold=list(EXPECTED_EVIDENCE_TYPES),
        )

    now = datetime.now(timezone.utc)

    # Group evidence by type
    evidence_by_type: Dict[str, List[dict]] = {}
    for item in evidence:
        etype = item.get("evidence_type", "")
        if etype:
            if etype not in evidence_by_type:
                evidence_by_type[etype] = []
            evidence_by_type[etype].append(item)

    available_types = list(evidence_by_type.keys())
    missing_types = [t for t in EXPECTED_EVIDENCE_TYPES if t not in evidence_by_type]

    # Calculate quality score per type
    quality_scores: Dict[str, float] = {}
    below_threshold: List[str] = []

    for etype, snapshots in evidence_by_type.items():
        # Use the most recent snapshot for quality assessment
        best_score = 0.0
        for snapshot in snapshots:
            # Calculate freshness
            collected_at = snapshot.get("collected_at")
            if collected_at is None:
                freshness = 0.0
            elif isinstance(collected_at, str):
                try:
                    dt = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
                    freshness = _calculate_freshness_score(dt, now)
                except (ValueError, TypeError):
                    freshness = 0.0
            elif isinstance(collected_at, datetime):
                freshness = _calculate_freshness_score(collected_at, now)
            else:
                freshness = 0.0

            # Calculate completeness
            data = snapshot.get("data", {})
            completeness = _calculate_completeness_score(data)

            # Combined score (weighted average: 60% freshness, 40% completeness)
            score = 0.6 * freshness + 0.4 * completeness
            best_score = max(best_score, score)

        quality_scores[etype] = round(min(1.0, max(0.0, best_score)), 2)

        if quality_scores[etype] < threshold:
            below_threshold.append(etype)

    # Also mark missing types as below threshold
    for mtype in missing_types:
        quality_scores[mtype] = 0.0
        below_threshold.append(mtype)

    # Evidence is sufficient if at least half of expected types are present
    # and at least half of those have quality above threshold
    types_above_threshold = [t for t in available_types if quality_scores.get(t, 0) >= threshold]
    sufficient = (
        len(available_types) >= len(EXPECTED_EVIDENCE_TYPES) / 2
        and len(types_above_threshold) >= len(available_types) / 2
    )

    return EvidenceQualityReport(
        sufficient=sufficient,
        available_types=available_types,
        missing_types=missing_types,
        quality_scores=quality_scores,
        below_threshold=below_threshold,
    )


def _extract_evidence_references(evidence: List[dict]) -> List[dict]:
    """Extract snapshot IDs and timestamps from evidence for referencing.

    Args:
        evidence: List of evidence snapshot dicts.

    Returns:
        List of dicts with "snapshot_id" and "timestamp" keys.
    """
    refs = []
    for item in evidence:
        snapshot_id = item.get("id") or item.get("snapshot_id")
        collected_at = item.get("collected_at")

        if snapshot_id is not None:
            ref = {
                "snapshot_id": str(snapshot_id),
                "timestamp": (
                    collected_at.isoformat()
                    if isinstance(collected_at, datetime)
                    else str(collected_at)
                    if collected_at
                    else None
                ),
            }
            refs.append(ref)

    return refs


def _analyze_pg_settings(evidence: List[dict], goal: str) -> List[dict]:
    """Analyze pg_settings evidence for potential tuning recommendations.

    Uses rule-based analysis to identify settings that may benefit from tuning.

    Args:
        evidence: List of evidence snapshots.
        goal: The DBA's stated goal.

    Returns:
        List of dicts with analysis results per setting.
    """
    results = []
    goal_lower = goal.lower()

    for item in evidence:
        if item.get("evidence_type") != "pg_settings":
            continue

        data = item.get("data", {})
        if not isinstance(data, dict):
            continue

        # Rule-based tuning suggestions
        # Check shared_buffers
        if "shared_buffers" in data and (
            "memory" in goal_lower or "performance" in goal_lower or "buffer" in goal_lower
        ):
            current = data["shared_buffers"]
            results.append(
                {
                    "setting_name": "shared_buffers",
                    "current_value": str(current),
                    "analysis": "shared_buffers may benefit from tuning based on goal",
                    "evidence_type": "pg_settings",
                }
            )

        # Check work_mem
        if "work_mem" in data and (
            "memory" in goal_lower or "query" in goal_lower or "sort" in goal_lower
        ):
            current = data["work_mem"]
            results.append(
                {
                    "setting_name": "work_mem",
                    "current_value": str(current),
                    "analysis": "work_mem may benefit from tuning for query performance",
                    "evidence_type": "pg_settings",
                }
            )

        # Check effective_cache_size
        if "effective_cache_size" in data and (
            "cache" in goal_lower or "performance" in goal_lower or "memory" in goal_lower
        ):
            current = data["effective_cache_size"]
            results.append(
                {
                    "setting_name": "effective_cache_size",
                    "current_value": str(current),
                    "analysis": "effective_cache_size impacts query planner cost estimates",
                    "evidence_type": "pg_settings",
                }
            )

        # Check max_connections
        if "max_connections" in data and (
            "connection" in goal_lower or "scale" in goal_lower or "concurrent" in goal_lower
        ):
            current = data["max_connections"]
            results.append(
                {
                    "setting_name": "max_connections",
                    "current_value": str(current),
                    "analysis": "max_connections may need adjustment for scaling",
                    "evidence_type": "pg_settings",
                }
            )

        # Check maintenance_work_mem
        if "maintenance_work_mem" in data and (
            "vacuum" in goal_lower or "maintenance" in goal_lower or "index" in goal_lower
        ):
            current = data["maintenance_work_mem"]
            results.append(
                {
                    "setting_name": "maintenance_work_mem",
                    "current_value": str(current),
                    "analysis": "maintenance_work_mem affects VACUUM and index operations",
                    "evidence_type": "pg_settings",
                }
            )

    return results


def _iter_statement_entries(data) -> List[dict]:
    """Return pg_stat_statements entries from supported evidence payload shapes."""
    if isinstance(data, list):
        return [entry for entry in data if isinstance(entry, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("queries", "statement_stats", "statements"):
        value = data.get(key)
        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, dict)]
    return []


def _safe_identifier_part(value: str) -> str:
    """Convert a SQL identifier fragment to a safe generated name part."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower()
    return cleaned or "expr"


def _quote_identifier_path(identifier: str) -> str:
    """Quote a possibly schema-qualified SQL identifier."""
    parts = [p for p in identifier.split(".") if p]
    return ".".join(f'"{part}"' for part in parts)


def _extract_index_candidate(query: str) -> Optional[dict]:
    """
    Extract a conservative CREATE INDEX candidate from a simple SELECT query.

    Handles demo-friendly equality predicates such as:
    SELECT ... FROM dba_demo.orders WHERE customer_id = 42 AND status = 'open'
    """
    if not query:
        return None

    table_match = re.search(
        r"\bfrom\s+([a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)?)\b",
        query,
        flags=re.IGNORECASE,
    )
    where_match = re.search(
        r"\bwhere\b\s+(.+?)(?:\bgroup\b|\border\b|\blimit\b|$)",
        query,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not table_match or not where_match:
        return None

    predicates = []
    for part in re.split(r"\band\b", where_match.group(1), flags=re.IGNORECASE):
        pred_match = re.search(
            r"\b([a-zA-Z_][\w]*)\s*=\s*('(?:''|[^'])*'|\$\d+|[-]?\d+(?:\.\d+)?)",
            part.strip(),
            flags=re.IGNORECASE,
        )
        if not pred_match:
            continue
        predicates.append(
            {
                "column": pred_match.group(1),
                "value": pred_match.group(2),
                "is_literal": not pred_match.group(2).startswith("$"),
            }
        )

    if not predicates:
        return None

    table_name = table_match.group(1)
    table_part = _safe_identifier_part(table_name.split(".")[-1])

    literal_predicates = [p for p in predicates if p["is_literal"]]
    if literal_predicates and len(predicates) > 1:
        partial = literal_predicates[-1]
        index_columns = [p["column"] for p in predicates if p is not partial]
        if not index_columns:
            index_columns = [predicates[0]["column"]]
            partial = None
    else:
        partial = None
        index_columns = [p["column"] for p in predicates]

    if not index_columns:
        return None

    index_name_parts = [table_part] + [_safe_identifier_part(c) for c in index_columns]
    if partial:
        index_name_parts.append(_safe_identifier_part(partial["column"]))
    index_name = "idx_" + "_".join(index_name_parts[:5])

    quoted_table = _quote_identifier_path(table_name)
    quoted_columns = ", ".join(_quote_identifier_path(c) for c in index_columns)
    sql = (
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} ON {quoted_table} ({quoted_columns})"
    )
    if partial:
        sql += f" WHERE {_quote_identifier_path(partial['column'])} = {partial['value']}"
    sql += ";"

    return {
        "index_name": index_name,
        "table_name": table_name,
        "index_columns": index_columns,
        "partial_predicate": (f"{partial['column']} = {partial['value']}" if partial else None),
        "sql_statement": sql,
        "rollback_sql_statement": f"DROP INDEX CONCURRENTLY IF EXISTS {index_name};",
        "uses_partial_index": partial is not None,
    }


def _analyze_query_indexes(evidence: List[dict], goal: str) -> List[dict]:
    """Analyze pg_stat_statements evidence for simple missing-index candidates."""
    if not any(word in goal.lower() for word in ("query", "performance", "index", "slow")):
        return []

    results = []
    seen_sql = set()
    for item in evidence:
        if item.get("evidence_type") != "pg_stat_statements":
            continue
        for statement in _iter_statement_entries(item.get("data", {})):
            query = statement.get("query", "")
            candidate = _extract_index_candidate(query)
            if not candidate or candidate["sql_statement"] in seen_sql:
                continue
            seen_sql.add(candidate["sql_statement"])
            mean_time = (
                statement.get("mean_exec_time")
                or statement.get("mean_exec_time_ms")
                or statement.get("total_exec_time")
                or statement.get("total_time")
            )
            results.append(
                {
                    "setting_name": candidate["index_name"],
                    "current_value": "missing",
                    "analysis": (
                        "Query statistics show a repeated equality-filter query; "
                        "a concurrent index can reduce scan work before approval."
                    ),
                    "evidence_type": "pg_stat_statements",
                    "change_type": "index",
                    "proposed_value": candidate["sql_statement"],
                    "sql_statement": candidate["sql_statement"],
                    "rollback_sql_statement": candidate["rollback_sql_statement"],
                    "table_name": candidate["table_name"],
                    "index_columns": candidate["index_columns"],
                    "partial_predicate": candidate["partial_predicate"],
                    "mean_exec_time_ms": mean_time,
                }
            )

    return results


def _calculate_recommendation_confidence(
    recommendation_analysis: dict,
    quality_report: EvidenceQualityReport,
    available_evidence_types: List[str],
) -> tuple:
    """Calculate confidence score and evidence gaps for a recommendation.

    Args:
        recommendation_analysis: The analysis result for a setting.
        quality_report: The evidence quality report.
        available_evidence_types: Types of evidence available.

    Returns:
        Tuple of (confidence_score, evidence_gaps).
    """
    # Start with base confidence from evidence quality
    evidence_type = recommendation_analysis.get("evidence_type", "")
    base_quality = quality_report.quality_scores.get(evidence_type, 0.0)

    # Confidence is higher when supporting evidence types are available
    if recommendation_analysis.get("change_type") == "index":
        supporting_types = ["pg_stat_statements", "pg_stat_database"]
    else:
        supporting_types = ["pg_settings", "pg_stat_database", "os_metrics"]
    available_supporting = [t for t in supporting_types if t in available_evidence_types]
    support_ratio = len(available_supporting) / max(len(supporting_types), 1)

    confidence = min(1.0, max(0.0, (base_quality * 0.6 + support_ratio * 0.4)))

    # Determine evidence gaps
    evidence_gaps = []
    if "pg_stat_database" not in available_evidence_types:
        evidence_gaps.append("pg_stat_database (query performance metrics)")
    if "os_metrics" not in available_evidence_types:
        evidence_gaps.append("os_metrics (system resource utilization)")
    if "pg_stat_statements" not in available_evidence_types:
        evidence_gaps.append("pg_stat_statements (query-level statistics)")
    if base_quality < DEFAULT_QUALITY_THRESHOLD:
        evidence_gaps.append(f"{evidence_type} (quality below threshold: {base_quality:.2f})")

    return round(confidence, 2), evidence_gaps


async def diagnose(
    evidence: List[dict],
    goal: str,
) -> DiagnosisResult:
    """Analyze evidence to produce diagnostic recommendations.

    Performs rule-based analysis of collected evidence to identify
    PostgreSQL settings that may benefit from tuning, given the stated goal.

    Each recommendation references only evidence from the provided list.
    Recommendations are marked as inconclusive if evidence quality is insufficient.

    Args:
        evidence: List of evidence snapshot dicts from the current loop run.
                 Each should have: evidence_type, collected_at, data, and optionally id.
        goal: The DBA's high-level goal (e.g., "improve query performance").

    Returns:
        DiagnosisResult with recommendations, confidence, and quality report.

    Requirements: 7.1, 7.2, 7.4, 7.6
    """
    # Check evidence quality first
    quality_report = check_evidence_quality(evidence)

    # If evidence is empty or all below threshold, return diagnostic-only result
    if not evidence or not quality_report.sufficient:
        insufficient_msg = (
            "Insufficient evidence for actionable recommendations. "
            f"Missing evidence types: {', '.join(quality_report.missing_types)}. "
            f"Types below quality threshold: {', '.join(quality_report.below_threshold)}."
        )

        return DiagnosisResult(
            recommendations=[],
            overall_confidence=0.0,
            diagnostic_summary=insufficient_msg,
            evidence_quality_report=quality_report,
        )

    # Analyze available evidence using rule-based approach
    available_types = [item.get("evidence_type", "") for item in evidence]
    analyses = _analyze_pg_settings(evidence, goal)
    analyses.extend(_analyze_query_indexes(evidence, goal))

    # Extract evidence references
    evidence_refs = _extract_evidence_references(evidence)

    # Build recommendations from analyses
    recommendations: List[Recommendation] = []

    for analysis in analyses:
        confidence, gaps = _calculate_recommendation_confidence(
            analysis, quality_report, available_types
        )

        # Determine if inconclusive
        setting_type = analysis.get("evidence_type", "")
        is_inconclusive = (
            quality_report.quality_scores.get(setting_type, 0.0) < DEFAULT_QUALITY_THRESHOLD
        )

        recommendation = Recommendation(
            setting_name=analysis["setting_name"],
            proposed_value="",  # Will be filled during plan generation
            current_value=analysis.get("current_value", ""),
            confidence_score=confidence,
            evidence_gaps=gaps,
            evidence_references=evidence_refs,
            is_inconclusive=is_inconclusive,
            reasoning=analysis.get("analysis", ""),
            change_type=analysis.get("change_type", "setting"),
            sql_statement=analysis.get("sql_statement", ""),
            rollback_sql_statement=analysis.get("rollback_sql_statement", ""),
        )
        recommendations.append(recommendation)

    # Calculate overall confidence
    if recommendations:
        overall_confidence = round(
            sum(r.confidence_score for r in recommendations) / len(recommendations), 2
        )
    else:
        overall_confidence = 0.0

    # Build diagnostic summary
    actionable_count = sum(1 for r in recommendations if not r.is_inconclusive)
    inconclusive_count = sum(1 for r in recommendations if r.is_inconclusive)

    diagnostic_summary = (
        f"Diagnosis for goal '{goal}': "
        f"{len(recommendations)} recommendation(s) identified. "
        f"{actionable_count} actionable, {inconclusive_count} inconclusive. "
        f"Evidence types available: {', '.join(quality_report.available_types)}."
    )

    return DiagnosisResult(
        recommendations=recommendations,
        overall_confidence=overall_confidence,
        diagnostic_summary=diagnostic_summary,
        evidence_quality_report=quality_report,
    )


def _generate_proposed_value(setting_name: str, current_value: str, goal: str) -> str:
    """Generate a proposed value for a setting based on rule-based heuristics.

    This is a simplified rule-based approach. In production, this would
    integrate with an LLM for more sophisticated value generation.

    Args:
        setting_name: Name of the PostgreSQL setting.
        current_value: Current value of the setting.
        goal: The DBA's goal.

    Returns:
        A proposed new value as a string.
    """
    # Simple heuristic: if goal mentions performance/memory, suggest increases
    if setting_name == "shared_buffers":
        # Try to parse and increase
        try:
            if current_value.endswith("MB"):
                val = int(current_value.replace("MB", ""))
                return f"{min(val * 2, 8192)}MB"
            elif current_value.endswith("GB"):
                val = int(current_value.replace("GB", ""))
                return f"{min(val * 2, 32)}GB"
        except (ValueError, TypeError):
            pass
        return "256MB"

    if setting_name == "work_mem":
        try:
            if current_value.endswith("MB"):
                val = int(current_value.replace("MB", ""))
                return f"{min(val * 2, 256)}MB"
        except (ValueError, TypeError):
            pass
        return "8MB"

    if setting_name == "effective_cache_size":
        try:
            if current_value.endswith("GB"):
                val = int(current_value.replace("GB", ""))
                return f"{min(val * 2, 64)}GB"
        except (ValueError, TypeError):
            pass
        return "8GB"

    if setting_name == "max_connections":
        try:
            val = int(current_value)
            return str(min(val + 50, 500))
        except (ValueError, TypeError):
            pass
        return "150"

    if setting_name == "maintenance_work_mem":
        try:
            if current_value.endswith("MB"):
                val = int(current_value.replace("MB", ""))
                return f"{min(val * 2, 2048)}MB"
        except (ValueError, TypeError):
            pass
        return "512MB"

    # Default: return same value (no change suggested)
    return current_value


async def generate_plan(
    diagnosis: DiagnosisResult,
    evidence: List[dict],
    current_settings: dict,
    rejection_feedback: Optional[str] = None,
) -> GeneratedPlan:
    """Generate an executable plan from a diagnosis result.

    Converts actionable recommendations into proposed changes with rollback
    instructions. Every proposed change has a corresponding reversal action.

    If all recommendations are inconclusive or evidence is insufficient,
    returns a non-actionable plan with only a diagnostic summary.

    Args:
        diagnosis: The DiagnosisResult from the diagnose() function.
        evidence: List of evidence snapshot dicts for reference.
        current_settings: Dict mapping setting names to their current values.
        rejection_feedback: Optional feedback from a previous plan rejection,
                          used to adjust recommendations.

    Returns:
        GeneratedPlan with changes, rollback instructions, and metadata.

    Requirements: 7.5, 7.6, 7.7
    """
    # Extract all evidence references
    all_evidence_refs = _extract_evidence_references(evidence)

    # If no recommendations or all inconclusive, return non-actionable plan
    if not diagnosis.recommendations:
        return GeneratedPlan(
            proposed_changes=[],
            rollback_instructions=[],
            evidence_references=all_evidence_refs,
            confidence_score=0.0,
            uncertainty_explanation=(
                f"No actionable recommendations could be generated. {diagnosis.diagnostic_summary}"
            ),
            is_actionable=False,
            diagnostic_summary=diagnosis.diagnostic_summary,
        )

    # Filter to actionable recommendations (non-inconclusive)
    actionable_recs = [r for r in diagnosis.recommendations if not r.is_inconclusive]

    # If all are inconclusive, return non-actionable plan
    if not actionable_recs:
        missing_evidence = set()
        for rec in diagnosis.recommendations:
            missing_evidence.update(rec.evidence_gaps)

        return GeneratedPlan(
            proposed_changes=[],
            rollback_instructions=[],
            evidence_references=all_evidence_refs,
            confidence_score=diagnosis.overall_confidence,
            uncertainty_explanation=(
                "All recommendations are inconclusive due to insufficient evidence. "
                f"Missing/insufficient evidence: {', '.join(missing_evidence)}."
            ),
            is_actionable=False,
            diagnostic_summary=diagnosis.diagnostic_summary,
        )

    # Handle rejection feedback: filter out settings mentioned in feedback
    if rejection_feedback:
        feedback_lower = rejection_feedback.lower()
        # Simple heuristic: if feedback mentions a setting, skip it
        actionable_recs = [
            r for r in actionable_recs if r.setting_name.lower() not in feedback_lower
        ]

        if not actionable_recs:
            return GeneratedPlan(
                proposed_changes=[],
                rollback_instructions=[],
                evidence_references=all_evidence_refs,
                confidence_score=0.0,
                uncertainty_explanation=(
                    "All recommendations were filtered based on rejection feedback. "
                    f"Feedback: {rejection_feedback}"
                ),
                is_actionable=False,
                diagnostic_summary=diagnosis.diagnostic_summary,
            )

    # Generate proposed changes and rollback instructions
    proposed_changes: List[dict] = []
    rollback_instructions: List[dict] = []

    goal = diagnosis.diagnostic_summary  # Use for value generation context

    for rec in actionable_recs:
        # Determine current value from current_settings or recommendation
        current_value = current_settings.get(rec.setting_name, rec.current_value)
        current_value_str = str(current_value)

        if rec.change_type == "index":
            change = {
                "change_type": "index",
                "setting_name": rec.setting_name,
                "proposed_value": rec.sql_statement,
                "current_value": current_value_str,
                "confidence_score": rec.confidence_score,
                "reasoning": rec.reasoning,
                "evidence_gaps": rec.evidence_gaps,
                "evidence_references": rec.evidence_references,
                "sql_statement": rec.sql_statement,
            }
            proposed_changes.append(change)
            rollback_instructions.append(
                {
                    "setting_name": rec.setting_name,
                    "restore_value": "drop_index",
                    "sql_statement": rec.rollback_sql_statement,
                    "evidence_references": rec.evidence_references,
                }
            )
            continue

        # Generate proposed value
        proposed_value = _generate_proposed_value(rec.setting_name, current_value_str, goal)

        # Skip if proposed value equals current value
        if proposed_value == current_value_str:
            continue

        # Build proposed change
        change = {
            "setting_name": rec.setting_name,
            "proposed_value": proposed_value,
            "current_value": current_value_str,
            "confidence_score": rec.confidence_score,
            "reasoning": rec.reasoning,
            "evidence_gaps": rec.evidence_gaps,
            "evidence_references": rec.evidence_references,
            "sql_statement": f"ALTER SYSTEM SET {rec.setting_name} = '{proposed_value}'",
        }
        proposed_changes.append(change)

        # Build corresponding rollback instruction
        rollback = {
            "setting_name": rec.setting_name,
            "restore_value": current_value_str,
            "sql_statement": f"ALTER SYSTEM SET {rec.setting_name} = '{current_value_str}'",
            "evidence_references": rec.evidence_references,
        }
        rollback_instructions.append(rollback)

    # If no actual changes after filtering, return non-actionable
    if not proposed_changes:
        return GeneratedPlan(
            proposed_changes=[],
            rollback_instructions=[],
            evidence_references=all_evidence_refs,
            confidence_score=diagnosis.overall_confidence,
            uncertainty_explanation=("No changes needed — proposed values match current settings."),
            is_actionable=False,
            diagnostic_summary=diagnosis.diagnostic_summary,
        )

    # Calculate plan confidence
    plan_confidence = round(
        sum(c.get("confidence_score", 0.0) for c in proposed_changes) / len(proposed_changes),
        2,
    )

    # Build uncertainty explanation
    all_gaps = set()
    for change in proposed_changes:
        all_gaps.update(change.get("evidence_gaps", []))

    uncertainty_explanation = ""
    if all_gaps:
        uncertainty_explanation = f"Confidence reduced by missing evidence: {', '.join(all_gaps)}."
    elif plan_confidence < 1.0:
        uncertainty_explanation = "Confidence below 1.0 due to evidence quality limitations."

    return GeneratedPlan(
        proposed_changes=proposed_changes,
        rollback_instructions=rollback_instructions,
        evidence_references=all_evidence_refs,
        confidence_score=plan_confidence,
        uncertainty_explanation=uncertainty_explanation,
        is_actionable=True,
        diagnostic_summary=diagnosis.diagnostic_summary,
    )
