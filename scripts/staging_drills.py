"""Live staging failure drills for agent recovery and single-writer safety."""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import subprocess
import time
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
from staging_preflight import load_env

from backend.config import settings
from backend.services.target_executor import (
    TargetPostgresExecutor,
    WriteInterlockError,
)

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    ".env.staging",
    "-f",
    "docker-compose.yml",
    "-f",
    "ops/staging/docker-compose.staging.yml",
]


def run(command: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def require_success(result: subprocess.CompletedProcess[str], action: str) -> str:
    output = (result.stdout + result.stderr).strip()
    if result.returncode:
        raise RuntimeError(f"{action} failed: {output[-2000:]}")
    return output


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_seconds: float,
    interval_seconds: float = 2,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_seconds)
    return False


def psql_scalar(values: dict[str, str], query: str) -> str:
    result = run(
        COMPOSE
        + [
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            values["POSTGRES_USER"],
            "-d",
            values["POSTGRES_DB"],
            "-Atc",
            query,
        ],
        timeout=30,
    )
    return require_success(result, "control database query").strip()


def buffer_count() -> int:
    result = run(
        COMPOSE
        + [
            "exec",
            "-T",
            "target-host-agent",
            "sh",
            "-c",
            "find /tmp/dbtune-agent-buffer -type f -name '*.json' | wc -l",
        ],
        timeout=30,
    )
    return int(require_success(result, "agent buffer count").strip())


def readiness_ok(base_url: str) -> bool:
    try:
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(
            f"{base_url}/health/ready",
            timeout=3,
            context=context,
        ) as response:
            return bool(response.status == 200)
    except Exception:
        return False


def control_dsn(values: dict[str, str]) -> str:
    return (
        f"postgresql://{quote(values['POSTGRES_USER'], safe='')}:"
        f"{quote(values['POSTGRES_PASSWORD'], safe='')}@127.0.0.1:"
        f"{values.get('PG_PORT', '15432')}/{quote(values['POSTGRES_DB'], safe='')}"
    )


async def assert_live_ambiguous_write_block(
    values: dict[str, str],
    host_id: str,
) -> str:
    pool = await asyncpg.create_pool(control_dsn(values), min_size=1, max_size=2)
    original = settings.write_execution_enabled
    try:
        executor = TargetPostgresExecutor(pool)
        policy = await executor.load_policy(UUID(host_id))
        policy = replace(policy, writes_enabled=True)
        settings.write_execution_enabled = True
        try:
            executor.assert_write_allowed(policy)
        except WriteInterlockError as exc:
            message = str(exc)
            if "multiple active Host Agents" not in message:
                raise RuntimeError(f"wrong write interlock fired: {message}") from exc
            return message
        raise RuntimeError("write execution was not blocked for duplicate agents")
    finally:
        settings.write_execution_enabled = original
        await pool.close()


def prometheus_duplicate_firing() -> bool:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:19090/api/v1/alerts",
            timeout=3,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return any(
            alert["labels"].get("alertname") == "DBTuneDuplicateAgent"
            and alert["state"] == "firing"
            for alert in payload["data"]["alerts"]
        )
    except Exception:
        return False


def local_alert_received(after_offset: int) -> bool:
    path = ROOT / "artifacts" / "staging-alerts" / "alerts.jsonl"
    if not path.exists() or path.stat().st_size <= after_offset:
        return False
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(after_offset)
        for line in handle:
            record = json.loads(line)
            alerts = record.get("payload", {}).get("alerts", [])
            if any(
                item.get("labels", {}).get("alertname") == "DBTuneDuplicateAgent"
                and item.get("status") == "firing"
                for item in alerts
            ):
                return True
    return False


def agent_buffer_replay(values: dict[str, str], base_url: str) -> dict[str, Any]:
    host_id = values["TARGET_AGENT_HOST_ID"]
    existing_buffer = buffer_count()
    if existing_buffer:
        raise RuntimeError(
            f"agent buffer must be empty before the drill; found {existing_buffer} entries"
        )
    before = int(
        psql_scalar(
            values,
            f"SELECT COUNT(*) FROM evidence_snapshots WHERE host_id='{host_id}'::uuid;",
        )
    )
    require_success(run(COMPOSE + ["stop", "app"]), "stop control-plane app")
    buffered = 0
    recovery_epoch = 0.0
    try:
        if not wait_until(
            lambda: buffer_count() >= 7,
            timeout_seconds=60,
            interval_seconds=2,
        ):
            raise RuntimeError("agent did not persist at least seven evidence snapshots")
        buffered = buffer_count()
    finally:
        recovery_epoch = float(psql_scalar(values, "SELECT EXTRACT(EPOCH FROM NOW());"))
        require_success(run(COMPOSE + ["start", "app"]), "restart control-plane app")

    if not wait_until(lambda: readiness_ok(base_url), timeout_seconds=120):
        raise RuntimeError("control plane did not recover after the buffer drill")
    if not wait_until(lambda: buffer_count() == 0, timeout_seconds=60):
        raise RuntimeError("agent buffer did not drain within 60 seconds of reconnection")
    after = int(
        psql_scalar(
            values,
            f"SELECT COUNT(*) FROM evidence_snapshots WHERE host_id='{host_id}'::uuid;",
        )
    )
    replayed = after - before
    buffered_interval_rows = int(
        psql_scalar(
            values,
            f"""
            SELECT COUNT(*)
            FROM evidence_snapshots
            WHERE host_id='{host_id}'::uuid
              AND created_at >= to_timestamp({recovery_epoch})
              AND collected_at <= to_timestamp({recovery_epoch});
            """,
        )
    )
    chronology_violations = int(
        psql_scalar(
            values,
            f"""
            SELECT COUNT(*) FROM (
                SELECT collected_at,
                       LAG(collected_at) OVER (ORDER BY created_at, id) AS previous
                FROM evidence_snapshots
                WHERE host_id='{host_id}'::uuid
                  AND created_at >= to_timestamp({recovery_epoch})
                  AND collected_at <= to_timestamp({recovery_epoch})
            ) replay
            WHERE previous IS NOT NULL AND collected_at < previous;
            """,
        )
    )
    if replayed < buffered:
        raise RuntimeError(f"only {replayed} rows arrived for {buffered} buffered snapshots")
    if buffered_interval_rows < buffered:
        raise RuntimeError(
            "only "
            f"{buffered_interval_rows} pre-recovery rows arrived for "
            f"{buffered} buffered snapshots"
        )
    if chronology_violations:
        raise RuntimeError(
            f"buffer replay contained {chronology_violations} chronological inversions"
        )
    return {
        "buffered_snapshots": buffered,
        "replayed_snapshots": replayed,
        "buffered_interval_rows": buffered_interval_rows,
        "remaining_buffer": buffer_count(),
        "chronology_violations": chronology_violations,
    }


def duplicate_agent(values: dict[str, str]) -> dict[str, Any]:
    host_id = values["TARGET_AGENT_HOST_ID"]
    duplicate_name = f"dbtune-duplicate-{uuid4().hex[:10]}"
    alert_path = ROOT / "artifacts" / "staging-alerts" / "alerts.jsonl"
    alert_offset = alert_path.stat().st_size if alert_path.exists() else 0
    instance_id = str(uuid4())
    result = run(
        COMPOSE
        + [
            "run",
            "-d",
            "--no-deps",
            "--name",
            duplicate_name,
            "-e",
            f"AGENT_INSTANCE_ID={instance_id}",
            "target-host-agent",
        ],
        timeout=60,
    )
    require_success(result, "start duplicate agent")
    detected = resolved = False
    try:
        detected = wait_until(
            lambda: psql_scalar(
                values,
                f"SELECT agent_write_ambiguous::text FROM hosts "
                f"WHERE id='{host_id}'::uuid;",
            )
            == "true",
            timeout_seconds=45,
        )
        if not detected:
            raise RuntimeError("duplicate agent ownership was not detected")
        write_block = asyncio.run(assert_live_ambiguous_write_block(values, host_id))
        if not wait_until(prometheus_duplicate_firing, timeout_seconds=75):
            raise RuntimeError("Prometheus duplicate-agent alert did not fire")
        local_delivery = wait_until(
            lambda: local_alert_received(alert_offset),
            timeout_seconds=45,
        )
        if values.get("ALERT_WEBHOOK_URL") == "http://alert-sink:9099/alerts":
            if not local_delivery:
                raise RuntimeError("local Alertmanager webhook did not receive the alert")
    finally:
        run(["docker", "rm", "-f", duplicate_name], timeout=30)

    resolved = wait_until(
        lambda: psql_scalar(
            values,
            f"SELECT agent_write_ambiguous::text FROM hosts WHERE id='{host_id}'::uuid;",
        )
        == "false",
        timeout_seconds=130,
        interval_seconds=3,
    )
    if not resolved:
        raise RuntimeError("duplicate-agent ownership did not resolve after lease expiry")
    detected_events = int(
        psql_scalar(
            values,
            f"SELECT COUNT(*) FROM host_events WHERE host_id='{host_id}'::uuid "
            "AND event_code='AGENT_DUPLICATE_DETECTED';",
        )
    )
    resolved_events = int(
        psql_scalar(
            values,
            f"SELECT COUNT(*) FROM host_events WHERE host_id='{host_id}'::uuid "
            "AND event_code='AGENT_DUPLICATE_RESOLVED';",
        )
    )
    if not detected_events or not resolved_events:
        raise RuntimeError("duplicate detection/resolution events are incomplete")
    return {
        "duplicate_instance_id": instance_id,
        "detected": detected,
        "write_block": write_block,
        "prometheus_alert_fired": True,
        "local_alert_delivered": local_delivery,
        "resolved": resolved,
        "detected_event_count": detected_events,
        "resolved_event_count": resolved_events,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("drill", choices=["agent_buffer_replay", "duplicate_agent"])
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.staging")
    parser.add_argument("--base-url", default="https://127.0.0.1:18443")
    args = parser.parse_args()
    values = load_env(args.env_file)
    if args.drill == "agent_buffer_replay":
        result = agent_buffer_replay(values, args.base_url)
    else:
        result = duplicate_agent(values)
    print(json.dumps({"drill": args.drill, "passed": True, **result}, sort_keys=True))


if __name__ == "__main__":
    main()
