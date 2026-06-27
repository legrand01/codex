"""
Collector for PostgreSQL lock information.

Queries pg_locks joined with pg_stat_activity to provide context about
current lock holders and waiters.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PG_LOCKS_QUERY = """
SELECT
    l.locktype,
    l.database,
    l.relation,
    l.page,
    l.tuple,
    l.virtualxid,
    l.transactionid,
    l.mode,
    l.granted,
    l.fastpath,
    a.pid,
    a.usename,
    a.application_name,
    a.state,
    a.query,
    a.wait_event_type,
    a.wait_event,
    a.query_start
FROM pg_locks l
LEFT JOIN pg_stat_activity a ON l.pid = a.pid
WHERE l.pid != pg_backend_pid()
ORDER BY l.granted, a.query_start;
"""


async def collect_locks(
    conn,
    host_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Collect current lock information from pg_locks.

    Args:
        conn: An asyncpg connection or connection pool.
        host_id: The unique identifier of this host.

    Returns:
        A dict with evidence data including lock details.
        Returns None on failure.
    """
    try:
        rows = await conn.fetch(PG_LOCKS_QUERY)
        locks = []
        for row in rows:
            lock_entry = {
                "locktype": row["locktype"],
                "database": row["database"],
                "relation": row["relation"],
                "page": row["page"],
                "tuple": row["tuple"],
                "virtualxid": row["virtualxid"],
                "transactionid": str(row["transactionid"]) if row["transactionid"] else None,
                "mode": row["mode"],
                "granted": row["granted"],
                "fastpath": row["fastpath"],
                "pid": row["pid"],
                "usename": row["usename"],
                "application_name": row["application_name"],
                "state": row["state"],
                "query": row["query"],
                "wait_event_type": row["wait_event_type"],
                "wait_event": row["wait_event"],
                "query_start": row["query_start"].isoformat() if row["query_start"] else None,
            }
            locks.append(lock_entry)

        # Compute summary metrics
        granted_count = sum(1 for l in locks if l["granted"])
        waiting_count = sum(1 for l in locks if not l["granted"])

        return {
            "host_id": host_id,
            "evidence_type": "locks",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "data": {
                "locks": locks,
                "total_locks": len(locks),
                "granted_count": granted_count,
                "waiting_count": waiting_count,
            },
        }
    except Exception as e:
        logger.error(f"Failed to collect locks: {e}")
        return None
