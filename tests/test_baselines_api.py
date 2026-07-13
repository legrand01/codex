"""Tests for baseline and advisory read APIs."""

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


def _baseline(run_id, host_id):
    return {
        "id": uuid4(),
        "organization_id": uuid4(),
        "run_id": run_id,
        "host_id": host_id,
        "workload_fingerprint_id": None,
        "status": "advisory_only",
        "objective_type": "system_wide_aqr",
        "objective_formula": "sum(runtime) / sum(calls)",
        "objective_direction": "minimize",
        "objective_score": 12.5,
        "metric_units": '{"objective_score":"ms"}',
        "fingerprint_membership": "[]",
        "warmup_window_seconds": 60,
        "requested_measurement_window_seconds": 300,
        "observed_measurement_window_seconds": 300.0,
        "workload_coverage_pct": 99.0,
        "runtime_variance_pct": 2.0,
        "safety_metrics": "{}",
        "evidence_references": "[]",
        "root_cause_category": "query_index",
        "root_cause_confidence": 0.8,
        "root_cause_summary": "Query work comes first.",
        "root_cause_details": "{}",
        "warnings": "[]",
        "captured_at": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc),
    }


@pytest.mark.asyncio
async def test_get_baseline_returns_comparable_protocol(client):
    run_id = uuid4()
    host_id = uuid4()
    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value=_baseline(run_id, host_id))
    _override(db)
    try:
        response = await client.get(f"/api/v1/runs/{run_id}/baseline")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["objective_score"] == 12.5
    assert data["metric_units"]["objective_score"] == "ms"
    assert data["root_cause_category"] == "query_index"
    assert "r.organization_id" in db.fetchrow.await_args.args[0]


@pytest.mark.asyncio
async def test_get_baseline_not_found(client):
    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value=None)
    _override(db)
    try:
        response = await client.get(f"/api/v1/runs/{uuid4()}/baseline")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_advisories_are_explicitly_non_executable(client):
    run_id = uuid4()
    host_id = uuid4()
    db = AsyncMock()
    db.fetch = AsyncMock(
        return_value=[
            {
                "id": uuid4(),
                "run_id": run_id,
                "host_id": host_id,
                "category": "query_index",
                "severity": "warning",
                "title": "Inspect the dominant query",
                "summary": "{}",
                "recommendations": '["Capture EXPLAIN"]',
                "evidence_references": "[]",
                "executable": False,
                "created_at": datetime.now(timezone.utc),
            }
        ]
    )
    _override(db)
    try:
        response = await client.get(f"/api/v1/runs/{run_id}/advisories")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()[0]["executable"] is False
    assert response.json()[0]["recommendations"] == ["Capture EXPLAIN"]
    assert "r.organization_id" in db.fetch.await_args.args[0]
