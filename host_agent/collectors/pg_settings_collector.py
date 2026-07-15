"""
Collector for pg_settings configuration snapshots.

Queries the PostgreSQL pg_settings system catalog to capture the current
configuration state of the managed database.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PG_SETTINGS_QUERY = """
SELECT name, setting, unit, category, short_desc, context, vartype, source,
       sourcefile, pending_restart
FROM pg_settings
ORDER BY name;
"""


async def collect_pg_settings(
    conn,
    host_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Collect pg_settings configuration snapshot.

    Args:
        conn: An asyncpg connection or connection pool.
        host_id: The unique identifier of this host.

    Returns:
        A dict with evidence data including host_id, collected_at (UTC),
        evidence_type, and the settings data. Returns None on failure.
    """
    try:
        rows = await conn.fetch(PG_SETTINGS_QUERY)
        settings = [
            {
                "name": row["name"],
                "setting": row["setting"],
                "unit": row["unit"],
                "category": row["category"],
                "short_desc": row["short_desc"],
                "context": row["context"],
                "vartype": row["vartype"],
                "source": row["source"],
                "sourcefile": row.get("sourcefile"),
                "pending_restart": bool(row.get("pending_restart", False)),
            }
            for row in rows
        ]

        return {
            "host_id": host_id,
            "evidence_type": "pg_settings",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "data": {
                "settings": settings,
                "total_count": len(settings),
            },
        }
    except Exception as e:
        logger.error(f"Failed to collect pg_settings: {e}")
        return None
