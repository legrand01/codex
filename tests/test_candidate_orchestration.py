"""Durable orchestration tests for measured candidate keep/rollback."""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from backend.models.config import LoopConfig
from backend.services.candidate_optimizer import CandidateDecision
from backend.services.durable_run_orchestrator import (
    DurableRunOrchestrator,
    RunProcessResult,
)


def _pool():
    pool = MagicMock()
    conn = AsyncMock()
    context = AsyncMock()
    context.__aenter__.return_value = conn
    context.__aexit__.return_value = False
    pool.acquire.return_value = context
    return pool, conn


def _run(run_id, host_id):
    return {
        "id": run_id,
        "host_id": host_id,
        "degradation_threshold_pct": 10,
        "objective_guardrails": {},
    }


def _plan(run_id, host_id):
    return {
        "id": uuid4(),
        "run_id": run_id,
        "host_id": host_id,
        "status": "applied",
        "proposed_changes": json.dumps(
            [{"setting_name": "work_mem", "proposed_value": "128kB"}]
        ),
        "pre_change_snapshot": json.dumps(
            {
                "work_mem": {
                    "value": "64kB",
                    "unit": "kB",
                    "context": "user",
                    "source": "configuration file",
                    "sourcefile": None,
                    "pending_restart": False,
                    "in_auto_conf": False,
                }
            }
        ),
        "applied_at": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("decision_name", ["rolled_back", "inconclusive"])
async def test_non_beneficial_candidate_restores_best_before_continuing(decision_name):
    run_id = uuid4()
    host_id = uuid4()
    plan = _plan(run_id, host_id)
    pool, conn = _pool()
    audit = MagicMock(log=AsyncMock())
    orchestrator = DurableRunOrchestrator(pool, audit_logger=audit)
    orchestrator._cancelled = AsyncMock(return_value=False)
    orchestrator._set_run = AsyncMock()
    orchestrator._set_plan_status = AsyncMock()
    orchestrator._load_baseline = AsyncMock(return_value={"objective_score": 100})
    continuation = RunProcessResult("waiting_approval", "next candidate")
    orchestrator._continue_candidate_search = AsyncMock(return_value=continuation)

    candidate = {
        "id": uuid4(),
        "run_id": run_id,
        "host_id": host_id,
        "plan_id": plan["id"],
        "iteration": 1,
        "best_score_before": 100,
        "decision": "pending_approval",
    }
    optimizer = MagicMock()
    optimizer.load_for_plan = AsyncMock(return_value=candidate)
    optimizer.measure_candidate = AsyncMock(return_value={"objective_score": 105})
    optimizer.persist_decision = AsyncMock()
    executor = MagicMock()
    executor.verify_expected_values = AsyncMock(return_value={"work_mem": "128kB"})
    executor.rollback = AsyncMock()
    decision = CandidateDecision(
        decision_name,
        "Candidate did not produce a safe comparable improvement",
        105,
        -5,
        -5,
        {},
        [],
        0.9,
    )
    with (
        patch(
            "backend.services.durable_run_orchestrator.CandidateOptimizer",
            return_value=optimizer,
        ),
        patch(
            "backend.services.durable_run_orchestrator.get_configuration_backend",
            new=AsyncMock(return_value=executor),
        ),
        patch(
            "backend.services.durable_run_orchestrator.evaluate_candidate",
            return_value=decision,
        ),
    ):
        result = await orchestrator._advance_plan(
            _run(run_id, host_id), plan, LoopConfig()
        )

    assert result == continuation
    executor.rollback.assert_awaited_once()
    optimizer.persist_decision.assert_awaited_once_with(candidate, decision)
    orchestrator._continue_candidate_search.assert_awaited_once()
    assert conn.execute.await_count == 1


@pytest.mark.asyncio
async def test_beneficial_candidate_stays_active_and_search_continues():
    run_id = uuid4()
    host_id = uuid4()
    plan = _plan(run_id, host_id)
    pool, _ = _pool()
    orchestrator = DurableRunOrchestrator(
        pool, audit_logger=MagicMock(log=AsyncMock())
    )
    orchestrator._cancelled = AsyncMock(return_value=False)
    orchestrator._set_run = AsyncMock()
    orchestrator._set_plan_status = AsyncMock()
    orchestrator._load_baseline = AsyncMock(return_value={"objective_score": 100})
    continuation = RunProcessResult("waiting_approval", "next candidate")
    orchestrator._continue_candidate_search = AsyncMock(return_value=continuation)
    candidate = {
        "id": uuid4(),
        "run_id": run_id,
        "host_id": host_id,
        "plan_id": plan["id"],
        "iteration": 1,
        "best_score_before": 100,
        "decision": "pending_approval",
    }
    optimizer = MagicMock(
        load_for_plan=AsyncMock(return_value=candidate),
        measure_candidate=AsyncMock(return_value={"objective_score": 75}),
        persist_decision=AsyncMock(),
    )
    executor = MagicMock(
        verify_expected_values=AsyncMock(return_value={"work_mem": "128kB"}),
        rollback=AsyncMock(),
    )
    decision = CandidateDecision(
        "kept", "Improved safely", 75, 25, 25, {}, [], 0.95
    )
    with (
        patch(
            "backend.services.durable_run_orchestrator.CandidateOptimizer",
            return_value=optimizer,
        ),
        patch(
            "backend.services.durable_run_orchestrator.get_configuration_backend",
            new=AsyncMock(return_value=executor),
        ),
        patch(
            "backend.services.durable_run_orchestrator.evaluate_candidate",
            return_value=decision,
        ),
    ):
        result = await orchestrator._advance_plan(
            _run(run_id, host_id), plan, LoopConfig()
        )

    assert result == continuation
    executor.rollback.assert_not_awaited()
    optimizer.persist_decision.assert_awaited_once_with(candidate, decision)
    orchestrator._set_plan_status.assert_not_awaited()
