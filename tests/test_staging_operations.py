"""Production staging and soak release-gate tests."""

import json
import subprocess
from pathlib import Path

from scripts.staging_init import replace_value
from scripts.staging_preflight import load_env
from scripts.staging_soak import (
    SoakState,
    classify_decision,
    external_gates,
    run_drill,
    sampling_coverage,
)


def test_replace_value_updates_exact_environment_key():
    result = replace_value("PORT=8000\nOTHER_PORT=9000\n", "PORT", "8443")
    assert result == "PORT=8443\nOTHER_PORT=9000\n"


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


def test_staging_compose_keeps_write_interlocks_disabled():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "ops/staging/docker-compose.staging.yml").read_text(
        encoding="utf-8"
    )
    assert compose.count("WRITE_EXECUTION_ENABLED=false") >= 2
    assert compose.count("PRODUCTION_WRITE_ENABLED=false") >= 2
