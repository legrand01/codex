"""
Application configuration using Pydantic Settings.
"""

from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_name: str = "Autonomous Postgres DBA Agent Platform"
    environment: str = "development"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000

    # Identity boundary
    auth_required: bool = True
    agent_auth_required: bool = True
    bootstrap_admin_token: str = ""

    # Database
    database_url: str = "postgresql://postgres:postgres@localhost:5432/dba_agent"
    db_pool_min_size: int = 5
    db_pool_max_size: int = 20

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # CORS
    cors_origins: List[str] = ["http://localhost:5173", "http://localhost:3000"]

    # Demo Mode
    demo_mode: bool = False

    # Guardrail defaults
    risk_threshold: int = 70
    dry_run_timeout_sec: int = 30
    approval_timeout_hours: int = 24

    # Production write interlocks. Both the global switch and the per-host
    # switch must be enabled before the control plane can mutate a target.
    write_execution_enabled: bool = False
    production_write_enabled: bool = False
    production_write_confirmation: str = ""
    require_live_target_dry_run: bool = True
    require_live_target_rollback: bool = True
    target_connect_timeout_sec: int = 10
    target_command_timeout_sec: int = 30
    target_verify_timeout_sec: int = 10
    agent_command_timeout_sec: int = 120
    agent_command_poll_interval_sec: float = 0.25
    agent_lease_seconds: int = 90

    # Loop Worker defaults
    max_iterations: int = 10
    max_steps: int = 20
    verification_window_sec: int = 60
    degradation_threshold_pct: float = 10.0

    # Host Agent defaults
    pg_settings_interval_sec: int = 60
    pg_stats_interval_sec: int = 30
    locks_replication_interval_sec: int = 15
    os_metrics_interval_sec: int = 15


settings = Settings()
