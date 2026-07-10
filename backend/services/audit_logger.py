"""
Audit Logger service for append-only audit trail.

Provides:
- log() to persist structured audit entries within 5 seconds
- query() to retrieve entries in chronological order, filterable by run_id and time range

The audit_log table has database-level rules preventing UPDATE and DELETE,
ensuring the append-only integrity of the log.

Requirements: 10.1, 10.2, 10.4, 10.5
"""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from backend.db.pool import get_pool
from backend.models.audit import AuditEntry

logger = logging.getLogger(__name__)

# Valid values for constrained fields
VALID_ACTOR_TYPES = ("human", "system")
VALID_RESULTS = ("success", "failure", "blocked")


class AuditLoggerError(Exception):
    """Base exception for audit logger errors."""

    pass


class AuditValidationError(AuditLoggerError):
    """Raised when audit entry validation fails."""

    pass


class AuditLogger:
    """
    Append-only audit logger that persists structured entries to the audit_log table.

    Uses the asyncpg connection pool directly for database operations.
    Enforces entry structure validation before persisting.
    """

    def __init__(self, pool=None):
        """
        Initialize the AuditLogger.

        Args:
            pool: Optional asyncpg connection pool. If None, uses get_pool().
        """
        self._pool = pool

    @property
    def pool(self):
        """Get the connection pool, falling back to the global pool."""
        if self._pool is not None:
            return self._pool
        pool = get_pool()
        if pool is None:
            raise RuntimeError("Database connection pool is not initialized.")
        return pool

    def _validate_entry(
        self,
        actor_type: str,
        actor_name: str,
        action_type: str,
        result: str,
    ) -> None:
        """
        Validate audit entry fields before persisting.

        Args:
            actor_type: Must be "human" or "system".
            actor_name: Must be non-empty.
            action_type: Must be non-empty.
            result: Must be "success", "failure", or "blocked".

        Raises:
            AuditValidationError: If any field fails validation.
        """
        if actor_type not in VALID_ACTOR_TYPES:
            raise AuditValidationError(
                f"actor_type must be one of {VALID_ACTOR_TYPES}, got '{actor_type}'"
            )

        if not actor_name or not actor_name.strip():
            raise AuditValidationError("actor_name must be non-empty")

        if not action_type or not action_type.strip():
            raise AuditValidationError("action_type must be non-empty")

        if result not in VALID_RESULTS:
            raise AuditValidationError(f"result must be one of {VALID_RESULTS}, got '{result}'")

    async def log(
        self,
        run_id: Optional[UUID] = None,
        actor_type: str = "system",
        actor_name: str = "",
        action_type: str = "",
        target_host_id: Optional[UUID] = None,
        result: str = "success",
        result_reason: Optional[str] = None,
        details: Optional[Dict] = None,
    ) -> AuditEntry:
        """
        Persist a structured audit entry to the append-only audit log.

        The entry is inserted with an ISO 8601 timestamp (UTC) and must
        be persisted within 5 seconds of the event occurring.

        Args:
            run_id: Optional UUID of the associated loop run.
            actor_type: "human" or "system".
            actor_name: Name/identifier of the actor performing the action.
            action_type: Type of action being logged (e.g., "plan_approved").
            target_host_id: Optional UUID of the target host.
            result: Outcome - "success", "failure", or "blocked".
            result_reason: Optional reason for the result (especially for failures/blocks).
            details: Optional JSON-serializable dictionary with additional context.

        Returns:
            The persisted AuditEntry with id and timestamp assigned by the database.

        Raises:
            AuditValidationError: If entry fields fail validation.
            AuditLoggerError: If persistence fails.
        """
        self._validate_entry(actor_type, actor_name, action_type, result)

        timestamp = datetime.now(timezone.utc)

        # Serialize details to JSON string for JSONB column
        details_json = json.dumps(details) if details is not None else None

        try:
            pool = self.pool
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO audit_log (
                        run_id, timestamp, actor_type, actor_name,
                        action_type, target_host_id, result, result_reason, details
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                    RETURNING id, run_id, timestamp, actor_type, actor_name,
                              action_type, target_host_id, result, result_reason, details
                    """,
                    run_id,
                    timestamp,
                    actor_type,
                    actor_name,
                    action_type,
                    target_host_id,
                    result,
                    result_reason,
                    details_json,
                )

            # Parse the returned row into an AuditEntry
            entry = AuditEntry(
                id=row["id"],
                run_id=row["run_id"],
                timestamp=row["timestamp"],
                actor_type=row["actor_type"],
                actor_name=row["actor_name"],
                action_type=row["action_type"],
                target_host_id=row["target_host_id"],
                result=row["result"],
                result_reason=row["result_reason"],
                details=json.loads(row["details"]) if row["details"] else None,
            )

            logger.debug(
                f"Audit entry logged: id={entry.id}, action={action_type}, "
                f"actor={actor_name}, result={result}"
            )
            return entry

        except (AuditValidationError, AuditLoggerError):
            raise
        except Exception as e:
            logger.error(f"Failed to persist audit entry: {e}")
            raise AuditLoggerError(f"Failed to persist audit entry: {e}") from e

    async def query(
        self,
        run_id: Optional[UUID] = None,
        time_range: Optional[Tuple[datetime, datetime]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[AuditEntry]:
        """
        Query audit log entries in chronological order.

        Results are ordered by timestamp ascending, with stable ordering
        for identical timestamps (by insertion order / sequence ID).

        Args:
            run_id: Optional filter by loop run UUID.
            time_range: Optional tuple of (start_time, end_time) for filtering.
            limit: Maximum number of entries to return (default 100).
            offset: Number of entries to skip for pagination (default 0).

        Returns:
            List of AuditEntry objects in chronological order.

        Raises:
            AuditLoggerError: If the query fails.
        """
        try:
            pool = self.pool

            # Build query dynamically based on filters
            conditions = []
            params = []
            param_idx = 1

            if run_id is not None:
                conditions.append(f"run_id = ${param_idx}")
                params.append(run_id)
                param_idx += 1

            if time_range is not None:
                start_time, end_time = time_range
                conditions.append(f"timestamp >= ${param_idx}")
                params.append(start_time)
                param_idx += 1
                conditions.append(f"timestamp <= ${param_idx}")
                params.append(end_time)
                param_idx += 1

            where_clause = ""
            if conditions:
                where_clause = "WHERE " + " AND ".join(conditions)

            # Add pagination params
            params.append(limit)
            limit_placeholder = f"${param_idx}"
            param_idx += 1

            params.append(offset)
            offset_placeholder = f"${param_idx}"

            query = f"""
                SELECT id, run_id, timestamp, actor_type, actor_name,
                       action_type, target_host_id, result, result_reason, details
                FROM audit_log
                {where_clause}
                ORDER BY timestamp ASC, id ASC
                LIMIT {limit_placeholder} OFFSET {offset_placeholder}
            """

            async with pool.acquire() as conn:
                rows = await conn.fetch(query, *params)

            entries = []
            for row in rows:
                details = None
                if row["details"] is not None:
                    # asyncpg returns JSONB as a string or dict depending on version
                    if isinstance(row["details"], str):
                        details = json.loads(row["details"])
                    else:
                        details = row["details"]

                entries.append(
                    AuditEntry(
                        id=row["id"],
                        run_id=row["run_id"],
                        timestamp=row["timestamp"],
                        actor_type=row["actor_type"],
                        actor_name=row["actor_name"],
                        action_type=row["action_type"],
                        target_host_id=row["target_host_id"],
                        result=row["result"],
                        result_reason=row["result_reason"],
                        details=details,
                    )
                )

            return entries

        except Exception as e:
            logger.error(f"Failed to query audit log: {e}")
            raise AuditLoggerError(f"Failed to query audit log: {e}") from e


# Module-level singleton instance (uses global pool)
_audit_logger: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    """
    Get or create the module-level AuditLogger singleton.

    Returns:
        The AuditLogger instance.
    """
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger
