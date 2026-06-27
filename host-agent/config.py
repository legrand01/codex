"""
Host Agent configuration module.

Provides AgentConfig with support for environment variables and validation
of collection interval ranges per evidence type.
"""

import os
from dataclasses import dataclass


def _env_int(key: str, default: int) -> int:
    """Read an integer from environment variable, falling back to default."""
    val = os.environ.get(key)
    if val is None:
        return default
    return int(val)


def _env_str(key: str, default: str) -> str:
    """Read a string from environment variable, falling back to default."""
    return os.environ.get(key, default)


# Interval range constraints per evidence type
INTERVAL_RANGES = {
    "pg_settings": (10, 3600),
    "pg_stats": (5, 600),
    "locks_replication": (5, 300),
    "os_metrics": (5, 300),
}


class ConfigValidationError(ValueError):
    """Raised when agent configuration values are out of permitted range."""
    pass


@dataclass
class AgentConfig:
    """
    Host Agent collection configuration.

    Attributes:
        host_id: Unique identifier for this host agent instance.
        hostname: Fleet hostname to register when host_id is not provided.
        control_plane_url: URL of the Control Plane API.
        pg_connection_string: PostgreSQL connection string for the managed host.
        pg_settings_interval: Collection interval for pg_settings (seconds). Range: [10, 3600].
        pg_stats_interval: Collection interval for pg_stat_database/statements
            (seconds). Range: [5, 600].
        locks_replication_interval: Collection interval for locks, replication,
            WAL (seconds). Range: [5, 300].
        os_metrics_interval: Collection interval for OS metrics (seconds). Range: [5, 300].
        max_query_entries: Maximum normalized query entries per collection cycle.
        buffer_max_bytes: Maximum local evidence buffer size in bytes (default 512 MB).
        heartbeat_interval: Interval for heartbeat reporting (seconds).
    """

    host_id: str = ""
    hostname: str = ""
    control_plane_url: str = "http://localhost:8000"
    pg_connection_string: str = ""

    # Collection intervals (seconds)
    pg_settings_interval: int = 60
    pg_stats_interval: int = 30
    locks_replication_interval: int = 15
    os_metrics_interval: int = 15

    # Limits
    max_query_entries: int = 100
    buffer_max_bytes: int = 512 * 1024 * 1024  # 512 MB
    heartbeat_interval: int = 30

    @classmethod
    def from_env(cls) -> "AgentConfig":
        """Create configuration from environment variables."""
        config = cls(
            host_id=_env_str("AGENT_HOST_ID", ""),
            hostname=_env_str("AGENT_HOSTNAME", _env_str("HOSTNAME", "")),
            control_plane_url=_env_str("CONTROL_PLANE_URL", "http://localhost:8000"),
            pg_connection_string=_env_str("PG_CONNECTION_STRING", ""),
            pg_settings_interval=_env_int("PG_SETTINGS_INTERVAL", 60),
            pg_stats_interval=_env_int("PG_STATS_INTERVAL", 30),
            locks_replication_interval=_env_int("LOCKS_REPLICATION_INTERVAL", 15),
            os_metrics_interval=_env_int("OS_METRICS_INTERVAL", 15),
            max_query_entries=_env_int("MAX_QUERY_ENTRIES", 100),
            buffer_max_bytes=_env_int("BUFFER_MAX_BYTES", 512 * 1024 * 1024),
            heartbeat_interval=_env_int("HEARTBEAT_INTERVAL", 30),
        )
        config.validate()
        return config

    def validate(self) -> None:
        """
        Validate all configuration values are within permitted ranges.

        Raises:
            ConfigValidationError: If any value is out of range.
        """
        if not self.host_id and not self.hostname:
            raise ConfigValidationError("host_id or hostname must be a non-empty string")

        if not self.pg_connection_string:
            raise ConfigValidationError("pg_connection_string must be a non-empty string")

        min_val, max_val = INTERVAL_RANGES["pg_settings"]
        if not (min_val <= self.pg_settings_interval <= max_val):
            raise ConfigValidationError(
                f"pg_settings_interval must be in [{min_val}, {max_val}], "
                f"got {self.pg_settings_interval}"
            )

        min_val, max_val = INTERVAL_RANGES["pg_stats"]
        if not (min_val <= self.pg_stats_interval <= max_val):
            raise ConfigValidationError(
                f"pg_stats_interval must be in [{min_val}, {max_val}], "
                f"got {self.pg_stats_interval}"
            )

        min_val, max_val = INTERVAL_RANGES["locks_replication"]
        if not (min_val <= self.locks_replication_interval <= max_val):
            raise ConfigValidationError(
                f"locks_replication_interval must be in [{min_val}, {max_val}], "
                f"got {self.locks_replication_interval}"
            )

        min_val, max_val = INTERVAL_RANGES["os_metrics"]
        if not (min_val <= self.os_metrics_interval <= max_val):
            raise ConfigValidationError(
                f"os_metrics_interval must be in [{min_val}, {max_val}], "
                f"got {self.os_metrics_interval}"
            )

        if self.max_query_entries < 1:
            raise ConfigValidationError(
                f"max_query_entries must be >= 1, got {self.max_query_entries}"
            )

        if self.buffer_max_bytes < 1:
            raise ConfigValidationError(
                f"buffer_max_bytes must be >= 1, got {self.buffer_max_bytes}"
            )
