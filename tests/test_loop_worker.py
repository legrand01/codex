"""
Tests for the DBA Loop Worker service.

Tests cover:
- Goal decomposition (step count limits)
- Core execution engine behavior
- Halt functionality
- Error handling (evidence collection failure, retry logic)
- Unresponsive detection
- Approval gate and guardrail integration
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from backend.models.config import LoopConfig
from backend.models.enums import WorkflowStep
from backend.services.loop_worker import (
    UNRESPONSIVE_TIMEOUT_SECONDS,
    DBALoopWorker,
    RunResult,
    StepResult,
)

# --- Goal Decomposition Tests ---


class TestGoalDecomposition:
    """Tests for DBALoopWorker.decompose_goal()."""

    def test_decompose_standard_goal(self):
        """Standard goal produces full workflow cycle."""
        worker = DBALoopWorker()
        steps = worker.decompose_goal("Optimize query performance", max_steps=20)
        assert len(steps) == 12  # Full cycle has 12 steps
        assert steps[0] == WorkflowStep.OBSERVE
        assert steps[-1] == WorkflowStep.REPORT

    def test_decompose_respects_max_steps(self):
        """Goal decomposition respects max_steps limit."""
        worker = DBALoopWorker()
        steps = worker.decompose_goal("Optimize query performance", max_steps=5)
        assert len(steps) == 5
        assert len(steps) <= 5

    def test_decompose_max_steps_of_one(self):
        """With max_steps=1, only the first step is returned."""
        worker = DBALoopWorker()
        steps = worker.decompose_goal("Optimize query performance", max_steps=1)
        assert len(steps) == 1
        assert steps[0] == WorkflowStep.OBSERVE

    def test_decompose_empty_goal(self):
        """Empty goal returns minimal step list."""
        worker = DBALoopWorker()
        steps = worker.decompose_goal("", max_steps=20)
        assert len(steps) >= 1
        assert steps[0] == WorkflowStep.REPORT

    def test_decompose_whitespace_goal(self):
        """Whitespace-only goal returns minimal step list."""
        worker = DBALoopWorker()
        steps = worker.decompose_goal("   ", max_steps=20)
        assert len(steps) >= 1

    def test_decompose_always_returns_positive_count(self):
        """Decomposition always returns at least 1 step."""
        worker = DBALoopWorker()
        for max_steps in [1, 5, 10, 20, 100]:
            steps = worker.decompose_goal("Test goal", max_steps=max_steps)
            assert len(steps) > 0
            assert len(steps) <= max_steps


# --- Halt Functionality Tests ---


class TestHaltRun:
    """Tests for DBALoopWorker.halt_run()."""

    @pytest.mark.asyncio
    async def test_halt_nonexistent_run(self):
        """Halting a non-existent run returns not_found."""
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        worker = DBALoopWorker(pool=mock_pool)
        result = await worker.halt_run(uuid4())
        assert result["success"] is False
        assert "not found" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_halt_completed_run_rejected(self):
        """Halting a completed run is rejected."""
        run_id = uuid4()
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": run_id,
                "status": "completed",
                "current_step": "report",
            }
        )
        mock_pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        worker = DBALoopWorker(pool=mock_pool)
        result = await worker.halt_run(run_id)
        assert result["success"] is False
        assert "no longer active" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_halt_failed_run_rejected(self):
        """Halting a failed run is rejected."""
        run_id = uuid4()
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": run_id,
                "status": "failed",
                "current_step": "observe",
            }
        )
        mock_pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        worker = DBALoopWorker(pool=mock_pool)
        result = await worker.halt_run(run_id)
        assert result["success"] is False
        assert "no longer active" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_halt_manually_halted_run_rejected(self):
        """Halting an already halted run is rejected."""
        run_id = uuid4()
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": run_id,
                "status": "manually_halted",
                "current_step": "diagnose",
            }
        )
        mock_pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        worker = DBALoopWorker(pool=mock_pool)
        result = await worker.halt_run(run_id)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_halt_running_run_succeeds(self):
        """Halting a running run succeeds and updates status."""
        run_id = uuid4()
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": run_id,
                "status": "running",
                "current_step": "observe",
            }
        )
        mock_conn.execute = AsyncMock()
        mock_pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        mock_audit = AsyncMock()
        mock_audit.log = AsyncMock()

        worker = DBALoopWorker(pool=mock_pool, audit_logger=mock_audit)
        result = await worker.halt_run(run_id)
        assert result["success"] is True
        assert result["status"] == "manually_halted"
        assert worker._halted is True


# --- Unresponsive Detection Tests ---


class TestUnresponsiveDetection:
    """Tests for unresponsive run detection."""

    def test_worker_not_unresponsive_initially(self):
        """A fresh worker is not unresponsive."""
        worker = DBALoopWorker()
        assert worker.is_unresponsive() is False

    def test_worker_becomes_unresponsive(self):
        """Worker becomes unresponsive after timeout."""
        worker = DBALoopWorker()
        # Simulate time passing by setting last transition to past
        worker._last_step_transition = time.monotonic() - (UNRESPONSIVE_TIMEOUT_SECONDS + 1)
        assert worker.is_unresponsive() is True

    def test_heartbeat_resets_unresponsive(self):
        """Updating heartbeat resets unresponsive status."""
        worker = DBALoopWorker()
        worker._last_step_transition = time.monotonic() - (UNRESPONSIVE_TIMEOUT_SECONDS + 1)
        assert worker.is_unresponsive() is True
        worker._update_heartbeat()
        assert worker.is_unresponsive() is False


# --- Evidence Collection Tests ---


class TestEvidenceCollection:
    """Tests for evidence collection with retry logic."""

    @pytest.mark.asyncio
    async def test_evidence_collection_success(self):
        """Successful evidence collection returns positive result."""
        run_id = uuid4()
        host_id = uuid4()

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": uuid4(),
                    "evidence_type": "pg_settings",
                    "collected_at": "2024-01-01",
                    "data": {},
                    "quality_score": 0.9,
                },
            ]
        )
        mock_pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        worker = DBALoopWorker(pool=mock_pool)
        result = await worker._collect_evidence(run_id, host_id)
        assert result.success is True
        assert result.data["snapshots_collected"] == 1

    @pytest.mark.asyncio
    async def test_evidence_collection_retry_on_failure(self):
        """Evidence collection retries once on failure."""
        run_id = uuid4()
        host_id = uuid4()

        call_count = 0

        mock_pool = MagicMock()
        mock_conn = AsyncMock()

        async def mock_fetch(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Connection error")
            return [
                {
                    "id": uuid4(),
                    "evidence_type": "pg_settings",
                    "collected_at": "2024-01-01",
                    "data": {},
                    "quality_score": 0.9,
                },
            ]

        mock_conn.fetch = mock_fetch
        mock_pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        worker = DBALoopWorker(pool=mock_pool)
        # Patch the sleep to avoid waiting
        with patch("backend.services.loop_worker.asyncio.sleep", new_callable=AsyncMock):
            result = await worker._collect_evidence(run_id, host_id)
        assert result.success is True
        assert call_count == 2  # First attempt + retry

    @pytest.mark.asyncio
    async def test_evidence_collection_fails_after_retry(self):
        """Evidence collection fails if retry also fails."""
        run_id = uuid4()
        host_id = uuid4()

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(side_effect=Exception("Persistent error"))
        mock_pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        worker = DBALoopWorker(pool=mock_pool)
        with patch("backend.services.loop_worker.asyncio.sleep", new_callable=AsyncMock):
            result = await worker._collect_evidence(run_id, host_id)
        assert result.success is False
        assert "failed after retry" in result.error.lower()


# --- Start Run Tests ---


class TestStartRun:
    """Tests for the start_run execution engine."""

    @pytest.mark.asyncio
    async def test_start_run_creates_record(self):
        """Starting a run creates a database record and executes."""
        host_id = uuid4()

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"id": host_id})
        # For evidence collection
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        mock_audit = AsyncMock()
        mock_audit.log = AsyncMock()

        worker = DBALoopWorker(pool=mock_pool, audit_logger=mock_audit)
        config = LoopConfig(max_iterations=1, max_steps=2)

        # Patch sleep and redis to avoid delays
        with patch("backend.services.loop_worker.asyncio.sleep", new_callable=AsyncMock):
            with patch("backend.db.redis_manager.get_redis_client", return_value=None):
                result = await worker.start_run(goal="Test goal", config=config, host_id=host_id)

        assert isinstance(result, RunResult)
        assert result.goal == "Test goal"
        assert result.status in ("completed", "failed")
        # Verify audit log was called for run start
        assert mock_audit.log.called

    @pytest.mark.asyncio
    async def test_start_run_limited_iterations(self):
        """Start run respects max_iterations limit."""
        host_id = uuid4()

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"id": host_id})
        mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": uuid4(),
                    "evidence_type": "pg_settings",
                    "collected_at": "2024-01-01",
                    "data": {},
                    "quality_score": 0.9,
                },
            ]
        )
        mock_pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        mock_audit = AsyncMock()
        mock_audit.log = AsyncMock()

        worker = DBALoopWorker(pool=mock_pool, audit_logger=mock_audit)
        config = LoopConfig(max_iterations=2, max_steps=1)  # Only observe step

        with patch("backend.services.loop_worker.asyncio.sleep", new_callable=AsyncMock):
            with patch("backend.db.redis_manager.get_redis_client", return_value=None):
                result = await worker.start_run(
                    goal="Test iterations", config=config, host_id=host_id
                )

        assert result.iterations_completed <= config.max_iterations

    @pytest.mark.asyncio
    async def test_start_run_non_actionable_plan_completes_without_approval(self):
        """Non-actionable diagnostics complete instead of waiting at approval."""
        host_id = uuid4()

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        mock_audit = AsyncMock()
        mock_audit.log = AsyncMock()

        worker = DBALoopWorker(pool=mock_pool, audit_logger=mock_audit)
        config = LoopConfig(max_iterations=3, max_steps=10)
        propose_result = StepResult(
            step=WorkflowStep.PROPOSE_PLAN,
            success=True,
            data={
                "is_actionable": False,
                "diagnostic_summary": "0 recommendation(s) identified.",
                "uncertainty_explanation": "No actionable recommendations.",
            },
        )
        propose_mock = AsyncMock(return_value=propose_result)
        guardrail_mock = AsyncMock()
        approval_mock = AsyncMock()

        with patch.object(
            worker,
            "decompose_goal",
            return_value=[
                WorkflowStep.PROPOSE_PLAN,
                WorkflowStep.SAFETY_CHECK,
                WorkflowStep.APPROVAL_GATE,
            ],
        ):
            with patch.object(worker, "_propose_plan", propose_mock):
                with patch.object(worker, "_submit_to_guardrail", guardrail_mock):
                    with patch.object(worker, "_wait_for_approval", approval_mock):
                        with patch("backend.db.redis_manager.get_redis_client", return_value=None):
                            result = await worker.start_run(
                                goal="Find slow query patterns",
                                config=config,
                                host_id=host_id,
                            )

        assert result.status == "completed"
        assert result.failure_reason is None
        guardrail_mock.assert_not_called()
        approval_mock.assert_not_called()
        assert any(
            call.kwargs.get("action_type") == "diagnostic_only_completed"
            for call in mock_audit.log.call_args_list
        )

    @pytest.mark.asyncio
    async def test_start_run_safety_check_uses_generated_plan_changes(self):
        """Safety check receives the plan produced by the planner."""
        host_id = uuid4()

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        mock_audit = AsyncMock()
        mock_audit.log = AsyncMock()

        worker = DBALoopWorker(pool=mock_pool, audit_logger=mock_audit)
        config = LoopConfig(max_iterations=1, max_steps=5)
        proposed_changes = [
            {
                "change_type": "setting",
                "setting_name": "work_mem",
                "proposed_value": "64MB",
            }
        ]
        rollback_instructions = [{"setting_name": "work_mem", "rollback_value": "4MB"}]
        propose_result = StepResult(
            step=WorkflowStep.PROPOSE_PLAN,
            success=True,
            data={
                "is_actionable": True,
                "plan_id": str(uuid4()),
                "proposed_changes": proposed_changes,
                "rollback_instructions": rollback_instructions,
            },
        )
        guardrail_result = StepResult(
            step=WorkflowStep.SAFETY_CHECK,
            success=True,
            data={"safety_check": "passed"},
        )
        guardrail_mock = AsyncMock(return_value=guardrail_result)

        with patch.object(
            worker,
            "decompose_goal",
            return_value=[WorkflowStep.PROPOSE_PLAN, WorkflowStep.SAFETY_CHECK],
        ):
            with patch.object(worker, "_propose_plan", AsyncMock(return_value=propose_result)):
                with patch.object(worker, "_submit_to_guardrail", guardrail_mock):
                    with patch("backend.db.redis_manager.get_redis_client", return_value=None):
                        result = await worker.start_run(
                            goal="Tune work_mem",
                            config=config,
                            host_id=host_id,
                        )

        assert result.status == "completed"
        guardrail_mock.assert_awaited_once()
        kwargs = guardrail_mock.await_args.kwargs
        assert kwargs["proposed_changes"] == proposed_changes
        assert kwargs["rollback_instructions"] == rollback_instructions


# --- Guardrail Integration Tests ---


class TestGuardrailIntegration:
    """Tests for approval gate and guardrail integration."""

    @pytest.mark.asyncio
    async def test_approval_gate_without_plan_fails_fast(self):
        """Approval gate does not wait forever when no plan exists."""
        run_id = uuid4()
        host_id = uuid4()

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        mock_audit = AsyncMock()
        mock_audit.log = AsyncMock()

        worker = DBALoopWorker(pool=mock_pool, audit_logger=mock_audit)
        result = await worker._wait_for_approval(
            run_id=run_id,
            host_id=host_id,
            config=LoopConfig(),
        )

        assert result.success is False
        assert "without a plan" in result.error
        assert any(
            call.kwargs.get("action_type") == "approval_gate_without_plan"
            for call in mock_audit.log.call_args_list
        )

    @pytest.mark.asyncio
    async def test_guardrail_rejection_stops_execution(self):
        """Guardrail rejection stops execution and records failure."""
        run_id = uuid4()
        host_id = uuid4()

        from backend.services.guardrail_engine import SafetyCheckResult

        mock_safety_result = SafetyCheckResult(
            passed=False,
            stage_results={"allowlist": {"passed": False}},
            blocked_at_stage="allowlist",
            errors=["Allowlist is empty"],
        )

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        mock_audit = AsyncMock()
        mock_audit.log = AsyncMock()

        worker = DBALoopWorker(pool=mock_pool, audit_logger=mock_audit)
        config = LoopConfig()

        with patch(
            "backend.services.guardrail_engine.full_safety_check",
            new_callable=AsyncMock,
            return_value=mock_safety_result,
        ):
            result = await worker._submit_to_guardrail(
                run_id=run_id,
                host_id=host_id,
                proposed_changes=[{"setting_name": "work_mem"}],
                rollback_instructions=[],
                pre_snapshot={},
                config=config,
            )

        assert result.success is False
        assert "guardrail" in result.error.lower() or "allowlist" in result.error.lower()
        # Verify audit log recorded the failure
        assert mock_audit.log.called
