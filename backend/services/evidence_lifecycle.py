"""Retention, rollup, and cleanup for raw evidence snapshots.

Raw evidence is intentionally short-lived compared with the compact aggregate
history.  Cleanup is fail-closed: it only considers evidence for terminal runs,
extends the raw window for snapshots referenced by durable product records, and
writes a rollup in the same transaction that deletes each batch.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Mapping, Optional
from uuid import UUID

from backend.config import settings

logger = logging.getLogger(__name__)

UNASSIGNED_RUN_KEY = UUID("00000000-0000-0000-0000-000000000000")
TERMINAL_RUN_STATUSES = ("completed", "failed", "manually_halted", "timed_out")


@dataclass(frozen=True)
class EvidenceRetentionPolicy:
    """Validated evidence-retention settings used by API and worker paths."""

    raw_retention_days: int = 30
    referenced_retention_days: int = 90
    rollup_retention_days: int = 365
    batch_size: int = 1000
    max_batches_per_run: int = 20

    def __post_init__(self) -> None:
        if self.raw_retention_days < 1:
            raise ValueError("raw_retention_days must be at least 1")
        if self.referenced_retention_days < self.raw_retention_days:
            raise ValueError(
                "referenced_retention_days must be greater than or equal to raw_retention_days"
            )
        if self.rollup_retention_days < self.referenced_retention_days:
            raise ValueError(
                "rollup_retention_days must be greater than or equal to referenced_retention_days"
            )
        if not 1 <= self.batch_size <= 10_000:
            raise ValueError("batch_size must be between 1 and 10000")
        if not 1 <= self.max_batches_per_run <= 1000:
            raise ValueError("max_batches_per_run must be between 1 and 1000")

    @classmethod
    def from_settings(cls) -> "EvidenceRetentionPolicy":
        return cls(
            raw_retention_days=settings.evidence_raw_retention_days,
            referenced_retention_days=settings.evidence_referenced_retention_days,
            rollup_retention_days=settings.evidence_rollup_retention_days,
            batch_size=settings.evidence_cleanup_batch_size,
            max_batches_per_run=settings.evidence_cleanup_max_batches,
        )

    def cutoffs(self, now: Optional[datetime] = None) -> tuple[datetime, datetime, datetime]:
        current = now or datetime.now(timezone.utc)
        return (
            current - timedelta(days=self.raw_retention_days),
            current - timedelta(days=self.referenced_retention_days),
            current - timedelta(days=self.rollup_retention_days),
        )


REFERENCE_PROTECTION_SQL = """
(
    EXISTS (
        SELECT 1 FROM plans p
        WHERE p.organization_id = $1
          AND p.evidence_references @>
              jsonb_build_array(jsonb_build_object('snapshot_id', e.id::text))
    )
    OR EXISTS (
        SELECT 1 FROM baseline_measurements b
        WHERE b.organization_id = $1
          AND b.evidence_references @>
              jsonb_build_array(jsonb_build_object('snapshot_id', e.id::text))
    )
    OR EXISTS (
        SELECT 1 FROM advisory_findings a
        WHERE a.organization_id = $1
          AND a.evidence_references @>
              jsonb_build_array(jsonb_build_object('snapshot_id', e.id::text))
    )
    OR EXISTS (
        SELECT 1 FROM tuning_candidates c
        WHERE c.organization_id = $1
          AND c.evidence_references @>
              jsonb_build_array(jsonb_build_object('snapshot_id', e.id::text))
    )
    OR EXISTS (
        SELECT 1 FROM workload_fingerprints w
        WHERE w.organization_id = $1 AND w.source_snapshot_id = e.id
    )
)
"""


ELIGIBLE_EVIDENCE_SQL = f"""
SELECT
    e.id, e.run_id, e.host_id, e.evidence_type, e.collected_at,
    e.quality_score,
    COALESCE(e.data_size_bytes, octet_length(e.data::text))::bigint AS data_size_bytes
FROM evidence_snapshots e
JOIN hosts h ON h.id = e.host_id
LEFT JOIN loop_runs r ON r.id = e.run_id
WHERE h.organization_id = $1
  AND e.collected_at < $2
  AND (e.run_id IS NULL OR r.status = ANY($4::varchar[]))
  AND (e.collected_at < $3 OR NOT {REFERENCE_PROTECTION_SQL})
ORDER BY e.collected_at, e.id
FOR UPDATE OF e SKIP LOCKED
LIMIT $5
"""


ELIGIBLE_EVIDENCE_COUNT_SQL = f"""
SELECT
    COUNT(*)::bigint AS snapshot_count,
    COALESCE(SUM(COALESCE(e.data_size_bytes, octet_length(e.data::text))), 0)::bigint
        AS total_bytes
FROM evidence_snapshots e
JOIN hosts h ON h.id = e.host_id
LEFT JOIN loop_runs r ON r.id = e.run_id
WHERE h.organization_id = $1
  AND e.collected_at < $2
  AND (e.run_id IS NULL OR r.status = ANY($4::varchar[]))
  AND (e.collected_at < $3 OR NOT {REFERENCE_PROTECTION_SQL})
"""


def _row_value(row: Mapping[str, Any] | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    value = row.get(key, default)
    return default if value is None else value


def _day_bucket(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


class EvidenceLifecycleManager:
    """Tenant-scoped evidence lifecycle operations over a pool or connection."""

    def __init__(self, database: Any, policy: Optional[EvidenceRetentionPolicy] = None):
        self.database = database
        self.policy = policy or EvidenceRetentionPolicy.from_settings()

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        acquire = getattr(self.database, "acquire", None)
        if acquire is None:
            yield self.database
            return
        async with acquire() as conn:
            yield conn

    async def list_organizations(self) -> list[UUID]:
        async with self._connection() as conn:
            rows = await conn.fetch("SELECT id FROM organizations ORDER BY id")
        return [row["id"] for row in rows]

    async def status(self, organization_id: UUID) -> dict[str, Any]:
        raw_cutoff, referenced_cutoff, rollup_cutoff = self.policy.cutoffs()
        async with self._connection() as conn:
            raw = await conn.fetchrow(
                """
                SELECT COUNT(*)::bigint AS snapshot_count,
                       COALESCE(SUM(e.data_size_bytes), 0)::bigint AS total_bytes,
                       COUNT(*) FILTER (WHERE e.data_size_bytes IS NULL)::bigint
                           AS unmeasured_snapshot_count,
                       MIN(e.collected_at) AS oldest_collected_at,
                       MAX(e.collected_at) AS newest_collected_at
                FROM evidence_snapshots e
                JOIN hosts h ON h.id = e.host_id
                WHERE h.organization_id = $1
                """,
                organization_id,
            )
            eligible = await conn.fetchrow(
                ELIGIBLE_EVIDENCE_COUNT_SQL,
                organization_id,
                raw_cutoff,
                referenced_cutoff,
                list(TERMINAL_RUN_STATUSES),
            )
            rollups = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(snapshot_count), 0)::bigint AS snapshot_count,
                       COALESCE(SUM(total_bytes), 0)::bigint AS total_bytes,
                       COUNT(*)::bigint AS rollup_rows,
                       MIN(first_collected_at) AS oldest_collected_at,
                       MAX(last_collected_at) AS newest_collected_at
                FROM evidence_rollups
                WHERE organization_id = $1
                """,
                organization_id,
            )
            last_run = await conn.fetchrow(
                """
                SELECT id, status, triggered_by, batches_completed,
                       snapshots_deleted, raw_bytes_reclaimed, rollup_rows_written,
                       expired_rollups_deleted, sizes_backfilled, error_message,
                       started_at, completed_at
                FROM evidence_maintenance_runs
                WHERE organization_id = $1
                ORDER BY started_at DESC
                LIMIT 1
                """,
                organization_id,
            )

        return {
            "policy": {
                "raw_retention_days": self.policy.raw_retention_days,
                "referenced_retention_days": self.policy.referenced_retention_days,
                "rollup_retention_days": self.policy.rollup_retention_days,
                "batch_size": self.policy.batch_size,
                "max_batches_per_run": self.policy.max_batches_per_run,
                "cleanup_enabled": settings.evidence_cleanup_enabled,
                "cleanup_interval_seconds": settings.evidence_cleanup_interval_seconds,
            },
            "cutoffs": {
                "raw": raw_cutoff,
                "referenced": referenced_cutoff,
                "rollup": rollup_cutoff,
            },
            "raw": {
                "snapshot_count": int(_row_value(raw, "snapshot_count", 0)),
                "total_bytes": int(_row_value(raw, "total_bytes", 0)),
                "unmeasured_snapshot_count": int(
                    _row_value(raw, "unmeasured_snapshot_count", 0)
                ),
                "oldest_collected_at": _row_value(raw, "oldest_collected_at"),
                "newest_collected_at": _row_value(raw, "newest_collected_at"),
            },
            "eligible": {
                "snapshot_count": int(_row_value(eligible, "snapshot_count", 0)),
                "total_bytes": int(_row_value(eligible, "total_bytes", 0)),
            },
            "rollups": {
                "snapshot_count": int(_row_value(rollups, "snapshot_count", 0)),
                "total_bytes": int(_row_value(rollups, "total_bytes", 0)),
                "rollup_rows": int(_row_value(rollups, "rollup_rows", 0)),
                "oldest_collected_at": _row_value(rollups, "oldest_collected_at"),
                "newest_collected_at": _row_value(rollups, "newest_collected_at"),
            },
            "last_run": dict(last_run) if last_run is not None else None,
        }

    async def run_cleanup(
        self,
        organization_id: UUID,
        *,
        triggered_by: str,
        max_batches: Optional[int] = None,
    ) -> dict[str, Any]:
        """Roll up and delete eligible raw evidence in bounded transactions."""
        raw_cutoff, referenced_cutoff, rollup_cutoff = self.policy.cutoffs()
        batch_limit = (
            self.policy.max_batches_per_run if max_batches is None else max_batches
        )
        if not 1 <= batch_limit <= self.policy.max_batches_per_run:
            raise ValueError(
                f"max_batches must be between 1 and {self.policy.max_batches_per_run}"
            )

        async with self._connection() as conn:
            acquired = await conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtext("
                "'dbtune:evidence-lifecycle:' || $1::uuid::text))",
                organization_id,
            )
            if not acquired:
                return {
                    "status": "skipped",
                    "reason": "Another evidence cleanup is already running for this organization",
                    "snapshots_deleted": 0,
                    "raw_bytes_reclaimed": 0,
                    "rollup_rows_written": 0,
                    "expired_rollups_deleted": 0,
                    "batches_completed": 0,
                    "sizes_backfilled": 0,
                }

            maintenance_id: Optional[UUID] = None
            totals = {
                "snapshots_deleted": 0,
                "raw_bytes_reclaimed": 0,
                "rollup_rows_written": 0,
                "expired_rollups_deleted": 0,
                "batches_completed": 0,
                "sizes_backfilled": 0,
            }
            try:
                maintenance_id = await conn.fetchval(
                    """
                    INSERT INTO evidence_maintenance_runs (
                        organization_id, status, triggered_by, raw_cutoff,
                        referenced_cutoff, rollup_cutoff, batch_size
                    ) VALUES ($1, 'running', $2, $3, $4, $5, $6)
                    RETURNING id
                    """,
                    organization_id,
                    triggered_by[:100],
                    raw_cutoff,
                    referenced_cutoff,
                    rollup_cutoff,
                    self.policy.batch_size,
                )

                # Existing deployments predate the persisted size column.
                # Backfill one bounded batch per maintenance run; inserts and
                # data updates are measured by the database trigger.
                backfilled = await conn.fetch(
                    """
                    WITH candidates AS (
                        SELECT e.id
                        FROM evidence_snapshots e
                        JOIN hosts h ON h.id = e.host_id
                        WHERE h.organization_id = $1 AND e.data_size_bytes IS NULL
                        ORDER BY e.collected_at DESC, e.id DESC
                        FOR UPDATE OF e SKIP LOCKED
                        LIMIT $2
                    )
                    UPDATE evidence_snapshots e
                    SET data_size_bytes = octet_length(e.data::text)
                    FROM candidates c
                    WHERE e.id = c.id
                    RETURNING e.id
                    """,
                    organization_id,
                    self.policy.batch_size,
                )
                totals["sizes_backfilled"] = len(backfilled)
                await conn.execute(
                    "UPDATE evidence_maintenance_runs SET sizes_backfilled = $2 WHERE id = $1",
                    maintenance_id,
                    totals["sizes_backfilled"],
                )

                for _ in range(batch_limit):
                    batch = await self._cleanup_batch(
                        conn, organization_id, raw_cutoff, referenced_cutoff
                    )
                    if batch["snapshots_deleted"] == 0:
                        break
                    for key in (
                        "snapshots_deleted",
                        "raw_bytes_reclaimed",
                        "rollup_rows_written",
                    ):
                        totals[key] += batch[key]
                    totals["batches_completed"] += 1
                    await conn.execute(
                        """
                        UPDATE evidence_maintenance_runs
                        SET batches_completed = $2, snapshots_deleted = $3,
                            raw_bytes_reclaimed = $4, rollup_rows_written = $5
                        WHERE id = $1
                        """,
                        maintenance_id,
                        totals["batches_completed"],
                        totals["snapshots_deleted"],
                        totals["raw_bytes_reclaimed"],
                        totals["rollup_rows_written"],
                    )

                expired = await conn.fetch(
                    """
                    DELETE FROM evidence_rollups
                    WHERE organization_id = $1 AND bucket_end < $2
                    RETURNING id
                    """,
                    organization_id,
                    rollup_cutoff,
                )
                totals["expired_rollups_deleted"] = len(expired)
                await conn.execute(
                    """
                    UPDATE evidence_maintenance_runs
                    SET status = 'completed', completed_at = NOW(),
                        expired_rollups_deleted = $2
                    WHERE id = $1
                    """,
                    maintenance_id,
                    totals["expired_rollups_deleted"],
                )
                await self._record_event(
                    conn,
                    organization_id,
                    "EVIDENCE_RETENTION_COMPLETED",
                    "Evidence lifecycle maintenance completed",
                    {"maintenance_id": str(maintenance_id), **totals},
                )
                return {
                    "id": maintenance_id,
                    "status": "completed",
                    "raw_cutoff": raw_cutoff,
                    "referenced_cutoff": referenced_cutoff,
                    "rollup_cutoff": rollup_cutoff,
                    **totals,
                }
            except Exception as exc:
                logger.exception(
                    "Evidence lifecycle maintenance failed for organization %s",
                    organization_id,
                )
                if maintenance_id is not None:
                    await conn.execute(
                        """
                        UPDATE evidence_maintenance_runs
                        SET status = 'failed', error_message = $2, completed_at = NOW(),
                            batches_completed = $3, snapshots_deleted = $4,
                            raw_bytes_reclaimed = $5, rollup_rows_written = $6,
                            sizes_backfilled = $7
                        WHERE id = $1
                        """,
                        maintenance_id,
                        str(exc)[:4000],
                        totals["batches_completed"],
                        totals["snapshots_deleted"],
                        totals["raw_bytes_reclaimed"],
                        totals["rollup_rows_written"],
                        totals["sizes_backfilled"],
                    )
                    await self._record_event(
                        conn,
                        organization_id,
                        "EVIDENCE_RETENTION_FAILED",
                        "Evidence lifecycle maintenance failed",
                        {"maintenance_id": str(maintenance_id), "error": str(exc)[:1000]},
                    )
                raise
            finally:
                await conn.fetchval(
                    "SELECT pg_advisory_unlock(hashtext("
                    "'dbtune:evidence-lifecycle:' || $1::uuid::text))",
                    organization_id,
                )

    async def _cleanup_batch(
        self,
        conn: Any,
        organization_id: UUID,
        raw_cutoff: datetime,
        referenced_cutoff: datetime,
    ) -> dict[str, int]:
        async with conn.transaction():
            rows = await conn.fetch(
                ELIGIBLE_EVIDENCE_SQL,
                organization_id,
                raw_cutoff,
                referenced_cutoff,
                list(TERMINAL_RUN_STATUSES),
                self.policy.batch_size,
            )
            if not rows:
                return {
                    "snapshots_deleted": 0,
                    "raw_bytes_reclaimed": 0,
                    "rollup_rows_written": 0,
                }

            grouped: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
            for row in rows:
                bucket = _day_bucket(row["collected_at"])
                grouped[
                    (
                        row["host_id"],
                        row["run_id"],
                        row["run_id"] or UNASSIGNED_RUN_KEY,
                        row["evidence_type"],
                        bucket,
                    )
                ].append(row)

            for (host_id, run_id, run_key, evidence_type, bucket), items in grouped.items():
                qualities = [
                    float(item["quality_score"])
                    for item in items
                    if item.get("quality_score") is not None
                ]
                first = min(item["collected_at"] for item in items)
                last = max(item["collected_at"] for item in items)
                await conn.execute(
                    """
                    INSERT INTO evidence_rollups (
                        organization_id, host_id, run_id, run_key, evidence_type,
                        bucket_start, bucket_end, snapshot_count, total_bytes,
                        quality_sample_count, min_quality_score, average_quality_score,
                        max_quality_score, first_collected_at, last_collected_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        $13, $14, $15
                    )
                    ON CONFLICT (
                        organization_id, host_id, run_key, evidence_type, bucket_start
                    ) DO UPDATE SET
                        bucket_end = GREATEST(evidence_rollups.bucket_end, EXCLUDED.bucket_end),
                        snapshot_count = evidence_rollups.snapshot_count + EXCLUDED.snapshot_count,
                        total_bytes = evidence_rollups.total_bytes + EXCLUDED.total_bytes,
                        min_quality_score = LEAST(
                            evidence_rollups.min_quality_score, EXCLUDED.min_quality_score
                        ),
                        average_quality_score = CASE
                            WHEN evidence_rollups.quality_sample_count +
                                 EXCLUDED.quality_sample_count = 0 THEN NULL
                            ELSE (
                                COALESCE(evidence_rollups.average_quality_score, 0) *
                                    evidence_rollups.quality_sample_count +
                                COALESCE(EXCLUDED.average_quality_score, 0) *
                                    EXCLUDED.quality_sample_count
                            ) / (
                                evidence_rollups.quality_sample_count +
                                EXCLUDED.quality_sample_count
                            )
                        END,
                        max_quality_score = GREATEST(
                            evidence_rollups.max_quality_score, EXCLUDED.max_quality_score
                        ),
                        quality_sample_count = evidence_rollups.quality_sample_count +
                            EXCLUDED.quality_sample_count,
                        first_collected_at = LEAST(
                            evidence_rollups.first_collected_at, EXCLUDED.first_collected_at
                        ),
                        last_collected_at = GREATEST(
                            evidence_rollups.last_collected_at, EXCLUDED.last_collected_at
                        ),
                        rolled_up_at = NOW()
                    """,
                    organization_id,
                    host_id,
                    run_id,
                    run_key,
                    evidence_type,
                    bucket,
                    bucket + timedelta(days=1),
                    len(items),
                    sum(int(item.get("data_size_bytes") or 0) for item in items),
                    len(qualities),
                    min(qualities) if qualities else None,
                    sum(qualities) / len(qualities) if qualities else None,
                    max(qualities) if qualities else None,
                    first,
                    last,
                )

            deleted = await conn.fetch(
                "DELETE FROM evidence_snapshots WHERE id = ANY($1::uuid[]) RETURNING id",
                [row["id"] for row in rows],
            )
            if len(deleted) != len(rows):
                raise RuntimeError(
                    "Evidence cleanup lost its row lock before delete; transaction rolled back"
                )
            return {
                "snapshots_deleted": len(deleted),
                "raw_bytes_reclaimed": sum(
                    int(row.get("data_size_bytes") or 0) for row in rows
                ),
                "rollup_rows_written": len(grouped),
            }

    async def _record_event(
        self,
        conn: Any,
        organization_id: UUID,
        event_code: str,
        message: str,
        details: dict[str, Any],
    ) -> None:
        await conn.execute(
            """
            INSERT INTO host_events (
                organization_id, severity, component, event_code, message, details
            )
            SELECT $1, default_severity, component, event_code, $3, $4::jsonb
            FROM event_code_catalog
            WHERE event_code = $2
            """,
            organization_id,
            event_code,
            message,
            json.dumps(details),
        )
