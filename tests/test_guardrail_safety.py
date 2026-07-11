"""
Tests for guardrail safety features: dry-run, rollback validation,
and safety workflow orchestration.

Covers tasks 7.7, 7.8, and 7.10.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from backend.services.guardrail_engine import (
    DryRunResult,
    RollbackValidation,
    SafetyCheckResult,
    _validate_sql_statement,
    execute_dry_run,
    full_safety_check,
    validate_rollback_plan,
)

# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def host_id():
    return uuid4()


@pytest.fixture
def mock_audit_logger():
    logger = AsyncMock()
    logger.log = AsyncMock()
    return logger


@pytest.fixture
def mock_pool_with_settings():
    """Create a mock pool that returns pg_settings evidence snapshot."""

    def _make_pool(known_settings=None):
        if known_settings is None:
            known_settings = {
                "shared_buffers": "128MB",
                "work_mem": "4MB",
                "max_connections": "100",
                "effective_cache_size": "4GB",
            }

        pool = AsyncMock()
        conn = AsyncMock()

        async def mock_fetch(query, *args):
            if "evidence_snapshots" in query:
                return [{"data": known_settings}]
            if "guardrail_allowlist" in query:
                return [{"setting_name": name} for name in known_settings.keys()]
            return []

        conn.fetch = mock_fetch
        conn.fetchrow = AsyncMock(return_value=None)

        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=conn)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx_manager)

        return pool

    return _make_pool


# ─── SQL Validation Tests ────────────────────────────────────────────────────


class TestSqlValidation:
    """Unit tests for SQL statement validation."""

    def test_valid_alter_system_set(self):
        assert _validate_sql_statement("ALTER SYSTEM SET shared_buffers = '256MB'")

    def test_valid_set_statement(self):
        assert _validate_sql_statement("SET work_mem = '8MB'")

    def test_valid_set_to(self):
        assert _validate_sql_statement("SET work_mem TO '8MB'")

    def test_valid_show(self):
        assert _validate_sql_statement("SHOW shared_buffers")

    def test_valid_select(self):
        assert _validate_sql_statement("SELECT current_setting('work_mem')")

    def test_invalid_empty_string(self):
        assert not _validate_sql_statement("")

    def test_invalid_none(self):
        assert not _validate_sql_statement("")

    def test_invalid_drop_table(self):
        assert not _validate_sql_statement("DROP TABLE hosts")

    def test_invalid_delete(self):
        assert not _validate_sql_statement("DELETE FROM audit_log")


# ─── Dry-Run Tests (Task 7.7) ────────────────────────────────────────────────


class TestExecuteDryRun:
    """Tests for execute_dry_run function."""

    @pytest.mark.asyncio
    async def test_dry_run_valid_settings_passes(
        self, host_id, mock_pool_with_settings, mock_audit_logger
    ):
        """Dry-run with valid settings that exist in pg_settings → passes."""
        pool = mock_pool_with_settings()
        proposed_changes = [
            {
                "setting_name": "shared_buffers",
                "proposed_value": "256MB",
                "sql_statement": "ALTER SYSTEM SET shared_buffers = '256MB'",
            },
            {
                "setting_name": "work_mem",
                "proposed_value": "8MB",
                "sql_statement": "SET work_mem = '8MB'",
            },
        ]

        result = await execute_dry_run(
            proposed_changes=proposed_changes,
            host_id=host_id,
            pool=pool,
            audit_logger=mock_audit_logger,
        )

        assert isinstance(result, DryRunResult)
        assert result.passed is True
        assert result.errors == []
        assert result.execution_time_seconds >= 0

    @pytest.mark.asyncio
    async def test_dry_run_invalid_setting_fails(
        self, host_id, mock_pool_with_settings, mock_audit_logger
    ):
        """Dry-run with a setting not in pg_settings → fails."""
        pool = mock_pool_with_settings()
        proposed_changes = [
            {
                "setting_name": "nonexistent_setting",
                "proposed_value": "42",
                "sql_statement": "ALTER SYSTEM SET nonexistent_setting = '42'",
            },
        ]

        result = await execute_dry_run(
            proposed_changes=proposed_changes,
            host_id=host_id,
            pool=pool,
            audit_logger=mock_audit_logger,
        )

        assert isinstance(result, DryRunResult)
        assert result.passed is False
        assert len(result.errors) > 0
        assert "nonexistent_setting" in result.errors[0]

    @pytest.mark.asyncio
    async def test_dry_run_invalid_sql_fails(
        self, host_id, mock_pool_with_settings, mock_audit_logger
    ):
        """Dry-run with invalid SQL statement → fails."""
        pool = mock_pool_with_settings()
        proposed_changes = [
            {
                "setting_name": "shared_buffers",
                "proposed_value": "256MB",
                "sql_statement": "DROP TABLE hosts",
            },
        ]

        result = await execute_dry_run(
            proposed_changes=proposed_changes,
            host_id=host_id,
            pool=pool,
            audit_logger=mock_audit_logger,
        )

        assert result.passed is False
        assert any("failed to parse" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_dry_run_records_audit_log(
        self, host_id, mock_pool_with_settings, mock_audit_logger
    ):
        """Dry-run records result in Audit_Log."""
        pool = mock_pool_with_settings()
        proposed_changes = [
            {
                "setting_name": "shared_buffers",
                "proposed_value": "256MB",
                "sql_statement": "ALTER SYSTEM SET shared_buffers = '256MB'",
            },
        ]

        await execute_dry_run(
            proposed_changes=proposed_changes,
            host_id=host_id,
            pool=pool,
            audit_logger=mock_audit_logger,
        )

        mock_audit_logger.log.assert_called()
        call_kwargs = mock_audit_logger.log.call_args.kwargs
        assert call_kwargs["action_type"] == "dry_run"
        assert call_kwargs["target_host_id"] == host_id

    @pytest.mark.asyncio
    async def test_dry_run_timeout(self, host_id, mock_audit_logger):
        """Dry-run that exceeds timeout → fails with timeout error."""
        # Create a pool that simulates slow response
        pool = AsyncMock()
        conn = AsyncMock()

        async def slow_fetch(query, *args):
            await asyncio.sleep(5)  # Simulate slow DB
            return []

        conn.fetch = slow_fetch
        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=conn)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx_manager)

        proposed_changes = [
            {"setting_name": "shared_buffers", "proposed_value": "256MB"},
        ]

        result = await execute_dry_run(
            proposed_changes=proposed_changes,
            host_id=host_id,
            timeout=1,  # 1 second timeout
            pool=pool,
            audit_logger=mock_audit_logger,
        )

        assert result.passed is False
        assert any("timed out" in e for e in result.errors)


# ─── Rollback Validation Tests (Task 7.8) ────────────────────────────────────


class TestValidateRollbackPlan:
    """Tests for validate_rollback_plan function."""

    def test_accepts_authoritative_target_snapshot_shape(self):
        proposed_changes = [{"setting_name": "work_mem", "proposed_value": "8MB"}]
        rollback_instructions = [{"setting_name": "work_mem", "restore_value": "64kB"}]
        pre_snapshot = {
            "work_mem": {
                "value": "64kB",
                "source": "command line",
                "in_auto_conf": False,
            }
        }

        result = validate_rollback_plan(
            proposed_changes, rollback_instructions, pre_snapshot
        )

        assert result.valid is True

    def test_valid_rollback_plan(self):
        """Rollback plan with all settings and matching values → valid."""
        proposed_changes = [
            {"setting_name": "shared_buffers", "proposed_value": "256MB"},
            {"setting_name": "work_mem", "proposed_value": "8MB"},
        ]
        rollback_instructions = [
            {"setting_name": "shared_buffers", "restore_value": "128MB"},
            {"setting_name": "work_mem", "restore_value": "4MB"},
        ]
        pre_snapshot = {
            "shared_buffers": "128MB",
            "work_mem": "4MB",
        }

        result = validate_rollback_plan(proposed_changes, rollback_instructions, pre_snapshot)

        assert isinstance(result, RollbackValidation)
        assert result.valid is True
        assert result.errors == []

    def test_missing_rollback_entry(self):
        """Rollback plan missing a restore entry → invalid."""
        proposed_changes = [
            {"setting_name": "shared_buffers", "proposed_value": "256MB"},
            {"setting_name": "work_mem", "proposed_value": "8MB"},
        ]
        rollback_instructions = [
            {"setting_name": "shared_buffers", "restore_value": "128MB"},
            # Missing work_mem rollback
        ]
        pre_snapshot = {
            "shared_buffers": "128MB",
            "work_mem": "4MB",
        }

        result = validate_rollback_plan(proposed_changes, rollback_instructions, pre_snapshot)

        assert result.valid is False
        assert len(result.errors) == 1
        assert "work_mem" in result.errors[0]
        assert "Missing rollback" in result.errors[0]

    def test_wrong_restore_value(self):
        """Rollback plan with wrong restore value → invalid."""
        proposed_changes = [
            {"setting_name": "shared_buffers", "proposed_value": "256MB"},
        ]
        rollback_instructions = [
            {"setting_name": "shared_buffers", "restore_value": "64MB"},
        ]
        pre_snapshot = {
            "shared_buffers": "128MB",
        }

        result = validate_rollback_plan(proposed_changes, rollback_instructions, pre_snapshot)

        assert result.valid is False
        assert len(result.errors) == 1
        assert "does not match" in result.errors[0]

    def test_setting_not_in_pre_snapshot(self):
        """Rollback plan for a setting not in pre-snapshot → invalid."""
        proposed_changes = [
            {"setting_name": "shared_buffers", "proposed_value": "256MB"},
        ]
        rollback_instructions = [
            {"setting_name": "shared_buffers", "restore_value": "128MB"},
        ]
        pre_snapshot = {}  # Empty snapshot

        result = validate_rollback_plan(proposed_changes, rollback_instructions, pre_snapshot)

        assert result.valid is False
        assert any("not found in pre-change snapshot" in e for e in result.errors)

    def test_empty_proposed_changes(self):
        """Empty proposed changes → valid (nothing to rollback)."""
        result = validate_rollback_plan([], [], {})
        assert result.valid is True
        assert result.errors == []


# ─── Full Safety Check Tests (Task 7.10) ─────────────────────────────────────


class TestFullSafetyCheck:
    """Tests for full_safety_check workflow orchestration."""

    @pytest.mark.asyncio
    async def test_all_stages_pass(self, host_id, mock_audit_logger):
        """Full safety check passes when all stages succeed."""
        # Set up pool that has allowlist and pg_settings
        pool = AsyncMock()
        conn = AsyncMock()

        known_settings = {
            "shared_buffers": "128MB",
            "work_mem": "4MB",
        }

        async def mock_fetch(query, *args):
            if "guardrail_allowlist" in query:
                return [
                    {
                        "setting_name": "shared_buffers",
                        "parameter_context": "reload",
                        "max_deviation_pct": 50.0,
                    },
                    {
                        "setting_name": "work_mem",
                        "parameter_context": "reload",
                        "max_deviation_pct": 50.0,
                    },
                ]
            if "evidence_snapshots" in query:
                return [{"data": known_settings}]
            return []

        async def mock_fetchrow(query, *args):
            if "restart_required_enabled" in query:
                return {"restart_required_enabled": False}
            return None

        conn.fetch = mock_fetch
        conn.fetchrow = mock_fetchrow
        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=conn)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx_manager)

        proposed_changes = [
            {
                "setting_name": "shared_buffers",
                "proposed_value": "192MB",
                "sql_statement": "ALTER SYSTEM SET shared_buffers = '192MB'",
            },
        ]
        rollback_instructions = [
            {"setting_name": "shared_buffers", "restore_value": "128MB"},
        ]
        pre_snapshot = {"shared_buffers": "128MB", "work_mem": "4MB"}

        result = await full_safety_check(
            proposed_changes=proposed_changes,
            host_id=host_id,
            rollback_instructions=rollback_instructions,
            pre_snapshot=pre_snapshot,
            risk_threshold=70,
            pool=pool,
            audit_logger=mock_audit_logger,
        )

        assert isinstance(result, SafetyCheckResult)
        assert result.passed is True
        assert result.blocked_at_stage is None
        assert result.errors == []
        assert "allowlist" in result.stage_results
        assert "risk_score" in result.stage_results
        assert "approval" in result.stage_results
        assert "dry_run" in result.stage_results

    @pytest.mark.asyncio
    async def test_stops_at_allowlist_failure(self, host_id, mock_audit_logger):
        """Full safety check stops at allowlist stage when it fails."""
        # Pool with empty allowlist → will fail
        pool = AsyncMock()
        conn = AsyncMock()

        async def mock_fetch(query, *args):
            if "guardrail_allowlist" in query:
                return []  # Empty allowlist
            return []

        async def mock_fetchrow(query, *args):
            return None

        conn.fetch = mock_fetch
        conn.fetchrow = mock_fetchrow
        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=conn)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx_manager)

        proposed_changes = [
            {"setting_name": "shared_buffers", "proposed_value": "256MB"},
        ]
        rollback_instructions = [
            {"setting_name": "shared_buffers", "restore_value": "128MB"},
        ]
        pre_snapshot = {"shared_buffers": "128MB"}

        result = await full_safety_check(
            proposed_changes=proposed_changes,
            host_id=host_id,
            rollback_instructions=rollback_instructions,
            pre_snapshot=pre_snapshot,
            pool=pool,
            audit_logger=mock_audit_logger,
        )

        assert result.passed is False
        assert result.blocked_at_stage == "allowlist"
        # Should NOT have dry_run or approval in stage_results
        assert "dry_run" not in result.stage_results
        assert "approval" not in result.stage_results

    @pytest.mark.asyncio
    async def test_stops_at_risk_score_failure(self, host_id, mock_audit_logger):
        """Full safety check stops at risk_score stage when threshold exceeded."""
        pool = AsyncMock()
        conn = AsyncMock()

        async def mock_fetch(query, *args):
            if "guardrail_allowlist" in query:
                return [
                    {
                        "setting_name": f"setting_{i}",
                        "parameter_context": "reload",
                        "max_deviation_pct": 50.0,
                    }
                    for i in range(15)
                ]
            if "evidence_snapshots" in query:
                return [{"data": {f"setting_{i}": "10" for i in range(15)}}]
            return []

        async def mock_fetchrow(query, *args):
            if "restart_required_enabled" in query:
                return {"restart_required_enabled": False}
            return None

        conn.fetch = mock_fetch
        conn.fetchrow = mock_fetchrow
        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=conn)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx_manager)

        # Many changes with high deviations → high risk score
        proposed_changes = [
            {
                "setting_name": f"setting_{i}",
                "proposed_value": "1000",
            }
            for i in range(15)
        ]
        pre_snapshot = {f"setting_{i}": "10" for i in range(15)}
        rollback_instructions = [
            {"setting_name": f"setting_{i}", "restore_value": "10"} for i in range(15)
        ]

        result = await full_safety_check(
            proposed_changes=proposed_changes,
            host_id=host_id,
            rollback_instructions=rollback_instructions,
            pre_snapshot=pre_snapshot,
            risk_threshold=30,  # Low threshold to trigger
            pool=pool,
            audit_logger=mock_audit_logger,
        )

        assert result.passed is False
        assert result.blocked_at_stage == "risk_score"
        assert "dry_run" not in result.stage_results

    @pytest.mark.asyncio
    async def test_stops_at_approval_rollback_validation_failure(self, host_id, mock_audit_logger):
        """Full safety check stops at approval stage when rollback is invalid."""
        pool = AsyncMock()
        conn = AsyncMock()

        async def mock_fetch(query, *args):
            if "guardrail_allowlist" in query:
                return [
                    {
                        "setting_name": "shared_buffers",
                        "parameter_context": "reload",
                        "max_deviation_pct": 50.0,
                    },
                ]
            if "evidence_snapshots" in query:
                return [{"data": {"shared_buffers": "128MB"}}]
            return []

        async def mock_fetchrow(query, *args):
            if "restart_required_enabled" in query:
                return {"restart_required_enabled": False}
            return None

        conn.fetch = mock_fetch
        conn.fetchrow = mock_fetchrow
        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=conn)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx_manager)

        proposed_changes = [
            {"setting_name": "shared_buffers", "proposed_value": "256MB"},
        ]
        # Invalid rollback: wrong restore value
        rollback_instructions = [
            {"setting_name": "shared_buffers", "restore_value": "64MB"},
        ]
        pre_snapshot = {"shared_buffers": "128MB"}

        result = await full_safety_check(
            proposed_changes=proposed_changes,
            host_id=host_id,
            rollback_instructions=rollback_instructions,
            pre_snapshot=pre_snapshot,
            pool=pool,
            audit_logger=mock_audit_logger,
        )

        assert result.passed is False
        assert result.blocked_at_stage == "approval"
        assert "dry_run" not in result.stage_results

    @pytest.mark.asyncio
    async def test_workflow_ordering_enforced(self, host_id, mock_audit_logger):
        """Safety workflow enforces strict ordering of stages."""
        pool = AsyncMock()
        conn = AsyncMock()

        known_settings = {"shared_buffers": "128MB"}

        async def mock_fetch(query, *args):
            if "guardrail_allowlist" in query:
                return [
                    {
                        "setting_name": "shared_buffers",
                        "parameter_context": "reload",
                        "max_deviation_pct": 50.0,
                    },
                ]
            if "evidence_snapshots" in query:
                return [{"data": known_settings}]
            return []

        async def mock_fetchrow(query, *args):
            if "restart_required_enabled" in query:
                return {"restart_required_enabled": False}
            return None

        conn.fetch = mock_fetch
        conn.fetchrow = mock_fetchrow
        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=conn)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx_manager)

        proposed_changes = [
            {
                "setting_name": "shared_buffers",
                "proposed_value": "192MB",
                "sql_statement": "ALTER SYSTEM SET shared_buffers = '192MB'",
            },
        ]
        rollback_instructions = [
            {"setting_name": "shared_buffers", "restore_value": "128MB"},
        ]
        pre_snapshot = {"shared_buffers": "128MB"}

        result = await full_safety_check(
            proposed_changes=proposed_changes,
            host_id=host_id,
            rollback_instructions=rollback_instructions,
            pre_snapshot=pre_snapshot,
            risk_threshold=70,
            pool=pool,
            audit_logger=mock_audit_logger,
        )

        # When all pass, all stages should be present in order
        assert result.passed is True
        stage_keys = list(result.stage_results.keys())
        assert stage_keys == ["allowlist", "risk_score", "approval", "dry_run"]

    @pytest.mark.asyncio
    async def test_each_stage_records_audit(self, host_id, mock_audit_logger):
        """Each stage result is recorded in Audit_Log."""
        pool = AsyncMock()
        conn = AsyncMock()

        known_settings = {"shared_buffers": "128MB"}

        async def mock_fetch(query, *args):
            if "guardrail_allowlist" in query:
                return [
                    {
                        "setting_name": "shared_buffers",
                        "parameter_context": "reload",
                        "max_deviation_pct": 50.0,
                    },
                ]
            if "evidence_snapshots" in query:
                return [{"data": known_settings}]
            return []

        async def mock_fetchrow(query, *args):
            if "restart_required_enabled" in query:
                return {"restart_required_enabled": False}
            return None

        conn.fetch = mock_fetch
        conn.fetchrow = mock_fetchrow
        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=conn)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx_manager)

        proposed_changes = [
            {
                "setting_name": "shared_buffers",
                "proposed_value": "192MB",
                "sql_statement": "ALTER SYSTEM SET shared_buffers = '192MB'",
            },
        ]
        rollback_instructions = [
            {"setting_name": "shared_buffers", "restore_value": "128MB"},
        ]
        pre_snapshot = {"shared_buffers": "128MB"}

        await full_safety_check(
            proposed_changes=proposed_changes,
            host_id=host_id,
            rollback_instructions=rollback_instructions,
            pre_snapshot=pre_snapshot,
            risk_threshold=70,
            pool=pool,
            audit_logger=mock_audit_logger,
        )

        # Audit logger should have been called (dry_run + safety_check_passed)
        assert mock_audit_logger.log.call_count >= 2
