"""Production staging and soak release-gate tests."""

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts import staging_init
from scripts.staging_init import replace_value
from scripts.staging_preflight import (
    load_env,
    validate_database_roles,
    validate_workload_bounds,
)
from scripts.staging_soak import (
    SoakState,
    classify_decision,
    external_gates,
    run_drill,
    sampling_coverage,
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

    def fake_certificate(hostname):
        del hostname
        (secret_dir / "tls.crt").write_text("certificate", encoding="utf-8")
        (secret_dir / "tls.key").write_text("key", encoding="utf-8")

    monkeypatch.setattr(staging_init, "create_local_certificate", fake_certificate)
    monkeypatch.setattr(sys, "argv", ["staging_init.py", "--local"])
    staging_init.main()

    values = load_env(env_file)
    assert validate_database_roles(values) == []
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

    values["CONTROL_DATABASE_URL"] = values["MIGRATION_DATABASE_URL"]
    assert validate_database_roles(values) == [
        "CONTROL_DATABASE_URL must use the dedicated dbtune_runtime role",
        "CONTROL_DATABASE_URL password must match POSTGRES_RUNTIME_PASSWORD",
        "bootstrap, migrator, runtime, and backup roles must be distinct",
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
    initial = SoakState.load_or_create(state_path, 86400, resume=False)
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
    assert not state_path.with_suffix(".json.tmp").exists()


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


def test_external_gates_require_explicit_evidence_for_every_gate(tmp_path):
    path = tmp_path / "external.json"
    path.write_text(
        """
        {
          "real_tls": {"verified": true, "evidence": "certificate check"},
          "external_alert_delivery": {"verified": true, "evidence": "page id"},
          "independent_off_host_restore": {"verified": true, "evidence": "restore id"},
          "staffed_go_no_go": {"verified": false, "evidence": ""}
        }
        """,
        encoding="utf-8",
    )
    complete, payload = external_gates(path)
    assert complete is False
    assert payload["real_tls"]["verified"] is True


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


def test_role_initializer_removes_unexpected_inherited_privileges():
    root = Path(__file__).resolve().parents[1]
    initializer = (
        root / "docker/dbtune-control/init-roles.sh"
    ).read_text(encoding="utf-8")
    assert "FROM pg_auth_members AS membership" in initializer
    assert "REVOKE %I FROM %I" in initializer
    assert "GRANT pg_read_all_data TO dbtune_backup" in initializer
