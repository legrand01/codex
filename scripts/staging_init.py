"""Initialize ignored staging secrets and generated configuration."""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.staging.example"
ENV_FILE = ROOT / ".env.staging"
SECRET_DIR = ROOT / ".staging" / "secrets"
GENERATED_DIR = ROOT / ".staging" / "generated"


def current_release_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    release_sha = result.stdout.strip()
    if len(release_sha) != 40:
        raise RuntimeError("Git did not return a full 40-character release commit")
    return release_sha


def replace_value(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    prefix = f"{key}="
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = prefix + value
            replaced = True
            break
    if not replaced:
        lines.append(prefix + value)
    return "\n".join(lines) + "\n"


def copy_tls_certificate(cert: Path, key: Path) -> None:
    shutil.copyfile(cert, SECRET_DIR / "tls.crt")
    shutil.copyfile(key, SECRET_DIR / "tls.key")


def create_local_certificate(hostname: str) -> None:
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "7",
            "-nodes",
            "-subj",
            f"/CN={hostname}",
            "-addext",
            f"subjectAltName=DNS:{hostname},IP:127.0.0.1",
            "-keyout",
            str(SECRET_DIR / "tls.key"),
            "-out",
            str(SECRET_DIR / "tls.crt"),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create .env.staging and TLS/Alertmanager files outside Git."
    )
    parser.add_argument("--hostname", default="localhost")
    parser.add_argument("--alert-webhook")
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--rotate-secrets", action="store_true")
    args = parser.parse_args()

    if ENV_FILE.exists() and not args.force:
        raise SystemExit(f"{ENV_FILE} already exists; pass --force to replace it")
    if not args.local and (not args.cert or not args.key or not args.alert_webhook):
        raise SystemExit("real staging requires --cert, --key, and --alert-webhook")
    if args.local and (args.cert or args.key):
        raise SystemExit("--local cannot be combined with --cert or --key")

    SECRET_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    existing: dict[str, str] = {}
    if ENV_FILE.exists() and not args.rotate_secrets:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                existing[key] = value
    password = existing.get("POSTGRES_PASSWORD") or secrets.token_urlsafe(36)
    migration_password = existing.get(
        "POSTGRES_MIGRATION_PASSWORD"
    ) or secrets.token_urlsafe(36)
    runtime_password = existing.get(
        "POSTGRES_RUNTIME_PASSWORD"
    ) or secrets.token_urlsafe(36)
    backup_password = existing.get(
        "POSTGRES_BACKUP_PASSWORD"
    ) or secrets.token_urlsafe(36)
    redis_password = existing.get("REDIS_PASSWORD") or secrets.token_urlsafe(36)
    admin_token = existing.get("BOOTSTRAP_ADMIN_TOKEN") or secrets.token_urlsafe(48)
    webhook = args.alert_webhook or "http://alert-sink:9099/alerts"
    public_origin = f"https://{args.hostname}"
    migration_database_url = (
        "postgresql://dbtune_migrator:"
        f"{quote(migration_password, safe='')}@postgres:5432/dba_agent"
    )
    runtime_database_url = (
        "postgresql://dbtune_runtime:"
        f"{quote(runtime_password, safe='')}@postgres:5432/dba_agent"
    )
    backup_database_url = (
        "postgresql://dbtune_backup:"
        f"{quote(backup_password, safe='')}@postgres:5432/dba_agent"
    )

    text = EXAMPLE.read_text(encoding="utf-8")
    values = {
        "RELEASE_COMMIT_SHA": current_release_sha(),
        "CORS_ORIGINS": json.dumps([public_origin], separators=(",", ":")),
        "POSTGRES_PASSWORD": password,
        "POSTGRES_MIGRATION_PASSWORD": migration_password,
        "POSTGRES_RUNTIME_PASSWORD": runtime_password,
        "POSTGRES_BACKUP_PASSWORD": backup_password,
        "MIGRATION_DATABASE_URL": migration_database_url,
        "CONTROL_DATABASE_URL": runtime_database_url,
        "BACKUP_DATABASE_URL": backup_database_url,
        "REDIS_PASSWORD": redis_password,
        "CONTROL_REDIS_URL": f"redis://:{quote(redis_password, safe='')}@redis:6379/0",
        "BOOTSTRAP_ADMIN_TOKEN": admin_token,
        "ALERT_WEBHOOK_URL": webhook,
    }
    for retained_key in (
        "TARGET_AGENT_HOST_ID",
        "TARGET_AGENT_TOKEN",
        "TARGET_AGENT_INSTANCE_ID",
    ):
        if existing.get(retained_key):
            values[retained_key] = existing[retained_key]
    for key, value in values.items():
        text = replace_value(text, key, value)
    ENV_FILE.write_text(text, encoding="utf-8")
    ENV_FILE.chmod(0o600)

    if args.local:
        create_local_certificate(args.hostname)
    else:
        copy_tls_certificate(args.cert.resolve(), args.key.resolve())
    (SECRET_DIR / "tls.key").chmod(0o600)
    (SECRET_DIR / "tls.crt").chmod(0o644)

    alertmanager = f"""route:
  receiver: staging-webhook
  group_by: [alertname]
  group_wait: 10s
  group_interval: 1m
  repeat_interval: 4h
receivers:
  - name: staging-webhook
    webhook_configs:
      - url: {json.dumps(webhook)}
        send_resolved: true
"""
    config_path = GENERATED_DIR / "alertmanager.yml"
    config_path.write_text(alertmanager, encoding="utf-8")
    config_path.chmod(0o600)

    print(f"Created {ENV_FILE}")
    print(f"Created TLS material under {SECRET_DIR}")
    print(f"Created {config_path}")
    if args.local:
        print("Local mode is for mechanics only; it does not satisfy the real staging gate.")


if __name__ == "__main__":
    main()
