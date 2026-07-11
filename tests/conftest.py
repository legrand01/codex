"""
Shared test fixtures and configuration for the test suite.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from hypothesis import settings as hypothesis_settings

from backend.config import settings as app_settings
from backend.main import app

# Configure Hypothesis for property-based tests
hypothesis_settings.register_profile(
    "ci",
    max_examples=100,
    deadline=5000,
)
hypothesis_settings.register_profile(
    "dev",
    max_examples=20,
    deadline=5000,
)
hypothesis_settings.load_profile("ci")


@pytest.fixture(autouse=True)
def legacy_metadata_dry_run(monkeypatch):
    """Legacy unit fixtures do not represent a real target PostgreSQL server.

    Production defaults to a live transactional dry-run.  Existing isolated
    guardrail tests exercise the metadata validator; dedicated executor and
    integration tests cover the live path.
    """
    monkeypatch.setattr(app_settings, "require_live_target_dry_run", False)
    monkeypatch.setattr(app_settings, "require_live_target_rollback", False)
    monkeypatch.setattr(app_settings, "auth_required", False)
    monkeypatch.setattr(app_settings, "agent_auth_required", False)


@pytest.fixture
async def client():
    """Create an async test client for the FastAPI application."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
