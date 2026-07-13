"""Integration boundary tests for the durable baseline gate."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from backend.models.config import LoopConfig
from backend.models.enums import WorkflowStep
from backend.services.durable_run_orchestrator import DurableRunOrchestrator
from backend.services.loop_worker import StepResult


@pytest.mark.asyncio
async def test_non_configuration_root_cause_never_reaches_plan_generation():
    run_id = uuid4()
    host_id = uuid4()
    run = {"id": run_id, "host_id": host_id, "goal": "Improve checkout latency"}
    audit = MagicMock()
    audit.log = AsyncMock()
    orchestrator = DurableRunOrchestrator(MagicMock(), audit_logger=audit)
    orchestrator._set_run = AsyncMock()
    orchestrator._complete_run = AsyncMock()

    baseline = {
        "id": uuid4(),
        "status": "advisory_only",
        "objective_type": "recommended_fingerprint",
        "objective_score": 12.4,
        "workload_coverage_pct": 96.0,
        "runtime_variance_pct": 3.0,
        "root_cause_category": "query_index",
        "root_cause_summary": "Inspect the dominant query before settings.",
    }
    with (
        patch(
            "backend.services.durable_run_orchestrator.capture_baseline",
            AsyncMock(return_value=baseline),
        ),
        patch(
            "backend.services.loop_worker.DBALoopWorker._collect_evidence",
            AsyncMock(
                return_value=StepResult(
                    step=WorkflowStep.OBSERVE, success=True, data={}
                )
            ),
        ),
        patch(
            "backend.services.loop_worker.DBALoopWorker._capture_target_snapshot",
            AsyncMock(
                return_value=StepResult(
                    step=WorkflowStep.SNAPSHOT,
                    success=True,
                    data={"pre_change_snapshot": {}},
                )
            ),
        ) as capture_target,
        patch(
            "backend.services.loop_worker.DBALoopWorker._propose_plan",
            AsyncMock(),
        ) as propose,
    ):
        result = await orchestrator._prepare_plan(run, LoopConfig())

    assert result.disposition == "completed"
    capture_target.assert_not_awaited()
    propose.assert_not_awaited()
    orchestrator._complete_run.assert_awaited_once_with(run_id, host_id)
    advisory_logs = [
        call
        for call in audit.log.await_args_list
        if call.kwargs.get("action_type") == "non_configuration_advisory_created"
    ]
    assert advisory_logs
    assert advisory_logs[0].kwargs["details"]["configuration_changes_proposed"] is False
