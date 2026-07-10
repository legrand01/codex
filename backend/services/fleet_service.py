"""
Fleet management service for heartbeat processing and health status logic.

Provides:
- Connection status classification based on heartbeat age
- Heartbeat processing with host updates and event publishing
- Health threshold crossing detection with configurable thresholds

Requirements: 1.2, 1.3
"""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, Optional
from uuid import UUID

from backend.db.pool import get_pool
from backend.db.redis_manager import publish
from backend.models.enums import ConnectionStatus, HealthStatus

logger = logging.getLogger(__name__)

# Default health metric thresholds
# Keys are metric names, values are dicts with "max" and/or "min" thresholds
DEFAULT_HEALTH_THRESHOLDS: Dict[str, Dict[str, float]] = {
    "cpu_usage_pct": {"max": 90.0},
    "memory_usage_pct": {"max": 90.0},
    "disk_io_utilization_pct": {"max": 85.0},
    "replication_lag_seconds": {"max": 30.0},
    "active_connections_pct": {"max": 90.0},
    "cache_hit_ratio": {"min": 0.80},
}

# Redis pub/sub channel for fleet status events
FLEET_STATUS_CHANNEL = "fleet:status_changes"
HEALTH_CHANGE_CHANNEL = "fleet:health_changes"


def classify_connection_status(
    last_heartbeat: Optional[datetime],
) -> ConnectionStatus:
    """
    Classify a host's connection status based on heartbeat age.

    Rules (per Requirement 1.2):
    - No heartbeat or > 300s ago → disconnected
    - 60-300s ago → degraded
    - < 60s ago → connected

    Args:
        last_heartbeat: The timestamp of the last heartbeat received,
                        or None if no heartbeat has ever been received.

    Returns:
        The classified ConnectionStatus.
    """
    if last_heartbeat is None:
        return ConnectionStatus.DISCONNECTED

    now = datetime.now(timezone.utc)

    # Ensure last_heartbeat is timezone-aware
    if last_heartbeat.tzinfo is None:
        last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)

    elapsed_seconds = (now - last_heartbeat).total_seconds()

    if elapsed_seconds < 0:
        # Future heartbeat (clock skew) — treat as connected
        return ConnectionStatus.CONNECTED

    if elapsed_seconds < 60:
        return ConnectionStatus.CONNECTED
    elif elapsed_seconds <= 300:
        return ConnectionStatus.DEGRADED
    else:
        return ConnectionStatus.DISCONNECTED


def evaluate_health_thresholds(
    metrics: Dict[str, float],
    thresholds: Optional[Dict[str, Dict[str, float]]] = None,
) -> HealthStatus:
    """
    Evaluate host metrics against configurable thresholds to determine health status.

    A host is considered unhealthy if ANY metric crosses its configured threshold.
    A host is healthy when ALL metrics are within their thresholds.

    Args:
        metrics: Dictionary of metric_name -> metric_value.
        thresholds: Optional custom thresholds. If None, uses DEFAULT_HEALTH_THRESHOLDS.
                    Format: {"metric_name": {"max": float} or {"min": float}}

    Returns:
        HealthStatus.UNHEALTHY if any threshold is crossed,
        HealthStatus.HEALTHY if all metrics are within bounds,
        HealthStatus.UNKNOWN if no metrics provided.
    """
    if not metrics:
        return HealthStatus.UNKNOWN

    if thresholds is None:
        thresholds = DEFAULT_HEALTH_THRESHOLDS

    for metric_name, value in metrics.items():
        if metric_name not in thresholds:
            continue

        threshold_config = thresholds[metric_name]

        # Check max threshold (value should be below max)
        if "max" in threshold_config:
            if value > threshold_config["max"]:
                return HealthStatus.UNHEALTHY

        # Check min threshold (value should be above min)
        if "min" in threshold_config:
            if value < threshold_config["min"]:
                return HealthStatus.UNHEALTHY

    return HealthStatus.HEALTHY


async def process_heartbeat(
    host_id: UUID,
    pg_version: Optional[str] = None,
    server_role: Optional[str] = None,
) -> Dict:
    """
    Process a heartbeat from a host agent.

    Updates the host's last_heartbeat timestamp, recalculates connection status,
    and optionally updates pg_version and server_role if they have changed.
    Publishes fleet status change events via Redis pub/sub.

    Args:
        host_id: The UUID of the host sending the heartbeat.
        pg_version: The host's current PostgreSQL version string, if reported.
        server_role: The host's current server role ('primary' or 'replica'), if reported.

    Returns:
        A dict with the updated host info including new connection_status.

    Raises:
        ValueError: If the host_id is not found in the database.
    """
    pool = get_pool()
    if pool is None:
        raise RuntimeError("Database connection pool is not initialized.")

    now = datetime.now(timezone.utc)
    new_connection_status = ConnectionStatus.CONNECTED  # heartbeat just received

    async with pool.acquire() as conn:
        # Fetch current host state
        host = await conn.fetchrow(
            "SELECT id, hostname, pg_version, server_role, connection_status, health_status "
            "FROM hosts WHERE id = $1",
            host_id,
        )

        if host is None:
            raise ValueError(f"Host with id {host_id} not found.")

        old_connection_status = host["connection_status"]
        old_pg_version = host["pg_version"]
        old_server_role = host["server_role"]

        # Build update fields
        update_fields = {
            "last_heartbeat": now,
            "connection_status": new_connection_status.value,
            "updated_at": now,
        }

        # Update pg_version if changed
        if pg_version is not None and pg_version != old_pg_version:
            update_fields["pg_version"] = pg_version

        # Update server_role if changed
        if server_role is not None and server_role != old_server_role:
            update_fields["server_role"] = server_role

        # Build dynamic UPDATE query
        set_clauses = []
        values = []
        for i, (col, val) in enumerate(update_fields.items(), start=1):
            set_clauses.append(f"{col} = ${i}")
            values.append(val)

        values.append(host_id)
        id_placeholder = f"${len(values)}"

        query = f"UPDATE hosts SET {', '.join(set_clauses)} WHERE id = {id_placeholder} RETURNING *"

        updated_host = await conn.fetchrow(query, *values)

    # Publish fleet status change event if connection status changed
    if old_connection_status != new_connection_status.value:
        event = {
            "event_type": "connection_status_change",
            "host_id": str(host_id),
            "hostname": host["hostname"],
            "old_status": old_connection_status,
            "new_status": new_connection_status.value,
            "timestamp": now.isoformat(),
        }
        try:
            await publish(FLEET_STATUS_CHANNEL, json.dumps(event))
        except Exception as e:
            logger.warning(f"Failed to publish fleet status event: {e}")

    return {
        "host_id": str(host_id),
        "hostname": updated_host["hostname"],
        "connection_status": new_connection_status.value,
        "pg_version": updated_host["pg_version"],
        "server_role": updated_host["server_role"],
        "last_heartbeat": now.isoformat(),
    }


async def check_health_thresholds(
    host_id: UUID,
    metrics: Dict[str, float],
    thresholds: Optional[Dict[str, Dict[str, float]]] = None,
) -> Dict:
    """
    Check host metrics against health thresholds and update health_status if needed.

    Evaluates metrics against configurable thresholds, transitions health_status
    to unhealthy when thresholds are crossed, and back to healthy when metrics
    recover. Publishes health change events via Redis pub/sub.

    The health_status update happens within the requirement of 30 seconds of
    metric threshold crossing (Requirement 1.3).

    Args:
        host_id: The UUID of the host to evaluate.
        metrics: Dictionary of metric_name -> metric_value to evaluate.
        thresholds: Optional custom thresholds. If None, uses defaults.

    Returns:
        A dict with health evaluation results including any status transition.

    Raises:
        ValueError: If the host_id is not found in the database.
    """
    pool = get_pool()
    if pool is None:
        raise RuntimeError("Database connection pool is not initialized.")

    new_health_status = evaluate_health_thresholds(metrics, thresholds)

    async with pool.acquire() as conn:
        # Fetch current health status
        host = await conn.fetchrow(
            "SELECT id, hostname, health_status FROM hosts WHERE id = $1",
            host_id,
        )

        if host is None:
            raise ValueError(f"Host with id {host_id} not found.")

        old_health_status = host["health_status"]

        # Only update if status actually changed
        if old_health_status != new_health_status.value:
            now = datetime.now(timezone.utc)
            await conn.execute(
                "UPDATE hosts SET health_status = $1, updated_at = $2 WHERE id = $3",
                new_health_status.value,
                now,
                host_id,
            )

            # Publish health change event
            event = {
                "event_type": "health_status_change",
                "host_id": str(host_id),
                "hostname": host["hostname"],
                "old_status": old_health_status,
                "new_status": new_health_status.value,
                "metrics": metrics,
                "timestamp": now.isoformat(),
            }
            try:
                await publish(HEALTH_CHANGE_CHANNEL, json.dumps(event))
            except Exception as e:
                logger.warning(f"Failed to publish health change event: {e}")

            return {
                "host_id": str(host_id),
                "health_status": new_health_status.value,
                "previous_status": old_health_status,
                "changed": True,
                "metrics_evaluated": metrics,
            }

    return {
        "host_id": str(host_id),
        "health_status": new_health_status.value,
        "previous_status": old_health_status,
        "changed": False,
        "metrics_evaluated": metrics,
    }
