"""
Collector for host OS metrics (CPU, memory, disk I/O).

Uses psutil to collect system-level metrics. Falls back to /proc if psutil
is unavailable.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def collect_os_metrics(
    host_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Collect host OS metrics including CPU utilization, memory usage, and disk I/O.

    Args:
        host_id: The unique identifier of this host.

    Returns:
        A dict with evidence data including OS metrics.
        Returns None on failure.
    """
    try:
        import psutil

        # CPU utilization (non-blocking, uses interval=None for instant reading)
        cpu_percent = psutil.cpu_percent(interval=None)

        # Memory usage
        memory = psutil.virtual_memory()
        memory_percent = memory.percent
        memory_total = memory.total
        memory_available = memory.available
        memory_used = memory.used

        # Disk I/O counters
        disk_io = psutil.disk_io_counters()
        disk_data = {}
        if disk_io:
            disk_data = {
                "read_count": disk_io.read_count,
                "write_count": disk_io.write_count,
                "read_bytes": disk_io.read_bytes,
                "write_bytes": disk_io.write_bytes,
                "read_time": disk_io.read_time,
                "write_time": disk_io.write_time,
            }

        # Load average (Unix-like systems)
        load_avg = None
        try:
            load_avg = list(psutil.getloadavg())
        except (AttributeError, OSError):
            pass

        return {
            "host_id": host_id,
            "evidence_type": "os_metrics",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "data": {
                "cpu_percent": cpu_percent,
                "memory_percent": memory_percent,
                "memory_total_bytes": memory_total,
                "memory_available_bytes": memory_available,
                "memory_used_bytes": memory_used,
                "disk_io": disk_data,
                "load_average": load_avg,
            },
        }
    except ImportError:
        logger.error("psutil not available, cannot collect OS metrics")
        return None
    except Exception as e:
        logger.error(f"Failed to collect OS metrics: {e}")
        return None
