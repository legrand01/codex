"""Focused recovery tests for the P0 write coordinator."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from backend.services.plan_execution import PlanExecutionError, PlanExecutionService


def _recovery_fixture():
    plan_id = uuid4()
    plan = {
        "id": plan_id,
        "run_id": uuid4(),
        "host_id": uuid4(),
        "proposed_changes": [{"setting_name": "work_mem", "proposed_value": "8MB"}],
        "pre_change_snapshot": {"work_mem": {"value": "4MB"}},
    }
    operation = {"id": uuid4()}
    executor = AsyncMock()
    audit = MagicMock()
    audit.log = AsyncMock()
    service = PlanExecutionService(MagicMock(), audit_logger=audit, target_executor=executor)
    service._complete = AsyncMock()
    service._reset_operation = AsyncMock()
    service._fail = AsyncMock()
    return service, executor, audit, plan, operation


@pytest.mark.asyncio
async def test_stale_apply_reconciles_verified_target_without_replaying_write():
    service, executor, audit, plan, operation = _recovery_fixture()
    executor.read_current_values.return_value = {"work_mem": "8MB"}

    outcome = await service._recover_stale_operation(plan, operation)

    assert outcome is not None
    assert outcome.already_completed is True
    service._complete.assert_awaited_once()
    executor.rollback.assert_not_awaited()
    audit.log.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_partial_apply_is_rolled_back_and_failed_closed():
    service, executor, _audit, plan, operation = _recovery_fixture()
    plan["proposed_changes"].append(
        {"setting_name": "maintenance_work_mem", "proposed_value": "128MB"}
    )
    plan["pre_change_snapshot"]["maintenance_work_mem"] = {"value": "64MB"}
    executor.read_current_values.return_value = {
        "work_mem": "8MB",
        "maintenance_work_mem": "64MB",
    }

    with pytest.raises(PlanExecutionError, match="partial stale apply"):
        await service._recover_stale_operation(plan, operation)

    executor.rollback.assert_awaited_once_with(plan["host_id"], plan["pre_change_snapshot"])
    service._fail.assert_awaited_once()
