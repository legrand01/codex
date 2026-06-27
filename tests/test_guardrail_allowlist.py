"""
Unit tests for Guardrail Engine allowlist enforcement.

Tests cover:
- Empty allowlist → rejection
- All settings in allowlist → passed
- One setting not in allowlist → rejection of entire plan
- Restart-required setting without permission → rejection
- Restart-required setting with permission → passed
- Multiple violations reported correctly

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.guardrail_engine import (
    AllowlistResult,
    check_allowlist,
    check_restart_permission,
)


# --- Fixtures and Helpers ---


def make_host_id():
    """Generate a random host UUID."""
    return uuid.uuid4()


class FakeConnection:
    """A fake asyncpg connection that returns configured query results."""

    def __init__(self, allowlist_rows=None, host_row=None):
        self.allowlist_rows = allowlist_rows if allowlist_rows is not None else []
        self.host_row = host_row

    async def fetch(self, query, *args):
        if "guardrail_allowlist" in query:
            return self.allowlist_rows
        return []

    async def fetchrow(self, query, *args):
        if "restart_required_enabled" in query:
            return self.host_row
        return None


class FakePool:
    """A fake asyncpg pool that returns a FakeConnection."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return FakeAcquireContext(self._conn)


class FakeAcquireContext:
    """Async context manager returning a fake connection."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        pass


class FakeAuditLogger:
    """A fake audit logger that records log calls."""

    def __init__(self):
        self.entries = []

    async def log(self, **kwargs):
        self.entries.append(kwargs)
        # Return a mock AuditEntry
        mock_entry = MagicMock()
        mock_entry.id = 1
        return mock_entry


# --- Tests ---


@pytest.mark.asyncio
async def test_empty_allowlist_rejects_plan():
    """When the allowlist is empty, the entire plan should be rejected."""
    host_id = make_host_id()
    proposed_changes = [
        {"setting_name": "shared_buffers", "proposed_value": "512MB"},
        {"setting_name": "work_mem", "proposed_value": "64MB"},
    ]

    conn = FakeConnection(allowlist_rows=[], host_row=None)
    pool = FakePool(conn)
    audit_logger = FakeAuditLogger()

    result = await check_allowlist(proposed_changes, host_id, pool=pool, audit_logger=audit_logger)

    assert result.passed is False
    assert len(result.violations) >= 1
    assert "empty" in result.violations[0].lower()
    assert "shared_buffers" in result.rejected_settings
    assert "work_mem" in result.rejected_settings

    # Verify audit log was called
    assert len(audit_logger.entries) == 1
    assert audit_logger.entries[0]["result"] == "blocked"
    assert audit_logger.entries[0]["target_host_id"] == host_id


@pytest.mark.asyncio
async def test_all_settings_in_allowlist_passes():
    """When all proposed settings are in the allowlist (reload-safe), the plan should pass."""
    host_id = make_host_id()
    proposed_changes = [
        {"setting_name": "shared_buffers", "proposed_value": "512MB"},
        {"setting_name": "work_mem", "proposed_value": "64MB"},
    ]

    allowlist_rows = [
        {
            "setting_name": "shared_buffers",
            "parameter_context": "reload",
            "max_deviation_pct": 50.0,
        },
        {
            "setting_name": "work_mem",
            "parameter_context": "reload",
            "max_deviation_pct": 100.0,
        },
    ]

    host_row = {"restart_required_enabled": False}
    conn = FakeConnection(allowlist_rows=allowlist_rows, host_row=host_row)
    pool = FakePool(conn)
    audit_logger = FakeAuditLogger()

    result = await check_allowlist(proposed_changes, host_id, pool=pool, audit_logger=audit_logger)

    assert result.passed is True
    assert result.violations == []
    assert result.rejected_settings == []

    # No audit log for passed checks
    assert len(audit_logger.entries) == 0


@pytest.mark.asyncio
async def test_one_setting_not_in_allowlist_rejects_entire_plan():
    """If any single setting is not in the allowlist, the entire plan is rejected."""
    host_id = make_host_id()
    proposed_changes = [
        {"setting_name": "shared_buffers", "proposed_value": "512MB"},
        {"setting_name": "max_connections", "proposed_value": "200"},  # Not in allowlist
    ]

    allowlist_rows = [
        {
            "setting_name": "shared_buffers",
            "parameter_context": "reload",
            "max_deviation_pct": 50.0,
        },
    ]

    host_row = {"restart_required_enabled": False}
    conn = FakeConnection(allowlist_rows=allowlist_rows, host_row=host_row)
    pool = FakePool(conn)
    audit_logger = FakeAuditLogger()

    result = await check_allowlist(proposed_changes, host_id, pool=pool, audit_logger=audit_logger)

    assert result.passed is False
    assert "max_connections" in result.rejected_settings
    assert any("max_connections" in v for v in result.violations)

    # Audit log recorded
    assert len(audit_logger.entries) == 1
    assert audit_logger.entries[0]["result"] == "blocked"
    assert "max_connections" in audit_logger.entries[0]["details"]["rejected_settings"]


@pytest.mark.asyncio
async def test_restart_required_setting_without_permission_rejects():
    """A restart-required setting should be rejected if the host doesn't have restart enabled."""
    host_id = make_host_id()
    proposed_changes = [
        {"setting_name": "shared_preload_libraries", "proposed_value": "pg_stat_statements"},
    ]

    allowlist_rows = [
        {
            "setting_name": "shared_preload_libraries",
            "parameter_context": "restart",
            "max_deviation_pct": None,
        },
    ]

    host_row = {"restart_required_enabled": False}
    conn = FakeConnection(allowlist_rows=allowlist_rows, host_row=host_row)
    pool = FakePool(conn)
    audit_logger = FakeAuditLogger()

    result = await check_allowlist(proposed_changes, host_id, pool=pool, audit_logger=audit_logger)

    assert result.passed is False
    assert "shared_preload_libraries" in result.rejected_settings
    assert any("restart" in v.lower() for v in result.violations)

    # Audit log recorded
    assert len(audit_logger.entries) == 1
    assert audit_logger.entries[0]["result"] == "blocked"


@pytest.mark.asyncio
async def test_restart_required_setting_with_permission_passes():
    """A restart-required setting should pass if the host has restart_required_enabled = True."""
    host_id = make_host_id()
    proposed_changes = [
        {"setting_name": "shared_preload_libraries", "proposed_value": "pg_stat_statements"},
    ]

    allowlist_rows = [
        {
            "setting_name": "shared_preload_libraries",
            "parameter_context": "restart",
            "max_deviation_pct": None,
        },
    ]

    host_row = {"restart_required_enabled": True}
    conn = FakeConnection(allowlist_rows=allowlist_rows, host_row=host_row)
    pool = FakePool(conn)
    audit_logger = FakeAuditLogger()

    result = await check_allowlist(proposed_changes, host_id, pool=pool, audit_logger=audit_logger)

    assert result.passed is True
    assert result.violations == []
    assert result.rejected_settings == []
    assert len(audit_logger.entries) == 0


@pytest.mark.asyncio
async def test_multiple_violations_reported_correctly():
    """Multiple violations should all be reported in the result."""
    host_id = make_host_id()
    proposed_changes = [
        {"setting_name": "max_connections", "proposed_value": "200"},  # Not in allowlist
        {"setting_name": "shared_preload_libraries", "proposed_value": "pg_stat_statements"},  # restart, no permission
        {"setting_name": "unknown_setting", "proposed_value": "value"},  # Not in allowlist
    ]

    allowlist_rows = [
        {
            "setting_name": "shared_preload_libraries",
            "parameter_context": "restart",
            "max_deviation_pct": None,
        },
        {
            "setting_name": "work_mem",
            "parameter_context": "reload",
            "max_deviation_pct": 100.0,
        },
    ]

    host_row = {"restart_required_enabled": False}
    conn = FakeConnection(allowlist_rows=allowlist_rows, host_row=host_row)
    pool = FakePool(conn)
    audit_logger = FakeAuditLogger()

    result = await check_allowlist(proposed_changes, host_id, pool=pool, audit_logger=audit_logger)

    assert result.passed is False
    # Should have violations for: max_connections (not in allowlist),
    # shared_preload_libraries (restart without permission), unknown_setting (not in allowlist)
    assert len(result.rejected_settings) == 3
    assert "max_connections" in result.rejected_settings
    assert "shared_preload_libraries" in result.rejected_settings
    assert "unknown_setting" in result.rejected_settings
    assert len(result.violations) == 3

    # Audit log recorded once with all violations
    assert len(audit_logger.entries) == 1
    details = audit_logger.entries[0]["details"]
    assert len(details["rejected_settings"]) == 3


@pytest.mark.asyncio
async def test_mixed_reload_and_restart_with_permission():
    """Mix of reload-safe and restart-required settings should pass when restart is enabled."""
    host_id = make_host_id()
    proposed_changes = [
        {"setting_name": "shared_buffers", "proposed_value": "512MB"},
        {"setting_name": "shared_preload_libraries", "proposed_value": "pg_stat_statements"},
    ]

    allowlist_rows = [
        {
            "setting_name": "shared_buffers",
            "parameter_context": "reload",
            "max_deviation_pct": 50.0,
        },
        {
            "setting_name": "shared_preload_libraries",
            "parameter_context": "restart",
            "max_deviation_pct": None,
        },
    ]

    host_row = {"restart_required_enabled": True}
    conn = FakeConnection(allowlist_rows=allowlist_rows, host_row=host_row)
    pool = FakePool(conn)
    audit_logger = FakeAuditLogger()

    result = await check_allowlist(proposed_changes, host_id, pool=pool, audit_logger=audit_logger)

    assert result.passed is True
    assert result.violations == []
    assert result.rejected_settings == []


@pytest.mark.asyncio
async def test_check_restart_permission_enabled():
    """check_restart_permission returns True when host has restart_required_enabled."""
    host_id = make_host_id()

    host_row = {"restart_required_enabled": True}
    conn = FakeConnection(host_row=host_row)
    pool = FakePool(conn)

    result = await check_restart_permission(host_id, ["shared_preload_libraries"], pool=pool)
    assert result is True


@pytest.mark.asyncio
async def test_check_restart_permission_disabled():
    """check_restart_permission returns False when host does not have restart enabled."""
    host_id = make_host_id()

    host_row = {"restart_required_enabled": False}
    conn = FakeConnection(host_row=host_row)
    pool = FakePool(conn)

    result = await check_restart_permission(host_id, ["shared_preload_libraries"], pool=pool)
    assert result is False


@pytest.mark.asyncio
async def test_check_restart_permission_host_not_found():
    """check_restart_permission returns False when host doesn't exist."""
    host_id = make_host_id()

    conn = FakeConnection(host_row=None)
    pool = FakePool(conn)

    result = await check_restart_permission(host_id, ["shared_preload_libraries"], pool=pool)
    assert result is False


@pytest.mark.asyncio
async def test_check_restart_permission_empty_settings():
    """check_restart_permission returns True when settings list is empty."""
    host_id = make_host_id()

    conn = FakeConnection(host_row={"restart_required_enabled": False})
    pool = FakePool(conn)

    result = await check_restart_permission(host_id, [], pool=pool)
    assert result is True


@pytest.mark.asyncio
async def test_audit_log_contains_host_id_in_details():
    """Violations should record the host identifier in the audit details."""
    host_id = make_host_id()
    proposed_changes = [
        {"setting_name": "dangerous_setting", "proposed_value": "bad_value"},
    ]

    allowlist_rows = [
        {
            "setting_name": "safe_setting",
            "parameter_context": "reload",
            "max_deviation_pct": 10.0,
        },
    ]

    host_row = {"restart_required_enabled": False}
    conn = FakeConnection(allowlist_rows=allowlist_rows, host_row=host_row)
    pool = FakePool(conn)
    audit_logger = FakeAuditLogger()

    result = await check_allowlist(proposed_changes, host_id, pool=pool, audit_logger=audit_logger)

    assert result.passed is False
    assert len(audit_logger.entries) == 1
    assert audit_logger.entries[0]["details"]["host_id"] == str(host_id)
    assert "dangerous_setting" in audit_logger.entries[0]["details"]["rejected_settings"]
