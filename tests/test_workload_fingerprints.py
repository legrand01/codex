"""Tests for evidence-backed workload fingerprint selection."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from backend.services.workload_fingerprints import analyze_workload_snapshots


def _snapshot(offset: int, statements: list[dict], limit: int = 100):
    return {
        "id": uuid4(),
        "collected_at": datetime.now(timezone.utc) - timedelta(minutes=offset),
        "data": {
            "queries": statements,
            "total_queries_collected": len(statements),
            "max_query_entries": limit,
        },
    }


def _statements(runtime_multiplier: float = 1.0):
    return [
        {
            "queryid": "checkout",
            "query": "select * from orders where customer_id = $1",
            "calls": 1000,
            "mean_exec_time": 20 * runtime_multiplier,
            "total_exec_time": 20_000 * runtime_multiplier,
        },
        {
            "queryid": "inventory",
            "query": "select available from inventory where sku = $1",
            "calls": 800,
            "mean_exec_time": 10 * runtime_multiplier,
            "total_exec_time": 8_000 * runtime_multiplier,
        },
        {
            "queryid": "admin",
            "query": "select * from users where id = $1",
            "calls": 10,
            "mean_exec_time": 2 * runtime_multiplier,
            "total_exec_time": 20 * runtime_multiplier,
        },
    ]


def test_recommendation_uses_runtime_and_calls_and_passes_stability_gates():
    result = analyze_workload_snapshots(
        [
            _snapshot(0, _statements(1.0)),
            _snapshot(1, _statements(1.05)),
            _snapshot(2, _statements(0.95)),
        ]
    )

    assert result["ready"] is True
    assert result["status"] == "ready"
    assert result["selected_query_ids"] == ["checkout", "inventory"]
    assert result["coverage_pct"] > 99
    assert result["membership_stability_pct"] == pytest.approx(100)
    assert result["runtime_variance_pct"] < 10
    assert result["candidates"][0]["query_id"] == "checkout"


def test_custom_membership_fails_closed_when_runtime_coverage_is_low():
    result = analyze_workload_snapshots(
        [_snapshot(0, _statements()), _snapshot(1, _statements())], ["admin"]
    )

    assert result["ready"] is False
    assert result["status"] == "low_coverage"
    assert result["selected_query_ids"] == ["admin"]
    assert result["coverage_pct"] < 1
    assert "at least 70%" in " ".join(result["warnings"])


def test_single_snapshot_is_not_treated_as_stable():
    result = analyze_workload_snapshots([_snapshot(0, _statements())])

    assert result["ready"] is False
    assert result["status"] == "insufficient_history"
    assert result["membership_stability_pct"] is None


def test_collector_limit_blocks_overconfident_coverage():
    statements = _statements()[:2]
    result = analyze_workload_snapshots(
        [_snapshot(0, statements, limit=2), _snapshot(1, statements, limit=2)]
    )

    assert result["collector_truncated"] is True
    assert result["ready"] is False
    assert result["status"] == "low_coverage"
    assert "statement limit" in " ".join(result["warnings"])


def test_missing_query_ids_are_excluded_from_selected_membership():
    result = analyze_workload_snapshots(
        [_snapshot(0, _statements()), _snapshot(1, _statements())],
        ["checkout", "no-longer-visible"],
    )

    assert result["selected_query_ids"] == ["checkout"]
