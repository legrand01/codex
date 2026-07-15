"""Property and disposition tests for Requirement 18 parameter coverage."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from hypothesis import given
from hypothesis import strategies as st

from backend.dependencies import get_db
from backend.main import app
from backend.services.parameter_catalog import (
    FINAL_DISPOSITIONS,
    RELOAD_ONLY_PARAMETERS,
    RESTART_PARAMETERS,
    catalog_version_name,
    derive_parameter_dispositions,
    parse_pg_major,
)


def _entries(mode="reload_only"):
    names = list(RELOAD_ONLY_PARAMETERS)
    if mode == "restart_enabled":
        names.extend(RESTART_PARAMETERS)
    return [
        {
            "setting_name": name,
            "apply_context": "restart" if name in RESTART_PARAMETERS else "reload",
            "display_order": index,
            "bounded_domain_available": name
            in {
                "work_mem",
                "random_page_cost",
                "checkpoint_completion_target",
                "effective_io_concurrency",
            },
        }
        for index, name in enumerate(names, 1)
    ]


def _settings(entries):
    return {
        item["setting_name"]: {
            "name": item["setting_name"],
            "setting": str(item["display_order"]),
            "unit": "kB" if item["setting_name"] == "work_mem" else None,
            "source": "configuration file",
            "sourcefile": "/etc/postgresql/conf.d/10-base.conf",
            "context": "postmaster"
            if item["apply_context"] == "restart"
            else "user",
            "pending_restart": False,
        }
        for item in entries
    }


@given(
    selected=st.sets(st.sampled_from(RELOAD_ONLY_PARAMETERS)),
    allowlisted=st.sets(st.sampled_from(RELOAD_ONLY_PARAMETERS)),
)
def test_completed_reload_catalog_has_one_final_disposition_per_entry(
    selected, allowlisted
):
    entries = _entries()
    settings = _settings(entries)
    rows = derive_parameter_dispositions(
        run={
            "status": "completed",
            "selected_parameters": list(selected),
            "platform_type": "self_managed",
        },
        catalog_entries=entries,
        allowlist={name: "reload" for name in allowlisted},
        baseline_settings=settings,
        current_settings=settings,
        candidates=[],
        baseline_status="ready",
        root_cause_category="configuration",
    )

    assert len(rows) == len(RELOAD_ONLY_PARAMETERS) == 15
    assert len({row["setting_name"] for row in rows}) == len(rows)
    assert all(row["final_disposition"] in FINAL_DISPOSITIONS for row in rows)


def test_kept_candidate_records_changed_and_verified_best():
    entries = _entries()
    settings = _settings(entries)
    rows = derive_parameter_dispositions(
        run={
            "status": "completed",
            "selected_parameters": ["work_mem"],
            "platform_type": "self_managed",
        },
        catalog_entries=entries,
        allowlist={"work_mem": "reload"},
        baseline_settings=settings,
        current_settings={
            **settings,
            "work_mem": {**settings["work_mem"], "setting": "8192"},
        },
        candidates=[
            {
                "iteration": 1,
                "parameter_values": {"work_mem": "8192"},
                "decision": "kept",
            }
        ],
        baseline_status="ready",
        root_cause_category="configuration",
    )

    work_mem = next(row for row in rows if row["setting_name"] == "work_mem")
    assert work_mem["baseline_value"] == "1"
    assert work_mem["best_verified_value"] == "8192"
    assert work_mem["current_value"] == "8192"
    assert work_mem["final_disposition"] == "changed_and_verified"


def test_restart_catalog_marks_pending_setting_restart_required():
    entries = _entries("restart_enabled")
    settings = _settings(entries)
    current = {
        **settings,
        "shared_buffers": {**settings["shared_buffers"], "pending_restart": True},
    }
    rows = derive_parameter_dispositions(
        run={
            "status": "completed",
            "selected_parameters": ["shared_buffers"],
            "platform_type": "self_managed",
        },
        catalog_entries=entries,
        allowlist={"shared_buffers": "restart"},
        baseline_settings=settings,
        current_settings=current,
        candidates=[],
        baseline_status="ready",
        root_cause_category="configuration",
    )

    shared = next(row for row in rows if row["setting_name"] == "shared_buffers")
    assert len(rows) == 19
    assert shared["pending_restart"] is True
    assert shared["final_disposition"] == "restart_required"


@pytest.mark.parametrize(
    ("version", "expected"),
    [("PostgreSQL 17.10", 17), ("16.4", 16), ("PostgreSQL 14.9", None), (None, None)],
)
def test_catalog_version_is_bound_to_supported_pg_major(version, expected):
    assert parse_pg_major(version) == expected
    if expected:
        assert catalog_version_name(expected, "self_managed") == (
            f"pg{expected}-self-managed-v1"
        )


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.mark.asyncio
async def test_parameter_disposition_api_is_tenant_scoped_and_ordered(client):
    run_id = uuid4()
    now = datetime.now(timezone.utc)
    db = AsyncMock()
    db.fetchval.return_value = True
    db.fetch.return_value = [
        {
            "id": uuid4(),
            "run_id": run_id,
            "host_id": uuid4(),
            "catalog_version": "pg17-self-managed-v1",
            "setting_name": "work_mem",
            "display_order": 1,
            "apply_context": "reload",
            "bounded_domain_available": True,
            "selected": True,
            "supported_on_target": True,
            "allowlisted": True,
            "current_value": "4096",
            "unit": "kB",
            "source": "configuration file",
            "sourcefile_or_provider": "/etc/postgresql/conf.d/base.conf",
            "setting_context": "user",
            "pending_restart": False,
            "baseline_value": "4096",
            "best_verified_value": "4096",
            "pending_candidate_value": None,
            "final_disposition": "retained_at_baseline",
            "disposition_reason": "No candidate safely beat baseline.",
            "updated_at": now,
        }
    ]

    async def dependency():
        yield db

    app.dependency_overrides[get_db] = dependency
    try:
        with patch(
            "backend.api.parameter_catalog.refresh_parameter_dispositions",
            AsyncMock(return_value=[]),
        ):
            response = await client.get(
                f"/api/v1/runs/{run_id}/parameter-dispositions"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()[0]["final_disposition"] == "retained_at_baseline"
    query = db.fetch.await_args.args[0]
    assert "organization_id = $2" in query
    assert "ORDER BY display_order" in query
