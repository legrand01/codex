"""P0 tests for fail-closed target PostgreSQL execution."""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from backend.config import settings
from backend.services.target_executor import (
    PRODUCTION_CONFIRMATION,
    TargetPostgresExecutor,
    TargetValidationError,
    TargetVerificationError,
    WriteInterlockError,
)


class FakeControlPool:
    def __init__(self, row):
        self.row = row

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def fetchrow(self, query, host_id):
        return self.row if self.row["id"] == host_id else None


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeTargetConnection:
    def __init__(
        self,
        *,
        context="user",
        reject_proposed=False,
        is_replica=False,
        reload_succeeds=True,
    ):
        self.context = context
        self.reject_proposed = reject_proposed
        self.is_replica = is_replica
        self.reload_succeeds = reload_succeeds
        self.values = {"work_mem": "4MB", "maintenance_work_mem": "64MB"}
        self.base_values = dict(self.values)
        self.pending = {}
        self.commands = []
        self.closed = False

    async def fetch(self, query, names):
        return [
            {
                "name": name,
                "current_value": self.values[name],
                "unit": "kB",
                "context": self.context,
                "source": "default",
                "sourcefile": None,
                "pending_restart": False,
                "in_auto_conf": False,
            }
            for name in names
            if name in self.values
        ]

    async def fetchval(self, query, *args):
        if "pg_is_in_recovery" in query:
            return self.is_replica
        if "set_config" in query:
            return args[1]
        if "pg_reload_conf" in query:
            if not self.reload_succeeds:
                return False
            if not self.reject_proposed:
                self.values.update(self.pending)
            self.pending.clear()
            return True
        if "current_setting" in query:
            return self.values[args[0]]
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def execute(self, query, *args):
        self.commands.append(query)
        if query.startswith("ALTER SYSTEM SET"):
            setting_name = query.split('"')[1]
            value = query.rsplit("'", 2)[1].replace("''", "'")
            self.pending[setting_name] = value
        elif query.startswith("ALTER SYSTEM RESET"):
            setting_name = query.split('"')[1]
            self.values[setting_name] = self.base_values[setting_name]
            self.pending.pop(setting_name, None)
        return "OK"

    def transaction(self):
        return FakeTransaction()

    async def close(self):
        self.closed = True


def make_policy_row(**overrides):
    row = {
        "id": uuid4(),
        "hostname": "db-primary",
        "environment": "staging",
        "server_role": "primary",
        "target_dsn_env": "TARGET_DATABASE_URL",
        "writes_enabled": True,
    }
    row.update(overrides)
    return row


def make_executor(
    row, target, dsn="postgresql://agent@db.example/test?sslmode=verify-full"
):
    async def connector(*args, **kwargs):
        return target

    return TargetPostgresExecutor(
        FakeControlPool(row),
        connector=connector,
        environ={"TARGET_DATABASE_URL": dsn},
    )


@pytest.fixture(autouse=True)
def reset_write_interlocks(monkeypatch):
    monkeypatch.setattr(settings, "write_execution_enabled", False)
    monkeypatch.setattr(settings, "production_write_enabled", False)
    monkeypatch.setattr(settings, "production_write_confirmation", "")
    monkeypatch.setattr(settings, "target_verify_timeout_sec", 0)


@pytest.mark.asyncio
async def test_writes_are_disabled_by_default():
    row = make_policy_row()
    executor = make_executor(row, FakeTargetConnection())

    with pytest.raises(WriteInterlockError, match="Global target write execution is disabled"):
        async with executor.connect(row["id"], for_write=True):
            pass


@pytest.mark.asyncio
async def test_production_requires_tls_and_explicit_confirmation(monkeypatch):
    row = make_policy_row(environment="production")
    target = FakeTargetConnection()
    monkeypatch.setattr(settings, "write_execution_enabled", True)
    monkeypatch.setattr(settings, "production_write_enabled", True)

    no_tls = make_executor(row, target, dsn="postgresql://agent@db.example/test")
    with pytest.raises(WriteInterlockError, match="sslmode=verify-full"):
        async with no_tls.connect(row["id"], for_write=True):
            pass

    encryption_without_peer_verification = make_executor(
        row,
        target,
        dsn="postgresql://agent@db.example/test?sslmode=require",
    )
    with pytest.raises(WriteInterlockError, match="sslmode=verify-full"):
        async with encryption_without_peer_verification.connect(
            row["id"], for_write=True
        ):
            pass

    tls = make_executor(row, target)
    with pytest.raises(WriteInterlockError, match="confirmation is missing"):
        async with tls.connect(row["id"], for_write=True):
            pass

    monkeypatch.setattr(settings, "production_write_confirmation", PRODUCTION_CONFIRMATION)
    async with tls.connect(row["id"], for_write=True):
        pass
    assert target.closed is True


@pytest.mark.asyncio
async def test_write_rejects_metadata_or_live_replica(monkeypatch):
    monkeypatch.setattr(settings, "write_execution_enabled", True)
    metadata_replica = make_policy_row(server_role="replica")
    executor = make_executor(metadata_replica, FakeTargetConnection())
    with pytest.raises(WriteInterlockError, match="confirmed primary"):
        async with executor.connect(metadata_replica["id"], for_write=True):
            pass

    live_replica = make_policy_row()
    executor = make_executor(live_replica, FakeTargetConnection(is_replica=True))
    with pytest.raises(WriteInterlockError, match="reports that it is a replica"):
        async with executor.connect(live_replica["id"], for_write=True):
            pass


@pytest.mark.asyncio
async def test_dry_run_validates_value_without_persisting_it():
    row = make_policy_row()
    target = FakeTargetConnection()
    executor = make_executor(row, target)

    result = await executor.dry_run(
        row["id"],
        [{"setting_name": "work_mem", "proposed_value": "8MB"}],
    )

    assert result.passed is True
    assert result.snapshot["work_mem"]["value"] == "4MB"
    assert target.values["work_mem"] == "4MB"


@pytest.mark.asyncio
async def test_dry_run_rejects_restart_only_setting():
    row = make_policy_row()
    executor = make_executor(row, FakeTargetConnection(context="postmaster"))

    result = await executor.dry_run(
        row["id"],
        [{"setting_name": "work_mem", "proposed_value": "8MB"}],
    )

    assert result.passed is False
    assert "unsupported context" in result.errors[0]


@pytest.mark.asyncio
async def test_executor_rejects_index_or_arbitrary_sql_changes():
    row = make_policy_row()
    executor = make_executor(row, FakeTargetConnection())

    result = await executor.dry_run(
        row["id"],
        [
            {
                "change_type": "index",
                "setting_name": "idx_users_email",
                "proposed_value": "CREATE INDEX idx_users_email ON users(email)",
            }
        ],
    )

    assert result.passed is False
    assert "does not execute arbitrary SQL" in result.errors[0]


@pytest.mark.asyncio
async def test_apply_requires_exact_snapshot_and_verifies_value(monkeypatch):
    row = make_policy_row()
    target = FakeTargetConnection()
    executor = make_executor(row, target)
    monkeypatch.setattr(settings, "write_execution_enabled", True)
    snapshot = await executor.capture_snapshot(row["id"], ["work_mem"])

    result = await executor.apply(
        row["id"],
        [{"setting_name": "work_mem", "proposed_value": "8MB"}],
        snapshot,
    )

    assert result.succeeded is True
    assert result.verified_values == {"work_mem": "8MB"}
    assert any(command.startswith("ALTER SYSTEM SET") for command in target.commands)


@pytest.mark.asyncio
async def test_apply_failure_restores_original_configuration(monkeypatch):
    row = make_policy_row()
    target = FakeTargetConnection(reject_proposed=True)
    executor = make_executor(row, target)
    monkeypatch.setattr(settings, "write_execution_enabled", True)
    snapshot = await executor.capture_snapshot(row["id"], ["work_mem"])

    with pytest.raises(TargetVerificationError):
        await executor.apply(
            row["id"],
            [{"setting_name": "work_mem", "proposed_value": "8MB"}],
            snapshot,
        )

    assert target.values["work_mem"] == "4MB"
    assert any(command.startswith("ALTER SYSTEM RESET") for command in target.commands)


@pytest.mark.asyncio
async def test_apply_blocks_if_setting_drifted_after_approval(monkeypatch):
    row = make_policy_row()
    target = FakeTargetConnection()
    executor = make_executor(row, target)
    monkeypatch.setattr(settings, "write_execution_enabled", True)
    snapshot = await executor.capture_snapshot(row["id"], ["work_mem"])
    target.values["work_mem"] = "16MB"

    with pytest.raises(TargetValidationError, match="drifted after approval"):
        await executor.apply(
            row["id"],
            [{"setting_name": "work_mem", "proposed_value": "8MB"}],
            snapshot,
        )

    assert not any(command.startswith("ALTER SYSTEM SET") for command in target.commands)


@pytest.mark.asyncio
async def test_failed_rollback_is_reported_not_silenced(monkeypatch):
    row = make_policy_row()
    target = FakeTargetConnection(reload_succeeds=False)
    executor = make_executor(row, target)
    monkeypatch.setattr(settings, "write_execution_enabled", True)
    snapshot = {
        "work_mem": {
            "value": "4MB",
            "in_auto_conf": False,
        }
    }

    with pytest.raises(TargetVerificationError, match="rollback reload"):
        await executor.rollback(row["id"], snapshot)
