"""
Configuration Pydantic models for the platform components.
"""

from typing import Optional

from pydantic import BaseModel, Field


class LoopConfig(BaseModel):
    """Configuration for DBA Loop Worker execution."""

    max_iterations: int = Field(default=10, ge=1)
    max_steps: int = Field(default=20, ge=1)
    approval_timeout_hours: int = Field(default=24, ge=1)
    verification_window_seconds: int = Field(default=60, ge=10, le=600)
    degradation_threshold_pct: float = Field(default=10.0, ge=0.0)


class AgentConfig(BaseModel):
    """Configuration for Host Agent evidence collection intervals."""

    pg_settings_interval_sec: int = Field(default=60, ge=10, le=3600)
    pg_stats_interval_sec: int = Field(default=30, ge=5, le=600)
    locks_replication_interval_sec: int = Field(default=15, ge=5, le=300)
    os_metrics_interval_sec: int = Field(default=15, ge=5, le=300)
    max_query_entries: int = Field(default=100, ge=1)


class GuardrailConfig(BaseModel):
    """Configuration for Guardrail Engine safety parameters."""

    risk_threshold: int = Field(default=70, ge=0, le=100)
    dry_run_timeout_sec: int = Field(default=30, ge=1)
    approval_timeout_hours: int = Field(default=24, ge=1)


class AllowlistEntry(BaseModel):
    """An entry in the guardrail allowlist for permitted PostgreSQL settings."""

    setting_name: str
    parameter_context: str  # "reload" or "restart"
    max_deviation_pct: Optional[float] = None
