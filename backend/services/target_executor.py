"""Fail-closed PostgreSQL target execution for P0 tuning operations.

The control-plane database stores only the name of an environment variable
containing the target DSN.  Writes require independent global and per-host
interlocks.  Only PostgreSQL settings that can be validated with ``SET LOCAL``
are supported in P0; arbitrary SQL, index DDL, replicas, and restart-only
parameters are rejected.
"""

import asyncio
import os
import re
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator, Callable, Dict, List, Mapping, Optional
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import asyncpg

from backend.config import settings

PRODUCTION_CONFIRMATION = "PRODUCTION_WRITES_AUTHORIZED"
EXECUTION_LOCK_ID = 7_340_971_311
SETTING_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
SUPPORTED_CONTEXTS = {"user", "superuser"}


class TargetExecutionError(RuntimeError):
    """Base target execution failure."""


class WriteInterlockError(TargetExecutionError):
    """Raised when a production-write safety interlock is not satisfied."""


class TargetValidationError(TargetExecutionError):
    """Raised when a proposed target change cannot be proven safe."""


class TargetVerificationError(TargetExecutionError):
    """Raised when an applied or rolled-back value cannot be verified."""


@dataclass(frozen=True)
class HostExecutionPolicy:
    host_id: UUID
    hostname: str
    environment: str
    server_role: Optional[str]
    target_dsn_env: Optional[str]
    writes_enabled: bool


@dataclass(frozen=True)
class SettingSnapshot:
    name: str
    value: str
    unit: Optional[str]
    context: str
    source: str
    sourcefile: Optional[str]
    pending_restart: bool
    in_auto_conf: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DryRunResult:
    passed: bool
    snapshot: Dict[str, Dict[str, Any]]
    errors: List[str]


@dataclass(frozen=True)
class ExecutionResult:
    succeeded: bool
    changed_settings: List[str]
    verified_values: Dict[str, str]
    rolled_back: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _quote_identifier(value: str) -> str:
    if not SETTING_NAME_PATTERN.fullmatch(value):
        raise TargetValidationError(f"Invalid PostgreSQL setting name: {value!r}")
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    if len(value) > 256 or any(ord(char) < 32 for char in value):
        raise TargetValidationError("PostgreSQL setting value is too long or contains controls")
    return "'" + value.replace("'", "''") + "'"


def _normalise_value(value: Any) -> str:
    normalised = str(value).strip().lower()
    aliases = {"true": "on", "false": "off", "yes": "on", "no": "off"}
    return aliases.get(normalised, normalised)


class TargetPostgresExecutor:
    """Read, validate, apply, verify, and roll back target settings."""

    def __init__(
        self,
        control_pool,
        connector: Callable[..., Any] = asyncpg.connect,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.control_pool = control_pool
        self.connector = connector
        self.environ = environ if environ is not None else os.environ

    async def load_policy(self, host_id: UUID) -> HostExecutionPolicy:
        async with self.control_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, hostname, environment, server_role,
                       target_dsn_env, writes_enabled
                FROM hosts
                WHERE id = $1
                """,
                host_id,
            )
        if row is None:
            raise TargetExecutionError(f"Target host {host_id} does not exist")
        return HostExecutionPolicy(
            host_id=row["id"],
            hostname=row["hostname"],
            environment=row["environment"],
            server_role=row["server_role"],
            target_dsn_env=row["target_dsn_env"],
            writes_enabled=bool(row["writes_enabled"]),
        )

    def resolve_dsn(self, policy: HostExecutionPolicy, *, for_write: bool) -> str:
        if not policy.target_dsn_env:
            raise TargetExecutionError("Target DSN secret reference is not configured")
        dsn = self.environ.get(policy.target_dsn_env, "").strip()
        if not dsn:
            raise TargetExecutionError(
                f"Target DSN environment variable {policy.target_dsn_env!r} is not set"
            )

        parsed = urlparse(dsn)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            raise TargetExecutionError("Target DSN must be a PostgreSQL connection URL")

        if policy.environment == "production":
            sslmode = parse_qs(parsed.query).get("sslmode", [""])[0]
            if sslmode not in {"require", "verify-ca", "verify-full"}:
                raise WriteInterlockError("Production target DSN must require TLS")

        if for_write:
            self.assert_write_allowed(policy)
        return dsn

    @staticmethod
    def assert_write_allowed(policy: HostExecutionPolicy) -> None:
        if not settings.write_execution_enabled:
            raise WriteInterlockError("Global target write execution is disabled")
        if not policy.writes_enabled:
            raise WriteInterlockError("Writes are disabled for this host")
        if policy.server_role != "primary":
            raise WriteInterlockError("Target writes require a confirmed primary server")
        if policy.environment == "production":
            if not settings.production_write_enabled:
                raise WriteInterlockError("Production target writes are disabled")
            if settings.production_write_confirmation != PRODUCTION_CONFIRMATION:
                raise WriteInterlockError("Production write confirmation is missing")

    @asynccontextmanager
    async def connect(self, host_id: UUID, *, for_write: bool) -> AsyncIterator[Any]:
        policy = await self.load_policy(host_id)
        dsn = self.resolve_dsn(policy, for_write=for_write)
        conn = await self.connector(
            dsn,
            timeout=settings.target_connect_timeout_sec,
            command_timeout=settings.target_command_timeout_sec,
        )
        try:
            is_replica = await conn.fetchval("SELECT pg_is_in_recovery()")
            if for_write and is_replica:
                raise WriteInterlockError("Connected target reports that it is a replica")
            yield conn
        finally:
            await conn.close()

    async def capture_snapshot(
        self, host_id: UUID, setting_names: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        names = self._validate_setting_names(setting_names)
        async with self.connect(host_id, for_write=False) as conn:
            rows = await conn.fetch(
                """
                SELECT name, current_setting(name) AS current_value, unit, context,
                       source, sourcefile, pending_restart,
                       COALESCE(sourcefile LIKE '%postgresql.auto.conf', FALSE) AS in_auto_conf
                FROM pg_settings
                WHERE name = ANY($1::text[])
                ORDER BY name
                """,
                names,
            )
        snapshot = {
            row["name"]: SettingSnapshot(
                name=row["name"],
                value=row["current_value"],
                unit=row["unit"],
                context=row["context"],
                source=row["source"],
                sourcefile=row["sourcefile"],
                pending_restart=bool(row["pending_restart"]),
                in_auto_conf=bool(row["in_auto_conf"]),
            ).to_dict()
            for row in rows
        }
        missing = sorted(set(names) - set(snapshot))
        if missing:
            raise TargetValidationError(f"Unknown PostgreSQL settings: {', '.join(missing)}")
        return snapshot

    async def dry_run(self, host_id: UUID, proposed_changes: List[dict]) -> DryRunResult:
        errors: List[str] = []
        try:
            changes = self._validate_changes(proposed_changes)
            snapshot = await self.capture_snapshot(host_id, [c["setting_name"] for c in changes])
            for change in changes:
                setting = snapshot[change["setting_name"]]
                if setting["context"] not in SUPPORTED_CONTEXTS:
                    errors.append(
                        f"Setting {change['setting_name']!r} has unsupported context "
                        f"{setting['context']!r}; P0 permits online SET-capable parameters only"
                    )
                if setting["pending_restart"]:
                    errors.append(f"Setting {change['setting_name']!r} already requires restart")

            if errors:
                return DryRunResult(False, snapshot, errors)

            async with self.connect(host_id, for_write=False) as conn:
                async with conn.transaction():
                    for change in changes:
                        await conn.fetchval(
                            "SELECT set_config($1, $2, true)",
                            change["setting_name"],
                            change["proposed_value"],
                        )
            return DryRunResult(True, snapshot, [])
        except TargetExecutionError as exc:
            return DryRunResult(False, {}, [str(exc)])
        except Exception as exc:
            return DryRunResult(False, {}, [f"Target dry-run failed: {exc}"])

    async def read_current_values(
        self, host_id: UUID, setting_names: List[str]
    ) -> Dict[str, str]:
        names = self._validate_setting_names(setting_names)
        async with self.connect(host_id, for_write=False) as conn:
            return {
                name: str(await conn.fetchval("SELECT current_setting($1)", name))
                for name in names
            }

    async def verify_expected_values(
        self, host_id: UUID, expected: Mapping[str, Any]
    ) -> Dict[str, str]:
        self._validate_setting_names(list(expected))
        async with self.connect(host_id, for_write=False) as conn:
            return await self._verify_values(conn, expected)

    async def apply(
        self,
        host_id: UUID,
        proposed_changes: List[dict],
        expected_snapshot: Mapping[str, Mapping[str, Any]],
    ) -> ExecutionResult:
        changes = self._validate_changes(proposed_changes)
        names = [change["setting_name"] for change in changes]
        if set(names) != set(expected_snapshot):
            raise TargetValidationError("Expected pre-change snapshot does not match the plan")

        async with self.connect(host_id, for_write=True) as conn:
            await conn.execute("SELECT pg_advisory_lock($1)", EXECUTION_LOCK_ID)
            applied: List[str] = []
            try:
                await self._assert_no_drift(conn, expected_snapshot)
                for change in changes:
                    name = change["setting_name"]
                    sql = (
                        f"ALTER SYSTEM SET {_quote_identifier(name)} = "
                        f"{_quote_literal(change['proposed_value'])}"
                    )
                    await conn.execute(sql)
                    applied.append(name)
                reloaded = await conn.fetchval("SELECT pg_reload_conf()")
                if reloaded is not True:
                    raise TargetVerificationError("PostgreSQL rejected configuration reload")
                verified = await self._verify_values(
                    conn,
                    {c["setting_name"]: c["proposed_value"] for c in changes},
                )
                return ExecutionResult(True, applied, verified)
            except Exception:
                if applied:
                    await self._restore_with_connection(conn, expected_snapshot, applied)
                raise
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", EXECUTION_LOCK_ID)

    async def rollback(
        self,
        host_id: UUID,
        snapshot: Mapping[str, Mapping[str, Any]],
    ) -> ExecutionResult:
        if not snapshot:
            raise TargetValidationError("Rollback snapshot is empty")
        self._validate_setting_names(list(snapshot))
        async with self.connect(host_id, for_write=True) as conn:
            await conn.execute("SELECT pg_advisory_lock($1)", EXECUTION_LOCK_ID)
            try:
                verified = await self._restore_with_connection(conn, snapshot, list(snapshot))
                return ExecutionResult(True, list(snapshot), verified, rolled_back=True)
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", EXECUTION_LOCK_ID)

    @staticmethod
    def _validate_setting_names(setting_names: List[str]) -> List[str]:
        if not setting_names:
            raise TargetValidationError("No PostgreSQL settings were provided")
        unique: List[str] = []
        for name in setting_names:
            _quote_identifier(name)
            if name not in unique:
                unique.append(name)
        return unique

    def _validate_changes(self, proposed_changes: List[dict]) -> List[Dict[str, str]]:
        if not proposed_changes:
            raise TargetValidationError("Plan contains no proposed changes")
        result: List[Dict[str, str]] = []
        for change in proposed_changes:
            if change.get("change_type", "setting") != "setting":
                raise TargetValidationError("P0 does not execute arbitrary SQL or index DDL")
            name = str(change.get("setting_name", ""))
            value = str(change.get("proposed_value", ""))
            _quote_identifier(name)
            _quote_literal(value)
            result.append({"setting_name": name, "proposed_value": value})
        if len({change["setting_name"] for change in result}) != len(result):
            raise TargetValidationError("Plan contains duplicate setting changes")
        return result

    async def _assert_no_drift(
        self, conn, expected_snapshot: Mapping[str, Mapping[str, Any]]
    ) -> None:
        for name, expected in expected_snapshot.items():
            current = await conn.fetchval("SELECT current_setting($1)", name)
            if _normalise_value(current) != _normalise_value(expected["value"]):
                raise TargetValidationError(
                    f"Setting {name!r} drifted after approval; expected "
                    f"{expected['value']!r}, found {current!r}"
                )

    async def _verify_values(self, conn, expected: Mapping[str, Any]) -> Dict[str, str]:
        deadline = asyncio.get_running_loop().time() + settings.target_verify_timeout_sec
        last_values: Dict[str, str] = {}
        while True:
            last_values = {
                name: str(await conn.fetchval("SELECT current_setting($1)", name))
                for name in expected
            }
            if all(
                _normalise_value(last_values[name]) == _normalise_value(value)
                for name, value in expected.items()
            ):
                return last_values
            if asyncio.get_running_loop().time() >= deadline:
                raise TargetVerificationError(
                    f"Target values did not converge: expected={dict(expected)!r}, "
                    f"observed={last_values!r}"
                )
            await asyncio.sleep(0.2)

    async def _restore_with_connection(
        self,
        conn,
        snapshot: Mapping[str, Mapping[str, Any]],
        setting_names: List[str],
    ) -> Dict[str, str]:
        for name in reversed(setting_names):
            state = snapshot[name]
            if state.get("in_auto_conf"):
                sql = (
                    f"ALTER SYSTEM SET {_quote_identifier(name)} = "
                    f"{_quote_literal(str(state['value']))}"
                )
            else:
                sql = f"ALTER SYSTEM RESET {_quote_identifier(name)}"
            await conn.execute(sql)
        reloaded = await conn.fetchval("SELECT pg_reload_conf()")
        if reloaded is not True:
            raise TargetVerificationError("PostgreSQL rejected rollback reload")
        expected = {name: snapshot[name]["value"] for name in setting_names}
        return await self._verify_values(conn, expected)
