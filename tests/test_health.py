"""
Tests for the health check endpoint and basic app configuration.
"""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app


@pytest.fixture
async def client():
    """Create an async test client for the FastAPI application."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_health_endpoint_returns_200(client):
    """Health check endpoint should return HTTP 200 with healthy status."""
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "autonomous-postgres-dba-agent"


@pytest.mark.asyncio
async def test_liveness_does_not_claim_dependencies_are_ready(client):
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"


@pytest.mark.asyncio
async def test_readiness_returns_503_without_initialized_dependencies(client):
    response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


@pytest.mark.asyncio
async def test_readiness_returns_200_when_dependencies_respond(client, monkeypatch):
    from backend.services import operational_health

    pool = AsyncMock()
    pool.fetchval.return_value = 1
    redis = AsyncMock()
    redis.ping.return_value = True
    monkeypatch.setattr(operational_health, "get_pool", lambda: pool)
    monkeypatch.setattr(operational_health, "get_redis_client", lambda: redis)

    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_metrics_remains_scrapeable_during_dependency_outage(client):
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "dbtune_up 1.0" in response.text
    assert "dbtune_postgres_up 0.0" in response.text


@pytest.mark.asyncio
async def test_root_endpoint(client):
    """Root endpoint should return API information."""
    response = await client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Autonomous Postgres DBA Agent Platform"
    assert data["version"] == "0.1.0"
    assert data["docs"] == "/docs"
