"""Durable 24-72 hour staging soak with recovery and restore drills."""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import re
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
SOAK_PROFILES = [
    "--profile",
    "tuning-lab",
    "--profile",
    "observability",
    "--profile",
    "operations",
]
REQUIRED_SOAK_SERVICES = {
    "alertmanager",
    "app",
    "backup",
    "frontend",
    "postgres",
    "prometheus",
    "redis",
    "target-host-agent",
    "target-postgres",
    "target-workload",
    "worker",
}
SOURCE_BUILT_SERVICES = {
    "app",
    "frontend",
    "target-host-agent",
    "worker",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_jsonl(
    path: Path,
    payload: dict[str, Any],
    lock: threading.Lock | None = None,
) -> None:
    def write() -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()

    if lock is None:
        write()
    else:
        with lock:
            write()


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


def verify_database_roles() -> tuple[bool, dict[str, Any], str]:
    result = run(
        COMPOSE
        + [
            "run",
            "--rm",
            "--no-deps",
            "migrate",
            "python",
            "-m",
            "backend.db.staging_roles",
        ],
        timeout=120,
    )
    detail = (result.stdout + result.stderr).strip()
    if result.returncode:
        return False, {}, detail
    try:
        evidence = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, {}, detail
    return evidence.get("passed") is True, evidence, detail


def release_identity() -> tuple[bool, dict[str, Any], str]:
    sha_result = run(["git", "rev-parse", "--verify", "HEAD"], timeout=30)
    branch_result = run(["git", "branch", "--show-current"], timeout=30)
    status_result = run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        timeout=30,
    )
    detail = "\n".join(
        part.strip()
        for part in (
            sha_result.stderr,
            branch_result.stderr,
            status_result.stderr,
        )
        if part.strip()
    )
    command_ok = all(
        result.returncode == 0
        for result in (sha_result, branch_result, status_result)
    )
    dirty_paths = [
        line[3:]
        for line in status_result.stdout.splitlines()
        if len(line) >= 4
    ]
    evidence = {
        "commit_sha": sha_result.stdout.strip(),
        "branch": branch_result.stdout.strip() or None,
        "worktree_clean": command_ok and not dirty_paths,
        "dirty_paths": dirty_paths,
    }
    return command_ok and not dirty_paths, evidence, detail


def running_image_manifest(
    expected_release_sha: str | None = None,
) -> tuple[bool, dict[str, dict[str, str]], str]:
    containers = run(COMPOSE + SOAK_PROFILES + ["ps", "-q"], timeout=30)
    if containers.returncode:
        detail = (containers.stdout + containers.stderr).strip()
        return False, {}, detail
    container_ids = [
        container_id.strip()
        for container_id in containers.stdout.splitlines()
        if container_id.strip()
    ]
    if not container_ids:
        return False, {}, "no running staging containers"

    inspect_result = run(
        [
            "docker",
            "inspect",
            "--format",
            (
                '{{index .Config.Labels "com.docker.compose.service"}}'
                "\t{{.Config.Image}}\t{{.Image}}"
                '\t{{index .Config.Labels "org.opencontainers.image.revision"}}'
            ),
        ]
        + container_ids,
        timeout=30,
    )
    detail = (inspect_result.stdout + inspect_result.stderr).strip()
    if inspect_result.returncode:
        return False, {}, detail

    manifest: dict[str, dict[str, str]] = {}
    for line in inspect_result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or not all(parts[:3]):
            return False, {}, f"invalid docker image identity row: {line}"
        service, configured_image, image_id, source_revision = parts
        if service in manifest:
            return False, {}, f"duplicate running service in image manifest: {service}"
        manifest[service] = {
            "configured_image": configured_image,
            "image_id": image_id,
            "source_revision": source_revision,
        }

    missing_services = sorted(REQUIRED_SOAK_SERVICES - manifest.keys())
    if missing_services:
        return (
            False,
            manifest,
            f"required staging services are not running: {missing_services}",
        )
    if expected_release_sha is not None:
        mismatched_revisions = sorted(
            service
            for service in SOURCE_BUILT_SERVICES
            if manifest[service]["source_revision"] != expected_release_sha
        )
        if mismatched_revisions:
            return (
                False,
                manifest,
                "source-built service image revisions do not match the release: "
                f"{mismatched_revisions}",
            )
    return True, dict(sorted(manifest.items())), detail


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
    evidence: dict[str, Any] | None = None
    try:
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
        elif name in {"agent_buffer_replay", "duplicate_agent"}:
            result = run(
                [
                    "venv/bin/python",
                    "scripts/staging_drills.py",
                    name,
                    "--base-url",
                    base_url,
                ],
                timeout=300,
            )
            passed = result.returncode == 0
            detail = (result.stdout + result.stderr).strip()
            if passed:
                evidence = json.loads(result.stdout)
                passed = evidence.get("passed") is True
        elif name == "regression_rollback":
            result = run(
                ["venv/bin/python", "scripts/drill_regression_rollback.py"],
                timeout=600,
            )
            passed = result.returncode == 0
            detail = (result.stdout + result.stderr).strip()
            if passed:
                evidence = json.loads(result.stdout)
                passed = evidence.get("passed") is True
        elif name == "backup_restore":
            result = run(
                ["bash", "scripts/verify_staging_restore.sh", str(output_dir / "backups")],
                timeout=300,
            )
            passed = result.returncode == 0
            detail = (result.stdout + result.stderr).strip()
        else:
            passed = False
            detail = "unknown drill"
    except Exception as exc:
        passed = False
        detail = f"{type(exc).__name__}: {exc}"
    return {
        "kind": "drill",
        "name": name,
        "timestamp": utc_now(),
        "passed": passed,
        "detail": detail[-2000:],
        "evidence": evidence,
        "recovery_seconds": round(time.monotonic() - started, 3),
    }


@dataclass
class SoakState:
    started_at_epoch: float
    requested_duration_seconds: float
    baseline_transactions: int | None
    completed_drills: list[str]
    release_sha: str = ""
    release_branch: str | None = None
    image_manifest: dict[str, dict[str, str]] = field(default_factory=dict)
    samples_total: int = 0
    ready_samples: int = 0
    max_transactions: int | None = None
    drill_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_sample_at: str | None = None
    last_sample_epoch: float | None = None
    max_sample_gap_seconds: float = 0.0

    @classmethod
    def load_or_create(
        cls,
        path: Path,
        duration_seconds: float,
        resume: bool,
        release: dict[str, Any] | None = None,
        images: dict[str, dict[str, str]] | None = None,
    ) -> "SoakState":
        if resume and path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if "drill_results" not in payload:
                # Task 28 state written before cumulative drill outcomes were
                # durable cannot prove those drills; schedule them again.
                payload["completed_drills"] = []
            allowed = cls.__dataclass_fields__
            return cls(**{key: value for key, value in payload.items() if key in allowed})
        baseline = target_transaction_count()
        release_payload = release or {}
        release_branch = release_payload.get("branch")
        return cls(
            started_at_epoch=time.time(),
            requested_duration_seconds=duration_seconds,
            baseline_transactions=baseline,
            completed_drills=[],
            release_sha=str(release_payload.get("commit_sha", "")),
            release_branch=(
                release_branch if isinstance(release_branch, str) else None
            ),
            image_manifest=images or {},
            max_transactions=baseline,
        )

    def save(self, path: Path) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(asdict(self), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)


EXTERNAL_GATES = {
    "real_tls",
    "external_alert_delivery",
    "independent_off_host_restore",
    "staffed_go_no_go",
}
EXTERNAL_EVIDENCE_FIELDS = {
    "real_tls": {
        "hostname",
        "certificate_sha256",
        "issuer",
    },
    "external_alert_delivery": {
        "receiver",
        "alert_id",
        "acknowledgement_id",
    },
    "independent_off_host_restore": {
        "source_backup_sha256",
        "source_host",
        "restore_host",
        "restore_test_id",
    },
    "staffed_go_no_go": {
        "approver",
        "decision",
        "scope",
    },
}
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def sampling_coverage(
    samples_total: int,
    observation_seconds: float,
    interval_seconds: float,
) -> tuple[int, float]:
    expected = max(1, math.ceil(max(0.0, observation_seconds) / interval_seconds))
    return expected, min(1.0, samples_total / expected)


def production_tls_mode(base_url: str, insecure: bool) -> bool:
    parsed = urlparse(base_url)
    hostname = parsed.hostname
    if insecure or parsed.scheme != "https" or not hostname:
        return False
    if hostname.lower() == "localhost":
        return False
    try:
        return not ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return True


def _parse_evidence_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return timestamp if timestamp.tzinfo is not None else None


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def external_gates(
    path: Path | None,
    expected_release_sha: str | None = None,
    expected_hostname: str | None = None,
    earliest_verified_at: datetime | None = None,
    staffed_not_before: datetime | None = None,
    now: datetime | None = None,
) -> tuple[bool, dict[str, Any]]:
    if path is None or not path.exists():
        return False, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, {"validation_errors": [f"cannot read evidence: {exc}"]}
    if not isinstance(payload, dict):
        return False, {"validation_errors": ["evidence root must be an object"]}

    errors: list[str] = []
    unexpected_root_fields = sorted(
        set(payload) - {"schema_version", "release_candidate_sha", "gates"}
    )
    if unexpected_root_fields:
        errors.append(
            f"unexpected top-level evidence fields: {unexpected_root_fields}"
        )
    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    release_sha = payload.get("release_candidate_sha")
    if (
        expected_release_sha is not None
        and release_sha != expected_release_sha
    ):
        errors.append("release_candidate_sha does not match the qualified commit")

    gates = payload.get("gates")
    if not isinstance(gates, dict):
        errors.append("gates must be an object")
        gates = {}
    unexpected_gates = sorted(set(gates) - EXTERNAL_GATES)
    if unexpected_gates:
        errors.append(f"unexpected external gates: {unexpected_gates}")
    validated_gates: dict[str, Any] = {}
    validation_time = now or datetime.now(timezone.utc)
    for gate in sorted(EXTERNAL_GATES):
        entry = gates.get(gate)
        if not isinstance(entry, dict):
            errors.append(f"{gate}: gate evidence must be an object")
            continue
        unexpected_entry_fields = sorted(
            set(entry)
            - {"verified", "verified_at", "verified_by", "evidence_id", "evidence"}
        )
        if unexpected_entry_fields:
            errors.append(
                f"{gate}: unexpected fields: {unexpected_entry_fields}"
            )
        if entry.get("verified") is not True:
            errors.append(f"{gate}: verified must be true")
        verified_at = _parse_evidence_timestamp(entry.get("verified_at"))
        if verified_at is None:
            errors.append(f"{gate}: verified_at must be a timezone-aware timestamp")
        else:
            if (
                earliest_verified_at is not None
                and verified_at < earliest_verified_at
            ):
                errors.append(f"{gate}: verified_at predates the qualification")
            if verified_at > validation_time + timedelta(minutes=5):
                errors.append(f"{gate}: verified_at is in the future")
            if (
                gate == "staffed_go_no_go"
                and staffed_not_before is not None
                and verified_at < staffed_not_before
            ):
                errors.append(
                    "staffed_go_no_go: verified_at predates qualification completion"
                )
        if not _nonempty_string(entry.get("verified_by")):
            errors.append(f"{gate}: verified_by is required")
        if not _nonempty_string(entry.get("evidence_id")):
            errors.append(f"{gate}: evidence_id is required")

        evidence = entry.get("evidence")
        if not isinstance(evidence, dict):
            errors.append(f"{gate}: evidence must be an object")
            validated_gates[gate] = {
                "verified": entry.get("verified"),
                "verified_at": entry.get("verified_at"),
                "verified_by": entry.get("verified_by"),
                "evidence_id": entry.get("evidence_id"),
                "evidence": {},
            }
            continue
        unexpected_evidence_fields = sorted(
            set(evidence) - EXTERNAL_EVIDENCE_FIELDS[gate]
        )
        if unexpected_evidence_fields:
            errors.append(
                f"{gate}: unexpected evidence fields: {unexpected_evidence_fields}"
            )
        for field_name in sorted(EXTERNAL_EVIDENCE_FIELDS[gate]):
            value = evidence.get(field_name)
            if not _nonempty_string(value):
                errors.append(f"{gate}: evidence.{field_name} is required")

        if gate == "real_tls":
            if (
                expected_hostname is not None
                and evidence.get("hostname") != expected_hostname
            ):
                errors.append(
                    "real_tls: evidence.hostname does not match the qualified URL"
                )
            certificate_sha = evidence.get("certificate_sha256")
            if not isinstance(certificate_sha, str) or not SHA256_PATTERN.fullmatch(
                certificate_sha
            ):
                errors.append(
                    "real_tls: evidence.certificate_sha256 must be 64 hex characters"
                )
        elif gate == "independent_off_host_restore":
            backup_sha = evidence.get("source_backup_sha256")
            if not isinstance(backup_sha, str) or not SHA256_PATTERN.fullmatch(
                backup_sha
            ):
                errors.append(
                    "independent_off_host_restore: "
                    "evidence.source_backup_sha256 must be 64 hex characters"
                )
            if evidence.get("source_host") == evidence.get("restore_host"):
                errors.append(
                    "independent_off_host_restore: source_host and restore_host "
                    "must differ"
                )
        elif gate == "staffed_go_no_go" and evidence.get("decision") != "GO":
            errors.append("staffed_go_no_go: evidence.decision must be GO")
        validated_gates[gate] = {
            "verified": entry.get("verified"),
            "verified_at": entry.get("verified_at"),
            "verified_by": entry.get("verified_by"),
            "evidence_id": entry.get("evidence_id"),
            "evidence": {
                field_name: evidence.get(field_name)
                for field_name in sorted(EXTERNAL_EVIDENCE_FIELDS[gate])
            },
        }

    validated = {
        "schema_version": payload.get("schema_version"),
        "release_candidate_sha": release_sha,
        "gates": validated_gates,
        "validation_errors": errors,
    }
    return not errors, validated


def classify_decision(
    mechanics_passed: bool,
    qualification_complete: bool,
    external_gates_complete: bool,
) -> str:
    if not mechanics_passed:
        return "NO_GO"
    if not qualification_complete:
        return "PENDING_QUALIFICATION"
    if not external_gates_complete:
        return "PENDING_EXTERNAL_GATES"
    return "GO"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-hours", type=float, default=24.0)
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--base-url", default="https://127.0.0.1:18443")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--external-evidence", type=Path)
    parser.add_argument(
        "--drills",
        default=(
            "worker_restart,redis_restart,agent_buffer_replay,"
            "duplicate_agent,regression_rollback,backup_restore"
        ),
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
    event_lock = threading.Lock()
    duration_seconds = args.duration_hours * 3600

    release_ok, release_evidence, release_detail = release_identity()
    append_jsonl(
        events_path,
        {
            "kind": "preflight",
            "timestamp": utc_now(),
            "release_identity_verified": release_ok,
            "evidence": release_evidence,
            "detail": release_detail,
        },
        event_lock,
    )
    if not release_ok:
        raise SystemExit(
            "staging soak refused: release must be a clean, identifiable Git commit"
        )

    images_ok, image_manifest, image_detail = running_image_manifest(
        expected_release_sha=str(release_evidence["commit_sha"])
    )
    append_jsonl(
        events_path,
        {
            "kind": "preflight",
            "timestamp": utc_now(),
            "running_images_verified": images_ok,
            "evidence": image_manifest,
            "detail": image_detail[-2000:],
        },
        event_lock,
    )
    if not images_ok:
        raise SystemExit(
            "staging soak refused: required services or image identities are missing"
        )

    state = SoakState.load_or_create(
        state_path,
        duration_seconds,
        args.resume,
        release=release_evidence,
        images=image_manifest,
    )
    if args.resume and abs(state.requested_duration_seconds - duration_seconds) > 1:
        raise SystemExit("resume duration differs from the persisted soak duration")
    if args.resume and (
        state.release_sha != release_evidence["commit_sha"]
        or state.image_manifest != image_manifest
    ):
        raise SystemExit(
            "resume release identity differs from the persisted commit or images"
        )
    state.save(state_path)
    state_lock = threading.Lock()

    interlocks_ok, interlock_detail = write_interlocks_disabled()
    append_jsonl(
        events_path,
        {
            "kind": "preflight",
            "timestamp": utc_now(),
            "write_interlocks_disabled": interlocks_ok,
            "detail": interlock_detail,
        },
        event_lock,
    )
    if not interlocks_ok:
        raise SystemExit("staging soak refused: worker write interlocks are not all disabled")

    database_roles_ok, database_role_evidence, database_role_detail = (
        verify_database_roles()
    )
    append_jsonl(
        events_path,
        {
            "kind": "preflight",
            "timestamp": utc_now(),
            "database_roles_verified": database_roles_ok,
            "evidence": database_role_evidence,
            "detail": database_role_detail[-2000:],
        },
        event_lock,
    )
    if not database_roles_ok:
        raise SystemExit(
            "staging soak refused: control-plane database roles are not least privilege"
        )

    drills = [item for item in args.drills.split(",") if item]
    drill_fractions = {
        name: (index + 1) / (len(drills) + 1)
        for index, name in enumerate(drills)
    }
    interrupted = False
    stop_sampler = threading.Event()

    def sample_until_stopped() -> None:
        next_sample_epoch = time.time()
        while not stop_sampler.is_set():
            elapsed = time.time() - state.started_at_epoch
            if elapsed >= state.requested_duration_seconds:
                return
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
            append_jsonl(events_path, sample, event_lock)
            with state_lock:
                state.samples_total += 1
                if sample["ready"]:
                    state.ready_samples += 1
                if transactions is not None:
                    state.max_transactions = max(
                        transactions,
                        state.max_transactions
                        if state.max_transactions is not None
                        else transactions,
                    )
                sample_epoch = time.time()
                previous_sample_epoch = (
                    state.last_sample_epoch
                    if state.last_sample_epoch is not None
                    else state.started_at_epoch
                )
                state.max_sample_gap_seconds = max(
                    state.max_sample_gap_seconds,
                    max(0.0, sample_epoch - previous_sample_epoch),
                )
                state.last_sample_at = sample["timestamp"]
                state.last_sample_epoch = sample_epoch
                state.save(state_path)
            next_sample_epoch += args.interval_seconds
            wait_seconds = max(0.0, next_sample_epoch - time.time())
            if wait_seconds == 0:
                next_sample_epoch = time.time()
            stop_sampler.wait(wait_seconds)

    sampler = threading.Thread(
        target=sample_until_stopped,
        name="dbtune-soak-sampler",
        daemon=True,
    )
    sampler.start()
    try:
        while True:
            elapsed = time.time() - state.started_at_epoch
            if elapsed >= state.requested_duration_seconds:
                break
            for drill in drills:
                due = elapsed >= state.requested_duration_seconds * drill_fractions[drill]
                if due and drill not in state.completed_drills:
                    result = run_drill(drill, args.base_url, args.insecure, args.output_dir)
                    append_jsonl(events_path, result, event_lock)
                    with state_lock:
                        state.completed_drills.append(drill)
                        state.drill_results[drill] = result
                        state.save(state_path)
            time.sleep(1)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stop_sampler.set()
        sampler.join(timeout=max(40, args.interval_seconds + 5))
        final_release_ok, final_release_evidence, final_release_detail = (
            release_identity()
        )
        final_images_ok, final_image_manifest, final_image_detail = (
            running_image_manifest(expected_release_sha=state.release_sha)
        )
        release_identity_unchanged = (
            final_release_ok
            and final_images_ok
            and final_release_evidence.get("commit_sha") == state.release_sha
            and final_image_manifest == state.image_manifest
        )
        append_jsonl(
            events_path,
            {
                "kind": "release_identity",
                "timestamp": utc_now(),
                "verified": release_identity_unchanged,
                "release": final_release_evidence,
                "images": final_image_manifest,
                "detail": "\n".join(
                    detail
                    for detail in (final_release_detail, final_image_detail[-2000:])
                    if detail
                ),
            },
            event_lock,
        )
        final_transactions = target_transaction_count()
        elapsed = max(0.0, time.time() - state.started_at_epoch)
        observation_seconds = min(elapsed, state.requested_duration_seconds)
        expected_samples, sample_coverage_ratio = sampling_coverage(
            state.samples_total,
            observation_seconds,
            args.interval_seconds,
        )
        observation_end_epoch = state.started_at_epoch + observation_seconds
        tail_gap_seconds = max(
            0.0,
            observation_end_epoch
            - (
                state.last_sample_epoch
                if state.last_sample_epoch is not None
                else state.started_at_epoch
            ),
        )
        max_sample_gap_seconds = max(
            state.max_sample_gap_seconds,
            tail_gap_seconds,
        )
        maximum_allowed_gap_seconds = max(60.0, args.interval_seconds * 3)
        sampling_continuous = (
            sample_coverage_ratio >= 0.995
            and max_sample_gap_seconds <= maximum_allowed_gap_seconds
        )
        readiness_ratio = (
            state.ready_samples / state.samples_total if state.samples_total else 0.0
        )
        transaction_progress = (
            state.baseline_transactions is not None
            and state.max_transactions is not None
            and state.max_transactions > state.baseline_transactions
        )
        all_drills_recorded = all(name in state.completed_drills for name in drills)
        drills_passed = all(
            state.drill_results.get(name, {}).get("passed") is True
            for name in drills
        ) and all_drills_recorded
        mechanics_passed = (
            interlocks_ok
            and database_roles_ok
            and release_identity_unchanged
            and sampling_continuous
            and readiness_ratio >= 0.995
            and transaction_progress
            and drills_passed
            and not interrupted
        )
        qualification_seconds = args.qualification_hours * 3600
        qualification_complete = elapsed >= qualification_seconds
        secure_transport = production_tls_mode(args.base_url, args.insecure)
        external_complete, external_evidence = external_gates(
            args.external_evidence,
            expected_release_sha=state.release_sha,
            expected_hostname=urlparse(args.base_url).hostname,
            earliest_verified_at=datetime.fromtimestamp(
                state.started_at_epoch,
                timezone.utc,
            ),
            staffed_not_before=datetime.fromtimestamp(
                state.started_at_epoch + qualification_seconds,
                timezone.utc,
            ),
        )
        external_complete = external_complete and secure_transport
        decision = classify_decision(
            mechanics_passed,
            qualification_complete,
            external_complete,
        )
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
            "samples": state.samples_total,
            "expected_samples": expected_samples,
            "sample_coverage_ratio": round(sample_coverage_ratio, 6),
            "max_sample_gap_seconds": round(max_sample_gap_seconds, 3),
            "maximum_allowed_sample_gap_seconds": round(
                maximum_allowed_gap_seconds,
                3,
            ),
            "sampling_continuous": sampling_continuous,
            "ready_samples": state.ready_samples,
            "readiness_ratio": round(readiness_ratio, 6),
            "baseline_transactions": state.baseline_transactions,
            "final_transactions": final_transactions,
            "transaction_progress": transaction_progress,
            "completed_drills": state.completed_drills,
            "drill_results": list(state.drill_results.values()),
            "database_roles_verified": database_roles_ok,
            "database_role_evidence": database_role_evidence,
            "release_identity_unchanged": release_identity_unchanged,
            "release": {
                "commit_sha": state.release_sha,
                "branch": state.release_branch,
                "worktree_clean": final_release_evidence.get("worktree_clean"),
            },
            "image_manifest": state.image_manifest,
            "mechanics_passed": mechanics_passed,
            "external_gates_complete": external_complete,
            "external_evidence": external_evidence,
            "production_tls_mode": secure_transport,
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
