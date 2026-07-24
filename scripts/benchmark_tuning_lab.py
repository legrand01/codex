"""Measure the tuning lab's analytical query without mutating its settings."""

import argparse
import asyncio
import json
import os
import statistics
from typing import Any, Dict, Iterable

import asyncpg

DEFAULT_DSN = (
    "postgresql://dbtune_workload:dbtune-workload-lab-only"
    "@127.0.0.1:55433/dbtune_target"
)
QUERY = """
SELECT customer_id, region_id, SUM(amount) AS revenue, COUNT(*) AS purchases
FROM sales_events
WHERE created_at >= NOW() - INTERVAL '30 days'
GROUP BY customer_id, region_id
ORDER BY revenue DESC
LIMIT 100
"""


def walk_plan(node: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from walk_plan(child)


async def benchmark(dsn: str, runs: int) -> Dict[str, Any]:
    conn = await asyncpg.connect(dsn, command_timeout=120)
    try:
        work_mem = await conn.fetchval("SELECT current_setting('work_mem')")
        active = await conn.fetchval(
            "SELECT COUNT(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND state = 'active' AND pid <> pg_backend_pid()"
        )
        samples = []
        for _ in range(runs):
            payload = await conn.fetchval(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + QUERY
            )
            document = json.loads(payload) if isinstance(payload, str) else payload
            report = document[0]
            nodes = list(walk_plan(report["Plan"]))
            samples.append(
                {
                    "execution_ms": float(report["Execution Time"]),
                    "temp_read_blocks": sum(int(node.get("Temp Read Blocks", 0)) for node in nodes),
                    "temp_written_blocks": sum(
                        int(node.get("Temp Written Blocks", 0)) for node in nodes
                    ),
                    "sort_methods": sorted(
                        {
                            str(node["Sort Method"])
                            for node in nodes
                            if node.get("Sort Method")
                        }
                    ),
                }
            )
        return {
            "work_mem": work_mem,
            "active_other_sessions": active,
            "runs": runs,
            "median_execution_ms": round(
                statistics.median(sample["execution_ms"] for sample in samples), 3
            ),
            "median_temp_read_blocks": statistics.median(
                sample["temp_read_blocks"] for sample in samples
            ),
            "median_temp_written_blocks": statistics.median(
                sample["temp_written_blocks"] for sample in samples
            ),
            "samples": samples,
        }
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("DBTUNE_LAB_DSN", DEFAULT_DSN))
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(benchmark(args.dsn, args.runs)), indent=2))


if __name__ == "__main__":
    main()
