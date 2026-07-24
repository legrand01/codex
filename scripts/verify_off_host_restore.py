"""Restore a staging backup into a dedicated independent PostgreSQL database."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
GIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
SAFE_DATABASE = re.compile(r"^dbtune_restore_[a-zA-Z0-9_]+$")
REQUIRED_SSL_MODE = "verify-full"


@dataclass(frozen=True)
class RestoreTarget:
    host: str
    port: int
    user: str
    password: str
    database: str
    sslmode: str
    sslrootcert: str | None = None
    sslcert: str | None = None
    sslkey: str | None = None

    def client_environment(self) -> dict[str, str]:
        environment = {
            key: value
            for key in ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR")
            if (value := os.environ.get(key))
        }
        environment.update(
            {
                "PGHOST": self.host,
                "PGPORT": str(self.port),
                "PGUSER": self.user,
                "PGPASSWORD": self.password,
                "PGDATABASE": self.database,
                "PGSSLMODE": self.sslmode,
            }
        )
        for name, value in (
            ("PGSSLROOTCERT", self.sslrootcert),
            ("PGSSLCERT", self.sslcert),
            ("PGSSLKEY", self.sslkey),
        ):
            if value:
                environment[name] = value
        return environment


def parse_restore_target(dsn: str) -> RestoreTarget:
    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("restore DSN must use postgres or postgresql")
    host = parsed.hostname or ""
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    database = unquote(parsed.path.lstrip("/"))
    if not host or not user or not password:
        raise ValueError("restore DSN must include host, user, and password")
    if not SAFE_DATABASE.fullmatch(database):
        raise ValueError(
            "restore database name must start with dbtune_restore_ and use "
            "letters, numbers, or underscores only"
        )

    query = parse_qs(parsed.query, keep_blank_values=True)
    sslmode = query.get("sslmode", [""])[-1]
    if sslmode != REQUIRED_SSL_MODE:
        raise ValueError("restore DSN sslmode must be verify-full")
    return RestoreTarget(
        host=host,
        port=parsed.port or 5432,
        user=user,
        password=password,
        database=database,
        sslmode=sslmode,
        sslrootcert=query.get("sslrootcert", [None])[-1],
        sslcert=query.get("sslcert", [None])[-1],
        sslkey=query.get("sslkey", [None])[-1],
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def expected_checksum(path: Path) -> str:
    token = path.read_text(encoding="utf-8").split(maxsplit=1)[0]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", token):
        raise ValueError("checksum file must begin with a 64-character SHA-256")
    return token.lower()


def client_major(executable: str) -> int:
    result = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"\b(\d+)(?:\.\d+)*\b", result.stdout + result.stderr)
    if result.returncode or match is None:
        raise RuntimeError(f"cannot determine PostgreSQL client version: {executable}")
    return int(match.group(1))


def run_psql(target: RestoreTarget, query: str, executable: str = "psql") -> str:
    result = subprocess.run(
        [
            executable,
            "--no-psqlrc",
            "--set=ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--dbname",
            target.database,
            "--command",
            query,
        ],
        env=target.client_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"psql validation failed: {result.stderr.strip()}")
    return result.stdout.strip()


def restore_dump(
    target: RestoreTarget,
    dump: Path,
    executable: str = "pg_restore",
) -> None:
    result = subprocess.run(
        [
            executable,
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            target.database,
            str(dump),
        ],
        env=target.client_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"pg_restore failed: {result.stderr.strip()}")


def build_evidence(
    *,
    release_sha: str,
    verified_by: str,
    restore_test_id: str,
    source_host: str,
    restore_host: str,
    dump_checksum: str,
    dump_size_bytes: int,
    migrations: int,
    tables: int,
    postgres_major: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "release_candidate_sha": release_sha,
        "gates": {
            "independent_off_host_restore": {
                "verified": True,
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "verified_by": verified_by,
                "evidence_id": restore_test_id,
                "evidence": {
                    "source_backup_sha256": dump_checksum,
                    "source_host": source_host,
                    "restore_host": restore_host,
                    "restore_test_id": restore_test_id,
                },
                "measurements": {
                    "dump_size_bytes": dump_size_bytes,
                    "schema_migrations": migrations,
                    "public_tables": tables,
                    "postgres_major": postgres_major,
                },
            }
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump", type=Path, required=True)
    parser.add_argument("--checksum", type=Path)
    parser.add_argument(
        "--restore-dsn-env",
        default="DBTUNE_OFFHOST_RESTORE_DSN",
        help="Environment variable containing the independent restore DSN.",
    )
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--source-host", required=True)
    parser.add_argument("--restore-host", required=True)
    parser.add_argument("--restore-test-id", required=True)
    parser.add_argument("--verified-by", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--psql", default="psql")
    parser.add_argument("--pg-restore", default="pg_restore")
    args = parser.parse_args()

    if not ENVIRONMENT_NAME.fullmatch(args.restore_dsn_env):
        print("restore DSN environment-variable name is invalid", file=sys.stderr)
        return 2
    if not GIT_SHA.fullmatch(args.release_sha):
        print("--release-sha must be a full 40-character Git commit", file=sys.stderr)
        return 2
    if args.source_host.strip().lower() == args.restore_host.strip().lower():
        print("source and restore hosts must be independently identified", file=sys.stderr)
        return 2
    for value_name in ("restore_test_id", "verified_by"):
        if not getattr(args, value_name).strip():
            print(f"--{value_name.replace('_', '-')} cannot be empty", file=sys.stderr)
            return 2

    dsn = os.environ.get(args.restore_dsn_env, "")
    if not dsn:
        print(f"{args.restore_dsn_env} is not set", file=sys.stderr)
        return 2
    try:
        target = parse_restore_target(dsn)
    except ValueError as exc:
        print(f"invalid restore target: {exc}", file=sys.stderr)
        return 2
    if target.host.lower() != args.restore_host.strip().lower():
        print(
            "--restore-host must match the hostname in the restore DSN",
            file=sys.stderr,
        )
        return 2
    if target.host.lower() == args.source_host.strip().lower():
        print(
            "restore DSN points to the source host rather than an independent host",
            file=sys.stderr,
        )
        return 2
    for executable in (args.psql, args.pg_restore):
        if shutil.which(executable) is None:
            print(f"{executable} is required", file=sys.stderr)
            return 2

    dump = args.dump.resolve()
    checksum_path = (
        args.checksum.resolve()
        if args.checksum
        else Path(f"{dump}.sha256")
    )
    if not dump.is_file() or dump.stat().st_size == 0:
        print("backup dump is missing or empty", file=sys.stderr)
        return 2
    if not checksum_path.is_file():
        print("backup checksum sidecar is missing", file=sys.stderr)
        return 2
    try:
        actual_checksum = sha256_file(dump)
        recorded_checksum = expected_checksum(checksum_path)
    except (OSError, ValueError) as exc:
        print(f"backup checksum validation failed: {exc}", file=sys.stderr)
        return 2
    if not hmac.compare_digest(actual_checksum, recorded_checksum):
        print("backup dump does not match its SHA-256 sidecar", file=sys.stderr)
        return 1

    try:
        server_version_num = int(
            run_psql(target, "SHOW server_version_num;", args.psql)
        )
        server_major = server_version_num // 10000
        restore_client_major = client_major(args.pg_restore)
        if restore_client_major != server_major:
            print(
                "pg_restore and target PostgreSQL major versions must match: "
                f"client={restore_client_major} target={server_major}",
                file=sys.stderr,
            )
            return 1
        existing_tables = int(
            run_psql(
                target,
                "SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public';",
                args.psql,
            )
        )
        if existing_tables:
            print(
                "independent restore database is not empty; refusing to modify it",
                file=sys.stderr,
            )
            return 1
        restore_dump(target, dump, args.pg_restore)
        migrations = int(
            run_psql(
                target,
                "SELECT COUNT(*) FROM schema_migrations;",
                args.psql,
            )
        )
        tables = int(
            run_psql(
                target,
                "SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public';",
                args.psql,
            )
        )
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if migrations < 19 or tables < 10:
        print(
            f"restored database is incomplete: migrations={migrations} tables={tables}",
            file=sys.stderr,
        )
        return 1

    evidence = build_evidence(
        release_sha=args.release_sha.lower(),
        verified_by=args.verified_by.strip(),
        restore_test_id=args.restore_test_id.strip(),
        source_host=args.source_host.strip(),
        restore_host=args.restore_host.strip(),
        dump_checksum=actual_checksum,
        dump_size_bytes=dump.stat().st_size,
        migrations=migrations,
        tables=tables,
        postgres_major=server_major,
    )
    rendered = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
        args.output.chmod(0o600)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
