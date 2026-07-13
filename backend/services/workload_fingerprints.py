"""Evidence-backed workload fingerprint analysis.

The service deliberately works from persisted pg_stat_statements snapshots.  It
does not claim that a visible sample represents the workload unless coverage,
membership stability, and runtime variance pass explicit gates.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

MIN_COVERAGE_PCT = 70.0
RECOMMENDED_COVERAGE_TARGET_PCT = 80.0
MIN_STABILITY_PCT = 60.0
MAX_RUNTIME_VARIANCE_PCT = 50.0
MAX_RECOMMENDED_MEMBERS = 12


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _as_float(value: Any) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _statement_entries(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("queries", "statement_stats", "statements"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _normalize_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    data = _as_dict(row.get("data"))
    members: dict[str, dict[str, Any]] = {}
    for statement in _statement_entries(data):
        query_id = statement.get("queryid", statement.get("query_id"))
        if query_id is None or str(query_id).strip() == "":
            continue
        calls = _as_int(statement.get("calls"))
        total = _as_float(statement.get("total_exec_time", statement.get("total_exec_time_ms")))
        mean = _as_float(statement.get("mean_exec_time", statement.get("mean_exec_time_ms")))
        if not mean and total and calls:
            mean = total / calls
        if not total and mean and calls:
            total = mean * calls
        key = str(query_id)
        members[key] = {
            "query_id": key,
            "query_text": statement.get("query"),
            "calls": calls,
            "average_query_runtime_ms": mean,
            "total_runtime_ms": total,
        }
    limit = _as_int(data.get("max_query_entries"))
    count = _as_int(data.get("total_queries_collected", data.get("statement_count", len(members))))
    return {
        "id": row.get("id"),
        "collected_at": row.get("collected_at"),
        "members": members,
        "collector_limit": limit or None,
        "collector_count": count or len(members),
    }


def _sort_snapshots(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = [_normalize_snapshot(row) for row in rows]

    def key(snapshot: Mapping[str, Any]):
        value = snapshot.get("collected_at")
        if isinstance(value, datetime):
            return value.timestamp()
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return 0.0

    return sorted(normalized, key=key, reverse=True)


def _recommended_ids(latest: Mapping[str, Any]) -> list[str]:
    members = list(latest["members"].values())
    for member in members:
        # Calls and average runtime are both explicit inputs.  log1p prevents a
        # very high call count from completely hiding a slower query family.
        member["impact_score"] = member["average_query_runtime_ms"] * math.log1p(member["calls"])
    members.sort(key=lambda item: (item["impact_score"], item["total_runtime_ms"]), reverse=True)
    total_runtime = sum(item["total_runtime_ms"] for item in members)
    selected: list[str] = []
    selected_runtime = 0.0
    for member in members[:MAX_RECOMMENDED_MEMBERS]:
        if member["total_runtime_ms"] <= 0:
            continue
        selected.append(member["query_id"])
        selected_runtime += member["total_runtime_ms"]
        if (
            total_runtime
            and selected_runtime / total_runtime * 100 >= RECOMMENDED_COVERAGE_TARGET_PCT
        ):
            break
    return selected


def analyze_workload_snapshots(
    rows: Sequence[Mapping[str, Any]],
    selected_query_ids: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Build candidates and fail-closed diagnostics from recent observations."""
    snapshots = _sort_snapshots(rows)
    if not snapshots or not snapshots[0]["members"]:
        return {
            "status": "no_workload",
            "ready": False,
            "candidates": [],
            "selected_query_ids": [],
            "coverage_pct": 0.0,
            "membership_stability_pct": None,
            "runtime_variance_pct": None,
            "source_snapshot_id": None,
            "source_collected_at": None,
            "snapshot_count": len(snapshots),
            "collector_truncated": False,
            "warnings": ["No pg_stat_statements workload is available for this host."],
        }

    latest = snapshots[0]
    recommended_ids = _recommended_ids(latest)
    requested = [str(query_id) for query_id in selected_query_ids or recommended_ids]
    selected_ids = list(dict.fromkeys(qid for qid in requested if qid in latest["members"]))
    latest_members = list(latest["members"].values())
    total_runtime = sum(item["total_runtime_ms"] for item in latest_members)
    selected_runtime = sum(latest["members"][qid]["total_runtime_ms"] for qid in selected_ids)
    coverage = selected_runtime / total_runtime * 100 if total_runtime else 0.0

    comparisons: list[float] = []
    latest_ids = set(latest["members"])
    for previous in snapshots[1:]:
        previous_ids = set(previous["members"])
        union = latest_ids | previous_ids
        if union:
            comparisons.append(len(latest_ids & previous_ids) / len(union) * 100)
    stability = statistics.fmean(comparisons) if comparisons else None

    variances: list[float] = []
    for query_id in selected_ids:
        runtimes = [
            snapshot["members"][query_id]["average_query_runtime_ms"]
            for snapshot in snapshots
            if query_id in snapshot["members"]
            and snapshot["members"][query_id]["average_query_runtime_ms"] > 0
        ]
        if len(runtimes) >= 2:
            mean = statistics.fmean(runtimes)
            if mean:
                variances.append(statistics.pstdev(runtimes) / mean * 100)
    variance = statistics.fmean(variances) if variances else None

    collector_limit = latest["collector_limit"]
    collector_truncated = bool(collector_limit and latest["collector_count"] >= collector_limit)
    warnings: list[str] = []
    if collector_truncated:
        warnings.append(
            "The collector reached its statement limit; runtime coverage may be understated."
        )
    if coverage < MIN_COVERAGE_PCT:
        warnings.append(
            f"Selected statements cover {coverage:.1f}% of visible runtime; "
            f"at least {MIN_COVERAGE_PCT:.0f}% is required."
        )
    if len(snapshots) < 2:
        warnings.append("At least two workload snapshots are required to establish stability.")
    elif stability is not None and stability < MIN_STABILITY_PCT:
        warnings.append(
            f"Workload membership stability is {stability:.1f}%; "
            f"at least {MIN_STABILITY_PCT:.0f}% is required."
        )
    if variance is not None and variance > MAX_RUNTIME_VARIANCE_PCT:
        warnings.append(
            f"Average query runtime variance is {variance:.1f}%; "
            f"the limit is {MAX_RUNTIME_VARIANCE_PCT:.0f}%."
        )

    if not selected_ids:
        status = "no_workload"
    elif collector_truncated or coverage < MIN_COVERAGE_PCT:
        status = "low_coverage"
    elif len(snapshots) < 2:
        status = "insufficient_history"
    elif stability is not None and stability < MIN_STABILITY_PCT:
        status = "unstable"
    elif variance is not None and variance > MAX_RUNTIME_VARIANCE_PCT:
        status = "high_variance"
    else:
        status = "ready"

    candidates = []
    for member in latest_members:
        impact = member["average_query_runtime_ms"] * math.log1p(member["calls"])
        candidates.append(
            {
                **member,
                "runtime_coverage_pct": (
                    member["total_runtime_ms"] / total_runtime * 100 if total_runtime else 0.0
                ),
                "impact_score": impact,
                "recommended": member["query_id"] in recommended_ids,
                "selected": member["query_id"] in selected_ids,
                "last_seen_at": latest["collected_at"],
            }
        )
    candidates.sort(key=lambda item: item["impact_score"], reverse=True)
    return {
        "status": status,
        "ready": status == "ready",
        "candidates": candidates,
        "selected_query_ids": selected_ids,
        "coverage_pct": coverage,
        "membership_stability_pct": stability,
        "runtime_variance_pct": variance,
        "source_snapshot_id": latest["id"],
        "source_collected_at": latest["collected_at"],
        "snapshot_count": len(snapshots),
        "collector_truncated": collector_truncated,
        "warnings": warnings,
    }
