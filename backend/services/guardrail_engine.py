"""
Guardrail Engine service for safety enforcement.

Provides:
- check_allowlist() to verify all proposed changes are on the allowlist
- check_restart_permission() to verify host has restart-required changes enabled
- calculate_risk_score() to compute risk based on deviation, host role, and setting count
- check_risk_score() to fetch current settings, compute risk, and block if threshold exceeded
- execute_dry_run() to verify SQL statements parse and settings exist
- validate_rollback_plan() to validate rollback instructions against pre-snapshot
- full_safety_check() to orchestrate the full safety workflow
- Audit logging of violations with disallowed setting names and host identifier

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from uuid import UUID

from backend.db.pool import get_pool
from backend.models.plans import RiskScore
from backend.services.audit_logger import AuditLogger, get_audit_logger

logger = logging.getLogger(__name__)


@dataclass
class AllowlistResult:
    """Result of allowlist enforcement check.

    Attributes:
        passed: Whether the plan passed the allowlist check.
        violations: List of human-readable violation descriptions.
        rejected_settings: List of setting names that caused rejection.
    """

    passed: bool
    violations: List[str] = field(default_factory=list)
    rejected_settings: List[str] = field(default_factory=list)


async def check_allowlist(
    proposed_changes: List[dict],
    host_id: UUID,
    pool=None,
    audit_logger: Optional[AuditLogger] = None,
) -> AllowlistResult:
    """
    Check proposed setting changes against the guardrail allowlist for a host.

    Enforcement rules:
    - If the allowlist is empty, reject the entire plan (Requirement 8.1)
    - If any proposed setting is not in the allowlist, reject the entire plan (Requirement 8.2)
    - Classify settings as reload-safe or restart-required based on parameter_context
      (Requirement 8.3)
    - Permit restart-required changes only when host has restart_required_enabled = true
      (Requirement 8.4)
    - Record violations in Audit_Log with disallowed setting names and host identifier
      (Requirement 8.2, 8.4)

    Args:
        proposed_changes: List of dicts, each with at minimum a "setting_name" key
                         representing the PostgreSQL setting to be modified.
        host_id: UUID of the target host.
        pool: Optional asyncpg connection pool. If None, uses get_pool().
        audit_logger: Optional AuditLogger instance. If None, uses get_audit_logger().

    Returns:
        AllowlistResult indicating whether the plan passed or was rejected,
        with violation details.
    """
    if pool is None:
        pool = get_pool()
        if pool is None:
            raise RuntimeError("Database connection pool is not initialized.")

    if audit_logger is None:
        audit_logger = get_audit_logger()

    violations: List[str] = []
    rejected_settings: List[str] = []

    async with pool.acquire() as conn:
        # Fetch the allowlist for this host
        allowlist_rows = await conn.fetch(
            """
            SELECT setting_name, parameter_context, max_deviation_pct
            FROM guardrail_allowlist
            WHERE host_id = $1
            """,
            host_id,
        )

        # Rule: If allowlist is empty, reject the entire plan
        if not allowlist_rows:
            violation_msg = "Allowlist is empty — all changes are rejected"
            violations.append(violation_msg)
            # All proposed settings are rejected when allowlist is empty
            for change in proposed_changes:
                setting_name = change.get("setting_name", "unknown")
                rejected_settings.append(setting_name)

            await audit_logger.log(
                actor_type="system",
                actor_name="guardrail_engine",
                action_type="allowlist_violation",
                target_host_id=host_id,
                result="blocked",
                result_reason="Allowlist is empty",
                details={
                    "rejected_settings": rejected_settings,
                    "host_id": str(host_id),
                },
            )

            return AllowlistResult(
                passed=False,
                violations=violations,
                rejected_settings=rejected_settings,
            )

        # Build lookup by setting_name for efficient access
        allowlist_map = {
            row["setting_name"]: {
                "parameter_context": row["parameter_context"],
                "max_deviation_pct": row["max_deviation_pct"],
            }
            for row in allowlist_rows
        }

        # Fetch host's restart_required_enabled flag
        host_row = await conn.fetchrow(
            "SELECT restart_required_enabled FROM hosts WHERE id = $1",
            host_id,
        )

        restart_enabled = False
        if host_row is not None:
            restart_enabled = bool(host_row["restart_required_enabled"])

    # Check each proposed change against the allowlist
    for change in proposed_changes:
        setting_name = change.get("setting_name", "unknown")

        # Rule: If any setting is not in the allowlist, reject
        if setting_name not in allowlist_map:
            violation_msg = f"Setting '{setting_name}' is not in the allowlist"
            violations.append(violation_msg)
            rejected_settings.append(setting_name)
            continue

        # Rule: Check parameter_context — restart-required needs explicit enablement
        entry = allowlist_map[setting_name]
        if entry["parameter_context"] == "restart":
            if not restart_enabled:
                violation_msg = (
                    f"Setting '{setting_name}' requires restart but "
                    f"restart_required_enabled is not set for this host"
                )
                violations.append(violation_msg)
                rejected_settings.append(setting_name)

    # If there are any violations, the entire plan is rejected
    if violations:
        await audit_logger.log(
            actor_type="system",
            actor_name="guardrail_engine",
            action_type="allowlist_violation",
            target_host_id=host_id,
            result="blocked",
            result_reason="; ".join(violations),
            details={
                "rejected_settings": rejected_settings,
                "host_id": str(host_id),
            },
        )

        return AllowlistResult(
            passed=False,
            violations=violations,
            rejected_settings=rejected_settings,
        )

    # All checks passed
    return AllowlistResult(
        passed=True,
        violations=[],
        rejected_settings=[],
    )


async def check_restart_permission(
    host_id: UUID,
    settings: List[str],
    pool=None,
) -> bool:
    """
    Check if the host has restart_required_enabled for restart-required settings.

    Args:
        host_id: UUID of the target host.
        settings: List of setting names to check (these are known to be restart-required).
        pool: Optional asyncpg connection pool. If None, uses get_pool().

    Returns:
        True if the host has restart_required_enabled = True (and thus allows
        restart-required settings), False otherwise.
    """
    if not settings:
        return True

    if pool is None:
        pool = get_pool()
        if pool is None:
            raise RuntimeError("Database connection pool is not initialized.")

    async with pool.acquire() as conn:
        host_row = await conn.fetchrow(
            "SELECT restart_required_enabled FROM hosts WHERE id = $1",
            host_id,
        )

    if host_row is None:
        return False

    return bool(host_row["restart_required_enabled"])


def calculate_risk_score(
    proposed_changes: List[dict],
    host_role: str,
    current_settings: dict,
    risk_threshold: int = 70,
) -> RiskScore:
    """
    Calculate a risk score for proposed PostgreSQL setting changes.

    The score is based on:
    - Number of affected settings (more settings = more risk)
    - Percentage deviation of proposed values from current values
    - Host role weight (primary=1.5, replica=1.0)

    The formula for each setting's contribution:
        setting_risk = deviation_pct * host_role_multiplier * base_weight

    Where deviation_pct = |proposed - current| / max(|current|, 1) * 100

    The total score is clamped to [0, 100].

    If the score exceeds the risk_threshold, the result is marked as blocked.

    Args:
        proposed_changes: List of dicts, each with "setting_name" and "proposed_value".
                         May also include "current_value" to override lookup.
        host_role: The role of the target host ("primary" or "replica").
        current_settings: Dict mapping setting names to their current numeric values.
        risk_threshold: Score above which execution is blocked (default: 70).

    Returns:
        RiskScore with score, breakdown, host_role_multiplier, blocked status, and block_reason.

    Requirements: 9.1, 9.2
    """
    # Determine host role multiplier
    host_role_multiplier = 1.5 if host_role == "primary" else 1.0

    breakdown: List[dict] = []
    total_risk = 0.0

    for change in proposed_changes:
        setting_name = change.get("setting_name", "unknown")
        proposed_value = change.get("proposed_value", 0)

        # Get current value from the change dict or from current_settings
        if "current_value" in change:
            current_value = change["current_value"]
        else:
            current_value = current_settings.get(setting_name, 0)

        # Convert to float for calculation
        try:
            proposed_num = float(proposed_value)
        except (TypeError, ValueError):
            proposed_num = 0.0

        try:
            current_num = float(current_value)
        except (TypeError, ValueError):
            current_num = 0.0

        # Calculate deviation percentage: |proposed - current| / max(|current|, 1) * 100
        denominator = max(abs(current_num), 1.0)
        deviation_pct = abs(proposed_num - current_num) / denominator * 100

        # Base weight per setting (default 1.0)
        base_weight = 1.0

        # Calculate per-setting risk contribution
        setting_risk = deviation_pct * host_role_multiplier * base_weight

        breakdown.append(
            {
                "setting_name": setting_name,
                "current_value": current_value,
                "proposed_value": proposed_value,
                "deviation_pct": round(deviation_pct, 2),
                "host_role_multiplier": host_role_multiplier,
                "base_weight": base_weight,
                "risk_contribution": round(setting_risk, 2),
            }
        )

        total_risk += setting_risk

    # Clamp score to [0, 100]
    score = int(min(100, max(0, round(total_risk))))

    # Determine if blocked
    blocked = score > risk_threshold
    block_reason = None
    if blocked:
        block_reason = (
            f"Risk score {score} exceeds threshold {risk_threshold}. "
            f"Plan affects {len(proposed_changes)} setting(s) on a {host_role} host."
        )

    return RiskScore(
        score=score,
        breakdown=breakdown,
        host_role_multiplier=host_role_multiplier,
        blocked=blocked,
        block_reason=block_reason,
    )


async def check_risk_score(
    proposed_changes: List[dict],
    host_id: UUID,
    risk_threshold: int = 70,
    pool=None,
    audit_logger: Optional[AuditLogger] = None,
) -> RiskScore:
    """
    Fetch current settings and host role, compute risk score, and audit if blocked.

    This is the high-level entry point that:
    1. Fetches the host role from the database
    2. Fetches current settings for the proposed changes from pg_settings evidence
    3. Calls calculate_risk_score()
    4. If blocked, records the decision in the Audit_Log

    Args:
        proposed_changes: List of dicts with "setting_name" and "proposed_value".
        host_id: UUID of the target host.
        risk_threshold: Score above which execution is blocked (default: 70).
        pool: Optional asyncpg connection pool. If None, uses get_pool().
        audit_logger: Optional AuditLogger instance. If None, uses get_audit_logger().

    Returns:
        RiskScore with score, breakdown, blocked status, and block reason.

    Requirements: 9.1, 9.2
    """
    if pool is None:
        pool = get_pool()
        if pool is None:
            raise RuntimeError("Database connection pool is not initialized.")

    if audit_logger is None:
        audit_logger = get_audit_logger()

    async with pool.acquire() as conn:
        # Fetch host role
        host_row = await conn.fetchrow(
            "SELECT server_role FROM hosts WHERE id = $1",
            host_id,
        )

        host_role = "replica"  # Default to replica (lower risk multiplier)
        if host_row is not None and host_row["server_role"]:
            host_role = host_row["server_role"]

        # Fetch current settings from the most recent pg_settings evidence snapshot
        # for this host
        settings_row = await conn.fetchrow(
            """
            SELECT data FROM evidence_snapshots
            WHERE host_id = $1 AND evidence_type = 'pg_settings'
            ORDER BY collected_at DESC
            LIMIT 1
            """,
            host_id,
        )

        current_settings: dict = {}
        if settings_row is not None and settings_row["data"]:
            data = settings_row["data"]
            # The data may be a dict of setting_name -> value
            if isinstance(data, dict):
                current_settings = data
            elif isinstance(data, list):
                # If it's a list of {name, setting} dicts
                for item in data:
                    if isinstance(item, dict) and "name" in item and "setting" in item:
                        try:
                            current_settings[item["name"]] = float(item["setting"])
                        except (TypeError, ValueError):
                            current_settings[item["name"]] = 0

    # Calculate the risk score
    risk_result = calculate_risk_score(
        proposed_changes=proposed_changes,
        host_role=host_role,
        current_settings=current_settings,
        risk_threshold=risk_threshold,
    )

    # If blocked, record in Audit_Log
    if risk_result.blocked:
        await audit_logger.log(
            actor_type="system",
            actor_name="guardrail_engine",
            action_type="risk_score_block",
            target_host_id=host_id,
            result="blocked",
            result_reason=risk_result.block_reason,
            details={
                "risk_score": risk_result.score,
                "risk_threshold": risk_threshold,
                "host_role": host_role,
                "host_role_multiplier": risk_result.host_role_multiplier,
                "proposed_changes_count": len(proposed_changes),
                "breakdown": risk_result.breakdown,
            },
        )

    return risk_result


@dataclass
class DryRunResult:
    """Result of a dry-run execution.

    Attributes:
        passed: Whether the dry-run passed without errors.
        errors: List of error descriptions encountered.
        execution_time_seconds: Time taken for the dry-run in seconds.
    """

    passed: bool
    errors: List[str] = field(default_factory=list)
    execution_time_seconds: float = 0.0


@dataclass
class RollbackValidation:
    """Result of rollback plan validation.

    Attributes:
        valid: Whether the rollback plan is valid.
        errors: List of validation error descriptions.
    """

    valid: bool
    errors: List[str] = field(default_factory=list)


@dataclass
class SafetyCheckResult:
    """Result of the full safety check workflow.

    Attributes:
        passed: Whether all safety stages passed.
        stage_results: Dict with keys: allowlist, risk_score, approval, dry_run.
        blocked_at_stage: The stage name where execution was blocked, if any.
        errors: List of error descriptions.
    """

    passed: bool
    stage_results: Dict[str, dict] = field(default_factory=dict)
    blocked_at_stage: Optional[str] = None
    errors: List[str] = field(default_factory=list)


# Simple SQL validation patterns for dry-run
_SQL_SET_PATTERN = re.compile(
    r"^\s*(ALTER\s+SYSTEM\s+SET|SET)\s+[\w.]+\s*(=|TO)\s*.+",
    re.IGNORECASE,
)

_SQL_SHOW_PATTERN = re.compile(
    r"^\s*SHOW\s+[\w.]+",
    re.IGNORECASE,
)

_SQL_SELECT_PATTERN = re.compile(
    r"^\s*SELECT\s+",
    re.IGNORECASE,
)


def _validate_sql_statement(sql: str) -> bool:
    """
    Basic validation that a SQL statement is syntactically plausible.

    Checks for common PostgreSQL configuration change patterns:
    - ALTER SYSTEM SET ... = ...
    - SET ... = ...
    - SET ... TO ...
    - SHOW ...
    - SELECT ...

    Args:
        sql: The SQL statement to validate.

    Returns:
        True if the SQL appears to be a valid statement pattern.
    """
    if not sql or not sql.strip():
        return False

    sql_stripped = sql.strip().rstrip(";")

    return bool(
        _SQL_SET_PATTERN.match(sql_stripped)
        or _SQL_SHOW_PATTERN.match(sql_stripped)
        or _SQL_SELECT_PATTERN.match(sql_stripped)
    )


async def execute_dry_run(
    proposed_changes: List[dict],
    host_id: UUID,
    timeout: int = 30,
    pool=None,
    audit_logger: Optional[AuditLogger] = None,
) -> DryRunResult:
    """
    Execute a dry-run of proposed changes on the target host.

    Verifies that:
    - Each proposed setting_name exists in the host's pg_settings
    - Any proposed SQL statements parse correctly (basic validation)
    - The operation completes within the configured timeout

    Records the result in the Audit_Log.

    Args:
        proposed_changes: List of dicts with keys: setting_name, proposed_value,
                         and optionally sql_statement.
        host_id: UUID of the target host.
        timeout: Maximum allowed seconds for the dry-run (default: 30).
        pool: Optional asyncpg connection pool. If None, uses get_pool().
        audit_logger: Optional AuditLogger instance. If None, uses get_audit_logger().

    Returns:
        DryRunResult with pass/fail status, errors, and execution time.

    Requirements: 9.3, 9.6
    """
    if pool is None:
        pool = get_pool()
        if pool is None:
            raise RuntimeError("Database connection pool is not initialized.")

    if audit_logger is None:
        audit_logger = get_audit_logger()

    start_time = time.monotonic()
    errors: List[str] = []

    try:
        # Wrap the dry-run logic in a timeout
        async def _run_checks():
            nonlocal errors

            async with pool.acquire() as conn:
                # Fetch the host's pg_settings (settings that exist for the host)
                settings_rows = await conn.fetch(
                    """
                    SELECT data
                    FROM evidence_snapshots
                    WHERE host_id = $1 AND evidence_type = 'pg_settings'
                    ORDER BY collected_at DESC
                    LIMIT 1
                    """,
                    host_id,
                )

                # Extract known setting names from the latest pg_settings snapshot
                known_settings = set()
                if settings_rows:
                    snapshot_data = settings_rows[0]["data"]
                    if isinstance(snapshot_data, dict):
                        # Settings data stored as dict with setting names as keys
                        known_settings = set(snapshot_data.keys())
                    elif isinstance(snapshot_data, list):
                        # Settings data stored as list of dicts with "name" key
                        for item in snapshot_data:
                            if isinstance(item, dict) and "name" in item:
                                known_settings.add(item["name"])

                # If no pg_settings snapshot exists, check the guardrail_allowlist
                # as a fallback source of valid setting names
                if not known_settings:
                    allowlist_rows = await conn.fetch(
                        """
                        SELECT setting_name
                        FROM guardrail_allowlist
                        WHERE host_id = $1
                        """,
                        host_id,
                    )
                    known_settings = {row["setting_name"] for row in allowlist_rows}

            # Validate each proposed change
            for change in proposed_changes:
                setting_name = change.get("setting_name", "")

                # Check that the setting exists
                if known_settings and setting_name not in known_settings:
                    errors.append(f"Setting '{setting_name}' does not exist in host's pg_settings")

                # Validate SQL statement if provided
                sql = change.get("sql_statement", "")
                if sql and not _validate_sql_statement(sql):
                    errors.append(f"SQL statement for '{setting_name}' failed to parse: {sql}")

        await asyncio.wait_for(_run_checks(), timeout=timeout)

    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start_time
        errors.append(f"Dry-run timed out after {timeout} seconds")
        result = DryRunResult(
            passed=False,
            errors=errors,
            execution_time_seconds=elapsed,
        )

        await audit_logger.log(
            actor_type="system",
            actor_name="guardrail_engine",
            action_type="dry_run",
            target_host_id=host_id,
            result="failure",
            result_reason=f"Dry-run timed out after {timeout} seconds",
            details={
                "host_id": str(host_id),
                "errors": errors,
                "execution_time_seconds": elapsed,
            },
        )
        return result

    except Exception as e:
        elapsed = time.monotonic() - start_time
        errors.append(f"Dry-run error: {str(e)}")
        result = DryRunResult(
            passed=False,
            errors=errors,
            execution_time_seconds=elapsed,
        )

        await audit_logger.log(
            actor_type="system",
            actor_name="guardrail_engine",
            action_type="dry_run",
            target_host_id=host_id,
            result="failure",
            result_reason=str(e),
            details={
                "host_id": str(host_id),
                "errors": errors,
                "execution_time_seconds": elapsed,
            },
        )
        return result

    elapsed = time.monotonic() - start_time
    passed = len(errors) == 0
    result_status = "success" if passed else "failure"

    result = DryRunResult(
        passed=passed,
        errors=errors,
        execution_time_seconds=elapsed,
    )

    await audit_logger.log(
        actor_type="system",
        actor_name="guardrail_engine",
        action_type="dry_run",
        target_host_id=host_id,
        result=result_status,
        result_reason="; ".join(errors) if errors else "Dry-run passed",
        details={
            "host_id": str(host_id),
            "passed": passed,
            "errors": errors,
            "execution_time_seconds": elapsed,
            "settings_checked": [c.get("setting_name", "") for c in proposed_changes],
        },
    )

    return result


def validate_rollback_plan(
    proposed_changes: List[dict],
    rollback_instructions: List[dict],
    pre_snapshot: dict,
) -> RollbackValidation:
    """
    Validate that a rollback plan is complete and correct.

    Validation rules:
    - Every setting in proposed_changes must have a corresponding entry in rollback_instructions
    - Each rollback instruction's restore_value must match the value in pre_snapshot

    Args:
        proposed_changes: List of dicts with at minimum a "setting_name" key.
        rollback_instructions: List of dicts with "setting_name" and "restore_value" keys.
        pre_snapshot: Dict mapping setting names to their current (pre-change) values.

    Returns:
        RollbackValidation indicating whether the rollback plan is valid.

    Requirements: 9.4
    """
    errors: List[str] = []

    # Build a lookup of rollback instructions by setting name
    rollback_map: Dict[str, dict] = {}
    for instruction in rollback_instructions:
        setting_name = instruction.get("setting_name", "")
        if setting_name:
            rollback_map[setting_name] = instruction

    # Check that every proposed change has a rollback entry
    for change in proposed_changes:
        setting_name = change.get("setting_name", "")

        if setting_name not in rollback_map:
            errors.append(f"Missing rollback instruction for setting '{setting_name}'")
            continue

        # Verify restore_value matches pre-snapshot value
        instruction = rollback_map[setting_name]
        restore_value = instruction.get("restore_value")
        pre_value = pre_snapshot.get(setting_name)

        if pre_value is None:
            errors.append(f"Setting '{setting_name}' not found in pre-change snapshot")
        elif str(restore_value) != str(pre_value):
            errors.append(
                f"Rollback restore value for '{setting_name}' "
                f"('{restore_value}') does not match pre-snapshot value ('{pre_value}')"
            )

    valid = len(errors) == 0
    return RollbackValidation(valid=valid, errors=errors)


async def full_safety_check(
    proposed_changes: List[dict],
    host_id: UUID,
    rollback_instructions: List[dict],
    pre_snapshot: dict,
    risk_threshold: int = 70,
    pool=None,
    audit_logger: Optional[AuditLogger] = None,
) -> SafetyCheckResult:
    """
    Execute the full safety check workflow in strict order.

    Workflow stages:
    1. Allowlist check + Risk score assessment (parallel/sequential)
    2. Rollback plan validation (approval gate equivalent for automated checks)
    3. Dry-run execution

    If any stage fails, execution halts immediately and subsequent stages are skipped.
    Each stage result is recorded in the Audit_Log.

    Args:
        proposed_changes: List of dicts with setting changes.
        host_id: UUID of the target host.
        rollback_instructions: List of rollback instruction dicts.
        pre_snapshot: Dict mapping setting names to pre-change values.
        risk_threshold: Maximum allowed risk score (default: 70).
        pool: Optional asyncpg connection pool.
        audit_logger: Optional AuditLogger instance.

    Returns:
        SafetyCheckResult with pass/fail status, stage results, and any blocking info.

    Requirements: 9.5, 9.7
    """
    if pool is None:
        pool = get_pool()
        if pool is None:
            raise RuntimeError("Database connection pool is not initialized.")

    if audit_logger is None:
        audit_logger = get_audit_logger()

    stage_results: Dict[str, dict] = {}
    errors: List[str] = []

    # ── Stage 1: Allowlist check ──
    try:
        allowlist_result = await check_allowlist(
            proposed_changes=proposed_changes,
            host_id=host_id,
            pool=pool,
            audit_logger=audit_logger,
        )
        stage_results["allowlist"] = {
            "passed": allowlist_result.passed,
            "violations": allowlist_result.violations,
            "rejected_settings": allowlist_result.rejected_settings,
        }

        if not allowlist_result.passed:
            errors.extend(allowlist_result.violations)
            await audit_logger.log(
                actor_type="system",
                actor_name="guardrail_engine",
                action_type="safety_check_blocked",
                target_host_id=host_id,
                result="blocked",
                result_reason="Allowlist check failed",
                details={"stage": "allowlist", "errors": errors},
            )
            return SafetyCheckResult(
                passed=False,
                stage_results=stage_results,
                blocked_at_stage="allowlist",
                errors=errors,
            )
    except Exception as e:
        errors.append(f"Allowlist check error: {str(e)}")
        stage_results["allowlist"] = {"passed": False, "error": str(e)}
        await audit_logger.log(
            actor_type="system",
            actor_name="guardrail_engine",
            action_type="safety_check_blocked",
            target_host_id=host_id,
            result="failure",
            result_reason=f"Allowlist check error: {str(e)}",
            details={"stage": "allowlist", "errors": errors},
        )
        return SafetyCheckResult(
            passed=False,
            stage_results=stage_results,
            blocked_at_stage="allowlist",
            errors=errors,
        )

    # ── Stage 1 continued: Risk score check ──
    # Calculate a simple risk score based on number of changes and deviation
    try:
        risk_score = _calculate_simple_risk_score(proposed_changes, pre_snapshot)
        blocked_by_risk = risk_score > risk_threshold

        stage_results["risk_score"] = {
            "passed": not blocked_by_risk,
            "score": risk_score,
            "threshold": risk_threshold,
        }

        if blocked_by_risk:
            error_msg = f"Risk score {risk_score} exceeds threshold {risk_threshold}"
            errors.append(error_msg)
            await audit_logger.log(
                actor_type="system",
                actor_name="guardrail_engine",
                action_type="safety_check_blocked",
                target_host_id=host_id,
                result="blocked",
                result_reason=error_msg,
                details={
                    "stage": "risk_score",
                    "score": risk_score,
                    "threshold": risk_threshold,
                },
            )
            return SafetyCheckResult(
                passed=False,
                stage_results=stage_results,
                blocked_at_stage="risk_score",
                errors=errors,
            )
    except Exception as e:
        errors.append(f"Risk score error: {str(e)}")
        stage_results["risk_score"] = {"passed": False, "error": str(e)}
        await audit_logger.log(
            actor_type="system",
            actor_name="guardrail_engine",
            action_type="safety_check_blocked",
            target_host_id=host_id,
            result="failure",
            result_reason=f"Risk score error: {str(e)}",
            details={"stage": "risk_score", "errors": errors},
        )
        return SafetyCheckResult(
            passed=False,
            stage_results=stage_results,
            blocked_at_stage="risk_score",
            errors=errors,
        )

    # ── Stage 2: Rollback plan validation (approval gate) ──
    try:
        rollback_result = validate_rollback_plan(
            proposed_changes=proposed_changes,
            rollback_instructions=rollback_instructions,
            pre_snapshot=pre_snapshot,
        )
        stage_results["approval"] = {
            "passed": rollback_result.valid,
            "errors": rollback_result.errors,
        }

        if not rollback_result.valid:
            errors.extend(rollback_result.errors)
            await audit_logger.log(
                actor_type="system",
                actor_name="guardrail_engine",
                action_type="safety_check_blocked",
                target_host_id=host_id,
                result="blocked",
                result_reason="Rollback plan validation failed",
                details={
                    "stage": "approval",
                    "errors": rollback_result.errors,
                },
            )
            return SafetyCheckResult(
                passed=False,
                stage_results=stage_results,
                blocked_at_stage="approval",
                errors=errors,
            )
    except Exception as e:
        errors.append(f"Rollback validation error: {str(e)}")
        stage_results["approval"] = {"passed": False, "error": str(e)}
        await audit_logger.log(
            actor_type="system",
            actor_name="guardrail_engine",
            action_type="safety_check_blocked",
            target_host_id=host_id,
            result="failure",
            result_reason=f"Rollback validation error: {str(e)}",
            details={"stage": "approval", "errors": errors},
        )
        return SafetyCheckResult(
            passed=False,
            stage_results=stage_results,
            blocked_at_stage="approval",
            errors=errors,
        )

    # ── Stage 3: Dry-run execution ──
    try:
        dry_run_result = await execute_dry_run(
            proposed_changes=proposed_changes,
            host_id=host_id,
            pool=pool,
            audit_logger=audit_logger,
        )
        stage_results["dry_run"] = {
            "passed": dry_run_result.passed,
            "errors": dry_run_result.errors,
            "execution_time_seconds": dry_run_result.execution_time_seconds,
        }

        if not dry_run_result.passed:
            errors.extend(dry_run_result.errors)
            # Audit log already recorded by execute_dry_run
            return SafetyCheckResult(
                passed=False,
                stage_results=stage_results,
                blocked_at_stage="dry_run",
                errors=errors,
            )
    except Exception as e:
        errors.append(f"Dry-run error: {str(e)}")
        stage_results["dry_run"] = {"passed": False, "error": str(e)}
        await audit_logger.log(
            actor_type="system",
            actor_name="guardrail_engine",
            action_type="safety_check_blocked",
            target_host_id=host_id,
            result="failure",
            result_reason=f"Dry-run error: {str(e)}",
            details={"stage": "dry_run", "errors": errors},
        )
        return SafetyCheckResult(
            passed=False,
            stage_results=stage_results,
            blocked_at_stage="dry_run",
            errors=errors,
        )

    # ── All stages passed ──
    await audit_logger.log(
        actor_type="system",
        actor_name="guardrail_engine",
        action_type="safety_check_passed",
        target_host_id=host_id,
        result="success",
        result_reason="All safety check stages passed",
        details={
            "stage_results": {
                k: {"passed": v.get("passed", False)} for k, v in stage_results.items()
            },
        },
    )

    return SafetyCheckResult(
        passed=True,
        stage_results=stage_results,
        blocked_at_stage=None,
        errors=[],
    )


def _calculate_simple_risk_score(
    proposed_changes: List[dict],
    pre_snapshot: dict,
) -> int:
    """
    Calculate a simple risk score based on the number of changes and deviation.

    Score components:
    - Base score: 10 points per setting change
    - Deviation bonus: additional points based on percentage deviation from current value

    The result is clamped to [0, 100].

    Args:
        proposed_changes: List of proposed change dicts with setting_name and proposed_value.
        pre_snapshot: Dict of current setting values.

    Returns:
        Integer risk score in range [0, 100].
    """
    if not proposed_changes:
        return 0

    total_score = 0.0
    base_per_setting = 10.0

    for change in proposed_changes:
        total_score += base_per_setting

        setting_name = change.get("setting_name", "")
        proposed_value = change.get("proposed_value")
        current_value = pre_snapshot.get(setting_name)

        # Calculate deviation if both values are numeric
        if proposed_value is not None and current_value is not None:
            try:
                proposed_num = float(proposed_value)
                current_num = float(current_value)
                if current_num != 0:
                    deviation_pct = abs(proposed_num - current_num) / abs(current_num) * 100
                    # Add deviation-based score (capped at 20 per setting)
                    total_score += min(deviation_pct * 0.2, 20.0)
            except (ValueError, TypeError):
                # Non-numeric values get a small fixed bonus for uncertainty
                total_score += 5.0

    return min(100, max(0, int(total_score)))
