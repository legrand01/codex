"""Managed ``postgres_tune.conf`` atomic apply and rollback tests."""

import hashlib
import os
import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from host_agent.managed_conf import ManagedConfError, ManagedPostgresConf


class FakeManagedConnection:
    def __init__(self, config_file: Path, managed_file: Path):
        self.config_file = config_file
        self.managed_file = managed_file
        self.values = {"work_mem": "4MB", "shared_buffers": "128MB"}
        self.contexts = {"work_mem": "user", "shared_buffers": "postmaster"}
        self.sources = {"work_mem": ("default", None), "shared_buffers": ("default", None)}
        self.pending_restart = {"work_mem": False, "shared_buffers": False}
        self.reload_succeeds = True
        self.parse_error = None
        self.later_override = False

    def _managed_rows(self):
        if not self.managed_file.exists():
            return []
        rows = []
        for seqno, line in enumerate(self.managed_file.read_text().splitlines(), 1):
            if not line or line.startswith("#"):
                continue
            name, raw = line.split("=", 1)
            rows.append(
                {
                    "seqno": seqno,
                    "sourcefile": str(self.managed_file),
                    "sourceline": seqno,
                    "name": name.strip(),
                    "setting": raw.strip().strip("'"),
                    "applied": True,
                    "error": self.parse_error,
                }
            )
        if self.later_override and rows:
            rows.append(
                {
                    "seqno": rows[-1]["seqno"] + 100,
                    "sourcefile": str(self.config_file.parent / "conf.d" / "zz-local.conf"),
                    "sourceline": 1,
                    "name": rows[0]["name"],
                    "setting": "16MB",
                    "applied": True,
                    "error": None,
                }
            )
        return rows

    async def fetch(self, query, *args):
        if "pg_db_role_setting" in query:
            return []
        if "dbtune_file_settings" in query:
            if "sourcefile = $1" in query:
                return self._managed_rows()
            names = set(args[0]) if args else set()
            return [row for row in self._managed_rows() if not names or row["name"] in names]
        if "FROM pg_settings" in query:
            names = set(args[0])
            return [
                {
                    "name": name,
                    "source": self.sources[name][0],
                    "sourcefile": self.sources[name][1],
                }
                for name in names
                if name in self.values
            ]
        return []

    async def fetchrow(self, query, *args):
        if "current_setting('config_file')" in query:
            return {
                "config_file": str(self.config_file),
                "data_directory": str(self.config_file.parent),
            }
        if "FROM pg_settings WHERE name=$1" in query:
            name = args[0]
            source, sourcefile = self.sources[name]
            return {
                "current_value": self.values[name],
                "setting": self.values[name],
                "context": self.contexts[name],
                "source": source,
                "sourcefile": sourcefile,
                "pending_restart": self.pending_restart[name],
            }
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "current_setting($1)" in query:
            return self.values[args[0]]
        if "pg_reload_conf" in query:
            if not self.reload_succeeds:
                return False
            rows = self._managed_rows()
            managed_names = {row["name"] for row in rows}
            for name in self.values:
                if name not in managed_names and self.sources[name][1] == str(self.managed_file):
                    self.values[name] = "4MB" if name == "work_mem" else "128MB"
                    self.sources[name] = ("default", None)
                    self.pending_restart[name] = False
            for row in rows:
                name = row["name"]
                self.sources[name] = ("configuration file", str(self.managed_file))
                if self.contexts[name] == "postmaster":
                    self.pending_restart[name] = True
                else:
                    self.values[name] = row["setting"]
            return True
        raise AssertionError(query)


@pytest.fixture
def managed_environment(tmp_path):
    config_file = tmp_path / "postgresql.conf"
    conf_dir = tmp_path / "conf.d"
    conf_dir.mkdir(mode=0o700)
    config_file.write_text("include_dir = 'conf.d'\n", encoding="utf-8")
    config_file.chmod(0o600)
    managed_file = conf_dir / "postgres_tune.conf"
    conn = FakeManagedConnection(config_file, managed_file)
    return ManagedPostgresConf(conn, str(managed_file)), conn, managed_file


@pytest.mark.asyncio
async def test_atomic_apply_and_exact_absence_rollback(managed_environment):
    manager, conn, managed_file = managed_environment
    snapshot = {
        "work_mem": {
            "value": "4MB",
            "context": "user",
            "source": "default",
            "sourcefile": None,
        }
    }

    applied = await manager.apply(
        [{"setting_name": "work_mem", "proposed_value": "8MB"}], snapshot
    )

    assert managed_file.read_text().endswith("work_mem = '8MB'\n")
    assert applied["verified_values"] == {"work_mem": "8MB"}
    assert applied["backend_snapshot"]["file"]["existed"] is False
    assert applied["backend_snapshot"]["applied_checksum"] == hashlib.sha256(
        managed_file.read_bytes()
    ).hexdigest()

    rolled_back = await manager.rollback(applied["backend_snapshot"], snapshot)
    assert rolled_back["rolled_back"] is True
    assert rolled_back["restored_absence"] is True
    assert not managed_file.exists()
    assert conn.values["work_mem"] == "4MB"
    assert conn.sources["work_mem"] == ("default", None)


@pytest.mark.asyncio
async def test_restart_setting_is_staged_not_claimed_active(managed_environment):
    manager, conn, _ = managed_environment
    snapshot = {
        "shared_buffers": {
            "value": "128MB",
            "context": "postmaster",
            "source": "default",
            "sourcefile": None,
        }
    }

    applied = await manager.apply(
        [{"setting_name": "shared_buffers", "proposed_value": "256MB"}], snapshot
    )

    assert applied["pending_restart"] == ["shared_buffers"]
    assert applied["verified_values"]["shared_buffers"] == "128MB"
    assert conn.values["shared_buffers"] == "128MB"


@pytest.mark.asyncio
async def test_auto_conf_override_is_blocked(managed_environment):
    manager, conn, _ = managed_environment
    conn.sources["work_mem"] = (
        "configuration file",
        str(conn.config_file.parent / "postgresql.auto.conf"),
    )

    result = await manager.preflight([{"setting_name": "work_mem", "proposed_value": "8MB"}])

    assert result["passed"] is False
    assert "postgresql.auto.conf" in result["errors"][0]


@pytest.mark.asyncio
async def test_rollback_refuses_to_clobber_external_edit(managed_environment):
    manager, _, managed_file = managed_environment
    snapshot = {
        "work_mem": {
            "value": "4MB",
            "context": "user",
            "source": "default",
            "sourcefile": None,
        }
    }
    applied = await manager.apply(
        [{"setting_name": "work_mem", "proposed_value": "8MB"}], snapshot
    )
    managed_file.write_text("work_mem = '16MB'\n", encoding="utf-8")

    with pytest.raises(ManagedConfError, match="refusing to overwrite external edits"):
        await manager.rollback(applied["backend_snapshot"], snapshot)


@pytest.mark.asyncio
async def test_invalid_pg_file_settings_restores_absence(managed_environment):
    manager, conn, managed_file = managed_environment
    conn.parse_error = "invalid value for parameter work_mem"
    snapshot = {
        "work_mem": {
            "value": "4MB",
            "context": "user",
            "source": "default",
            "sourcefile": None,
        }
    }

    with pytest.raises(ManagedConfError, match="was restored"):
        await manager.apply(
            [{"setting_name": "work_mem", "proposed_value": "not-memory"}], snapshot
        )

    assert not managed_file.exists()


@pytest.mark.asyncio
async def test_later_include_override_blocks_and_restores(managed_environment):
    manager, conn, managed_file = managed_environment
    conn.later_override = True
    snapshot = {
        "work_mem": {
            "value": "4MB",
            "context": "user",
            "source": "default",
            "sourcefile": None,
        }
    }

    with pytest.raises(ManagedConfError, match="later configuration source"):
        await manager.apply(
            [{"setting_name": "work_mem", "proposed_value": "8MB"}], snapshot
        )

    assert not managed_file.exists()


@pytest.mark.asyncio
async def test_reload_failure_restores_file_before_reporting_failure(managed_environment):
    manager, conn, managed_file = managed_environment
    conn.reload_succeeds = False
    snapshot = {
        "work_mem": {
            "value": "4MB",
            "context": "user",
            "source": "default",
            "sourcefile": None,
        }
    }

    with pytest.raises(ManagedConfError, match="was restored"):
        await manager.apply(
            [{"setting_name": "work_mem", "proposed_value": "8MB"}], snapshot
        )

    assert not managed_file.exists()


@given(previous=st.binary(max_size=512), replacement=st.binary(max_size=512))
def test_byte_exact_restore_property(previous, replacement):
    with tempfile.TemporaryDirectory() as raw_dir:
        root = Path(raw_dir)
        conf_dir = root / "conf.d"
        conf_dir.mkdir(mode=0o700)
        managed_file = conf_dir / "postgres_tune.conf"
        managed_file.write_bytes(previous)
        manager = ManagedPostgresConf(None, str(managed_file))
        snapshot = manager._capture_file()

        manager._atomic_write(replacement, snapshot)
        manager._restore_file(
            snapshot, expected_checksum=hashlib.sha256(replacement).hexdigest()
        )

        assert managed_file.read_bytes() == previous
        assert os.stat(managed_file).st_mode & 0o777 == snapshot["mode"]


def test_atomic_replace_failure_preserves_previous_bytes(monkeypatch):
    with tempfile.TemporaryDirectory() as raw_dir:
        root = Path(raw_dir)
        conf_dir = root / "conf.d"
        conf_dir.mkdir(mode=0o700)
        managed_file = conf_dir / "postgres_tune.conf"
        managed_file.write_bytes(b"work_mem = '4MB'\n")
        manager = ManagedPostgresConf(None, str(managed_file))
        snapshot = manager._capture_file()

        def fail_replace(*args):
            raise OSError("simulated rename failure")

        monkeypatch.setattr(os, "replace", fail_replace)
        with pytest.raises(OSError, match="rename failure"):
            manager._atomic_write(b"work_mem = '8MB'\n", snapshot)

        assert managed_file.read_bytes() == b"work_mem = '4MB'\n"
