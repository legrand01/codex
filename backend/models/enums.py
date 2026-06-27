"""
Enum classes for the Autonomous Postgres DBA Agent Platform.
"""

from enum import Enum


class HealthStatus(str, Enum):
    """Health status of a PostgreSQL host."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ConnectionStatus(str, Enum):
    """Connection status of a Host Agent based on heartbeat timing."""

    CONNECTED = "connected"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"


class WorkflowStep(str, Enum):
    """Workflow steps in the DBA loop execution."""

    OBSERVE = "observe"
    SNAPSHOT = "snapshot"
    DIAGNOSE = "diagnose"
    PROPOSE_PLAN = "propose_plan"
    SAFETY_CHECK = "safety_check"
    APPROVAL_GATE = "approval_gate"
    DRY_RUN = "dry_run"
    APPLY = "apply"
    VERIFY = "verify"
    MEASURE = "measure"
    KEEP_ROLLBACK = "keep_rollback"
    REPORT = "report"


class PlanStatus(str, Enum):
    """Status of a plan throughout its lifecycle."""

    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    PENDING_FORWARDING = "pending_forwarding"
    FORWARDING_FAILED = "forwarding_failed"
    DRY_RUN_PASSED = "dry_run_passed"
    DRY_RUN_FAILED = "dry_run_failed"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"
    BLOCKED = "blocked"
