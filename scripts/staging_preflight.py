"""Fail-closed validation for the production-like staging environment."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def validate_workload_bounds(
    values: dict[str, str],
    *,
    allow_local: bool,
) -> list[str]:
    failures: list[str] = []
    try:
        clients = int(values.get("STAGING_PGBENCH_CLIENTS", "2"))
        jobs = int(values.get("STAGING_PGBENCH_JOBS", "2"))
        rate = int(values.get("STAGING_PGBENCH_RATE", "2"))
        target_cpus = float(values.get("STAGING_TARGET_POSTGRES_CPUS", "2.0"))
        workload_cpus = float(values.get("STAGING_TARGET_WORKLOAD_CPUS", "0.5"))
    except ValueError:
        return ["staging workload clients, jobs, rate, and CPU limit must be numeric"]
    if clients < 1:
        failures.append("STAGING_PGBENCH_CLIENTS must be at least 1")
    if jobs < 1 or jobs > clients:
        failures.append("STAGING_PGBENCH_JOBS must be between 1 and the client count")
    if rate < 1:
        failures.append("STAGING_PGBENCH_RATE must be rate-limited above zero")
    if target_cpus <= 0:
        failures.append("STAGING_TARGET_POSTGRES_CPUS must be greater than zero")
    if workload_cpus <= 0:
        failures.append("STAGING_TARGET_WORKLOAD_CPUS must be greater than zero")
    if allow_local and (
        clients > 4 or rate > 10 or target_cpus > 2 or workload_cpus > 0.5
    ):
        failures.append(
            "local staging is capped at 4 clients, 10 transactions/s, "
            "2 PostgreSQL CPUs, and 0.5 workload CPUs"
        )
    return failures


def validate_database_roles(values: dict[str, str]) -> list[str]:
    failures: list[str] = []
    bootstrap_user = values.get("POSTGRES_USER", "")
    if bootstrap_user != "dbtune_bootstrap":
        failures.append("POSTGRES_USER must be the dedicated dbtune_bootstrap role")

    role_specs = {
        "MIGRATION_DATABASE_URL": (
            "dbtune_migrator",
            "POSTGRES_MIGRATION_PASSWORD",
        ),
        "CONTROL_DATABASE_URL": (
            "dbtune_runtime",
            "POSTGRES_RUNTIME_PASSWORD",
        ),
        "BACKUP_DATABASE_URL": (
            "dbtune_backup",
            "POSTGRES_BACKUP_PASSWORD",
        ),
    }
    usernames: list[str] = []
    passwords = [values.get("POSTGRES_PASSWORD", "")]
    expected_database = values.get("POSTGRES_DB", "")
    for dsn_key, (expected_user, password_key) in role_specs.items():
        password = values.get(password_key, "")
        passwords.append(password)
        if len(password) < 24 or "CHANGE_ME" in password:
            failures.append(f"{password_key} must contain at least 24 random characters")

        dsn = values.get(dsn_key, "")
        parsed = urlparse(dsn)
        username = unquote(parsed.username or "")
        dsn_password = unquote(parsed.password or "")
        database = parsed.path.lstrip("/")
        usernames.append(username)
        if parsed.scheme not in {"postgresql", "postgres"}:
            failures.append(f"{dsn_key} must be a PostgreSQL DSN")
        if "CHANGE_ME" in dsn or username != expected_user:
            failures.append(f"{dsn_key} must use the dedicated {expected_user} role")
        if dsn_password != password:
            failures.append(f"{dsn_key} password must match {password_key}")
        if database != expected_database:
            failures.append(f"{dsn_key} must connect to POSTGRES_DB")

    if len(set(usernames + [bootstrap_user])) != 4:
        failures.append("bootstrap, migrator, runtime, and backup roles must be distinct")
    if any(not password for password in passwords) or len(set(passwords)) != 4:
        failures.append("all database roles must use distinct non-empty passwords")
    return failures


def validate_release_identity(
    values: dict[str, str],
    root: Path = ROOT,
) -> list[str]:
    failures: list[str] = []
    expected_sha = values.get("RELEASE_COMMIT_SHA", "")
    if len(expected_sha) != 40 or any(
        character not in "0123456789abcdefABCDEF" for character in expected_sha
    ):
        failures.append("RELEASE_COMMIT_SHA must be a full 40-character Git commit")

    sha_result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    status_result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if sha_result.returncode or status_result.returncode:
        failures.append("staging must run from an identifiable Git checkout")
        return failures
    if sha_result.stdout.strip() != expected_sha:
        failures.append("RELEASE_COMMIT_SHA must match the checked-out commit")
    if status_result.stdout.strip():
        failures.append("staging release checkout must be clean before images are built")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.staging")
    parser.add_argument("--allow-local", action="store_true")
    parser.add_argument("--require-target-agent", action="store_true")
    args = parser.parse_args()

    failures: list[str] = []
    if not args.env_file.exists():
        raise SystemExit(f"missing {args.env_file}; run scripts/staging_init.py")
    values = load_env(args.env_file)
    failures.extend(validate_workload_bounds(values, allow_local=args.allow_local))
    failures.extend(validate_database_roles(values))
    failures.extend(validate_release_identity(values))

    expected = {
        "ENVIRONMENT": "production",
        "DEBUG": "false",
        "DEMO_MODE": "false",
        "AUTH_REQUIRED": "true",
        "AGENT_AUTH_REQUIRED": "true",
        "WRITE_EXECUTION_ENABLED": "false",
        "PRODUCTION_WRITE_ENABLED": "false",
        "PRODUCTION_WRITE_CONFIRMATION": "",
    }
    for setting_name, expected_value in expected.items():
        if values.get(setting_name, "").lower() != expected_value:
            failures.append(f"{setting_name} must be {expected_value!r}")

    password = values.get("POSTGRES_PASSWORD", "")
    if len(password) < 24 or "CHANGE_ME" in password or password == "postgres":
        failures.append("POSTGRES_PASSWORD must be a non-default secret of at least 24 characters")
    token = values.get("BOOTSTRAP_ADMIN_TOKEN", "")
    if len(token) < 32 or "CHANGE_ME" in token:
        failures.append("BOOTSTRAP_ADMIN_TOKEN must contain at least 32 random characters")
    redis_password = values.get("REDIS_PASSWORD", "")
    if len(redis_password) < 24 or "CHANGE_ME" in redis_password:
        failures.append("REDIS_PASSWORD must contain at least 24 random characters")
    redis_url = values.get("CONTROL_REDIS_URL", "")
    if "CHANGE_ME" in redis_url or urlparse(redis_url).scheme not in {"redis", "rediss"}:
        failures.append("CONTROL_REDIS_URL must contain authenticated Redis credentials")

    try:
        origins = json.loads(values.get("CORS_ORIGINS", "[]"))
    except json.JSONDecodeError:
        origins = []
    if not origins or any(not origin.startswith("https://") for origin in origins):
        failures.append("CORS_ORIGINS must contain explicit HTTPS origins only")

    webhook = values.get("ALERT_WEBHOOK_URL", "")
    webhook_scheme = urlparse(webhook).scheme
    if "CHANGE_ME" in webhook or webhook_scheme not in (
        {"http", "https"} if args.allow_local else {"https"}
    ):
        failures.append("ALERT_WEBHOOK_URL must be a configured HTTPS receiver")

    cert = ROOT / ".staging" / "secrets" / "tls.crt"
    key = ROOT / ".staging" / "secrets" / "tls.key"
    generated_alerts = ROOT / ".staging" / "generated" / "alertmanager.yml"
    for required_path in (cert, key, generated_alerts):
        if not required_path.is_file() or required_path.stat().st_size == 0:
            failures.append(f"missing generated staging file: {required_path}")
    if cert.exists():
        check = subprocess.run(
            ["openssl", "x509", "-checkend", "3600", "-noout", "-in", str(cert)],
            capture_output=True,
            text=True,
        )
        if check.returncode:
            failures.append("TLS certificate is invalid or expires within one hour")

    if args.require_target_agent:
        for key_name in ("TARGET_AGENT_HOST_ID", "TARGET_AGENT_TOKEN"):
            if not values.get(key_name):
                failures.append(f"{key_name} is required for a tuning-lab soak")

    if failures:
        print("Staging preflight failed:")
        for failure in failures:
            print(f"  - {failure}")
        raise SystemExit(1)
    print(
        "Staging preflight passed: secrets, TLS, alert route, auth, "
        "release identity, database role separation, and write interlocks"
    )


if __name__ == "__main__":
    main()
