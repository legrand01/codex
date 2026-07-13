"""Tests for measured candidate history API."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from backend.dependencies import get_db
from backend.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


def _override(mock_db):
    async def dependency():
        yield mock_db

    app.dependency_overrides[get_db] = dependency


@pytest.mark.asyncio
async def test_candidate_history_returns_measurement_and_decision(client):
    run_id = uuid4()
    host_id = uuid4()
    plan_id = uuid4()
    now = datetime.now(timezone.utc)
    db = AsyncMock()
    db.fetch = AsyncMock(
        return_value=[
            {
                "id": uuid4(),
                "organization_id": uuid4(),
                "run_id": run_id,
                "host_id": host_id,
                "plan_id": plan_id,
                "plan_status": "applied",
                "iteration": 1,
                "domain_version": "p0-bounded-v1",
                "parameter_values": '{"work_mem":"128kB"}',
                "pre_change_snapshot": "{}",
                "baseline_score": 100.0,
                "best_score_before": 100.0,
                "objective_score": 75.0,
                "baseline_delta_pct": 25.0,
                "best_delta_pct": 25.0,
                "objective_formula": "sum(runtime) / sum(calls)",
                "objective_direction": "minimize",
                "metric_units": '{"objective_score":"ms"}',
                "warmup_window_seconds": 0,
                "measurement_window_seconds": 60,
                "observed_measurement_window_seconds": 60.0,
                "workload_coverage_pct": 98.0,
                "runtime_variance_pct": 2.0,
                "safety_metrics": "{}",
                "safety_deltas": '{"cpu_utilization_pct":2.0}',
                "guardrail_violations": "[]",
                "evidence_references": "[]",
                "confidence_score": 0.9,
                "decision": "kept",
                "decision_reason": "Objective improved safely",
                "warmup_started_at": now,
                "warmup_completed_at": now,
                "measurement_started_at": now,
                "measured_at": now,
                "decided_at": now,
                "created_at": now,
            }
        ]
    )
    _override(db)
    try:
        response = await client.get(f"/api/v1/runs/{run_id}/candidates")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()[0]
    assert data["parameter_values"] == {"work_mem": "128kB"}
    assert data["decision"] == "kept"
    assert data["baseline_delta_pct"] == 25.0
    assert data["plan_status"] == "applied"
    assert "r.organization_id" in db.fetch.await_args.args[0]


@pytest.mark.asyncio
async def test_candidate_history_is_empty_when_run_has_none(client):
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[])
    _override(db)
    try:
        response = await client.get(f"/api/v1/runs/{uuid4()}/candidates")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == []
