"""
Collector for PostgreSQL replication lag metrics.

Queries pg_stat_replication on primary servers and pg_stat_wal_receiver
on replicas to capture replication lag information.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PG_STAT_REPLICATION_QUERY = """
SELECT
    pid,
    usesysid,
    usename,
    application_name,
    client_addr,
    client_hostname,
    client_port,
    state,
    sent_lsn,
    write_lsn,
    flush_lsn,
    replay_lsn,
    sync_state,
    reply_time
FROM pg_stat_replication;
"""

PG_REPLICATION_LAG_QUERY = """
SELECT
    CASE
        WHEN pg_is_in_recovery() THEN
            EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))
        ELSE 0
    END AS replay_lag_seconds,
    pg_is_in_recovery() AS is_replica;
"""


async def collect_replication(
    conn,
    host_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Collect replication lag metrics.

    Queries pg_stat_replication for connected standby information (on primary)
    and computes replay lag on replicas.

    Args:
        conn: An asyncpg connection or connection pool.
        host_id: The unique identifier of this host.

    Returns:
        A dict with evidence data including replication details.
        Returns None on failure.
    """
    try:
        # Get replication lag info
        lag_row = await conn.fetchrow(PG_REPLICATION_LAG_QUERY)
        is_replica = lag_row["is_replica"] if lag_row else False
        replay_lag_seconds = float(lag_row["replay_lag_seconds"]) if lag_row else 0.0

        # Get replication slot info (only meaningful on primary)
        replication_slots = []
        try:
            rows = await conn.fetch(PG_STAT_REPLICATION_QUERY)
            for row in rows:
                slot_info = {
                    "pid": row["pid"],
                    "usename": row["usename"],
                    "application_name": row["application_name"],
                    "client_addr": str(row["client_addr"]) if row["client_addr"] else None,
                    "client_hostname": row["client_hostname"],
                    "state": row["state"],
                    "sent_lsn": str(row["sent_lsn"]) if row["sent_lsn"] else None,
                    "write_lsn": str(row["write_lsn"]) if row["write_lsn"] else None,
                    "flush_lsn": str(row["flush_lsn"]) if row["flush_lsn"] else None,
                    "replay_lsn": str(row["replay_lsn"]) if row["replay_lsn"] else None,
                    "sync_state": row["sync_state"],
                    "reply_time": row["reply_time"].isoformat() if row["reply_time"] else None,
                }
                replication_slots.append(slot_info)
        except Exception as e:
            logger.warning(f"Could not query pg_stat_replication: {e}")

        return {
            "host_id": host_id,
            "evidence_type": "replication",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "data": {
                "is_replica": is_replica,
                "replay_lag_seconds": replay_lag_seconds,
                "replication_connections": replication_slots,
                "connection_count": len(replication_slots),
            },
        }
    except Exception as e:
        logger.error(f"Failed to collect replication info: {e}")
        return None
