"""Task 25 configuration, capability, event, and single-writer contracts."""

from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from hypothesis import given
from hypothesis import strategies as st

from backend.api.configurations import compare_configurations, download_configuration
from backend.api.events import list_events
from backend.api.fleet import get_setup_guide
from backend.security import DEVELOPMENT_PRINCIPAL
from backend.services.operational_events import (
    OperationalEventError,
    OperationalEventRecorder,
)
from backend.services.target_executor import HostExecutionPolicy, TargetPostgresExecutor


def _version(version_id, host_id, value, *, status="superseded"):
    now = datetime.now(timezone.utc)
    return {
        "id": version_id,
        "host_id": host_id,
        "run_id": uuid4(),
        "plan_id": uuid4(),
        "database_name": "app",
        "configuration_backend": "managed_conf_file",
        "status": status,
        "parameters": [{"setting_name": "work_mem", "proposed_value": value}],
        "source_provenance": {"target_dsn": "must-not-export"},
        "verification_result": {"succeeded": True},
        "created_at": now,
        "applied_at": now,
        "verified_at": now,
        "superseded_at": now if status == "superseded" else None,
        "rolled_back_at": None,
        "origin_configuration_version_id": None,
    }


class VersionDB:
    def __init__(self, rows):
        self.rows = list(rows)

    async def fetchrow(self, _query, version_id, organization_id):
        assert organization_id == DEVELOPMENT_PRINCIPAL.organization_id
        return next((row for row in self.rows if row["id"] == version_id), None)


@pytest.mark.asyncio
async def test_configuration_compare_is_parameter_scoped():
    host_id, left_id, right_id = uuid4(), uuid4(), uuid4()
    db = VersionDB([
        _version(left_id, host_id, "4MB"),
        _version(right_id, host_id, "8MB"),
    ])
    result = await compare_configurations(
        left_id, right_id, db=db, principal=DEVELOPMENT_PRINCIPAL
    )
    assert result.differences[0].model_dump() == {
        "setting_name": "work_mem",
        "left_value": "4MB",
        "right_value": "8MB",
        "changed": True,
    }


@pytest.mark.asyncio
async def test_configuration_download_is_redacted_and_declarative():
    version_id, host_id = uuid4(), uuid4()
    response = await download_configuration(
        version_id,
        db=VersionDB([_version(version_id, host_id, "8MB")]),
        principal=DEVELOPMENT_PRINCIPAL,
    )
    body = response.body.decode()
    assert "work_mem = '8MB'" in body
    assert "must-not-export" not in body
    assert "target_dsn" not in body


class SetupDB:
    def __init__(self, host):
        self.host = host

    async def fetchrow(self, _query, *_args):
        return self.host

    async def fetch(self, _query, *_args):
        return [
            {"setting_name": "work_mem", "parameter_context": "reload"},
            {"setting_name": "shared_buffers", "parameter_context": "restart"},
        ]


@pytest.mark.asyncio
async def test_setup_guide_uses_pg15_parameter_grants_and_managed_file():
    host_id = uuid4()
    result = await get_setup_guide(
        host_id,
        mode="reload_only",
        db=SetupDB(
            {
                "id": host_id,
                "pg_version": "17.2",
                "platform_type": "self_managed",
                "configuration_backend": "managed_conf_file",
                "managed_conf_path": "/pgdata/conf.d/postgres_tune.conf",
                "target_dsn_env": "TARGET_DSN",
            }
        ),
        principal=DEVELOPMENT_PRINCIPAL,
    )
    assert "GRANT pg_monitor TO dbtune_agent;" in result.sql
    assert any("postgresql.auto.conf untouched" in item for item in result.file_instructions)
    assert result.agent_environment["AGENT_INSTANCE_ID"].startswith("<unique UUID")


class EventsDB:
    def __init__(self, row):
        self.row = row
        self.queries = []

    async def fetchval(self, query, *_args):
        self.queries.append(query)
        return 1

    async def fetch(self, query, *_args):
        self.queries.append(query)
        return [self.row]


@pytest.mark.asyncio
async def test_event_filters_and_deep_links():
    run_id, host_id, configuration_id = uuid4(), uuid4(), uuid4()
    db = EventsDB(
        {
            "id": 7,
            "host_id": host_id,
            "run_id": run_id,
            "configuration_version_id": configuration_id,
            "occurred_at": datetime.now(timezone.utc),
            "severity": "error",
            "component": "configuration",
            "event_code": "CONFIG_APPLY_FAILED",
            "message": "apply failed",
            "details": '{"error":"reload"}',
            "host_name": "db-primary",
        }
    )
    result = await list_events(
        severity=["error"], code=["CONFIG_APPLY_FAILED"], host_id=host_id,
        run_id=run_id, component=["configuration"], q="reload",
        time_from=None, time_to=None, page=1, page_size=50,
        db=db, principal=DEVELOPMENT_PRINCIPAL,
    )
    assert result.total == 1
    assert result.events[0].run_href == f"/tuning/{run_id}?tab=activity"
    assert result.events[0].configuration_href == (
        f"/tuning/{run_id}?tab=configuration"
    )
    assert "plainto_tsquery" in db.queries[0]


@pytest.mark.asyncio
async def test_event_catalog_rejects_unknown_codes():
    with pytest.raises(OperationalEventError, match="Unknown operational event"):
        await OperationalEventRecorder().record("NOT_REAL", "message")


@given(agent_write_ambiguous=st.booleans())
def test_property_42_duplicate_agents_exclude_writes(
    agent_write_ambiguous,
):
    from backend.config import settings

    policy = HostExecutionPolicy(
        host_id=uuid4(), hostname="db", environment="staging",
        server_role="primary", target_dsn_env="TARGET_DSN",
        writes_enabled=True, agent_write_ambiguous=agent_write_ambiguous,
    )
    with patch.object(settings, "write_execution_enabled", True):
        if agent_write_ambiguous:
            with pytest.raises(Exception, match="multiple active Host Agents"):
                TargetPostgresExecutor.assert_write_allowed(policy)
        else:
            TargetPostgresExecutor.assert_write_allowed(policy)


@pytest.mark.asyncio
async def test_compare_rejects_cross_host_versions():
    left_id, right_id = uuid4(), uuid4()
    db = VersionDB([
        _version(left_id, uuid4(), "4MB"),
        _version(right_id, uuid4(), "8MB"),
    ])
    with pytest.raises(HTTPException) as exc:
        await compare_configurations(
            left_id, right_id, db=db, principal=DEVELOPMENT_PRINCIPAL
        )
    assert exc.value.status_code == 409
