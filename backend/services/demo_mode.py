"""
Demo Mode service for the Autonomous Postgres DBA Agent Platform.

Provides:
- Demo mode activation with realistic seed data
- Demo mode deactivation and cleanup
- Connection blocking for non-synthetic hosts while demo is active
- Synthetic evidence, fleet, plan, and loop run generation

Requirements: 14.1, 14.2, 14.3, 14.4, 14.6
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Set
from uuid import uuid4

from backend.models.enums import (
    ConnectionStatus,
    HealthStatus,
    PlanStatus,
    WorkflowStep,
)

logger = logging.getLogger(__name__)

# Module-level state for demo mode
_demo_active: bool = False
_demo_data: Dict[str, Any] = {}

# Synthetic host addresses that are allowed during demo mode
SYNTHETIC_HOST_ADDRESSES: Set[str] = {
    "demo-pg-primary-01.synthetic.local",
    "demo-pg-replica-02.synthetic.local",
    "demo-pg-replica-03.synthetic.local",
    "demo-pg-degraded-04.synthetic.local",
    "demo-pg-disconnected-05.synthetic.local",
}


def is_demo_active() -> bool:
    """
    Check if demo mode is currently active.

    Returns:
        True if demo mode is active, False otherwise.
    """
    return _demo_active


def is_synthetic_address(address: str) -> bool:
    """
    Check if a host address is designated as synthetic.

    Args:
        address: The host address to check.

    Returns:
        True if the address is in the synthetic host set.
    """
    return address in SYNTHETIC_HOST_ADDRESSES


def block_non_synthetic_connection(address: str) -> bool:
    """
    Block connection attempts to non-synthetic hosts while demo mode is active.

    Per Requirement 14.4: While Demo_Mode is active, reject any connection attempts
    to real database hosts and do not transmit network requests to any host address
    not designated as synthetic.

    Args:
        address: The target host address being connected to.

    Returns:
        True if the connection is allowed (synthetic), False if blocked.

    Raises:
        ConnectionRefusedError: If demo mode is active and address is not synthetic.
    """
    if not _demo_active:
        return True

    if is_synthetic_address(address):
        return True

    raise ConnectionRefusedError(
        f"Demo Mode active: connection to non-synthetic host '{address}' is blocked. "
        f"Only synthetic hosts are permitted during demo mode."
    )


def get_demo_data() -> Dict[str, Any]:
    """
    Get the current demo data state.

    Returns:
        Dictionary containing all seeded demo data, or empty dict if demo is not active.
    """
    if not _demo_active:
        return {}
    return _demo_data


def _generate_demo_hosts() -> List[Dict[str, Any]]:
    """
    Generate synthetic fleet hosts with various connection and health states.

    Requirement 14.1: At least 3 hosts representing each Host_Agent connection
    status (connected, disconnected, degraded) and at least one in each health
    state (healthy, unhealthy).

    Returns:
        List of host dictionaries with synthetic fleet data.
    """
    now = datetime.now(timezone.utc)

    hosts = [
        {
            "id": uuid4(),
            "hostname": "demo-pg-primary-01.synthetic.local",
            "pg_version": "16.2",
            "server_role": "primary",
            "health_status": HealthStatus.HEALTHY.value,
            "connection_status": ConnectionStatus.CONNECTED.value,
            "last_heartbeat": now - timedelta(seconds=10),
            "restart_required_enabled": False,
        },
        {
            "id": uuid4(),
            "hostname": "demo-pg-replica-02.synthetic.local",
            "pg_version": "16.2",
            "server_role": "replica",
            "health_status": HealthStatus.UNHEALTHY.value,
            "connection_status": ConnectionStatus.CONNECTED.value,
            "last_heartbeat": now - timedelta(seconds=25),
            "restart_required_enabled": False,
        },
        {
            "id": uuid4(),
            "hostname": "demo-pg-replica-03.synthetic.local",
            "pg_version": "15.6",
            "server_role": "replica",
            "health_status": HealthStatus.HEALTHY.value,
            "connection_status": ConnectionStatus.DEGRADED.value,
            "last_heartbeat": now - timedelta(seconds=120),
            "restart_required_enabled": False,
        },
        {
            "id": uuid4(),
            "hostname": "demo-pg-degraded-04.synthetic.local",
            "pg_version": "15.6",
            "server_role": "replica",
            "health_status": HealthStatus.UNHEALTHY.value,
            "connection_status": ConnectionStatus.DEGRADED.value,
            "last_heartbeat": now - timedelta(seconds=200),
            "restart_required_enabled": False,
        },
        {
            "id": uuid4(),
            "hostname": "demo-pg-disconnected-05.synthetic.local",
            "pg_version": "14.10",
            "server_role": "replica",
            "health_status": HealthStatus.UNKNOWN.value,
            "connection_status": ConnectionStatus.DISCONNECTED.value,
            "last_heartbeat": now - timedelta(seconds=600),
            "restart_required_enabled": False,
        },
    ]

    return hosts


def _generate_demo_evidence(
    hosts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Generate synthetic evidence including all required categories.

    Requirement 14.2: Generate synthetic Evidence containing at least one sample for:
    - Slow query samples
    - Configuration drift scenarios
    - Replication lag events
    - Checkpoint pressure signals
    - Weak-evidence cases that do not meet the Evidence_Quality_Threshold

    Returns:
        List of evidence snapshot dictionaries.
    """
    now = datetime.now(timezone.utc)
    primary_host_id = hosts[0]["id"]
    replica_host_id = hosts[1]["id"]
    run_id = uuid4()

    evidence = [
        # Slow query sample (pg_stat_statements)
        {
            "id": uuid4(),
            "run_id": run_id,
            "host_id": primary_host_id,
            "evidence_type": "pg_stat_statements",
            "collected_at": now - timedelta(minutes=5),
            "data": {
                "queries": [
                    {
                        "query": "SELECT * FROM orders WHERE customer_id = $1 AND status = $2",
                        "calls": 15420,
                        "mean_exec_time_ms": 245.8,
                        "max_exec_time_ms": 12500.3,
                        "rows": 892340,
                        "shared_blks_hit": 45000,
                        "shared_blks_read": 23000,
                    },
                    {
                        "query": (
                            "UPDATE inventory SET quantity = quantity - $1 "
                            "WHERE product_id = $2"
                        ),
                        "calls": 8900,
                        "mean_exec_time_ms": 89.2,
                        "max_exec_time_ms": 5600.1,
                        "rows": 8900,
                        "shared_blks_hit": 12000,
                        "shared_blks_read": 8500,
                    },
                ],
                "total_queries_collected": 2,
            },
            "quality_score": 0.92,
        },
        # Configuration drift (pg_settings)
        {
            "id": uuid4(),
            "run_id": run_id,
            "host_id": primary_host_id,
            "evidence_type": "pg_settings",
            "collected_at": now - timedelta(minutes=4),
            "data": {
                "settings": {
                    "shared_buffers": "4GB",
                    "effective_cache_size": "12GB",
                    "work_mem": "32MB",
                    "maintenance_work_mem": "512MB",
                    "max_connections": "200",
                    "random_page_cost": "1.1",
                    "checkpoint_completion_target": "0.9",
                    "wal_buffers": "64MB",
                },
                "drift_detected": [
                    {
                        "setting": "work_mem",
                        "expected": "64MB",
                        "actual": "32MB",
                        "drift_reason": "Manual override detected",
                    },
                    {
                        "setting": "random_page_cost",
                        "expected": "1.5",
                        "actual": "1.1",
                        "drift_reason": "Configuration change not tracked",
                    },
                ],
            },
            "quality_score": 0.95,
        },
        # Replication lag event
        {
            "id": uuid4(),
            "run_id": run_id,
            "host_id": replica_host_id,
            "evidence_type": "replication",
            "collected_at": now - timedelta(minutes=3),
            "data": {
                "replication_lag_bytes": 52428800,
                "replication_lag_seconds": 45.2,
                "replay_lag_seconds": 38.7,
                "write_lag_seconds": 6.5,
                "flush_lag_seconds": 6.5,
                "state": "streaming",
                "sent_lsn": "0/5A000000",
                "write_lsn": "0/59800000",
                "flush_lsn": "0/59800000",
                "replay_lsn": "0/58000000",
            },
            "quality_score": 0.88,
        },
        # Checkpoint pressure (WAL/checkpoint metrics)
        {
            "id": uuid4(),
            "run_id": run_id,
            "host_id": primary_host_id,
            "evidence_type": "wal_checkpoint",
            "collected_at": now - timedelta(minutes=2),
            "data": {
                "checkpoint_frequency_per_hour": 12,
                "wal_generation_rate_mb_per_min": 85.4,
                "last_checkpoint_age_seconds": 180,
                "checkpoints_timed": 45,
                "checkpoints_requested": 28,
                "buffers_checkpoint": 125000,
                "buffers_backend": 8500,
                "maxwritten_clean": 3,
                "checkpoint_write_time_ms": 45000,
                "checkpoint_sync_time_ms": 2200,
            },
            "quality_score": 0.91,
        },
        # Weak evidence case (below quality threshold)
        {
            "id": uuid4(),
            "run_id": run_id,
            "host_id": primary_host_id,
            "evidence_type": "locks",
            "collected_at": now - timedelta(minutes=1),
            "data": {
                "active_locks": [
                    {
                        "locktype": "relation",
                        "mode": "AccessShareLock",
                        "granted": True,
                        "pid": 12345,
                    }
                ],
                "lock_waits": [],
                "deadlocks_detected": 0,
                "note": "Insufficient lock contention data for diagnosis",
            },
            "quality_score": 0.35,
        },
        # OS metrics
        {
            "id": uuid4(),
            "run_id": run_id,
            "host_id": primary_host_id,
            "evidence_type": "os_metrics",
            "collected_at": now - timedelta(seconds=30),
            "data": {
                "cpu_usage_pct": 72.5,
                "memory_usage_pct": 84.3,
                "disk_io_ops_per_sec": 1250,
                "disk_usage_pct": 65.0,
                "load_average_1m": 4.2,
                "load_average_5m": 3.8,
                "network_bytes_in": 15000000,
                "network_bytes_out": 8500000,
            },
            "quality_score": 0.94,
        },
        # pg_stat_database
        {
            "id": uuid4(),
            "run_id": run_id,
            "host_id": primary_host_id,
            "evidence_type": "pg_stat_database",
            "collected_at": now - timedelta(seconds=45),
            "data": {
                "database_name": "production",
                "numbackends": 145,
                "xact_commit": 1250000,
                "xact_rollback": 3200,
                "blks_read": 450000,
                "blks_hit": 12500000,
                "tup_returned": 8900000,
                "tup_fetched": 3400000,
                "tup_inserted": 125000,
                "tup_updated": 89000,
                "tup_deleted": 12000,
                "conflicts": 0,
                "deadlocks": 2,
                "temp_files": 45,
                "temp_bytes": 524288000,
            },
            "quality_score": 0.93,
        },
    ]

    return evidence


def _generate_demo_loop_runs(
    hosts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Generate synthetic loop runs demonstrating various outcomes.

    Requirement 14.3: Execute loops against synthetic data producing at least
    one successful and one blocked/inconclusive outcome.

    Returns:
        List of loop run dictionaries.
    """
    now = datetime.now(timezone.utc)
    primary_host_id = hosts[0]["id"]
    replica_host_id = hosts[1]["id"]

    runs = [
        # Successful loop run
        {
            "id": uuid4(),
            "host_id": primary_host_id,
            "goal": "Optimize query performance for slow customer order lookups",
            "status": "completed",
            "current_step": WorkflowStep.REPORT.value,
            "current_iteration": 3,
            "max_iterations": 10,
            "max_steps": 20,
            "approval_timeout_hours": 24,
            "verification_window_seconds": 60,
            "degradation_threshold_pct": 10.0,
            "started_at": now - timedelta(hours=2),
            "last_step_transition_at": now - timedelta(minutes=15),
            "completed_at": now - timedelta(minutes=15),
            "failure_reason": None,
        },
        # Blocked/inconclusive loop run (guardrail enforcement)
        {
            "id": uuid4(),
            "host_id": replica_host_id,
            "goal": "Reduce replication lag below 10 seconds",
            "status": "failed",
            "current_step": WorkflowStep.SAFETY_CHECK.value,
            "current_iteration": 1,
            "max_iterations": 10,
            "max_steps": 20,
            "approval_timeout_hours": 24,
            "verification_window_seconds": 60,
            "degradation_threshold_pct": 10.0,
            "started_at": now - timedelta(hours=1),
            "last_step_transition_at": now - timedelta(minutes=45),
            "completed_at": now - timedelta(minutes=45),
            "failure_reason": "Guardrail blocked: proposed setting 'max_wal_senders' "
            "is not on the allowlist for host demo-pg-replica-02",
        },
        # Active loop run (currently in observe step)
        {
            "id": uuid4(),
            "host_id": primary_host_id,
            "goal": "Investigate checkpoint pressure and optimize WAL configuration",
            "status": "running",
            "current_step": WorkflowStep.APPROVAL_GATE.value,
            "current_iteration": 2,
            "max_iterations": 10,
            "max_steps": 20,
            "approval_timeout_hours": 24,
            "verification_window_seconds": 60,
            "degradation_threshold_pct": 10.0,
            "started_at": now - timedelta(minutes=30),
            "last_step_transition_at": now - timedelta(minutes=5),
            "completed_at": None,
            "failure_reason": None,
        },
    ]

    return runs


def _generate_demo_plans(
    hosts: List[Dict[str, Any]],
    runs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Generate synthetic plans including one requiring Approval_Gate interaction.

    Requirement 14.6: Generate at least one Plan that requires Approval_Gate
    interaction, allowing the user to exercise approve/reject workflows.

    Returns:
        List of plan dictionaries.
    """
    now = datetime.now(timezone.utc)
    primary_host_id = hosts[0]["id"]
    replica_host_id = hosts[1]["id"]
    successful_run_id = runs[0]["id"]
    blocked_run_id = runs[1]["id"]
    active_run_id = runs[2]["id"]

    plans = [
        # Applied plan (from successful run)
        {
            "id": uuid4(),
            "run_id": successful_run_id,
            "host_id": primary_host_id,
            "status": PlanStatus.APPLIED.value,
            "proposed_changes": [
                {
                    "setting": "work_mem",
                    "current_value": "32MB",
                    "proposed_value": "64MB",
                    "rationale": "Increase work_mem to reduce disk-based sorting for "
                    "complex ORDER BY queries identified in slow query analysis",
                },
                {
                    "setting": "random_page_cost",
                    "current_value": "1.1",
                    "proposed_value": "1.2",
                    "rationale": "Slightly increase random_page_cost to favor "
                    "sequential scan plans for large table scans",
                },
            ],
            "evidence_references": [
                {
                    "snapshot_id": str(uuid4()),
                    "timestamp": (now - timedelta(minutes=90)).isoformat(),
                },
                {
                    "snapshot_id": str(uuid4()),
                    "timestamp": (now - timedelta(minutes=88)).isoformat(),
                },
            ],
            "risk_score": 35,
            "confidence_score": 0.87,
            "uncertainty_explanation": "work_mem increase is well-supported by query patterns; "
            "random_page_cost adjustment is exploratory",
            "rollback_instructions": [
                {
                    "setting": "work_mem",
                    "restore_value": "32MB",
                    "method": "ALTER SYSTEM SET work_mem = '32MB'",
                },
                {
                    "setting": "random_page_cost",
                    "restore_value": "1.1",
                    "method": "ALTER SYSTEM SET random_page_cost = '1.1'",
                },
            ],
            "rejection_reason": None,
            "approved_by": "demo_dba@example.com",
            "approved_at": now - timedelta(minutes=60),
            "applied_at": now - timedelta(minutes=55),
            "submission_time": now - timedelta(minutes=75),
        },
        # Blocked plan (from failed run - guardrail violation)
        {
            "id": uuid4(),
            "run_id": blocked_run_id,
            "host_id": replica_host_id,
            "status": PlanStatus.BLOCKED.value,
            "proposed_changes": [
                {
                    "setting": "max_wal_senders",
                    "current_value": "10",
                    "proposed_value": "20",
                    "rationale": "Increase max_wal_senders to accommodate additional replicas",
                },
            ],
            "evidence_references": [
                {
                    "snapshot_id": str(uuid4()),
                    "timestamp": (now - timedelta(minutes=50)).isoformat(),
                },
            ],
            "risk_score": 82,
            "confidence_score": 0.62,
            "uncertainty_explanation": (
                "max_wal_senders requires restart and is not on the allowlist"
            ),
            "rollback_instructions": [
                {
                    "setting": "max_wal_senders",
                    "restore_value": "10",
                    "method": "ALTER SYSTEM SET max_wal_senders = '10'",
                },
            ],
            "rejection_reason": None,
            "approved_by": None,
            "approved_at": None,
            "applied_at": None,
            "submission_time": now - timedelta(minutes=50),
        },
        # Pending approval plan (requires Approval_Gate interaction - Req 14.6)
        {
            "id": uuid4(),
            "run_id": active_run_id,
            "host_id": primary_host_id,
            "status": PlanStatus.PENDING_APPROVAL.value,
            "proposed_changes": [
                {
                    "setting": "checkpoint_completion_target",
                    "current_value": "0.9",
                    "proposed_value": "0.7",
                    "rationale": "Reduce checkpoint_completion_target to spread checkpoint "
                    "writes more evenly, reducing I/O spikes during checkpoints",
                },
                {
                    "setting": "max_wal_size",
                    "current_value": "1GB",
                    "proposed_value": "2GB",
                    "rationale": "Increase max_wal_size to allow more WAL accumulation "
                    "between checkpoints, reducing checkpoint frequency",
                },
            ],
            "evidence_references": [
                {
                    "snapshot_id": str(uuid4()),
                    "timestamp": (now - timedelta(minutes=8)).isoformat(),
                },
                {
                    "snapshot_id": str(uuid4()),
                    "timestamp": (now - timedelta(minutes=6)).isoformat(),
                },
            ],
            "risk_score": 45,
            "confidence_score": 0.79,
            "uncertainty_explanation": "Checkpoint tuning is well-supported by WAL pressure "
            "evidence but impact on write-heavy workloads needs verification",
            "rollback_instructions": [
                {
                    "setting": "checkpoint_completion_target",
                    "restore_value": "0.9",
                    "method": "ALTER SYSTEM SET checkpoint_completion_target = '0.9'",
                },
                {
                    "setting": "max_wal_size",
                    "restore_value": "1GB",
                    "method": "ALTER SYSTEM SET max_wal_size = '1GB'",
                },
            ],
            "rejection_reason": None,
            "approved_by": None,
            "approved_at": None,
            "applied_at": None,
            "submission_time": now - timedelta(minutes=5),
        },
    ]

    return plans


def _generate_demo_audit_log(
    hosts: List[Dict[str, Any]],
    runs: List[Dict[str, Any]],
    plans: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Generate synthetic audit log entries for demo data.

    Returns:
        List of audit log entry dictionaries.
    """
    now = datetime.now(timezone.utc)
    primary_host_id = hosts[0]["id"]
    successful_run_id = runs[0]["id"]

    entries = [
        {
            "run_id": successful_run_id,
            "timestamp": now - timedelta(hours=2),
            "actor_type": "system",
            "actor_name": "DBA_Loop_Worker",
            "action_type": "loop_start",
            "target_host_id": primary_host_id,
            "result": "success",
            "result_reason": None,
            "details": {"goal": "Optimize query performance for slow customer order lookups"},
        },
        {
            "run_id": successful_run_id,
            "timestamp": now - timedelta(minutes=75),
            "actor_type": "system",
            "actor_name": "Guardrail_Engine",
            "action_type": "risk_assessment",
            "target_host_id": primary_host_id,
            "result": "success",
            "result_reason": "Risk score 35 is below threshold 70",
            "details": {"risk_score": 35, "threshold": 70},
        },
        {
            "run_id": successful_run_id,
            "timestamp": now - timedelta(minutes=60),
            "actor_type": "human",
            "actor_name": "demo_dba@example.com",
            "action_type": "plan_approval",
            "target_host_id": primary_host_id,
            "result": "success",
            "result_reason": None,
            "details": {"plan_id": str(plans[0]["id"])},
        },
        {
            "run_id": successful_run_id,
            "timestamp": now - timedelta(minutes=55),
            "actor_type": "system",
            "actor_name": "Guardrail_Engine",
            "action_type": "plan_apply",
            "target_host_id": primary_host_id,
            "result": "success",
            "result_reason": None,
            "details": {"plan_id": str(plans[0]["id"]), "changes_applied": 2},
        },
        {
            "run_id": runs[1]["id"],
            "timestamp": now - timedelta(minutes=45),
            "actor_type": "system",
            "actor_name": "Guardrail_Engine",
            "action_type": "allowlist_violation",
            "target_host_id": hosts[1]["id"],
            "result": "blocked",
            "result_reason": "Setting 'max_wal_senders' is not on the allowlist",
            "details": {
                "disallowed_settings": ["max_wal_senders"],
                "plan_id": str(plans[1]["id"]),
            },
        },
    ]

    return entries


def activate_demo_mode() -> Dict[str, Any]:
    """
    Activate demo mode and seed all synthetic data.

    Seeds the platform with realistic fleet data, evidence snapshots, loop runs,
    plans, and audit log entries to demonstrate full platform functionality.

    Requirements: 14.1, 14.2, 14.3, 14.4, 14.6

    Returns:
        Dictionary with demo activation summary including counts of seeded data.

    Raises:
        RuntimeError: If demo mode is already active.
    """
    global _demo_active, _demo_data

    if _demo_active:
        raise RuntimeError("Demo mode is already active.")

    logger.info("Activating demo mode with synthetic data...")

    # Generate all demo data
    hosts = _generate_demo_hosts()
    evidence = _generate_demo_evidence(hosts)
    runs = _generate_demo_loop_runs(hosts)
    plans = _generate_demo_plans(hosts, runs)
    audit_entries = _generate_demo_audit_log(hosts, runs, plans)

    # Store demo data
    _demo_data = {
        "hosts": hosts,
        "evidence": evidence,
        "runs": runs,
        "plans": plans,
        "audit_entries": audit_entries,
        "activated_at": datetime.now(timezone.utc).isoformat(),
    }

    _demo_active = True

    logger.info(
        f"Demo mode activated: {len(hosts)} hosts, {len(evidence)} evidence snapshots, "
        f"{len(runs)} loop runs, {len(plans)} plans, {len(audit_entries)} audit entries"
    )

    return {
        "status": "activated",
        "summary": {
            "hosts_seeded": len(hosts),
            "evidence_snapshots": len(evidence),
            "loop_runs": len(runs),
            "plans": len(plans),
            "audit_entries": len(audit_entries),
            "pending_approval_plans": sum(
                1 for p in plans if p["status"] == PlanStatus.PENDING_APPROVAL.value
            ),
            "successful_runs": sum(1 for r in runs if r["status"] == "completed"),
            "blocked_runs": sum(1 for r in runs if r["status"] == "failed"),
        },
        "activated_at": _demo_data["activated_at"],
    }


def deactivate_demo_mode() -> Dict[str, Any]:
    """
    Deactivate demo mode and clear all synthetic data.

    Returns:
        Dictionary confirming deactivation.

    Raises:
        RuntimeError: If demo mode is not currently active.
    """
    global _demo_active, _demo_data

    if not _demo_active:
        raise RuntimeError("Demo mode is not currently active.")

    logger.info("Deactivating demo mode and clearing synthetic data...")

    activated_at = _demo_data.get("activated_at")
    _demo_data = {}
    _demo_active = False

    logger.info("Demo mode deactivated.")

    return {
        "status": "deactivated",
        "deactivated_at": datetime.now(timezone.utc).isoformat(),
        "was_activated_at": activated_at,
    }


def get_demo_status() -> Dict[str, Any]:
    """
    Get the current demo mode status and summary.

    Returns:
        Dictionary with demo mode status information.
    """
    if not _demo_active:
        return {
            "active": False,
            "message": "Demo mode is not active.",
        }

    return {
        "active": True,
        "activated_at": _demo_data.get("activated_at"),
        "summary": {
            "hosts_seeded": len(_demo_data.get("hosts", [])),
            "evidence_snapshots": len(_demo_data.get("evidence", [])),
            "loop_runs": len(_demo_data.get("runs", [])),
            "plans": len(_demo_data.get("plans", [])),
            "audit_entries": len(_demo_data.get("audit_entries", [])),
        },
    }
