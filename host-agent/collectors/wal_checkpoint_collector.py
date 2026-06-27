"""
Collector for WAL and checkpoint metrics.

Queries pg_stat_bgwriter (or pg_stat_checkpointer in PG 17+) for checkpoint
frequency, WAL generation rate, and last checkpoint age.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Works on PostgreSQL < 17 (pg_stat_bgwriter has checkpoint columns)
PG_STAT_BGWRITER_QUERY = """
SELECT
    checkpoints_timed,
    checkpoints_req,
    checkpoint_write_time,
    checkpoint_sync_time,
    buffers_checkpoint,
    buffers_clean,
    buffers_backend,
    maxwritten_clean,
    buffers_alloc
FROM pg_stat_bgwriter;
"""

# WAL metrics
PG_WAL_QUERY = """
SELECT
    pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0') AS wal_bytes_total,
    (SELECT setting::bigint FROM pg_settings WHERE name = 'wal_segment_size') AS wal_segment_size;
"""

# Last checkpoint age
PG_CHECKPOINT_AGE_QUERY = """
SELECT
    EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time())) AS uptime_seconds,
    EXTRACT(EPOCH FROM (now() - stats_reset)) AS stats_age_seconds
FROM pg_stat_bgwriter;
"""


async def collect_wal_checkpoint(
    conn,
    host_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Collect WAL/checkpoint metrics.

    Captures checkpoint frequency, WAL generation rate, and last checkpoint age
    from pg_stat_bgwriter.

    Args:
        conn: An asyncpg connection or connection pool.
        host_id: The unique identifier of this host.

    Returns:
        A dict with evidence data including WAL and checkpoint metrics.
        Returns None on failure.
    """
    try:
        # Checkpoint stats
        bgwriter_row = await conn.fetchrow(PG_STAT_BGWRITER_QUERY)
        checkpoint_data = {}
        if bgwriter_row:
            checkpoint_data = {
                "checkpoints_timed": bgwriter_row["checkpoints_timed"],
                "checkpoints_req": bgwriter_row["checkpoints_req"],
                "checkpoint_write_time": float(bgwriter_row["checkpoint_write_time"]),
                "checkpoint_sync_time": float(bgwriter_row["checkpoint_sync_time"]),
                "buffers_checkpoint": bgwriter_row["buffers_checkpoint"],
                "buffers_clean": bgwriter_row["buffers_clean"],
                "buffers_backend": bgwriter_row["buffers_backend"],
                "maxwritten_clean": bgwriter_row["maxwritten_clean"],
                "buffers_alloc": bgwriter_row["buffers_alloc"],
            }

        # WAL metrics (only on non-replica)
        wal_data = {}
        try:
            wal_row = await conn.fetchrow(PG_WAL_QUERY)
            if wal_row:
                wal_data = {
                    "wal_bytes_total": wal_row["wal_bytes_total"],
                    "wal_segment_size": wal_row["wal_segment_size"],
                }
        except Exception as e:
            # May fail on replicas
            logger.warning(f"Could not query WAL metrics: {e}")

        # Stats age for computing checkpoint frequency
        stats_age_data = {}
        try:
            age_row = await conn.fetchrow(PG_CHECKPOINT_AGE_QUERY)
            if age_row:
                stats_age_data = {
                    "uptime_seconds": float(age_row["uptime_seconds"]),
                    "stats_age_seconds": (
                        float(age_row["stats_age_seconds"])
                        if age_row["stats_age_seconds"]
                        else None
                    ),
                }
        except Exception as e:
            logger.warning(f"Could not query checkpoint age: {e}")

        return {
            "host_id": host_id,
            "evidence_type": "wal_checkpoint",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "data": {
                "checkpoint": checkpoint_data,
                "wal": wal_data,
                "stats_age": stats_age_data,
            },
        }
    except Exception as e:
        logger.error(f"Failed to collect WAL/checkpoint metrics: {e}")
        return None
