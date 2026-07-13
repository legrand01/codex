"""Tests for comparable baseline measurement and root-cause routing."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from backend.services.baseline_measurement import build_baseline_measurement

NOW = datetime.now(timezone.utc)


def _row(evidence_type: str, seconds: int, data: dict, quality: float = 0.9):
    return {
        "id": uuid4(),
        "evidence_type": evidence_type,
        "collected_at": NOW + timedelta(seconds=seconds),
        "data": data,
        "quality_score": quality,
    }


def _statements(query: str = "SELECT pg_sleep($1)", limit: int = 100):
    return [
        _row(
            "pg_stat_statements",
            seconds,
            {
                "queries": [
                    {
                        "queryid": "slow-family",
                        "query": query,
                        "calls": 1000 + seconds,
                        "mean_exec_time": 20 + seconds / 1000,
                        "total_exec_time": (1000 + seconds) * (20 + seconds / 1000),
                    }
                ],
                "total_queries_collected": 1,
                "max_query_entries": limit,
            },
        )
        for seconds in (0, 150, 300)
    ]


def _database(temp_files=(0, 2)):
    return [
        _row(
            "pg_stat_database",
            seconds,
            {
                "database_stats": [
                    {
                        "datname": "appdb",
                        "xact_commit": 10_000 + index * 30_000,
                        "xact_rollback": 100,
                        "numbackends": 20,
                        "temp_files": temp_files[index],
                        "temp_bytes": temp_files[index] * 1024,
                        "deadlocks": 0,
                    }
                ]
            },
        )
        for index, seconds in enumerate((0, 300))
    ]


def _supporting(waiting_locks=0, cpu=25, memory=45):
    return [
        _row(
            "os_metrics",
            300,
            {"cpu_percent": cpu, "memory_percent": memory, "disk_io": {}},
        ),
        _row(
            "locks",
            300,
            {
                "waiting_count": waiting_locks,
                "total_locks": waiting_locks + 2,
                "locks": [],
            },
        ),
        _row("replication", 300, {"replay_lag_seconds": 0}),
        _row(
            "wal_checkpoint",
            300,
            {"checkpoint": {"checkpoints_req": 0}, "wal": {}},
        ),
        _row(
            "pg_settings",
            300,
            {
                "settings": [
                    {"name": "max_connections", "setting": "100"},
                    {"name": "work_mem", "setting": "4096"},
                ]
            },
        ),
    ]


def _run(target="recommended_fingerprint"):
    return {
        "tuning_target": target,
        "database_name": "appdb",
        "selected_parameters": ["work_mem"],
        "warmup_window_seconds": 60,
        "measurement_window_seconds": 300,
    }


FINGERPRINT = {"members": [{"query_id": "slow-family"}]}


def test_configuration_baseline_persists_comparable_protocol():
    evidence = _statements() + _database() + _supporting()
    result = build_baseline_measurement(_run(), evidence, FINGERPRINT)

    assert result["status"] == "ready"
    assert result["root_cause_category"] == "configuration"
    assert result["objective_formula"] == "sum(member total runtime ms) / sum(member calls)"
    assert result["objective_direction"] == "minimize"
    assert result["metric_units"]["objective_score"] == "ms"
    assert result["observed_measurement_window_seconds"] == pytest.approx(300)
    assert result["objective_score"] == pytest.approx(21.3)
    assert result["workload_coverage_pct"] == pytest.approx(100)
    assert result["fingerprint_membership"][0]["query_id"] == "slow-family"
    assert result["safety_metrics"]["transaction_rate_per_second"] == pytest.approx(100)
    assert result["advisory"] is None


def test_dominant_query_is_routed_to_non_executable_advisory():
    query = (
        "SELECT customer_id, SUM(amount) FROM sales\n"
        "WHERE created_at > $1\nGROUP BY customer_id\nORDER BY SUM(amount) DESC"
    )
    evidence = _statements(query) + _database((0, 0)) + _supporting()
    result = build_baseline_measurement(_run(), evidence, FINGERPRINT)

    assert result["status"] == "advisory_only"
    assert result["root_cause_category"] == "query_index"
    assert result["advisory"]["executable"] is False
    assert "EXPLAIN" in result["advisory"]["recommendations"][0]
    assert result["root_cause_details"]["dominant_query"]["query_id"] == "slow-family"
    assert len(result["evidence_references"]) <= 2 * len(
        {row["evidence_type"] for row in evidence}
    )


def test_unstable_or_truncated_workload_pauses_baseline():
    evidence = _statements(limit=1) + _database((0, 0)) + _supporting()
    result = build_baseline_measurement(_run(), evidence, FINGERPRINT)

    assert result["status"] == "paused"
    assert "statement limit" in " ".join(result["warnings"])


def test_lock_contention_wins_when_query_is_not_actionable():
    evidence = _statements() + _database((0, 0)) + _supporting(waiting_locks=4)
    result = build_baseline_measurement(_run(), evidence, FINGERPRINT)

    assert result["status"] == "advisory_only"
    assert result["root_cause_category"] == "lock_contention"
    assert result["advisory"]["executable"] is False


def test_tps_objective_uses_transaction_counter_delta_and_units():
    evidence = _statements() + _database((0, 0)) + _supporting()
    result = build_baseline_measurement(_run("transactions_per_second"), evidence, None)

    assert result["objective_score"] == pytest.approx(100)
    assert result["objective_direction"] == "maximize"
    assert result["metric_units"]["objective_score"] == "transactions/second"


def test_baseline_ignores_statement_activity_before_requested_window():
    evidence = _statements() + _database((0, 0)) + _supporting()
    evidence.append(
        _row(
            "pg_stat_statements",
            -120,
            {
                "queries": [
                    {
                        "queryid": "slow-family",
                        "query": "SELECT pg_sleep($1)",
                        "calls": 1,
                        "mean_exec_time": 5000,
                        "total_exec_time": 5000,
                    }
                ],
                "total_queries_collected": 1,
                "max_query_entries": 100,
            },
        )
    )

    result = build_baseline_measurement(_run(), evidence, FINGERPRINT)

    assert result["observed_measurement_window_seconds"] == pytest.approx(300)
    assert result["objective_score"] == pytest.approx(21.3)


def test_missing_required_telemetry_pauses_as_insufficient_evidence():
    evidence = _statements() + _database((0, 0))
    result = build_baseline_measurement(_run(), evidence, FINGERPRINT)

    assert result["status"] == "paused"
    assert result["root_cause_category"] == "insufficient_evidence"
    assert result["advisory"]["executable"] is False
