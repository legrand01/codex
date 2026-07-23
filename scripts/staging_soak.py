"""Durable 24-72 hour staging soak with recovery and restore drills."""

from __future__ import annotations

import argparse
import json
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "artifacts" / "staging-soak"
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
        handle.flush()


def run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def request_json(url: str, insecure: bool, timeout: float = 5.0) -> tuple[int, Any]:
    context = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body: Any = json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {"error": str(exc)}
        return exc.code, body
    except Exception as exc:
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def target_transaction_count() -> int | None:
    result = run(
        COMPOSE
        + [
            "--profile",
            "tuning-lab",
            "exec",
            "-T",
            "target-postgres",
            "psql",
            "-U",
            "dbtune",
            "-d",
            "dbtune_target",
            "-Atc",
            "SELECT COUNT(*) FROM ledger;",
        ],
        timeout=30,
    )
    if result.returncode:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def write_interlocks_disabled() -> tuple[bool, str]:
    code = (
        "from backend.config import settings; "
        "print(settings.write_execution_enabled, "
        "settings.production_write_enabled, "
        "repr(settings.production_write_confirmation))"
    )
    result = run(COMPOSE + ["exec", "-T", "worker", "python", "-c", code], timeout=30)
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0 and output.endswith("False False ''"), output


def wait_for_ready(base_url: str, insecure: bool, timeout_seconds: int = 120) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status, payload = request_json(f"{base_url}/health/ready", insecure)
        if status == 200 and payload.get("status") == "ready":
            return True
        time.sleep(2)
    return False


def run_drill(name: str, base_url: str, insecure: bool, output_dir: Path) -> dict[str, Any]:
    started = time.monotonic()
    if name == "worker_restart":
        command = COMPOSE + ["restart", "worker"]
        result = run(command)
        passed = result.returncode == 0 and wait_for_ready(base_url, insecure)
        detail = (result.stdout + result.stderr).strip()
    elif name == "redis_restart":
        command = COMPOSE + ["restart", "redis"]
        result = run(command)
        passed = result.returncode == 0 and wait_for_ready(base_url, insecure)
        detail = (result.stdout + result.stderr).strip()
    elif name == "backup_restore":
        result = run(
            ["bash", "scripts/verify_staging_restore.sh", str(output_dir / "backups")],
            timeout=300,
        )
        passed = result.returncode == 0
        detail = (result.stdout + result.stderr).strip()
    else:
        return {
            "kind": "drill",
            "name": name,
            "timestamp": utc_now(),
            "passed": False,
            "detail": "unknown drill",
            "recovery_seconds": 0,
        }
    return {
        "kind": "drill",
        "name": name,
        "timestamp": utc_now(),
        "passed": passed,
        "detail": detail[-2000:],
        "recovery_seconds": round(time.monotonic() - started, 3),
    }


@dataclass
class SoakState:
    started_at_epoch: float
    requested_duration_seconds: float
    baseline_transactions: int | None
    completed_drills: list[str]

    @classmethod
    def load_or_create(
        cls,
        path: Path,
        duration_seconds: float,
        resume: bool,
    ) -> "SoakState":
        if resume and path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return cls(**payload)
        return cls(time.time(), duration_seconds, target_transaction_count(), [])

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.__dict__, indent=2, sort_keys=True), encoding="utf-8")


def classify_decision(mechanics_passed: bool, qualification_complete: bool) -> str:
    if not mechanics_passed:
        return "NO_GO"
    if not qualification_complete:
        return "PENDING_QUALIFICATION"
    return "GO"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-hours", type=float, default=24.0)
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--base-url", default="https://127.0.0.1:18443")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--drills",
        default="worker_restart,redis_restart,backup_restore",
        help="Comma-separated drills; use an empty value to disable.",
    )
    parser.add_argument(
        "--qualification-hours",
        type=float,
        default=24.0,
        help="Minimum observed time before a GO decision is possible.",
    )
    args = parser.parse_args()
    if not 0 < args.duration_hours <= 72:
        raise SystemExit("--duration-hours must be greater than 0 and no more than 72")
    if args.interval_seconds < 1:
        raise SystemExit("--interval-seconds must be at least 1")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    events_path = args.output_dir / "events.jsonl"
    state_path = args.output_dir / "run-state.json"
    summary_path = args.output_dir / "summary.json"
    duration_seconds = args.duration_hours * 3600
    state = SoakState.load_or_create(state_path, duration_seconds, args.resume)
    state.save(state_path)

    interlocks_ok, interlock_detail = write_interlocks_disabled()
    append_jsonl(
        events_path,
        {
            "kind": "preflight",
            "timestamp": utc_now(),
            "write_interlocks_disabled": interlocks_ok,
            "detail": interlock_detail,
        },
    )
    if not interlocks_ok:
        raise SystemExit("staging soak refused: worker write interlocks are not all disabled")

    drills = [item for item in args.drills.split(",") if item]
    drill_fractions = {
        name: (index + 1) / (len(drills) + 1)
        for index, name in enumerate(drills)
    }
    samples: list[dict[str, Any]] = []
    drill_results: list[dict[str, Any]] = []
    interrupted = False
    try:
        while True:
            elapsed = time.time() - state.started_at_epoch
            if elapsed >= state.requested_duration_seconds:
                break
            status, readiness = request_json(
                f"{args.base_url}/health/ready",
                args.insecure,
            )
            transactions = target_transaction_count()
            sample = {
                "kind": "sample",
                "timestamp": utc_now(),
                "elapsed_seconds": round(elapsed, 3),
                "ready": status == 200 and readiness.get("status") == "ready",
                "readiness": readiness,
                "target_transactions": transactions,
            }
            samples.append(sample)
            append_jsonl(events_path, sample)

            for drill in drills:
                due = elapsed >= state.requested_duration_seconds * drill_fractions[drill]
                if due and drill not in state.completed_drills:
                    result = run_drill(drill, args.base_url, args.insecure, args.output_dir)
                    drill_results.append(result)
                    append_jsonl(events_path, result)
                    state.completed_drills.append(drill)
                    state.save(state_path)
            time.sleep(
                min(
                    args.interval_seconds,
                    max(
                        0.0,
                        state.requested_duration_seconds
                        - (time.time() - state.started_at_epoch),
                    ),
                )
            )
    except KeyboardInterrupt:
        interrupted = True
    finally:
        final_transactions = target_transaction_count()
        elapsed = max(0.0, time.time() - state.started_at_epoch)
        ready_samples = sum(1 for sample in samples if sample["ready"])
        readiness_ratio = ready_samples / len(samples) if samples else 0.0
        transaction_progress = (
            final_transactions is not None
            and state.baseline_transactions is not None
            and final_transactions > state.baseline_transactions
        )
        all_drills_recorded = all(name in state.completed_drills for name in drills)
        drills_passed = all(item["passed"] for item in drill_results) and all_drills_recorded
        mechanics_passed = (
            interlocks_ok
            and readiness_ratio >= 0.995
            and transaction_progress
            and drills_passed
            and not interrupted
        )
        qualification_seconds = args.qualification_hours * 3600
        qualification_complete = elapsed >= qualification_seconds
        decision = classify_decision(mechanics_passed, qualification_complete)
        summary = {
            "started_at": datetime.fromtimestamp(
                state.started_at_epoch, timezone.utc
            ).isoformat(),
            "finished_at": utc_now(),
            "elapsed_seconds": round(elapsed, 3),
            "requested_duration_seconds": state.requested_duration_seconds,
            "qualification_seconds": qualification_seconds,
            "qualification_complete": qualification_complete,
            "interrupted": interrupted,
            "samples": len(samples),
            "readiness_ratio": round(readiness_ratio, 6),
            "baseline_transactions": state.baseline_transactions,
            "final_transactions": final_transactions,
            "transaction_progress": transaction_progress,
            "completed_drills": state.completed_drills,
            "drill_results": drill_results,
            "mechanics_passed": mechanics_passed,
            "decision": decision,
        }
        summary_path.write_text(
            json.dumps(summary, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        if decision == "NO_GO":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
