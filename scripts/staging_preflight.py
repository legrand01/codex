"""Fail-closed validation for the production-like staging environment."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from urllib.parse import urlparse

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
    database_url = values.get("CONTROL_DATABASE_URL", "")
    if "postgres:postgres@" in database_url or "CHANGE_ME" in database_url:
        failures.append("CONTROL_DATABASE_URL still contains default or placeholder credentials")
    if urlparse(database_url).scheme not in {"postgresql", "postgres"}:
        failures.append("CONTROL_DATABASE_URL must be a PostgreSQL DSN")
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
    print("Staging preflight passed: secrets, TLS, alert route, auth, and write interlocks")


if __name__ == "__main__":
    main()
