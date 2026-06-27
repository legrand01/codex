"""
Tests for the health check endpoint and basic app configuration.
"""

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
async def test_root_endpoint(client):
    """Root endpoint should return API information."""
    response = await client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Autonomous Postgres DBA Agent Platform"
    assert data["version"] == "0.1.0"
    assert data["docs"] == "/docs"
