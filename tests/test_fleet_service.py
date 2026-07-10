"""
Unit tests for backend/services/fleet_service.py.

Tests the pure functions:
- classify_connection_status: heartbeat age → ConnectionStatus
- evaluate_health_thresholds: metrics dict → HealthStatus
"""

from datetime import datetime, timedelta, timezone

from backend.models.enums import ConnectionStatus, HealthStatus
from backend.services.fleet_service import (
    classify_connection_status,
    evaluate_health_thresholds,
)

# =============================================================================
# Tests for classify_connection_status
# =============================================================================


class TestClassifyConnectionStatus:
    """Tests for heartbeat-to-connection-status classification."""

    def test_none_heartbeat_returns_disconnected(self):
        """No heartbeat ever received should classify as disconnected."""
        result = classify_connection_status(None)
        assert result == ConnectionStatus.DISCONNECTED

    def test_heartbeat_just_now_returns_connected(self):
        """Heartbeat received just now (0 seconds ago) should be connected."""
        now = datetime.now(timezone.utc)
        result = classify_connection_status(now)
        assert result == ConnectionStatus.CONNECTED

    def test_heartbeat_30_seconds_ago_returns_connected(self):
        """Heartbeat 30 seconds ago should be connected (< 60s threshold)."""
        heartbeat = datetime.now(timezone.utc) - timedelta(seconds=30)
        result = classify_connection_status(heartbeat)
        assert result == ConnectionStatus.CONNECTED

    def test_heartbeat_59_seconds_ago_returns_connected(self):
        """Heartbeat 59 seconds ago should still be connected."""
        heartbeat = datetime.now(timezone.utc) - timedelta(seconds=59)
        result = classify_connection_status(heartbeat)
        assert result == ConnectionStatus.CONNECTED

    def test_heartbeat_60_seconds_ago_returns_degraded(self):
        """Heartbeat exactly 60 seconds ago should be degraded."""
        heartbeat = datetime.now(timezone.utc) - timedelta(seconds=60)
        result = classify_connection_status(heartbeat)
        assert result == ConnectionStatus.DEGRADED

    def test_heartbeat_180_seconds_ago_returns_degraded(self):
        """Heartbeat 180 seconds ago (3 min) should be degraded."""
        heartbeat = datetime.now(timezone.utc) - timedelta(seconds=180)
        result = classify_connection_status(heartbeat)
        assert result == ConnectionStatus.DEGRADED

    def test_heartbeat_299_seconds_ago_returns_degraded(self):
        """Heartbeat 299 seconds ago should be degraded (within 60-300s range)."""
        heartbeat = datetime.now(timezone.utc) - timedelta(seconds=299)
        result = classify_connection_status(heartbeat)
        assert result == ConnectionStatus.DEGRADED

    def test_heartbeat_301_seconds_ago_returns_disconnected(self):
        """Heartbeat 301 seconds ago should be disconnected (> 300s)."""
        heartbeat = datetime.now(timezone.utc) - timedelta(seconds=301)
        result = classify_connection_status(heartbeat)
        assert result == ConnectionStatus.DISCONNECTED

    def test_heartbeat_600_seconds_ago_returns_disconnected(self):
        """Heartbeat 10 minutes ago should be disconnected."""
        heartbeat = datetime.now(timezone.utc) - timedelta(seconds=600)
        result = classify_connection_status(heartbeat)
        assert result == ConnectionStatus.DISCONNECTED

    def test_heartbeat_in_future_returns_connected(self):
        """Heartbeat slightly in the future (clock skew) should be connected."""
        heartbeat = datetime.now(timezone.utc) + timedelta(seconds=5)
        result = classify_connection_status(heartbeat)
        assert result == ConnectionStatus.CONNECTED

    def test_naive_datetime_treated_as_utc(self):
        """A naive datetime (no tzinfo) should be handled gracefully."""
        # 30 seconds ago but naive
        heartbeat = datetime.utcnow() - timedelta(seconds=30)
        result = classify_connection_status(heartbeat)
        assert result == ConnectionStatus.CONNECTED


# =============================================================================
# Tests for evaluate_health_thresholds
# =============================================================================


class TestEvaluateHealthThresholds:
    """Tests for health threshold evaluation logic."""

    def test_empty_metrics_returns_unknown(self):
        """No metrics provided should result in unknown health status."""
        result = evaluate_health_thresholds({})
        assert result == HealthStatus.UNKNOWN

    def test_all_metrics_within_bounds_returns_healthy(self):
        """All metrics within thresholds should be healthy."""
        metrics = {
            "cpu_usage_pct": 50.0,
            "memory_usage_pct": 60.0,
            "replication_lag_seconds": 5.0,
        }
        result = evaluate_health_thresholds(metrics)
        assert result == HealthStatus.HEALTHY

    def test_cpu_exceeds_max_threshold_returns_unhealthy(self):
        """CPU usage above max threshold should be unhealthy."""
        metrics = {"cpu_usage_pct": 95.0}
        result = evaluate_health_thresholds(metrics)
        assert result == HealthStatus.UNHEALTHY

    def test_memory_exceeds_max_threshold_returns_unhealthy(self):
        """Memory usage above max threshold should be unhealthy."""
        metrics = {"memory_usage_pct": 92.0}
        result = evaluate_health_thresholds(metrics)
        assert result == HealthStatus.UNHEALTHY

    def test_cache_hit_below_min_threshold_returns_unhealthy(self):
        """Cache hit ratio below min threshold should be unhealthy."""
        metrics = {"cache_hit_ratio": 0.5}
        result = evaluate_health_thresholds(metrics)
        assert result == HealthStatus.UNHEALTHY

    def test_cache_hit_at_min_threshold_returns_healthy(self):
        """Cache hit ratio exactly at min threshold should be healthy (not below)."""
        metrics = {"cache_hit_ratio": 0.80}
        result = evaluate_health_thresholds(metrics)
        assert result == HealthStatus.HEALTHY

    def test_cpu_at_max_threshold_returns_healthy(self):
        """CPU exactly at max threshold should be healthy (not above)."""
        metrics = {"cpu_usage_pct": 90.0}
        result = evaluate_health_thresholds(metrics)
        assert result == HealthStatus.HEALTHY

    def test_unknown_metric_ignored(self):
        """Metrics not in thresholds config should be ignored (no crash)."""
        metrics = {"unknown_metric": 9999.0}
        result = evaluate_health_thresholds(metrics)
        assert result == HealthStatus.HEALTHY

    def test_custom_thresholds(self):
        """Custom thresholds should override defaults."""
        custom_thresholds = {
            "cpu_usage_pct": {"max": 50.0},
        }
        # 60% CPU would be fine with default (90%) but exceeds custom (50%)
        metrics = {"cpu_usage_pct": 60.0}
        result = evaluate_health_thresholds(metrics, thresholds=custom_thresholds)
        assert result == HealthStatus.UNHEALTHY

    def test_one_bad_metric_makes_host_unhealthy(self):
        """If ANY metric crosses threshold, host is unhealthy."""
        metrics = {
            "cpu_usage_pct": 50.0,  # fine
            "memory_usage_pct": 50.0,  # fine
            "replication_lag_seconds": 100.0,  # exceeds 30s max
        }
        result = evaluate_health_thresholds(metrics)
        assert result == HealthStatus.UNHEALTHY

    def test_recovery_to_healthy(self):
        """Metrics returning to normal should classify as healthy."""
        # First check: unhealthy
        bad_metrics = {"cpu_usage_pct": 95.0}
        assert evaluate_health_thresholds(bad_metrics) == HealthStatus.UNHEALTHY

        # Second check: recovered
        good_metrics = {"cpu_usage_pct": 70.0}
        assert evaluate_health_thresholds(good_metrics) == HealthStatus.HEALTHY
