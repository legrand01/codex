"""
Application configuration using Pydantic Settings.
"""

from typing import List

from pydantic import ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # Application
    app_name: str = "Autonomous Postgres DBA Agent Platform"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000

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
