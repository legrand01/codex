"""P0 identity-boundary and production fail-closed tests."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from backend.config import settings
from backend.main import app, lifespan
from backend.security import hash_token, require_agent


@pytest.mark.asyncio
async def test_http_api_requires_valid_bearer_token(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "bootstrap_admin_token", "a" * 32)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/api/v1/demo/status")
        allowed = await client.get(
            "/api/v1/demo/status",
            headers={"Authorization": f"Bearer {'a' * 32}"},
        )
        health = await client.get("/health")

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert health.status_code == 200


@pytest.mark.asyncio
async def test_agent_token_is_bound_to_host(monkeypatch):
    host_id = uuid4()
    token = "agent-secret"
    monkeypatch.setattr(settings, "agent_auth_required", True)
    db = AsyncMock()
    db.fetchrow.return_value = {"agent_token_hash": hash_token(token)}

    assert await require_agent(host_id, token, db) == host_id
    with pytest.raises(HTTPException) as exc:
        await require_agent(host_id, "wrong-token", db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_production_startup_fails_when_database_validation_fails(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    with patch(
        "backend.db.pool.create_pool",
        new_callable=AsyncMock,
        side_effect=RuntimeError("database unavailable"),
    ):
        with pytest.raises(RuntimeError, match="database unavailable"):
            async with lifespan(app):
                pass
