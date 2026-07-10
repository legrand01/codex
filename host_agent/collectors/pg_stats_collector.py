"""
Collector for pg_stat_database and pg_stat_statements query samples.

Queries pg_stat_database for database-level statistics and pg_stat_statements
for normalized query performance data.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PG_STAT_DATABASE_QUERY = """
SELECT
    datname,
    numbackends,
    xact_commit,
    xact_rollback,
    blks_read,
    blks_hit,
    tup_returned,
    tup_fetched,
    tup_inserted,
    tup_updated,
    tup_deleted,
    conflicts,
    temp_files,
    temp_bytes,
    deadlocks
FROM pg_stat_database
WHERE datname IS NOT NULL
ORDER BY datname;
"""

PG_STAT_STATEMENTS_QUERY = """
SELECT
    queryid,
    query,
    calls,
    total_exec_time,
    mean_exec_time,
    rows,
    shared_blks_hit,
    shared_blks_read
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT $1;
"""


async def collect_pg_stats(
    conn,
    host_id: str,
    max_query_entries: int = 100,
) -> Optional[Dict[str, Any]]:
    """
    Collect pg_stat_database and pg_stat_statements samples.

    Args:
        conn: An asyncpg connection or connection pool.
        host_id: The unique identifier of this host.
        max_query_entries: Maximum number of normalized query entries to capture.

    Returns:
        A dict with evidence data including database stats and statement samples.
        Returns None on failure.
    """
    try:
        # Collect database-level stats
        db_rows = await conn.fetch(PG_STAT_DATABASE_QUERY)
        database_stats = [
            {
                "datname": row["datname"],
                "numbackends": row["numbackends"],
                "xact_commit": row["xact_commit"],
                "xact_rollback": row["xact_rollback"],
                "blks_read": row["blks_read"],
                "blks_hit": row["blks_hit"],
                "tup_returned": row["tup_returned"],
                "tup_fetched": row["tup_fetched"],
                "tup_inserted": row["tup_inserted"],
                "tup_updated": row["tup_updated"],
                "tup_deleted": row["tup_deleted"],
                "conflicts": row["conflicts"],
                "temp_files": row["temp_files"],
                "temp_bytes": row["temp_bytes"],
                "deadlocks": row["deadlocks"],
            }
            for row in db_rows
        ]

        # Collect statement-level stats (may not be available)
        statement_stats = []
        try:
            stmt_rows = await conn.fetch(PG_STAT_STATEMENTS_QUERY, max_query_entries)
            statement_stats = [
                {
                    "queryid": str(row["queryid"]),
                    "query": row["query"],
                    "calls": row["calls"],
                    "total_exec_time": float(row["total_exec_time"]),
                    "mean_exec_time": float(row["mean_exec_time"]),
                    "rows": row["rows"],
                    "shared_blks_hit": row["shared_blks_hit"],
                    "shared_blks_read": row["shared_blks_read"],
                }
                for row in stmt_rows
            ]
        except Exception as e:
            # pg_stat_statements extension may not be installed
            logger.warning(f"pg_stat_statements not available: {e}")

        return {
            "host_id": host_id,
            "evidence_type": "pg_stats",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "data": {
                "database_stats": database_stats,
                "statement_stats": statement_stats,
                "statement_count": len(statement_stats),
                "max_query_entries": max_query_entries,
            },
        }
    except Exception as e:
        logger.error(f"Failed to collect pg_stats: {e}")
        return None
