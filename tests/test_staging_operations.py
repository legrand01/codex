"""Production staging and soak release-gate tests."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts import staging_init
from scripts.staging_init import replace_value
from scripts.staging_preflight import (
    host_runtime_database_url,
    load_env,
    runtime_database_identity,
    validate_database_roles,
    validate_release_identity,
    validate_workload_bounds,
)
from scripts.staging_soak import (
    SoakState,
    classify_decision,
    external_gates,
    production_tls_mode,
    release_identity,
    run_drill,
    running_image_manifest,
    sampling_coverage,
    tls_peer_evidence,
    verify_database_roles,
)


def test_replace_value_updates_exact_environment_key():
    result = replace_value("PORT=8000\nOTHER_PORT=9000\n", "PORT", "8443")
    assert result == "PORT=8443\nOTHER_PORT=9000\n"


def test_staging_init_generates_distinct_database_role_credentials(
    tmp_path, monkeypatch
):
    secret_dir = tmp_path / ".staging" / "secrets"
    generated_dir = tmp_path / ".staging" / "generated"
    env_file = tmp_path / ".env.staging"
    example = Path(__file__).resolve().parents[1] / ".env.staging.example"
    monkeypatch.setattr(staging_init, "ROOT", tmp_path)
    monkeypatch.setattr(staging_init, "EXAMPLE", example)
    monkeypatch.setattr(staging_init, "ENV_FILE", env_file)
    monkeypatch.setattr(staging_init, "SECRET_DIR", secret_dir)
    monkeypatch.setattr(staging_init, "GENERATED_DIR", generated_dir)
    monkeypatch.setattr(staging_init, "current_release_sha", lambda: "a" * 40)

    def fake_certificate(hostname):
        del hostname
        (secret_dir / "tls.crt").write_text("certificate", encoding="utf-8")
        (secret_dir / "tls.key").write_text("key", encoding="utf-8")

    monkeypatch.setattr(staging_init, "create_local_certificate", fake_certificate)
    monkeypatch.setattr(sys, "argv", ["staging_init.py", "--local"])
    staging_init.main()

    values = load_env(env_file)
    assert validate_database_roles(values) == []
    assert values["RELEASE_COMMIT_SHA"] == "a" * 40
    passwords = {
        values["POSTGRES_PASSWORD"],
        values["POSTGRES_MIGRATION_PASSWORD"],
        values["POSTGRES_RUNTIME_PASSWORD"],
        values["POSTGRES_BACKUP_PASSWORD"],
    }
    assert len(passwords) == 4


def test_load_env_ignores_comments_and_preserves_json(tmp_path):
    env_file = tmp_path / ".env.staging"
    env_file.write_text(
        '# comment\nCORS_ORIGINS=["https://staging.example"]\nDEBUG=false\n',
        encoding="utf-8",
    )
    assert load_env(env_file) == {
        "CORS_ORIGINS": '["https://staging.example"]',
        "DEBUG": "false",
    }


def test_local_staging_workload_must_be_rate_limited_and_cpu_bounded():
    assert validate_workload_bounds({}, allow_local=True) == []
    assert validate_workload_bounds(
        {
            "STAGING_PGBENCH_CLIENTS": "8",
            "STAGING_PGBENCH_JOBS": "4",
            "STAGING_PGBENCH_RATE": "0",
            "STAGING_TARGET_POSTGRES_CPUS": "8",
        },
        allow_local=True,
    ) == [
        "STAGING_PGBENCH_RATE must be rate-limited above zero",
        "local staging is capped at 4 clients, 10 transactions/s, "
        "2 PostgreSQL CPUs, and 0.5 workload CPUs",
    ]


def test_database_roles_must_be_distinct_and_least_privilege_ready():
    values = {
        "POSTGRES_USER": "dbtune_bootstrap",
        "POSTGRES_PASSWORD": "bootstrap-password-that-is-long",
        "POSTGRES_DB": "dba_agent",
        "POSTGRES_MIGRATION_PASSWORD": "migration-password-that-is-long",
        "POSTGRES_RUNTIME_PASSWORD": "runtime-password-that-is-long-enough",
        "POSTGRES_BACKUP_PASSWORD": "backup-password-that-is-long-enough",
        "MIGRATION_DATABASE_URL": (
            "postgresql://dbtune_migrator:migration-password-that-is-long"
            "@postgres:5432/dba_agent"
        ),
        "CONTROL_DATABASE_URL": (
            "postgresql://dbtune_runtime:runtime-password-that-is-long-enough"
            "@postgres:5432/dba_agent"
        ),
        "BACKUP_DATABASE_URL": (
            "postgresql://dbtune_backup:backup-password-that-is-long-enough"
            "@postgres:5432/dba_agent"
        ),
    }
    assert validate_database_roles(values) == []
    assert runtime_database_identity(values) == (
        "dbtune_runtime",
        "runtime-password-that-is-long-enough",
        "dba_agent",
    )
    assert host_runtime_database_url(values) == (
        "postgresql://dbtune_runtime:runtime-password-that-is-long-enough"
        "@127.0.0.1:15432/dba_agent"
    )

    values["CONTROL_DATABASE_URL"] = values["MIGRATION_DATABASE_URL"]
    assert validate_database_roles(values) == [
        "CONTROL_DATABASE_URL must use the dedicated dbtune_runtime role",
        "CONTROL_DATABASE_URL password must match POSTGRES_RUNTIME_PASSWORD",
        "bootstrap, migrator, runtime, and backup roles must be distinct",
    ]


def test_failure_drills_never_connect_with_bootstrap_credentials():
    root = Path(__file__).resolve().parents[1]
    for script_name in ("staging_drills.py", "drill_regression_rollback.py"):
        script = (root / "scripts" / script_name).read_text(encoding="utf-8")
        assert 'values["POSTGRES_USER"]' not in script
        assert "values['POSTGRES_USER']" not in script
        assert "host_runtime_database_url" in script


def test_release_identity_must_match_clean_checkout(monkeypatch, tmp_path):
    values = {"RELEASE_COMMIT_SHA": "a" * 40}
    results = iter(
        [
            subprocess.CompletedProcess(["git"], 0, f"{'a' * 40}\n", ""),
            subprocess.CompletedProcess(["git"], 0, "", ""),
        ]
    )
    monkeypatch.setattr(
        "scripts.staging_preflight.subprocess.run",
        lambda *args, **kwargs: next(results),
    )
    assert validate_release_identity(values, root=tmp_path) == []

    dirty_results = iter(
        [
            subprocess.CompletedProcess(["git"], 0, f"{'a' * 40}\n", ""),
            subprocess.CompletedProcess(["git"], 0, " M backend/main.py\n", ""),
        ]
    )
    monkeypatch.setattr(
        "scripts.staging_preflight.subprocess.run",
        lambda *args, **kwargs: next(dirty_results),
    )
    assert validate_release_identity(values, root=tmp_path) == [
        "staging release checkout must be clean before images are built"
    ]


def test_workload_rejects_non_numeric_values_without_shell_evaluation(tmp_path):
    root = Path(__file__).resolve().parents[1]
    marker = tmp_path / "must-not-exist"
    environment = dict(os.environ)
    environment["PGBENCH_CLIENTS"] = f"$(touch {marker})"

    result = subprocess.run(
        ["sh", str(root / "docker/dbtune-target/run-workload.sh")],
        capture_output=True,
        text=True,
        timeout=5,
        env=environment,
    )

    assert result.returncode == 2
    assert "clients must be a non-negative integer" in result.stderr
    assert not marker.exists()


def test_soak_state_is_resumable(tmp_path, monkeypatch):
    state_path = tmp_path / "run-state.json"
    monkeypatch.setattr("scripts.staging_soak.target_transaction_count", lambda: 42)
    images = {
        "app": {
            "configured_image": "dbtune-app:candidate",
            "image_id": "sha256:abc",
        }
    }
    tls_peer = {
        "hostname": "staging.dbtune.example",
        "certificate_sha256": "b" * 64,
    }
    initial = SoakState.load_or_create(
        state_path,
        86400,
        resume=False,
        release={"commit_sha": "abc123", "branch": "codex/release"},
        images=images,
        tls_peer=tls_peer,
    )
    initial.completed_drills.append("worker_restart")
    initial.drill_results["worker_restart"] = {"passed": True}
    initial.samples_total = 100
    initial.ready_samples = 99
    initial.last_sample_epoch = initial.started_at_epoch + 300
    initial.max_sample_gap_seconds = 31.5
    initial.save(state_path)

    restored = SoakState.load_or_create(state_path, 86400, resume=True)
    assert restored.baseline_transactions == 42
    assert restored.completed_drills == ["worker_restart"]
    assert restored.drill_results["worker_restart"]["passed"] is True
    assert restored.samples_total == 100
    assert restored.ready_samples == 99
    assert restored.last_sample_epoch == initial.started_at_epoch + 300
    assert restored.max_sample_gap_seconds == 31.5
    assert restored.started_at_epoch == initial.started_at_epoch
    assert restored.release_sha == "abc123"
    assert restored.release_branch == "codex/release"
    assert restored.image_manifest == images
    assert restored.tls_peer == tls_peer
    assert not state_path.with_suffix(".json.tmp").exists()


def test_release_identity_requires_a_clean_worktree(monkeypatch):
    results = iter(
        [
            subprocess.CompletedProcess(["git"], 0, "abc123\n", ""),
            subprocess.CompletedProcess(["git"], 0, "codex/release\n", ""),
            subprocess.CompletedProcess(["git"], 0, "", ""),
        ]
    )
    monkeypatch.setattr(
        "scripts.staging_soak.run",
        lambda command, timeout=120: next(results),
    )

    passed, evidence, detail = release_identity()

    assert passed is True
    assert evidence == {
        "commit_sha": "abc123",
        "branch": "codex/release",
        "worktree_clean": True,
        "dirty_paths": [],
    }
    assert detail == ""

    dirty_results = iter(
        [
            subprocess.CompletedProcess(["git"], 0, "abc123\n", ""),
            subprocess.CompletedProcess(["git"], 0, "codex/release\n", ""),
            subprocess.CompletedProcess(["git"], 0, " M backend/main.py\n", ""),
        ]
    )
    monkeypatch.setattr(
        "scripts.staging_soak.run",
        lambda command, timeout=120: next(dirty_results),
    )
    passed, evidence, _ = release_identity()
    assert passed is False
    assert evidence["dirty_paths"] == ["backend/main.py"]


def test_running_image_manifest_requires_every_production_soak_service(monkeypatch):
    services = [
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
    ]
    rows = "\n".join(
        (
            f"{service}\trepository/{service}:candidate\tsha256:{index:064x}"
            f"\t{'a' * 40 if service in {'app', 'frontend', 'target-host-agent', 'worker'} else ''}"
        )
        for index, service in enumerate(services, start=1)
    )
    results = iter(
        [
            subprocess.CompletedProcess(["docker"], 0, "one\ntwo\n", ""),
            subprocess.CompletedProcess(["docker"], 0, rows, ""),
        ]
    )
    monkeypatch.setattr(
        "scripts.staging_soak.run",
        lambda command, timeout=120: next(results),
    )

    passed, manifest, _ = running_image_manifest(expected_release_sha="a" * 40)

    assert passed is True
    assert set(manifest) == set(services)
    assert manifest["app"]["image_id"] == f"sha256:{2:064x}"
    assert manifest["app"]["source_revision"] == "a" * 40


def test_short_success_cannot_be_mislabeled_go():
    assert classify_decision(True, False, False) == "PENDING_QUALIFICATION"
    assert classify_decision(True, True, False) == "PENDING_EXTERNAL_GATES"
    assert classify_decision(True, True, True) == "GO"
    assert classify_decision(False, True, True) == "NO_GO"


def test_sampling_coverage_exposes_sleep_or_monitoring_gaps():
    expected, complete_ratio = sampling_coverage(2880, 86400, 30)
    assert expected == 2880
    assert complete_ratio == 1.0

    expected, sleeping_ratio = sampling_coverage(1920, 86400, 30)
    assert expected == 2880
    assert sleeping_ratio == 1920 / 2880
    assert sleeping_ratio < 0.995


def test_external_gates_are_structured_and_bound_to_release(tmp_path):
    path = tmp_path / "external.json"
    release_sha = "a" * 40
    verified_at = "2026-07-24T08:00:00+00:00"
    evidence = {
        "schema_version": 1,
        "release_candidate_sha": release_sha,
        "gates": {
            "real_tls": {
                "verified": True,
                "verified_at": verified_at,
                "verified_by": "release-operator",
                "evidence_id": "tls-check-42",
                "evidence": {
                    "hostname": "staging.dbtune.example",
                    "certificate_sha256": "b" * 64,
                    "issuer": "Example CA",
                },
            },
            "external_alert_delivery": {
                "verified": True,
                "verified_at": verified_at,
                "verified_by": "on-call-engineer",
                "evidence_id": "incident-42",
                "evidence": {
                    "receiver": "pager",
                    "alert_id": "alert-42",
                    "acknowledgement_id": "ack-42",
                },
            },
            "independent_off_host_restore": {
                "verified": True,
                "verified_at": verified_at,
                "verified_by": "database-operator",
                "evidence_id": "restore-42",
                "evidence": {
                    "source_backup_sha256": "c" * 64,
                    "source_host": "staging-primary",
                    "restore_host": "restore-validator",
                    "restore_test_id": "restore-42",
                },
                "measurements": {
                    "dump_size_bytes": 1024,
                    "postgres_major": 16,
                    "schema_migrations": 19,
                    "public_tables": 29,
                },
            },
            "staffed_go_no_go": {
                "verified": True,
                "verified_at": verified_at,
                "verified_by": "release-manager",
                "evidence_id": "change-42",
                "evidence": {
                    "approver": "release-manager",
                    "decision": "GO",
                    "scope": "one self-managed reload-only PostgreSQL target",
                },
            },
        },
    }
    path.write_text(json.dumps(evidence), encoding="utf-8")

    complete, payload = external_gates(
        path,
        expected_release_sha=release_sha,
        expected_hostname="staging.dbtune.example",
        expected_certificate_sha256="b" * 64,
    )

    assert complete is True
    assert payload["validation_errors"] == []

    evidence["unexpected_secret"] = "must-not-enter-summary"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    complete, payload = external_gates(path, expected_release_sha=release_sha)
    assert complete is False
    assert "unexpected_secret" not in payload
    assert payload["validation_errors"] == [
        "unexpected top-level evidence fields: ['unexpected_secret']"
    ]
    del evidence["unexpected_secret"]
    path.write_text(json.dumps(evidence), encoding="utf-8")

    complete, payload = external_gates(path, expected_release_sha="d" * 40)
    assert complete is False
    assert payload["validation_errors"] == [
        "release_candidate_sha does not match the qualified commit"
    ]

    complete, payload = external_gates(
        path,
        expected_release_sha=release_sha,
        expected_hostname="different.dbtune.example",
    )
    assert complete is False
    assert payload["validation_errors"] == [
        "real_tls: evidence.hostname does not match the qualified URL"
    ]

    complete, payload = external_gates(
        path,
        expected_release_sha=release_sha,
        expected_hostname="staging.dbtune.example",
        expected_certificate_sha256="e" * 64,
    )
    assert complete is False
    assert payload["validation_errors"] == [
        "real_tls: evidence.certificate_sha256 does not match "
        "the observed peer certificate"
    ]

    complete, payload = external_gates(
        path,
        expected_release_sha=release_sha,
        expected_hostname="staging.dbtune.example",
        earliest_verified_at=datetime(2026, 7, 24, 9, tzinfo=timezone.utc),
        staffed_not_before=datetime(2026, 7, 25, 9, tzinfo=timezone.utc),
        now=datetime(2026, 7, 25, 10, tzinfo=timezone.utc),
    )
    assert complete is False
    errors = payload["validation_errors"]
    assert sum("predates the qualification" in error for error in errors) == 4
    assert (
        "staffed_go_no_go: verified_at predates qualification completion"
        in errors
    )


def test_production_tls_mode_rejects_insecure_and_loopback_urls():
    assert production_tls_mode("https://staging.dbtune.example", False) is True
    assert production_tls_mode("https://staging.dbtune.example", True) is False
    assert production_tls_mode("http://staging.dbtune.example", False) is False
    assert production_tls_mode("https://127.0.0.1:18443", False) is False
    assert production_tls_mode("https://localhost:18443", False) is False


def test_tls_peer_evidence_records_verified_certificate(monkeypatch):
    certificate_der = b"test-certificate-der"

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    class FakeTlsConnection(FakeConnection):
        def getpeercert(self, binary_form=False):
            if binary_form:
                return certificate_der
            return {
                "issuer": ((("commonName", "Example CA"),),),
                "notAfter": "Jul 25 00:00:00 2027 GMT",
            }

    class FakeContext:
        def wrap_socket(self, connection, server_hostname):
            assert isinstance(connection, FakeConnection)
            assert server_hostname == "staging.dbtune.example"
            return FakeTlsConnection()

    monkeypatch.setattr(
        "scripts.staging_soak.socket.create_connection",
        lambda address, timeout: FakeConnection(),
    )
    monkeypatch.setattr(
        "scripts.staging_soak.ssl.create_default_context",
        lambda: FakeContext(),
    )

    passed, evidence, detail = tls_peer_evidence(
        "https://staging.dbtune.example",
        insecure=False,
    )

    assert passed is True
    assert evidence == {
        "hostname": "staging.dbtune.example",
        "port": "443",
        "certificate_sha256": hashlib.sha256(certificate_der).hexdigest(),
        "issuer": "commonName=Example CA",
        "not_after": "Jul 25 00:00:00 2027 GMT",
    }
    assert detail == ""


def test_json_drill_keeps_complete_structured_evidence(tmp_path, monkeypatch):
    payload = {
        "drill": "regression_rollback",
        "passed": True,
        "baseline": {"median_execution_ms": 100.0},
        "degraded": {"median_execution_ms": 900.0},
        "samples": [{"value": "x" * 3000}],
    }
    monkeypatch.setattr(
        "scripts.staging_soak.run",
        lambda command, timeout=120: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    result = run_drill(
        "regression_rollback",
        "https://staging.example",
        False,
        tmp_path,
    )

    assert result["passed"] is True
    assert result["evidence"] == payload
    assert len(result["detail"]) == 2000


def test_database_role_verification_is_structured_and_fail_closed(monkeypatch):
    payload = {
        "passed": True,
        "database_owner": "dbtune_migrator",
        "runtime_can_create": False,
        "backup_can_write": False,
    }
    monkeypatch.setattr(
        "scripts.staging_soak.run",
        lambda command, timeout=120: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )
    passed, evidence, detail = verify_database_roles()
    assert passed is True
    assert evidence == payload
    assert json.loads(detail) == payload

    monkeypatch.setattr(
        "scripts.staging_soak.run",
        lambda command, timeout=120: subprocess.CompletedProcess(
            command,
            0,
            stdout="not-json",
            stderr="",
        ),
    )
    assert verify_database_roles() == (False, {}, "not-json")


def test_staging_compose_keeps_write_interlocks_disabled():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "ops/staging/docker-compose.staging.yml").read_text(
        encoding="utf-8"
    )
    assert compose.count("WRITE_EXECUTION_ENABLED=false") >= 2
    assert compose.count("PRODUCTION_WRITE_ENABLED=false") >= 2
    assert "PGBENCH_RATE=${STAGING_PGBENCH_RATE:-2}" in compose
    assert 'cpus: "${STAGING_TARGET_POSTGRES_CPUS:-2.0}"' in compose
    assert "control-role-init:" in compose
    assert "DATABASE_URL=${MIGRATION_DATABASE_URL:" in compose
    assert "PGUSER=dbtune_backup" in compose
    assert compose.count("RELEASE_COMMIT_SHA: ${RELEASE_COMMIT_SHA:") == 5

    for dockerfile_name in ("Dockerfile.backend", "Dockerfile.frontend"):
        dockerfile = (root / "docker" / dockerfile_name).read_text(encoding="utf-8")
        assert 'LABEL org.opencontainers.image.revision="${RELEASE_COMMIT_SHA}"' in (
            dockerfile
        )


def test_role_initializer_removes_unexpected_inherited_privileges():
    root = Path(__file__).resolve().parents[1]
    initializer = (
        root / "docker/dbtune-control/init-roles.sh"
    ).read_text(encoding="utf-8")
    assert "FROM pg_auth_members AS membership" in initializer
    assert "REVOKE %I FROM %I" in initializer
    assert "GRANT pg_read_all_data TO dbtune_backup" in initializer
