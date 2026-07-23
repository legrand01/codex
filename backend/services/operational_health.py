"""Dependency-aware health and low-cardinality Prometheus metrics."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict

from backend.db.pool import get_pool
from backend.db.redis_manager import get_redis_client

STARTED_AT = time.time()


@dataclass(frozen=True)
class DependencyCheck:
    ok: bool
    latency_seconds: float
    error: str | None = None

    def as_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "status": "up" if self.ok else "down",
            "latency_ms": round(self.latency_seconds * 1000, 3),
        }
        if self.error:
            result["error"] = self.error
        return result


async def _check_postgres(timeout_seconds: float) -> DependencyCheck:
    started = time.perf_counter()
    pool = get_pool()
    if pool is None:
        return DependencyCheck(False, 0.0, "connection pool is not initialized")
    try:
        await asyncio.wait_for(pool.fetchval("SELECT 1"), timeout=timeout_seconds)
        return DependencyCheck(True, time.perf_counter() - started)
    except Exception as exc:
        return DependencyCheck(
            False,
            time.perf_counter() - started,
            f"{type(exc).__name__}: {exc}",
        )


async def _check_redis(timeout_seconds: float) -> DependencyCheck:
    started = time.perf_counter()
    client = get_redis_client()
    if client is None:
        return DependencyCheck(False, 0.0, "client is not initialized")
    try:
        pong = await asyncio.wait_for(client.ping(), timeout=timeout_seconds)
        if not pong:
            raise RuntimeError("PING did not return PONG")
        return DependencyCheck(True, time.perf_counter() - started)
    except Exception as exc:
        return DependencyCheck(
            False,
            time.perf_counter() - started,
            f"{type(exc).__name__}: {exc}",
        )


async def dependency_status(timeout_seconds: float = 2.0) -> Dict[str, Any]:
    """Return bounded dependency checks suitable for a readiness probe."""
    postgres, redis = await asyncio.gather(
        _check_postgres(timeout_seconds),
        _check_redis(timeout_seconds),
    )
    ready = postgres.ok and redis.ok
    return {
        "status": "ready" if ready else "not_ready",
        "service": "autonomous-postgres-dba-agent",
        "dependencies": {
            "postgres": postgres.as_dict(),
            "redis": redis.as_dict(),
        },
    }


async def _database_metrics() -> Dict[str, float]:
    metrics = {
        "dbtune_run_jobs_queued": 0.0,
        "dbtune_run_jobs_stale_claimed": 0.0,
        "dbtune_agent_duplicate_hosts": 0.0,
        "dbtune_evidence_freshness_seconds": -1.0,
    }
    pool = get_pool()
    if pool is None:
        return metrics
    try:
        row = await pool.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM run_jobs WHERE status = 'queued') AS queued,
                (
                    SELECT COUNT(*) FROM run_jobs
                    WHERE status = 'claimed' AND lease_expires_at < NOW()
                ) AS stale_claimed,
                (
                    SELECT COUNT(*) FROM hosts WHERE agent_write_ambiguous = TRUE
                ) AS duplicate_hosts,
                COALESCE(
                    EXTRACT(EPOCH FROM (NOW() - MAX(collected_at))),
                    -1
                ) AS evidence_freshness_seconds
            FROM evidence_snapshots
            """
        )
        if row is not None:
            metrics["dbtune_run_jobs_queued"] = float(row["queued"])
            metrics["dbtune_run_jobs_stale_claimed"] = float(row["stale_claimed"])
            metrics["dbtune_agent_duplicate_hosts"] = float(row["duplicate_hosts"])
            metrics["dbtune_evidence_freshness_seconds"] = float(
                row["evidence_freshness_seconds"]
            )
    except Exception:
        # Readiness already exposes dependency failure. Metrics must remain
        # scrapeable during migrations and partial outages.
        return metrics
    return metrics


def _metric(name: str, help_text: str, value: float) -> str:
    return f"# HELP {name} {help_text}\n# TYPE {name} gauge\n{name} {value}\n"


async def prometheus_metrics() -> str:
    """Render bounded, secret-free Prometheus exposition text."""
    status = await dependency_status()
    db_metrics = await _database_metrics()
    postgres_up = 1.0 if status["dependencies"]["postgres"]["status"] == "up" else 0.0
    redis_up = 1.0 if status["dependencies"]["redis"]["status"] == "up" else 0.0
    parts = [
        _metric("dbtune_up", "Control plane process is running.", 1.0),
        _metric(
            "dbtune_uptime_seconds",
            "Control plane process uptime in seconds.",
            max(0.0, time.time() - STARTED_AT),
        ),
        _metric("dbtune_postgres_up", "Control plane PostgreSQL is reachable.", postgres_up),
        _metric("dbtune_redis_up", "Control plane Redis is reachable.", redis_up),
        _metric(
            "dbtune_postgres_latency_seconds",
            "Control plane PostgreSQL readiness probe latency.",
            status["dependencies"]["postgres"]["latency_ms"] / 1000,
        ),
        _metric(
            "dbtune_redis_latency_seconds",
            "Control plane Redis readiness probe latency.",
            status["dependencies"]["redis"]["latency_ms"] / 1000,
        ),
    ]
    descriptions = {
        "dbtune_run_jobs_queued": "Durable tuning jobs waiting for a worker.",
        "dbtune_run_jobs_stale_claimed": "Claimed tuning jobs with expired leases.",
        "dbtune_agent_duplicate_hosts": "Hosts with ambiguous active agent ownership.",
        "dbtune_evidence_freshness_seconds": "Age of the newest evidence snapshot, or -1.",
    }
    parts.extend(_metric(name, descriptions[name], value) for name, value in db_metrics.items())
    return "".join(parts)
