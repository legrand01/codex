"""Production staging and soak release-gate tests."""

from pathlib import Path

from scripts.staging_init import replace_value
from scripts.staging_preflight import load_env
from scripts.staging_soak import SoakState, classify_decision


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
    initial.save(state_path)

    restored = SoakState.load_or_create(state_path, 86400, resume=True)
    assert restored.baseline_transactions == 42
    assert restored.completed_drills == ["worker_restart"]
    assert restored.started_at_epoch == initial.started_at_epoch


def test_short_success_cannot_be_mislabeled_go():
    assert classify_decision(True, False) == "PENDING_QUALIFICATION"
    assert classify_decision(True, True) == "GO"
    assert classify_decision(False, True) == "NO_GO"


def test_staging_compose_keeps_write_interlocks_disabled():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "ops/staging/docker-compose.staging.yml").read_text(
        encoding="utf-8"
    )
    assert compose.count("WRITE_EXECUTION_ENABLED=false") >= 2
    assert compose.count("PRODUCTION_WRITE_ENABLED=false") >= 2
