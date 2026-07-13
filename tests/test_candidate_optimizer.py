"""Correctness tests for deterministic candidate generation and decisions."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from backend.services.candidate_optimizer import (
    CandidateOptimizer,
    evaluate_candidate,
    generate_candidate_values,
)

DOMAIN = {
    "strategy": "multipliers",
    "values": [2.0, 4.0, 8.0, 0.5, 1.5],
    "minimum": 64,
    "maximum": 1048576,
    "default_max_deviation_pct": 700,
}


def _baseline(direction="minimize", score=100.0):
    return {
        "objective_type": "recommended_fingerprint",
        "objective_formula": "sum(runtime) / sum(calls)",
        "objective_direction": direction,
        "objective_score": score,
        "metric_units": {"objective_score": "ms"},
        "fingerprint_membership": [{"query_id": "checkout"}],
        "workload_coverage_pct": 95.0,
        "safety_metrics": {
            "transaction_rate_per_second": 100.0,
            "deadlocks_delta": 0,
            "waiting_locks": 0,
            "replication_lag_seconds": 0,
            "cpu_utilization_pct": 50.0,
            "memory_utilization_pct": 60.0,
        },
    }


def _measurement(score=80.0):
    return {
        "objective_type": "recommended_fingerprint",
        "objective_formula": "sum(runtime) / sum(calls)",
        "objective_direction": "minimize",
        "objective_score": score,
        "metric_units": {"objective_score": "ms"},
        "fingerprint_membership": [{"query_id": "checkout"}],
        "requested_measurement_window_seconds": 300,
        "observed_measurement_window_seconds": 300,
        "workload_coverage_pct": 94.0,
        "runtime_variance_pct": 4.0,
        "safety_metrics": {
            "transaction_rate_per_second": 102.0,
            "deadlocks_delta": 0,
            "waiting_locks": 0,
            "replication_lag_seconds": 0,
            "cpu_utilization_pct": 52.0,
            "memory_utilization_pct": 62.0,
        },
    }


def test_domain_generation_has_expected_values_without_baseline_duplicates():
    values = generate_candidate_values("64kB", "memory_kb", DOMAIN, 100)

    assert values == ["128kB", "96kB"]
    assert "256kB" not in values
    assert "64kB" not in values


def test_candidate_is_kept_only_when_it_beats_baseline_and_best():
    decision = evaluate_candidate(
        _baseline(),
        _measurement(80),
        best_score_before=90,
        degradation_threshold_pct=10,
    )

    assert decision.decision == "kept"
    assert decision.baseline_delta_pct == pytest.approx(20)
    assert decision.best_delta_pct == pytest.approx(100 / 9)
    assert decision.guardrail_violations == []


def test_candidate_rolls_back_when_it_does_not_beat_best():
    decision = evaluate_candidate(
        _baseline(),
        _measurement(89.5),
        best_score_before=90,
        degradation_threshold_pct=10,
    )

    assert decision.decision == "rolled_back"
    assert decision.best_delta_pct < 1
    assert "both must be at least" in decision.reason


def test_candidate_rolls_back_on_safety_regression_even_with_objective_gain():
    measurement = _measurement(75)
    measurement["safety_metrics"]["cpu_utilization_pct"] = 70

    decision = evaluate_candidate(
        _baseline(),
        measurement,
        best_score_before=100,
        degradation_threshold_pct=10,
    )

    assert decision.decision == "rolled_back"
    assert any("cpu_utilization_pct" in item for item in decision.guardrail_violations)


def test_candidate_is_inconclusive_when_measurement_is_not_comparable():
    measurement = _measurement(70)
    measurement["observed_measurement_window_seconds"] = 100

    decision = evaluate_candidate(
        _baseline(),
        measurement,
        best_score_before=100,
        degradation_threshold_pct=10,
    )

    assert decision.decision == "inconclusive"
    assert decision.baseline_delta_pct is None
    assert "window is incomplete" in decision.reason


def test_maximize_objective_uses_the_same_positive_improvement_semantics():
    baseline = _baseline("maximize", 100)
    measurement = _measurement(120)
    measurement["objective_direction"] = "maximize"

    decision = evaluate_candidate(
        baseline,
        measurement,
        best_score_before=110,
        degradation_threshold_pct=10,
    )

    assert decision.decision == "kept"
    assert decision.baseline_delta_pct == pytest.approx(20)
    assert decision.best_delta_pct == pytest.approx(100 / 11)


@pytest.mark.asyncio
async def test_restart_safe_measurement_casts_timestamp_boundary_explicitly():
    """asyncpg must not infer the measurement timestamp as an interval."""
    connection = AsyncMock()
    connection.fetchval.return_value = 1
    connection.fetch.side_effect = [[], []]
    acquire = AsyncMock()
    acquire.__aenter__.return_value = connection
    acquire.__aexit__.return_value = False
    pool = MagicMock()
    pool.acquire.return_value = acquire
    optimizer = CandidateOptimizer(pool, sleeper=AsyncMock())
    candidate = {
        "id": uuid4(),
        "measurement_result": None,
        "warmup_window_seconds": 0,
        "measurement_window_seconds": 30,
        "measurement_started_at": datetime.now(timezone.utc) - timedelta(minutes=2),
    }
    run = {"host_id": uuid4(), "workload_fingerprint_id": uuid4()}
    measurement = {
        "objective_score": 10.0,
        "observed_measurement_window_seconds": 30.0,
        "workload_coverage_pct": 100.0,
        "runtime_variance_pct": 0.0,
        "safety_metrics": {},
        "evidence_references": [],
    }

    with patch(
        "backend.services.candidate_optimizer.build_baseline_measurement",
        return_value=measurement,
    ):
        result = await optimizer.measure_candidate(candidate, run)

    evidence_query = connection.fetch.await_args_list[0].args[0]
    assert "$2::timestamptz - INTERVAL '5 seconds'" in evidence_query
    assert result == measurement
