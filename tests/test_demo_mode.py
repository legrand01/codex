"""
Unit tests for Demo Mode service and API endpoints.

Tests cover:
- Demo mode activation and data seeding (Task 16.1)
- Demo mode deactivation and cleanup
- Connection blocking for non-synthetic hosts (Task 16.2)
- API endpoint behavior
- Data validation for seeded demo content

Requirements: 14.1, 14.2, 14.3, 14.4, 14.6
"""

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.models.enums import (
    ConnectionStatus,
    HealthStatus,
    PlanStatus,
)
from backend.services.demo_mode import (
    SYNTHETIC_HOST_ADDRESSES,
    activate_demo_mode,
    block_non_synthetic_connection,
    deactivate_demo_mode,
    get_demo_data,
    get_demo_status,
    is_demo_active,
    is_synthetic_address,
)

# =============================================================================
# Helper to reset demo mode state between tests
# =============================================================================


@pytest.fixture(autouse=True)
def reset_demo_state():
    """Ensure demo mode is deactivated before and after each test."""
    import backend.services.demo_mode as dm

    dm._demo_active = False
    dm._demo_data = {}
    yield
    dm._demo_active = False
    dm._demo_data = {}


@pytest.fixture
async def client():
    """Create an async test client for the FastAPI application."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# =============================================================================
# Tests for is_demo_active
# =============================================================================


class TestIsDemoActive:
    """Tests for demo mode status checking."""

    def test_demo_not_active_by_default(self):
        """Demo mode should not be active by default."""
        assert is_demo_active() is False

    def test_demo_active_after_activation(self):
        """Demo mode should be active after activation."""
        activate_demo_mode()
        assert is_demo_active() is True

    def test_demo_not_active_after_deactivation(self):
        """Demo mode should not be active after deactivation."""
        activate_demo_mode()
        deactivate_demo_mode()
        assert is_demo_active() is False


# =============================================================================
# Tests for activate_demo_mode (Task 16.1)
# =============================================================================


class TestActivateDemoMode:
    """Tests for demo mode activation and data seeding."""

    def test_activation_returns_success_status(self):
        """Activation should return status 'activated'."""
        result = activate_demo_mode()
        assert result["status"] == "activated"

    def test_activation_seeds_at_least_3_hosts(self):
        """
        Requirement 14.1: Seed fleet with at least 3 PostgreSQL hosts.
        """
        result = activate_demo_mode()
        assert result["summary"]["hosts_seeded"] >= 3

    def test_activation_seeds_all_connection_statuses(self):
        """
        Requirement 14.1: Hosts represent each connection status
        (connected, disconnected, degraded).
        """
        activate_demo_mode()
        data = get_demo_data()
        hosts = data["hosts"]

        statuses = {h["connection_status"] for h in hosts}
        assert ConnectionStatus.CONNECTED.value in statuses
        assert ConnectionStatus.DEGRADED.value in statuses
        assert ConnectionStatus.DISCONNECTED.value in statuses

    def test_activation_seeds_healthy_and_unhealthy_hosts(self):
        """
        Requirement 14.1: At least one host in each health state
        (healthy, unhealthy).
        """
        activate_demo_mode()
        data = get_demo_data()
        hosts = data["hosts"]

        health_statuses = {h["health_status"] for h in hosts}
        assert HealthStatus.HEALTHY.value in health_statuses
        assert HealthStatus.UNHEALTHY.value in health_statuses

    def test_activation_seeds_synthetic_evidence(self):
        """
        Requirement 14.2: Generate synthetic Evidence with required categories.
        """
        activate_demo_mode()
        data = get_demo_data()
        evidence = data["evidence"]

        evidence_types = {e["evidence_type"] for e in evidence}

        # Must have slow queries (pg_stat_statements)
        assert "pg_stat_statements" in evidence_types
        # Must have config drift (pg_settings)
        assert "pg_settings" in evidence_types
        # Must have replication lag
        assert "replication" in evidence_types
        # Must have checkpoint pressure
        assert "wal_checkpoint" in evidence_types
        # Must have weak evidence (locks with low quality score)
        assert "locks" in evidence_types

    def test_activation_seeds_weak_evidence_below_threshold(self):
        """
        Requirement 14.2: Include weak-evidence cases below
        Evidence_Quality_Threshold.
        """
        activate_demo_mode()
        data = get_demo_data()
        evidence = data["evidence"]

        # At least one evidence snapshot should have quality_score below 0.5
        low_quality = [
            e for e in evidence if e["quality_score"] is not None and e["quality_score"] < 0.5
        ]
        assert len(low_quality) >= 1

    def test_activation_seeds_loop_runs_with_success_and_blocked(self):
        """
        Requirement 14.3: Produce at least one successful and one
        blocked/inconclusive outcome.
        """
        activate_demo_mode()
        data = get_demo_data()
        runs = data["runs"]

        statuses = [r["status"] for r in runs]
        assert "completed" in statuses, "Must have at least one successful run"
        assert "failed" in statuses, "Must have at least one blocked/failed run"

    def test_activation_seeds_plan_requiring_approval_gate(self):
        """
        Requirement 14.6: Generate at least one Plan requiring
        Approval_Gate interaction.
        """
        activate_demo_mode()
        data = get_demo_data()
        plans = data["plans"]

        pending_plans = [p for p in plans if p["status"] == PlanStatus.PENDING_APPROVAL.value]
        assert len(pending_plans) >= 1

    def test_activation_raises_if_already_active(self):
        """Activating demo mode when already active should raise RuntimeError."""
        activate_demo_mode()
        with pytest.raises(RuntimeError, match="already active"):
            activate_demo_mode()

    def test_activation_includes_activated_at_timestamp(self):
        """Activation result should include a timestamp."""
        result = activate_demo_mode()
        assert "activated_at" in result
        assert result["activated_at"] is not None

    def test_activation_seeds_audit_entries(self):
        """Activation should seed audit log entries."""
        activate_demo_mode()
        data = get_demo_data()
        assert len(data["audit_entries"]) > 0


# =============================================================================
# Tests for deactivate_demo_mode
# =============================================================================


class TestDeactivateDemoMode:
    """Tests for demo mode deactivation."""

    def test_deactivation_returns_success_status(self):
        """Deactivation should return status 'deactivated'."""
        activate_demo_mode()
        result = deactivate_demo_mode()
        assert result["status"] == "deactivated"

    def test_deactivation_clears_demo_data(self):
        """Deactivation should clear all demo data."""
        activate_demo_mode()
        deactivate_demo_mode()
        assert get_demo_data() == {}

    def test_deactivation_raises_if_not_active(self):
        """Deactivating when not active should raise RuntimeError."""
        with pytest.raises(RuntimeError, match="not currently active"):
            deactivate_demo_mode()

    def test_deactivation_includes_timestamps(self):
        """Deactivation result should include deactivated_at timestamp."""
        activate_demo_mode()
        result = deactivate_demo_mode()
        assert "deactivated_at" in result


# =============================================================================
# Tests for connection blocking (Task 16.2)
# =============================================================================


class TestConnectionBlocking:
    """Tests for Demo Mode connection isolation."""

    def test_non_synthetic_connection_blocked_when_demo_active(self):
        """
        Requirement 14.4: Block connections to non-synthetic hosts
        while demo mode is active.
        """
        activate_demo_mode()
        with pytest.raises(ConnectionRefusedError, match="non-synthetic host"):
            block_non_synthetic_connection("real-production-db.example.com")

    def test_synthetic_connection_allowed_when_demo_active(self):
        """Synthetic host connections should be allowed during demo mode."""
        activate_demo_mode()
        result = block_non_synthetic_connection("demo-pg-primary-01.synthetic.local")
        assert result is True

    def test_any_connection_allowed_when_demo_inactive(self):
        """When demo mode is not active, all connections should be allowed."""
        result = block_non_synthetic_connection("real-production-db.example.com")
        assert result is True

    def test_all_synthetic_addresses_are_allowed(self):
        """All defined synthetic addresses should pass the check."""
        activate_demo_mode()
        for addr in SYNTHETIC_HOST_ADDRESSES:
            result = block_non_synthetic_connection(addr)
            assert result is True

    def test_is_synthetic_address_true_for_synthetic(self):
        """is_synthetic_address should return True for synthetic hosts."""
        assert is_synthetic_address("demo-pg-primary-01.synthetic.local") is True

    def test_is_synthetic_address_false_for_real(self):
        """is_synthetic_address should return False for non-synthetic hosts."""
        assert is_synthetic_address("production-db.company.com") is False


# =============================================================================
# Tests for get_demo_status
# =============================================================================


class TestGetDemoStatus:
    """Tests for demo status reporting."""

    def test_status_when_inactive(self):
        """Status should report inactive when demo is off."""
        status = get_demo_status()
        assert status["active"] is False

    def test_status_when_active(self):
        """Status should report active with summary when demo is on."""
        activate_demo_mode()
        status = get_demo_status()
        assert status["active"] is True
        assert "summary" in status
        assert status["summary"]["hosts_seeded"] >= 3


# =============================================================================
# Tests for API endpoints (Task 16.2)
# =============================================================================


class TestDemoAPI:
    """Tests for demo mode API endpoints."""

    @pytest.mark.asyncio
    async def test_activate_endpoint(self, client):
        """POST /api/v1/demo/activate should activate demo mode."""
        response = await client.post("/api/v1/demo/activate")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "activated"
        assert data["summary"]["hosts_seeded"] >= 3

    @pytest.mark.asyncio
    async def test_activate_endpoint_conflict_when_already_active(self, client):
        """POST /api/v1/demo/activate should return 409 if already active."""
        await client.post("/api/v1/demo/activate")
        response = await client.post("/api/v1/demo/activate")
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_deactivate_endpoint(self, client):
        """POST /api/v1/demo/deactivate should deactivate demo mode."""
        await client.post("/api/v1/demo/activate")
        response = await client.post("/api/v1/demo/deactivate")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "deactivated"

    @pytest.mark.asyncio
    async def test_deactivate_endpoint_conflict_when_not_active(self, client):
        """POST /api/v1/demo/deactivate should return 409 if not active."""
        response = await client.post("/api/v1/demo/deactivate")
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_status_endpoint_inactive(self, client):
        """GET /api/v1/demo/status should return inactive status."""
        response = await client.get("/api/v1/demo/status")
        assert response.status_code == 200
        data = response.json()
        assert data["active"] is False

    @pytest.mark.asyncio
    async def test_status_endpoint_active(self, client):
        """GET /api/v1/demo/status should return active status with summary."""
        await client.post("/api/v1/demo/activate")
        response = await client.get("/api/v1/demo/status")
        assert response.status_code == 200
        data = response.json()
        assert data["active"] is True
        assert "summary" in data

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, client):
        """Test activate → status → deactivate → status lifecycle."""
        # Initially inactive
        response = await client.get("/api/v1/demo/status")
        assert response.json()["active"] is False

        # Activate
        response = await client.post("/api/v1/demo/activate")
        assert response.status_code == 200

        # Verify active
        response = await client.get("/api/v1/demo/status")
        assert response.json()["active"] is True

        # Deactivate
        response = await client.post("/api/v1/demo/deactivate")
        assert response.status_code == 200

        # Verify inactive again
        response = await client.get("/api/v1/demo/status")
        assert response.json()["active"] is False


# =============================================================================
# Tests for demo data quality and structure
# =============================================================================


class TestDemoDataQuality:
    """Tests for the quality and completeness of seeded demo data."""

    def test_all_hosts_have_required_fields(self):
        """Every seeded host must have all required fields."""
        activate_demo_mode()
        data = get_demo_data()
        for host in data["hosts"]:
            assert "id" in host
            assert "hostname" in host
            assert "pg_version" in host
            assert "server_role" in host
            assert "health_status" in host
            assert "connection_status" in host
            assert "last_heartbeat" in host

    def test_all_evidence_has_required_fields(self):
        """Every evidence snapshot must have required fields."""
        activate_demo_mode()
        data = get_demo_data()
        for ev in data["evidence"]:
            assert "id" in ev
            assert "run_id" in ev
            assert "host_id" in ev
            assert "evidence_type" in ev
            assert "collected_at" in ev
            assert "data" in ev
            assert "quality_score" in ev

    def test_all_plans_have_rollback_instructions(self):
        """Every plan must have rollback instructions (Req 7.5)."""
        activate_demo_mode()
        data = get_demo_data()
        for plan in data["plans"]:
            assert "rollback_instructions" in plan
            assert len(plan["rollback_instructions"]) > 0
            # Each rollback instruction must have required fields
            for instruction in plan["rollback_instructions"]:
                assert "setting" in instruction
                assert "restore_value" in instruction

    def test_plans_have_evidence_references(self):
        """Every plan must reference evidence snapshots."""
        activate_demo_mode()
        data = get_demo_data()
        for plan in data["plans"]:
            assert "evidence_references" in plan
            assert len(plan["evidence_references"]) > 0

    def test_runs_have_valid_workflow_steps(self):
        """Loop runs must have valid workflow step values."""
        activate_demo_mode()
        data = get_demo_data()
        valid_steps = {
            step.value
            for step in __import__("backend.models.enums", fromlist=["WorkflowStep"]).WorkflowStep
        }
        for run in data["runs"]:
            assert run["current_step"] in valid_steps

    def test_hosts_have_valid_server_roles(self):
        """All hosts must have valid server roles."""
        activate_demo_mode()
        data = get_demo_data()
        for host in data["hosts"]:
            assert host["server_role"] in ("primary", "replica")

    def test_risk_scores_within_valid_range(self):
        """All plan risk scores must be between 0 and 100."""
        activate_demo_mode()
        data = get_demo_data()
        for plan in data["plans"]:
            assert 0 <= plan["risk_score"] <= 100

    def test_confidence_scores_within_valid_range(self):
        """All plan confidence scores must be between 0.0 and 1.0."""
        activate_demo_mode()
        data = get_demo_data()
        for plan in data["plans"]:
            assert 0.0 <= plan["confidence_score"] <= 1.0
