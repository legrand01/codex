"""
Post-apply verification and rollback decision service.

Provides:
- compute_metric_delta() to calculate percentage change between pre/post values
- collect_verification_evidence() to gather evidence after the observation window
- compare_evidence() to compute per-metric deltas
- verify_and_decide() to orchestrate verification and make rollback/keep decision

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional
from uuid import UUID

from backend.models.config import LoopConfig
from backend.services.audit_logger import AuditLogger, get_audit_logger

logger = logging.getLogger(__name__)

# Metric categories to compare during verification
METRIC_CATEGORIES = [
    "pg_stat_database",
    "pg_stat_statements",
    "locks",
    "replication",
    "wal_checkpoint",
    "os_metrics",
]


def compute_metric_delta(pre_value: float, post_value: float) -> float:
    """
    Compute the percentage change between pre-apply and post-apply metric values.

    Formula: (post - pre) / pre * 100

    Handles zero denominator gracefully by returning:
    - 0.0 if both pre and post are zero (no change)
    - float('inf') if pre is zero and post is positive (infinite increase)
    - float('-inf') if pre is zero and post is negative (infinite decrease)

    Args:
        pre_value: The metric value before applying the change.
        post_value: The metric value after applying the change.

    Returns:
        The percentage change as a float.
    """
    if pre_value == 0.0:
        if post_value == 0.0:
            return 0.0
        elif post_value > 0.0:
            return float("inf")
        else:
            return float("-inf")

    return (post_value - pre_value) / pre_value * 100.0


async def collect_verification_evidence(
    host_id: UUID,
    observation_window_seconds: int = 60,
    pool=None,
    after_time: Optional[datetime] = None,
) -> Optional[Dict]:
    """
    Collect verification evidence from the target host after waiting
    for the observation window.

    Waits for the configurable observation window (10-600s, default 60s),
    then collects evidence from the host for all metric categories.

    Args:
        host_id: UUID of the target host.
        observation_window_seconds: How long to wait before collecting (10-600s).
        pool: Optional database connection pool.

    Returns:
        Dict with metric categories mapped to their values, or None on failure.
    """
    # Validate observation window range
    if observation_window_seconds < 10 or observation_window_seconds > 600:
        logger.error(f"Invalid observation window: {observation_window_seconds}s (must be 10-600s)")
        return None

    # Wait for the observation window
    logger.info(f"Waiting {observation_window_seconds}s observation window for host {host_id}")
    await asyncio.sleep(observation_window_seconds)

    # Collect evidence from the host
    try:
        if pool is None:
            from backend.db.pool import get_pool

            pool = get_pool()

        if pool is None:
            logger.error("Database pool is not available for evidence collection")
            return None

        evidence = {}
        found = 0
        async with pool.acquire() as conn:
            for category in METRIC_CATEGORIES:
                row = await conn.fetchrow(
                    """
                    SELECT data FROM evidence_snapshots
                    WHERE host_id = $1 AND evidence_type = $2
                      AND ($3::timestamptz IS NULL OR collected_at > $3)
                    ORDER BY collected_at DESC
                    LIMIT 1
                    """,
                    host_id,
                    category,
                    after_time,
                )
                if row and row["data"]:
                    # Data is stored as JSONB - parse it
                    data = row["data"]
                    if isinstance(data, str):
                        import json

                        data = json.loads(data)
                    evidence[category] = data
                    found += 1
                else:
                    evidence[category] = {}

        return evidence if found else None

    except Exception as e:
        logger.error(f"Failed to collect verification evidence: {e}")
        return None


def compare_evidence(pre_evidence: Dict, post_evidence: Dict) -> Dict[str, float]:
    """
    Compare pre-apply and post-apply evidence for same metric categories.

    Computes per-metric delta for each category present in both pre and post
    evidence. For categories containing multiple numeric metrics, computes
    an aggregate (average of absolute deltas).

    Args:
        pre_evidence: Dict of metric category -> metric values before change.
        post_evidence: Dict of metric category -> metric values after change.

    Returns:
        Dict mapping metric_name to delta percentage.
    """
    deltas = {}

    for category in METRIC_CATEGORIES:
        pre_data = pre_evidence.get(category, {})
        post_data = post_evidence.get(category, {})

        if not pre_data and not post_data:
            continue

        # Compute deltas for numeric values within each category
        category_deltas = []

        # Get all numeric keys present in both pre and post
        all_keys = set()
        if isinstance(pre_data, dict):
            all_keys.update(pre_data.keys())
        if isinstance(post_data, dict):
            all_keys.update(post_data.keys())

        for key in all_keys:
            pre_val = pre_data.get(key) if isinstance(pre_data, dict) else None
            post_val = post_data.get(key) if isinstance(post_data, dict) else None

            # Only compare numeric values
            if isinstance(pre_val, (int, float)) and isinstance(post_val, (int, float)):
                delta = compute_metric_delta(float(pre_val), float(post_val))
                if delta != float("inf") and delta != float("-inf"):
                    category_deltas.append(delta)

        # Aggregate: use the maximum absolute delta for the category
        # This is conservative - if any single metric in the category
        # degrades significantly, we flag the whole category
        if category_deltas:
            # Use the worst (most negative or most positive) delta
            max_abs_delta = max(category_deltas, key=abs)
            deltas[category] = max_abs_delta

    return deltas


async def verify_and_decide(
    run_id: UUID,
    host_id: UUID,
    plan_id: UUID,
    pre_evidence: Dict,
    config: LoopConfig,
    pool=None,
    audit_logger: Optional[AuditLogger] = None,
    applied_at: Optional[datetime] = None,
) -> Dict:
    """
    Orchestrate post-apply verification and make rollback/keep decision.

    1. Collects post-apply evidence within the observation window
    2. Compares pre/post evidence
    3. If any metric degrades beyond threshold: initiates rollback, logs to audit
    4. If all within threshold: marks as "kept"
    5. If collection fails: initiates rollback, logs failure

    Args:
        run_id: UUID of the current loop run.
        host_id: UUID of the target host.
        plan_id: UUID of the plan being verified.
        pre_evidence: Evidence collected before applying the change.
        config: LoopConfig with verification_window_seconds and degradation_threshold_pct.
        pool: Optional database connection pool.
        audit_logger: Optional AuditLogger instance.

    Returns:
        Dict with:
        - decision: "kept" or "rolled_back"
        - deltas: Dict of metric -> delta percentage
        - triggering_metric: Name of the metric that triggered rollback (if applicable)
        - triggering_delta: The delta value that triggered rollback (if applicable)
        - threshold: The configured threshold
        - failure_reason: Reason for rollback if due to collection failure
    """
    if audit_logger is None:
        audit_logger = get_audit_logger()

    threshold = config.degradation_threshold_pct
    observation_window = config.verification_window_seconds

    # Step 1: Collect post-apply evidence
    post_evidence = await collect_verification_evidence(
        host_id=host_id,
        observation_window_seconds=observation_window,
        pool=pool,
        after_time=applied_at,
    )

    # Step 2: Handle collection failure -> rollback
    if not pre_evidence or not post_evidence:
        failure_reason = (
            "Failed to collect verification evidence within observation window "
            f"({observation_window}s) - host may be unavailable"
        )
        logger.warning(
            f"Verification evidence collection failed for run {run_id}, initiating rollback"
        )

        # Log failure to audit
        await audit_logger.log(
            run_id=run_id,
            actor_type="system",
            actor_name="verification_service",
            action_type="verification_collection_failed",
            target_host_id=host_id,
            result="failure",
            result_reason=failure_reason,
            details={
                "plan_id": str(plan_id),
                "observation_window_seconds": observation_window,
            },
        )

        # Initiate rollback
        await _initiate_rollback(plan_id, pool)

        return {
            "decision": "rolled_back",
            "deltas": {},
            "triggering_metric": None,
            "triggering_delta": None,
            "threshold": threshold,
            "failure_reason": failure_reason,
        }

    # Step 3: Compare pre/post evidence
    deltas = compare_evidence(pre_evidence, post_evidence)

    # Step 4: Check if any metric degrades beyond threshold
    for metric_name, delta in deltas.items():
        # A positive delta in "degradation" metrics means things got worse
        # We check if the absolute change exceeds threshold
        # For degradation, we check if delta exceeds threshold (positive = worse)
        lower_is_worse = metric_name == "pg_stat_database"
        degraded = delta < -threshold if lower_is_worse else delta > threshold
        if degraded:
            logger.warning(
                f"Metric '{metric_name}' degraded by {delta:.2f}% "
                f"(threshold: {threshold}%) for run {run_id}"
            )

            # Log the degradation to audit
            await audit_logger.log(
                run_id=run_id,
                actor_type="system",
                actor_name="verification_service",
                action_type="metric_degradation_detected",
                target_host_id=host_id,
                result="failure",
                result_reason=(
                    f"Metric '{metric_name}' degraded by {delta:.2f}% (threshold: {threshold}%)"
                ),
                details={
                    "plan_id": str(plan_id),
                    "triggering_metric": metric_name,
                    "delta": delta,
                    "threshold": threshold,
                    "all_deltas": deltas,
                },
            )

            # Initiate rollback
            await _initiate_rollback(plan_id, pool)

            return {
                "decision": "rolled_back",
                "deltas": deltas,
                "triggering_metric": metric_name,
                "triggering_delta": delta,
                "threshold": threshold,
                "failure_reason": None,
            }

    # Step 5: All metrics within threshold -> mark as kept
    logger.info(f"All metrics within threshold ({threshold}%) for run {run_id}, change kept")

    await audit_logger.log(
        run_id=run_id,
        actor_type="system",
        actor_name="verification_service",
        action_type="verification_passed",
        target_host_id=host_id,
        result="success",
        result_reason="All metrics within degradation threshold",
        details={
            "plan_id": str(plan_id),
            "threshold": threshold,
            "deltas": deltas,
        },
    )

    return {
        "decision": "kept",
        "deltas": deltas,
        "triggering_metric": None,
        "triggering_delta": None,
        "threshold": threshold,
        "failure_reason": None,
    }


async def _initiate_rollback(plan_id: UUID, pool=None) -> None:
    """
    Initiate rollback for the given plan.

    Updates the plan status and triggers the rollback service.

    Args:
        plan_id: UUID of the plan to rollback.
        pool: Optional database connection pool.
    """
    if pool is None:
        from backend.db.pool import get_pool

        pool = get_pool()
    if pool is None:
        raise RuntimeError("Database pool is unavailable for rollback")

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT host_id, rollback_instructions, pre_change_snapshot, apply_result
                FROM plans
                WHERE id = $1
                """,
                plan_id,
            )
        if not row or not row["rollback_instructions"] or not row["pre_change_snapshot"]:
            raise RuntimeError(f"Plan {plan_id} lacks verified rollback state")

        import json

        from backend.services.rollback_service import execute_rollback

        instructions = row["rollback_instructions"]
        snapshot = row["pre_change_snapshot"]
        apply_result = row["apply_result"] or {}
        if isinstance(instructions, str):
            instructions = json.loads(instructions)
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        if isinstance(apply_result, str):
            apply_result = json.loads(apply_result)

        await execute_rollback(
            plan_id,
            instructions,
            host_id=row["host_id"],
            pre_change_snapshot=snapshot,
            control_pool=pool,
            backend_snapshot=apply_result.get("backend_snapshot"),
        )

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE plans
                SET status = 'rolled_back', rolled_back_at = NOW()
                WHERE id = $1
                """,
                plan_id,
            )
    except Exception:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE plans SET status = 'rollback_failed' WHERE id = $1",
                plan_id,
            )
        raise
