"""Comparable baseline measurement and root-cause routing for tuning sessions."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID, uuid4

from backend.services.workload_fingerprints import analyze_workload_snapshots

ROOT_CAUSE_CATEGORIES = {
    "configuration",
    "query_index",
    "lock_contention",
    "vacuum_bloat",
    "resource_saturation",
    "connection_pressure",
    "insufficient_evidence",
}


def _json(value: Any, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _number(value: Any) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _typed_rows(evidence: Sequence[Mapping[str, Any]], evidence_type: str):
    return sorted(
        [row for row in evidence if row.get("evidence_type") == evidence_type],
        key=lambda row: _timestamp(row.get("collected_at")),
    )


def _bounded_evidence(
    evidence: Sequence[Mapping[str, Any]], requested_window_seconds: int
) -> list[Mapping[str, Any]]:
    """Limit baseline inputs to the requested trailing observation window."""
    timestamps = [_timestamp(row.get("collected_at")) for row in evidence]
    latest = max(timestamps, default=0.0)
    if latest <= 0:
        return list(evidence)
    cutoff = latest - requested_window_seconds
    return [row for row in evidence if _timestamp(row.get("collected_at")) >= cutoff]


def _statement_entries(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("queries", "statement_stats", "statements"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _database_entry(data: Mapping[str, Any], database_name: Optional[str]):
    entries = data.get("database_stats")
    if not isinstance(entries, list):
        return data
    records = [entry for entry in entries if isinstance(entry, dict)]
    if database_name:
        match = next((entry for entry in records if entry.get("datname") == database_name), None)
        if match:
            return match
    return records[0] if records else {}


def _statement_counter_map(row: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    data = _json(row.get("data"), {})
    for entry in _statement_entries(data):
        query_id = entry.get("queryid", entry.get("query_id"))
        if query_id is None:
            continue
        calls = _number(entry.get("calls"))
        total_runtime = _number(
            entry.get("total_exec_time", entry.get("total_exec_time_ms"))
        )
        if not total_runtime and calls:
            total_runtime = calls * _number(
                entry.get("mean_exec_time", entry.get("mean_exec_time_ms"))
            )
        result[str(query_id)] = {
            "query_id": str(query_id),
            "query_text": entry.get("query"),
            "calls": calls,
            "total_runtime_ms": total_runtime,
        }
    return result


def _statement_window_candidates(
    rows: Sequence[Mapping[str, Any]], selected_query_ids: Sequence[str]
) -> tuple[list[dict[str, Any]], float, bool]:
    """Calculate statement-family counter deltas for the observed window."""
    if not rows:
        return [], 0.0, False
    before = _statement_counter_map(rows[0]) if len(rows) >= 2 else {}
    after = _statement_counter_map(rows[-1])
    selected_ids = {str(query_id) for query_id in selected_query_ids}
    candidates: list[dict[str, Any]] = []
    counters_reset = False
    for query_id, latest in after.items():
        previous = before.get(query_id)
        if previous is None:
            calls = latest["calls"]
            total_runtime = latest["total_runtime_ms"]
        elif (
            latest["calls"] < previous["calls"]
            or latest["total_runtime_ms"] < previous["total_runtime_ms"]
        ):
            counters_reset = True
            calls = latest["calls"]
            total_runtime = latest["total_runtime_ms"]
        else:
            calls = latest["calls"] - previous["calls"]
            total_runtime = latest["total_runtime_ms"] - previous["total_runtime_ms"]
        average_runtime = total_runtime / calls if calls else 0.0
        candidates.append(
            {
                **latest,
                "calls": calls,
                "total_runtime_ms": total_runtime,
                "average_query_runtime_ms": average_runtime,
                "selected": query_id in selected_ids,
                "impact_score": average_runtime * math.log1p(calls),
            }
        )
    visible_runtime = sum(item["total_runtime_ms"] for item in candidates)
    for item in candidates:
        item["runtime_coverage_pct"] = (
            item["total_runtime_ms"] / visible_runtime * 100 if visible_runtime else 0.0
        )
    candidates.sort(key=lambda item: item["impact_score"], reverse=True)
    selected_runtime = sum(
        item["total_runtime_ms"] for item in candidates if item["selected"]
    )
    coverage = selected_runtime / visible_runtime * 100 if visible_runtime else 0.0
    return candidates, coverage, counters_reset


def _settings_map(evidence: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = _typed_rows(evidence, "pg_settings")
    if not rows:
        return {}
    data = _json(rows[-1].get("data"), {})
    raw = data.get("settings", data) if isinstance(data, dict) else {}
    if isinstance(raw, dict):
        return raw
    result = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                result[str(item["name"])] = item.get("setting")
    return result


def _observed_span(rows: Sequence[Mapping[str, Any]]) -> float:
    if len(rows) < 2:
        return 0.0
    return max(
        0.0, _timestamp(rows[-1].get("collected_at")) - _timestamp(rows[0].get("collected_at"))
    )


def _transaction_rate(
    rows: Sequence[Mapping[str, Any]], database_name: Optional[str]
) -> Optional[float]:
    if len(rows) < 2:
        return None
    first_data = _json(rows[0].get("data"), {})
    last_data = _json(rows[-1].get("data"), {})
    first = _database_entry(first_data, database_name)
    last = _database_entry(last_data, database_name)
    elapsed = _observed_span(rows)
    if elapsed <= 0:
        return None
    before = _number(first.get("xact_commit")) + _number(first.get("xact_rollback"))
    after = _number(last.get("xact_commit")) + _number(last.get("xact_rollback"))
    if after < before:
        return None
    return (after - before) / elapsed


def _delta(
    rows: Sequence[Mapping[str, Any]],
    database_name: Optional[str],
    key: str,
) -> float:
    if len(rows) < 2:
        return 0.0
    first = _database_entry(_json(rows[0].get("data"), {}), database_name)
    last = _database_entry(_json(rows[-1].get("data"), {}), database_name)
    return max(0.0, _number(last.get(key)) - _number(first.get(key)))


def _latest_data(evidence: Sequence[Mapping[str, Any]], evidence_type: str):
    rows = _typed_rows(evidence, evidence_type)
    return _json(rows[-1].get("data"), {}) if rows else {}


def _evidence_window_references(
    evidence: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Keep the first and last snapshot for each evidence type.

    These endpoints are enough to locate the bounded measurement window without
    copying every high-frequency collection ID into the baseline API payload.
    """
    references: list[dict[str, str]] = []
    evidence_types = sorted({str(row.get("evidence_type")) for row in evidence})
    for evidence_type in evidence_types:
        rows = _typed_rows(evidence, evidence_type)
        endpoints = rows if len(rows) <= 1 else [rows[0], rows[-1]]
        for row in endpoints:
            collected_at = row.get("collected_at")
            references.append(
                {
                    "snapshot_id": str(row["id"]),
                    "evidence_type": evidence_type,
                    "collected_at": (
                        collected_at.isoformat()
                        if isinstance(collected_at, datetime)
                        else str(collected_at)
                    ),
                }
            )
    return references


def _safety_metrics(
    evidence: Sequence[Mapping[str, Any]], database_name: Optional[str]
) -> dict[str, Any]:
    database_rows = _typed_rows(evidence, "pg_stat_database")
    latest_database = (
        _database_entry(_json(database_rows[-1].get("data"), {}), database_name)
        if database_rows
        else {}
    )
    locks = _latest_data(evidence, "locks")
    replication = _latest_data(evidence, "replication")
    os_metrics = _latest_data(evidence, "os_metrics")
    wal = _latest_data(evidence, "wal_checkpoint")
    return {
        "connections": _number(latest_database.get("numbackends")),
        "transaction_rate_per_second": _transaction_rate(database_rows, database_name),
        "deadlocks_delta": _delta(database_rows, database_name, "deadlocks"),
        "temp_files_delta": _delta(database_rows, database_name, "temp_files"),
        "temp_bytes_delta": _delta(database_rows, database_name, "temp_bytes"),
        "waiting_locks": _number(locks.get("waiting_count")),
        "total_locks": _number(locks.get("total_locks")),
        "replication_lag_seconds": _number(replication.get("replay_lag_seconds")),
        "cpu_utilization_pct": _number(os_metrics.get("cpu_percent")),
        "memory_utilization_pct": _number(os_metrics.get("memory_percent")),
        "disk_io": os_metrics.get("disk_io", {}),
        "checkpoint": wal.get("checkpoint", {}),
        "wal": wal.get("wal", {}),
    }


def _find_numeric(value: Any, keys: set[str]) -> list[float]:
    found: list[float] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and isinstance(child, (int, float)):
                found.append(float(child))
            else:
                found.extend(_find_numeric(child, keys))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_numeric(child, keys))
    return found


def _classify_root_cause(
    run: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    safety: Mapping[str, Any],
) -> dict[str, Any]:
    available = {str(row.get("evidence_type")) for row in evidence}
    scores: dict[str, float] = {category: 0.0 for category in ROOT_CAUSE_CATEGORIES}
    details: dict[str, Any] = {}
    if not {"pg_stat_statements", "pg_stat_database", "os_metrics"}.issubset(available):
        scores["insufficient_evidence"] = 1.0
        missing = sorted({"pg_stat_statements", "pg_stat_database", "os_metrics"} - available)
        details["missing_evidence"] = missing

    query_candidates = []
    for candidate in candidates:
        query = str(candidate.get("query_text") or "").lower().strip()
        if not query.startswith("select"):
            continue
        if any(
            marker in query
            for marker in (
                "pg_stat_",
                "pg_catalog",
                "information_schema",
                "current_setting",
                "pg_sleep",
            )
        ):
            continue
        coverage = _number(candidate.get("runtime_coverage_pct"))
        mean = _number(candidate.get("average_query_runtime_ms"))
        if (
            mean >= 10
            and coverage >= 10
            and re.search(r"\b(where|join|group\s+by|order\s+by)\b", query)
        ):
            query_candidates.append(candidate)
    if query_candidates:
        dominant_query = max(
            query_candidates, key=lambda item: _number(item.get("runtime_coverage_pct"))
        )
        coverage = _number(dominant_query.get("runtime_coverage_pct"))
        scores["query_index"] = min(0.95, 0.55 + coverage / 200)
        details["dominant_query"] = {
            "query_id": dominant_query.get("query_id"),
            "average_query_runtime_ms": dominant_query.get("average_query_runtime_ms"),
            "runtime_coverage_pct": coverage,
            "query_text": dominant_query.get("query_text"),
        }

    waiting_locks = _number(safety.get("waiting_locks"))
    if waiting_locks:
        scores["lock_contention"] = min(0.98, 0.75 + waiting_locks / 100)
        details["waiting_locks"] = waiting_locks

    dead_tuples = _find_numeric(
        [_json(row.get("data"), {}) for row in evidence], {"n_dead_tup", "dead_tuples"}
    )
    if dead_tuples and max(dead_tuples) >= 10_000:
        scores["vacuum_bloat"] = min(0.95, 0.65 + math.log10(max(dead_tuples)) / 20)
        details["dead_tuples"] = max(dead_tuples)

    saturation = max(
        _number(safety.get("cpu_utilization_pct")),
        _number(safety.get("memory_utilization_pct")),
    )
    if saturation >= 80:
        scores["resource_saturation"] = min(0.99, saturation / 100)
        details["max_resource_utilization_pct"] = saturation

    settings = _settings_map(evidence)
    max_connections = _number(settings.get("max_connections"))
    connection_ratio = (
        _number(safety.get("connections")) / max_connections * 100 if max_connections else 0.0
    )
    if connection_ratio >= 80:
        scores["connection_pressure"] = min(0.99, connection_ratio / 100)
        details["connection_utilization_pct"] = connection_ratio

    selected_parameters = set(_json(run.get("selected_parameters"), []))
    if safety.get("temp_files_delta", 0) and "work_mem" in selected_parameters:
        scores["configuration"] = max(scores["configuration"], 0.7)
        details["configuration_signal"] = "new temporary files with work_mem in scope"
    checkpoint = safety.get("checkpoint")
    if isinstance(checkpoint, dict) and _number(checkpoint.get("checkpoints_req")):
        if selected_parameters & {"max_wal_size", "min_wal_size", "checkpoint_completion_target"}:
            scores["configuration"] = max(scores["configuration"], 0.65)
            details["configuration_signal"] = "requested checkpoints with WAL settings in scope"
    if selected_parameters and not scores["configuration"]:
        scores["configuration"] = 0.35

    category, score = max(scores.items(), key=lambda item: item[1])
    if score < 0.5:
        category, score = "insufficient_evidence", 0.6
    summaries = {
        "configuration": "Observed evidence supports a bounded configuration experiment.",
        "query_index": (
            "A small set of slow normalized statements dominates runtime; investigate "
            "query plans and indexes before changing server settings."
        ),
        "lock_contention": (
            "Waiting locks are the dominant performance signal; resolve blockers "
            "before configuration tuning."
        ),
        "vacuum_bloat": (
            "Dead-tuple evidence indicates vacuum or bloat work should precede "
            "configuration tuning."
        ),
        "resource_saturation": "Host CPU or memory saturation is the dominant constraint.",
        "connection_pressure": (
            "Backend utilization is near max_connections and should be addressed "
            "before parameter search."
        ),
        "insufficient_evidence": (
            "The evidence does not establish a safe, dominant configuration lever."
        ),
    }
    return {
        "category": category,
        "confidence": min(1.0, score),
        "summary": summaries[category],
        "details": details,
        "scores": scores,
    }


def _advisory(root_cause: Mapping[str, Any], evidence_references: list[dict]):
    category = root_cause["category"]
    if category == "configuration":
        return None
    recommendations = {
        "query_index": [
            "Capture EXPLAIN (ANALYZE, BUFFERS) for the dominant normalized "
            "statement in a safe environment.",
            "Review predicate, join, grouping, and ordering columns for a selective "
            "index before changing PostgreSQL settings.",
        ],
        "lock_contention": [
            "Identify the blocking transaction and shorten or reschedule the "
            "lock-holding operation.",
            "Re-measure after the lock queue returns to its normal baseline.",
        ],
        "vacuum_bloat": [
            "Inspect autovacuum history, dead tuples, and table/index bloat.",
            "Complete governed maintenance before starting configuration experiments.",
        ],
        "resource_saturation": [
            "Correlate CPU, memory, and storage utilization with the dominant statements.",
            "Reduce workload pressure or add capacity before parameter search.",
        ],
        "connection_pressure": [
            "Review connection-pool sizing and idle backend lifetime.",
            "Re-measure with backend utilization below the configured safety threshold.",
        ],
        "insufficient_evidence": [
            "Collect a longer representative workload window with pg_stat_statements "
            "and host metrics.",
            "Start a fresh baseline after coverage and variance stabilize.",
        ],
    }
    return {
        "category": category,
        "severity": "warning" if category != "resource_saturation" else "critical",
        "title": root_cause["summary"],
        "summary": json.dumps(root_cause.get("details", {}), default=str),
        "recommendations": recommendations[category],
        "evidence_references": evidence_references,
        "executable": False,
    }


def build_baseline_measurement(
    run: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    fingerprint: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build one comparable baseline from a bounded evidence window."""
    target = str(run.get("tuning_target") or "system_wide_aqr")
    database_name = run.get("database_name")
    requested_window = int(run.get("measurement_window_seconds") or 300)
    evidence = _bounded_evidence(evidence, requested_window)
    statement_rows = _typed_rows(evidence, "pg_stat_statements")
    database_rows = _typed_rows(evidence, "pg_stat_database")

    initial = analyze_workload_snapshots(statement_rows)
    if fingerprint:
        members = fingerprint.get("members", [])
        selected_ids = [str(member["query_id"]) for member in members]
    else:
        selected_ids = [str(item["query_id"]) for item in initial["candidates"]]
    workload = analyze_workload_snapshots(statement_rows, selected_ids)
    window_candidates, window_coverage, counters_reset = _statement_window_candidates(
        statement_rows, selected_ids
    )
    selected = [item for item in window_candidates if item["selected"]]
    calls = sum(_number(item.get("calls")) for item in selected)
    total_runtime = sum(_number(item.get("total_runtime_ms")) for item in selected)
    average_runtime = total_runtime / calls if calls else None
    tps = _transaction_rate(database_rows, database_name)

    formulas = {
        "recommended_fingerprint": (
            "sum(member total runtime ms) / sum(member calls)",
            "minimize",
            "ms",
        ),
        "custom_fingerprint": (
            "sum(member total runtime ms) / sum(member calls)",
            "minimize",
            "ms",
        ),
        "system_wide_aqr": ("sum(visible total runtime ms) / sum(visible calls)", "minimize", "ms"),
        "transactions_per_second": (
            "delta(commits + rollbacks) / observed seconds",
            "maximize",
            "transactions/second",
        ),
        "composite": (
            "transactions per second / weighted average query runtime ms",
            "maximize",
            "transactions/ms",
        ),
    }
    formula, direction, unit = formulas[target]
    if target in {"recommended_fingerprint", "custom_fingerprint", "system_wide_aqr"}:
        objective_score = average_runtime
    elif target == "transactions_per_second":
        objective_score = tps
    else:
        objective_score = tps / max(average_runtime or 0, 0.001) if tps is not None else None

    statement_span = _observed_span(statement_rows)
    database_span = _observed_span(database_rows)
    observed_window = database_span if target == "transactions_per_second" else statement_span
    if target == "composite":
        observed_window = min(statement_span, database_span)
    minimum_observed = requested_window * 0.8
    warnings = list(workload["warnings"])
    if counters_reset:
        warnings.append(
            "pg_stat_statements counters reset inside the baseline window; "
            "the post-reset interval was used."
        )
    if observed_window < minimum_observed:
        warnings.append(
            f"Only {observed_window:.0f}s of the requested {requested_window}s "
            "baseline window is available."
        )
    if objective_score is None:
        warnings.append("The selected objective could not be calculated from the evidence window.")

    safety = _safety_metrics(evidence, database_name)
    root_cause = _classify_root_cause(run, evidence, window_candidates, safety)
    evidence_references = _evidence_window_references(evidence)
    objective_uses_workload = target != "transactions_per_second"
    stable = (
        objective_score is not None
        and observed_window >= minimum_observed
        and (not objective_uses_workload or workload["ready"])
    )
    if not stable or root_cause["category"] == "insufficient_evidence":
        status = "paused"
    elif root_cause["category"] != "configuration":
        status = "advisory_only"
    else:
        status = "ready"

    membership = [
        {
            "query_id": item["query_id"],
            "calls": item["calls"],
            "average_query_runtime_ms": item["average_query_runtime_ms"],
            "total_runtime_ms": item["total_runtime_ms"],
            "runtime_coverage_pct": item["runtime_coverage_pct"],
        }
        for item in selected
    ]
    result = {
        "status": status,
        "objective_type": target,
        "objective_formula": formula,
        "objective_direction": direction,
        "objective_score": objective_score,
        "metric_units": {"objective_score": unit, "runtime": "ms", "coverage": "%"},
        "fingerprint_membership": membership,
        "warmup_window_seconds": int(run.get("warmup_window_seconds") or 60),
        "requested_measurement_window_seconds": requested_window,
        "observed_measurement_window_seconds": observed_window,
        "workload_coverage_pct": window_coverage if objective_uses_workload else 100.0,
        "runtime_variance_pct": workload["runtime_variance_pct"]
        if objective_uses_workload
        else None,
        "safety_metrics": safety,
        "evidence_references": evidence_references,
        "root_cause_category": root_cause["category"],
        "root_cause_confidence": root_cause["confidence"],
        "root_cause_summary": root_cause["summary"],
        "root_cause_details": root_cause["details"],
        "warnings": warnings,
    }
    result["advisory"] = _advisory(root_cause, evidence_references)
    return result


async def capture_baseline(run_id: UUID, pool) -> dict[str, Any]:
    """Persist the first immutable baseline for a tuning session."""
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM baseline_measurements WHERE run_id = $1", run_id
        )
        if existing:
            return dict(existing)
        run_row = await conn.fetchrow("SELECT * FROM loop_runs WHERE id = $1", run_id)
        if run_row is None:
            raise RuntimeError(f"Run {run_id} does not exist")
        run = dict(run_row)
        history_seconds = int(run.get("measurement_window_seconds") or 300) + 120
        evidence_rows = await conn.fetch(
            """
            SELECT id, evidence_type, collected_at, data, quality_score
            FROM evidence_snapshots
            WHERE host_id = $1
              AND collected_at >= NOW() - ($2 * INTERVAL '1 second')
            ORDER BY collected_at ASC
            LIMIT 500
            """,
            run["host_id"],
            history_seconds,
        )
        fingerprint = None
        if run.get("workload_fingerprint_id"):
            members = await conn.fetch(
                """
                SELECT query_id, query_text, calls, average_query_runtime_ms,
                       total_runtime_ms, runtime_coverage_pct
                FROM workload_fingerprint_members
                WHERE fingerprint_id = $1 ORDER BY ordinal
                """,
                run["workload_fingerprint_id"],
            )
            fingerprint = {"members": [dict(member) for member in members]}

    evidence = [
        {
            **dict(row),
            "data": _json(row["data"], {}),
        }
        for row in evidence_rows
    ]
    measurement = build_baseline_measurement(run, evidence, fingerprint)
    baseline_id = uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO baseline_measurements (
                    id, organization_id, run_id, host_id, workload_fingerprint_id,
                    status, objective_type, objective_formula, objective_direction,
                    objective_score, metric_units, fingerprint_membership,
                    warmup_window_seconds, requested_measurement_window_seconds,
                    observed_measurement_window_seconds, workload_coverage_pct,
                    runtime_variance_pct, safety_metrics, evidence_references,
                    root_cause_category, root_cause_confidence, root_cause_summary,
                    root_cause_details, warnings
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb,
                    $12::jsonb, $13, $14, $15, $16, $17, $18::jsonb, $19::jsonb,
                    $20, $21, $22, $23::jsonb, $24::jsonb
                )
                RETURNING *
                """,
                baseline_id,
                run["organization_id"],
                run_id,
                run["host_id"],
                run.get("workload_fingerprint_id"),
                measurement["status"],
                measurement["objective_type"],
                measurement["objective_formula"],
                measurement["objective_direction"],
                measurement["objective_score"],
                json.dumps(measurement["metric_units"]),
                json.dumps(measurement["fingerprint_membership"]),
                measurement["warmup_window_seconds"],
                measurement["requested_measurement_window_seconds"],
                measurement["observed_measurement_window_seconds"],
                measurement["workload_coverage_pct"],
                measurement["runtime_variance_pct"],
                json.dumps(measurement["safety_metrics"]),
                json.dumps(measurement["evidence_references"]),
                measurement["root_cause_category"],
                measurement["root_cause_confidence"],
                measurement["root_cause_summary"],
                json.dumps(measurement["root_cause_details"]),
                json.dumps(measurement["warnings"]),
            )
            await conn.execute(
                """
                UPDATE loop_runs
                SET baseline_score = $2,
                    best_score = COALESCE(best_score, $2)
                WHERE id = $1
                """,
                run_id,
                measurement["objective_score"],
            )
            advisory = measurement["advisory"]
            if advisory:
                await conn.execute(
                    """
                    INSERT INTO advisory_findings (
                        organization_id, run_id, host_id, category, severity,
                        title, summary, recommendations, evidence_references,
                        executable
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb,
                              $9::jsonb, FALSE)
                    ON CONFLICT (run_id, category) DO NOTHING
                    """,
                    run["organization_id"],
                    run_id,
                    run["host_id"],
                    advisory["category"],
                    advisory["severity"],
                    advisory["title"],
                    advisory["summary"],
                    json.dumps(advisory["recommendations"]),
                    json.dumps(advisory["evidence_references"]),
                )
    result = dict(row)
    result["root_cause_details"] = measurement["root_cause_details"]
    result["advisory"] = measurement["advisory"]
    return result
