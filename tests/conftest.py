"""
Shared test fixtures and configuration for the test suite.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from hypothesis import settings as hypothesis_settings

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


@pytest.fixture
async def client():
    """Create an async test client for the FastAPI application."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
