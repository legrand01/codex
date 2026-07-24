"""Atomic management of the enrolled PostgreSQL ``postgres_tune.conf`` file."""

import asyncio
import base64
import hashlib
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

SETTING_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
INCLUDE_DIRECTIVE = re.compile(
    r"^\s*(include|include_if_exists|include_dir)\s*(?:=\s*)?'([^']+)'\s*(?:#.*)?$"
)


class ManagedConfError(RuntimeError):
    """The managed file could not be proven safe or effective."""


def _normalise(value: Any) -> str:
    result = str(value).strip().lower()
    return {"true": "on", "false": "off", "yes": "on", "no": "off"}.get(
        result, result
    )


def _quote(value: Any) -> str:
    text = str(value)
    if len(text) > 256 or any(ord(char) < 32 for char in text):
        raise ManagedConfError("Setting value is too long or contains control characters")
    return "'" + text.replace("'", "''") + "'"


class ManagedPostgresConf:
    """Validate, atomically replace, reload, verify, and restore one include file."""

    def __init__(self, conn, managed_conf_path: str) -> None:
        self.conn = conn
        raw_path = Path(managed_conf_path)
        if not raw_path.is_absolute():
            raise ManagedConfError("Managed configuration path must be absolute")
        if ".." in raw_path.parts:
            raise ManagedConfError("Managed configuration path must not contain traversal")
        self.path = raw_path

    async def preflight(
        self,
        changes: List[Mapping[str, Any]],
        expected_snapshot: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        names = self._validate_changes(changes)
        errors: List[str] = []
        try:
            paths = await self._verify_filesystem_and_include()
            await self._verify_database_conflicts(names)
            await self._verify_file_order(names, allow_missing_managed=True)
            if expected_snapshot:
                await self._assert_no_drift(expected_snapshot)
        except Exception as exc:
            errors.append(str(exc))
            paths = {}
        return {"passed": not errors, "errors": errors, "environment": paths}

    async def apply(
        self,
        changes: List[Mapping[str, Any]],
        expected_snapshot: Mapping[str, Mapping[str, Any]],
    ) -> Dict[str, Any]:
        names = self._validate_changes(changes)
        preflight = await self.preflight(changes, expected_snapshot)
        if not preflight["passed"]:
            raise ManagedConfError("; ".join(preflight["errors"]))

        file_snapshot = self._capture_file()
        content = self._render(changes)
        applied_checksum: Optional[str] = None
        try:
            self._atomic_write(content, file_snapshot)
            applied_checksum = hashlib.sha256(content).hexdigest()
            await self._validate_postgresql_parse(names)
            await self._verify_file_order(names, allow_missing_managed=False)
            reloaded = await self.conn.fetchval("SELECT pg_reload_conf()")
            if reloaded is not True:
                raise ManagedConfError("PostgreSQL rejected pg_reload_conf()")
            verified, pending = await self._wait_for_applied(
                changes, expected_snapshot
            )
            snapshot = {
                "file": file_snapshot,
                "applied_checksum": applied_checksum,
                "managed_conf_path": str(self.path),
            }
            return {
                "succeeded": True,
                "verified_values": verified,
                "pending_restart": pending,
                "backend_snapshot": snapshot,
                "sourcefile": str(self.path),
            }
        except Exception as exc:
            try:
                self._restore_file(file_snapshot, expected_checksum=applied_checksum)
                await self.conn.fetchval("SELECT pg_reload_conf()")
                await self._wait_for_original(expected_snapshot)
            except Exception as restore_exc:
                raise ManagedConfError(
                    f"Managed apply failed ({exc}); immediate restore also failed ({restore_exc})"
                ) from exc
            raise ManagedConfError(f"Managed apply failed and was restored: {exc}") from exc

    async def rollback(
        self,
        backend_snapshot: Mapping[str, Any],
        expected_snapshot: Mapping[str, Mapping[str, Any]],
    ) -> Dict[str, Any]:
        file_snapshot = backend_snapshot.get("file")
        if not isinstance(file_snapshot, Mapping):
            raise ManagedConfError("Exact managed-file rollback bytes are missing")
        managed_path = backend_snapshot.get("managed_conf_path")
        if managed_path != str(self.path):
            raise ManagedConfError("Rollback path does not match the enrolled managed path")
        self._restore_file(
            file_snapshot,
            expected_checksum=backend_snapshot.get("applied_checksum"),
        )
        await self._validate_postgresql_parse([])
        reloaded = await self.conn.fetchval("SELECT pg_reload_conf()")
        if reloaded is not True:
            raise ManagedConfError("PostgreSQL rejected rollback pg_reload_conf()")
        verified = await self._wait_for_original(expected_snapshot)
        pending = [
            name
            for name, state in expected_snapshot.items()
            if state.get("context") == "postmaster" and state.get("pending_restart")
        ]
        return {
            "succeeded": True,
            "rolled_back": True,
            "verified_values": verified,
            "pending_restart": pending,
            "restored_absence": not bool(file_snapshot.get("existed")),
        }

    def _validate_changes(self, changes: List[Mapping[str, Any]]) -> List[str]:
        if not changes:
            return []
        names: List[str] = []
        for change in changes:
            name = str(change.get("setting_name", ""))
            if not SETTING_NAME.fullmatch(name):
                raise ManagedConfError(f"Invalid PostgreSQL setting name {name!r}")
            _quote(change.get("proposed_value", ""))
            if name in names:
                raise ManagedConfError(f"Duplicate setting {name!r}")
            names.append(name)
        return names

    async def _verify_filesystem_and_include(self) -> Dict[str, Any]:
        row = await self.conn.fetchrow(
            """
            SELECT current_setting('config_file') AS config_file,
                   current_setting('data_directory') AS data_directory
            """
        )
        if row is None:
            raise ManagedConfError("PostgreSQL did not report its configuration paths")
        config_file = Path(row["config_file"]).resolve(strict=True)
        if self.path.name != "postgres_tune.conf" or self.path.parent.name != "conf.d":
            raise ManagedConfError("Managed file must be named conf.d/postgres_tune.conf")
        if self.path.is_symlink():
            raise ManagedConfError("Managed configuration path must not be a symlink")
        parent = self.path.parent
        if not parent.is_dir() or parent.is_symlink():
            raise ManagedConfError("Managed configuration directory is missing or is a symlink")
        if not os.access(parent, os.W_OK):
            raise ManagedConfError("Host Agent cannot write the managed configuration directory")
        config_stat = config_file.stat()
        parent_stat = parent.stat()
        if parent_stat.st_uid != config_stat.st_uid:
            raise ManagedConfError(
                "Managed configuration directory owner differs from postgresql.conf owner"
            )
        if stat.S_IMODE(parent_stat.st_mode) & 0o022:
            raise ManagedConfError("Managed configuration directory is group/world writable")
        if self.path.exists():
            file_stat = self.path.stat()
            if file_stat.st_uid != config_stat.st_uid:
                raise ManagedConfError("Managed file owner differs from postgresql.conf owner")
            if stat.S_IMODE(file_stat.st_mode) & 0o022:
                raise ManagedConfError("Managed configuration file is group/world writable")
        if shutil.disk_usage(parent).free < 1024 * 1024:
            raise ManagedConfError("Less than 1 MiB is free for an atomic configuration write")
        if not self._is_included(config_file):
            raise ManagedConfError(
                f"{self.path} is not included by PostgreSQL config_file {config_file}"
            )
        return {
            "config_file": str(config_file),
            "data_directory": row["data_directory"],
            "managed_conf_path": str(self.path),
            "owner_uid": config_stat.st_uid,
            "owner_gid": config_stat.st_gid,
        }

    def _is_included(self, config_file: Path) -> bool:
        try:
            lines = config_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ManagedConfError(f"Cannot read PostgreSQL config_file: {exc}") from exc
        for line in lines:
            match = INCLUDE_DIRECTIVE.match(line)
            if not match:
                continue
            directive, raw = match.groups()
            included = Path(raw)
            if not included.is_absolute():
                included = config_file.parent / included
            included = included.resolve(strict=False)
            if directive == "include_dir" and included == self.path.parent:
                return True
            if directive in {"include", "include_if_exists"} and included == self.path:
                return True
        return False

    async def _verify_database_conflicts(self, names: List[str]) -> None:
        if not names:
            return
        rows = await self.conn.fetch(
            """
            SELECT name, source, sourcefile
            FROM pg_settings
            WHERE name = ANY($1::text[])
            """,
            names,
        )
        found = {row["name"] for row in rows}
        missing = sorted(set(names) - found)
        if missing:
            raise ManagedConfError("Unknown PostgreSQL settings: " + ", ".join(missing))
        for row in rows:
            sourcefile = str(row["sourcefile"] or "")
            if row["source"] == "command line":
                raise ManagedConfError(f"{row['name']} is overridden on the command line")
            if sourcefile.endswith("postgresql.auto.conf"):
                raise ManagedConfError(f"{row['name']} is overridden by postgresql.auto.conf")
        overrides = await self.conn.fetch(
            "SELECT setconfig FROM pg_db_role_setting WHERE setconfig IS NOT NULL"
        )
        for row in overrides:
            for assignment in row["setconfig"] or []:
                name = assignment.split("=", 1)[0]
                if name in names:
                    raise ManagedConfError(f"{name} has a database/user override")

    async def _verify_file_order(
        self, names: List[str], *, allow_missing_managed: bool
    ) -> None:
        if not names:
            return
        rows = await self.conn.fetch(
            """
            SELECT seqno, sourcefile, sourceline, name, applied, error
            FROM public.dbtune_file_settings()
            WHERE name = ANY($1::text[]) OR error IS NOT NULL
            ORDER BY seqno
            """,
            names,
        )
        for row in rows:
            if row["error"]:
                raise ManagedConfError(
                    f"Existing PostgreSQL configuration parse error: {row['error']}"
                )
        managed_seq = [
            row["seqno"]
            for row in rows
            if Path(row["sourcefile"] or "").resolve(strict=False) == self.path
        ]
        if not managed_seq:
            if allow_missing_managed:
                return
            raise ManagedConfError("Managed file is not visible in pg_file_settings")
        last_managed = max(managed_seq)
        later = [
            row["name"]
            for row in rows
            if row["name"] in names
            and row["seqno"] > last_managed
            and Path(row["sourcefile"] or "").resolve(strict=False) != self.path
        ]
        if later:
            raise ManagedConfError(
                "Settings are overridden by a later configuration source: "
                + ", ".join(sorted(set(later)))
            )

    async def _assert_no_drift(
        self, expected_snapshot: Mapping[str, Mapping[str, Any]]
    ) -> None:
        for name, expected in expected_snapshot.items():
            current = await self.conn.fetchval("SELECT current_setting($1)", name)
            if _normalise(current) != _normalise(expected["value"]):
                raise ManagedConfError(
                    f"{name} drifted after approval: expected {expected['value']!r}, "
                    f"found {current!r}"
                )

    def _capture_file(self) -> Dict[str, Any]:
        if not self.path.exists():
            config_stat = self.path.parent.stat()
            return {
                "existed": False,
                "bytes_b64": "",
                "checksum": None,
                "mode": 0o600,
                "uid": config_stat.st_uid,
                "gid": config_stat.st_gid,
            }
        raw = self.path.read_bytes()
        file_stat = self.path.stat()
        return {
            "existed": True,
            "bytes_b64": base64.b64encode(raw).decode("ascii"),
            "checksum": hashlib.sha256(raw).hexdigest(),
            "mode": stat.S_IMODE(file_stat.st_mode),
            "uid": file_stat.st_uid,
            "gid": file_stat.st_gid,
        }

    def _render(self, changes: List[Mapping[str, Any]]) -> bytes:
        lines = [
            "# Managed by Postgres Tune Doctor. Manual edits will be replaced.",
            "# Rollback provenance is retained by the control plane.",
        ]
        for change in sorted(changes, key=lambda item: str(item["setting_name"])):
            lines.append(
                f"{change['setting_name']} = {_quote(change['proposed_value'])}"
            )
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _atomic_write(
        self, content: bytes, previous: Mapping[str, Any]
    ) -> None:
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(temp, flags, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, int(previous.get("mode") or 0o600))
            try:
                os.chown(temp, int(previous["uid"]), int(previous["gid"]))
            except PermissionError:
                temp_stat = temp.stat()
                if (
                    temp_stat.st_uid != int(previous["uid"])
                    or temp_stat.st_gid != int(previous["gid"])
                ):
                    raise
            os.replace(temp, self.path)
            self._fsync_directory()
        finally:
            if temp.exists():
                temp.unlink()

    def _restore_file(
        self, snapshot: Mapping[str, Any], *, expected_checksum: Optional[str]
    ) -> None:
        if expected_checksum and self.path.exists():
            current = hashlib.sha256(self.path.read_bytes()).hexdigest()
            if current != expected_checksum:
                raise ManagedConfError(
                    "Managed file changed after apply; refusing to overwrite external edits"
                )
        if snapshot.get("existed"):
            raw = base64.b64decode(str(snapshot.get("bytes_b64") or ""), validate=True)
            if hashlib.sha256(raw).hexdigest() != snapshot.get("checksum"):
                raise ManagedConfError("Stored rollback bytes do not match their checksum")
            self._atomic_write(raw, snapshot)
        elif self.path.exists():
            self.path.unlink()
            self._fsync_directory()

    def _fsync_directory(self) -> None:
        directory_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    async def _validate_postgresql_parse(self, names: List[str]) -> None:
        rows = await self.conn.fetch(
            """
            SELECT name, applied, error
            FROM public.dbtune_file_settings()
            WHERE sourcefile = $1
            ORDER BY seqno
            """,
            str(self.path),
        )
        errors = [row["error"] for row in rows if row["error"]]
        if errors:
            raise ManagedConfError("PostgreSQL rejected managed file: " + "; ".join(errors))
        seen = {row["name"] for row in rows if row["applied"]}
        missing = sorted(set(names) - seen)
        if missing:
            raise ManagedConfError(
                "Managed settings were not applied by pg_file_settings: " + ", ".join(missing)
            )

    async def _verify_applied(
        self,
        changes: List[Mapping[str, Any]],
        expected_snapshot: Mapping[str, Mapping[str, Any]],
    ) -> tuple:
        verified: Dict[str, str] = {}
        pending: List[str] = []
        for change in changes:
            name = str(change["setting_name"])
            row = await self.conn.fetchrow(
                "SELECT current_setting(name) AS current_value, setting, context, "
                "sourcefile, pending_restart "
                "FROM pg_settings WHERE name=$1",
                name,
            )
            if row is None:
                raise ManagedConfError(f"Post-reload setting {name!r} disappeared")
            if Path(row["sourcefile"] or "").resolve(strict=False) != self.path:
                raise ManagedConfError(f"{name} does not report the managed file as sourcefile")
            verified[name] = str(row["current_value"])
            if row["context"] == "postmaster":
                if not row["pending_restart"]:
                    raise ManagedConfError(f"{name} was not staged as pending_restart")
                pending.append(name)
            elif _normalise(row["current_value"]) != _normalise(
                change["proposed_value"]
            ):
                raise ManagedConfError(
                    f"{name} did not converge: expected {change['proposed_value']!r}, "
                    f"found {row['current_value']!r}"
                )
        return verified, pending

    async def _wait_for_applied(
        self,
        changes: List[Mapping[str, Any]],
        expected_snapshot: Mapping[str, Mapping[str, Any]],
    ) -> tuple:
        deadline = asyncio.get_running_loop().time() + 10
        last_error: Optional[Exception] = None
        while True:
            try:
                return await self._verify_applied(changes, expected_snapshot)
            except ManagedConfError as exc:
                last_error = exc
            if asyncio.get_running_loop().time() >= deadline:
                raise ManagedConfError(f"Reload did not converge: {last_error}")
            await asyncio.sleep(0.2)

    async def _verify_original(
        self, expected_snapshot: Mapping[str, Mapping[str, Any]]
    ) -> Dict[str, str]:
        verified: Dict[str, str] = {}
        for name, expected in expected_snapshot.items():
            row = await self.conn.fetchrow(
                "SELECT current_setting(name) AS current_value, setting, source, "
                "sourcefile, pending_restart "
                "FROM pg_settings WHERE name=$1",
                name,
            )
            if row is None or _normalise(row["current_value"]) != _normalise(
                expected["value"]
            ):
                found = None if row is None else row["current_value"]
                raise ManagedConfError(
                    f"Rollback verification failed for {name}: expected {expected['value']!r}, "
                    f"found {found!r}"
                )
            expected_file = expected.get("sourcefile")
            observed_file = row["sourcefile"]
            if (expected_file or None) != (observed_file or None):
                raise ManagedConfError(
                    f"Rollback provenance failed for {name}: expected {expected_file!r}, "
                    f"found {observed_file!r}"
                )
            verified[name] = str(row["current_value"])
        return verified

    async def _wait_for_original(
        self, expected_snapshot: Mapping[str, Mapping[str, Any]]
    ) -> Dict[str, str]:
        deadline = asyncio.get_running_loop().time() + 10
        last_error: Optional[Exception] = None
        while True:
            try:
                return await self._verify_original(expected_snapshot)
            except ManagedConfError as exc:
                last_error = exc
            if asyncio.get_running_loop().time() >= deadline:
                raise ManagedConfError(f"Rollback reload did not converge: {last_error}")
            await asyncio.sleep(0.2)
