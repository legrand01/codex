"""Register the isolated transaction lab and provision its Host Agent."""

from __future__ import annotations

import argparse
import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from staging_init import replace_value
from staging_preflight import load_env

ROOT = Path(__file__).resolve().parents[1]


def api_request(
    base_url: str,
    token: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    insecure: bool = False,
) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    context = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(request, timeout=15, context=context) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {detail}") from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.staging")
    parser.add_argument("--base-url", default="https://127.0.0.1:18443")
    parser.add_argument("--insecure", action="store_true")
    args = parser.parse_args()
    values = load_env(args.env_file)
    token = values["BOOTSTRAP_ADMIN_TOKEN"]

    fleet = api_request(
        args.base_url,
        token,
        "/api/v1/fleet/",
        insecure=args.insecure,
    )
    existing = next(
        (host for host in fleet["hosts"] if host["hostname"] == "dbtune-target"),
        None,
    )
    if existing is None:
        host = api_request(
            args.base_url,
            token,
            "/api/v1/fleet/",
            method="POST",
            payload={
                "hostname": "dbtune-target",
                "database_name": "dbtune_target",
                "pg_version": "16",
                "server_role": "primary",
            },
            insecure=args.insecure,
        )
    else:
        host = existing
    host_id = host["id"]

    api_request(
        args.base_url,
        token,
        f"/api/v1/fleet/{host_id}/execution-policy",
        method="PUT",
        payload={
            "environment": "staging",
            "target_dsn_env": "DBTUNE_LAB_TARGET_DSN",
            "writes_enabled": False,
            "database_name": "dbtune_target",
            "platform_type": "self_managed",
            "configuration_backend": "managed_conf_file",
            "managed_conf_enrolled": True,
            "managed_conf_path": "/var/lib/postgresql/data/conf.d/postgres_tune.conf",
            "restart_required_enabled": False,
        },
        insecure=args.insecure,
    )
    agent = api_request(
        args.base_url,
        token,
        f"/api/v1/fleet/{host_id}/agent-token",
        method="POST",
        insecure=args.insecure,
    )

    text = args.env_file.read_text(encoding="utf-8")
    text = replace_value(text, "TARGET_AGENT_HOST_ID", host_id)
    text = replace_value(text, "TARGET_AGENT_TOKEN", agent["agent_token"])
    args.env_file.write_text(text, encoding="utf-8")
    args.env_file.chmod(0o600)
    print(f"Provisioned staging transaction lab host {host_id}")
    print("Recreate target-host-agent to load its one-time token.")


if __name__ == "__main__":
    main()
