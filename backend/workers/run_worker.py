"""Standalone durable run worker process."""

import asyncio
import logging
import os
import socket

from backend.config import settings
from backend.db.pool import close_pool, create_pool, get_pool
from backend.services.durable_run_orchestrator import DurableRunOrchestrator
from backend.services.evidence_lifecycle import EvidenceLifecycleManager
from backend.services.run_queue import ClaimedRunJob, RunQueue

logger = logging.getLogger(__name__)


async def _heartbeat(queue: RunQueue, job: ClaimedRunJob) -> None:
    while True:
        await asyncio.sleep(max(1, queue.lease_seconds // 3))
        if not await queue.heartbeat(job):
            raise RuntimeError(f"Lost lease for run job {job.job_id}")


async def _evidence_maintenance(pool) -> None:
    """Run bounded tenant-scoped evidence maintenance on a fixed cadence."""
    manager = EvidenceLifecycleManager(pool)
    while True:
        try:
            for organization_id in await manager.list_organizations():
                result = await manager.run_cleanup(
                    organization_id,
                    triggered_by="worker:scheduler",
                )
                logger.info(
                    "Evidence maintenance organization=%s status=%s deleted=%s bytes=%s",
                    organization_id,
                    result["status"],
                    result.get("snapshots_deleted", 0),
                    result.get("raw_bytes_reclaimed", 0),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Tuning work remains available if maintenance fails. The manager
            # records a durable failure event for the affected organization.
            logger.exception("Scheduled evidence maintenance failed")
        await asyncio.sleep(max(60, settings.evidence_cleanup_interval_seconds))


async def run_forever() -> None:
    await create_pool()
    pool = get_pool()
    if pool is None:
        raise RuntimeError("Control-plane database pool was not initialized")

    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    queue = RunQueue(pool)
    orchestrator = DurableRunOrchestrator(pool)
    maintenance = (
        asyncio.create_task(_evidence_maintenance(pool))
        if settings.evidence_cleanup_enabled
        else None
    )
    logger.info("Durable run worker started as %s", worker_id)

    try:
        while True:
            job = await queue.claim(worker_id)
            if job is None:
                await asyncio.sleep(1)
                continue
            if await queue.cancellation_requested(job):
                await queue.cancel(job)
                continue

            heartbeat = asyncio.create_task(_heartbeat(queue, job))
            try:
                result = await orchestrator.process(job.run_id)
                if result.disposition == "waiting_approval":
                    await queue.mark_waiting_approval(job)
                elif result.disposition == "completed":
                    await queue.complete(job)
                elif result.disposition == "cancelled":
                    await queue.cancel(job)
                else:
                    await queue.fail(job, result.message, retry=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Run job %s failed", job.job_id)
                await queue.fail(job, str(exc), retry=True)
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
    finally:
        if maintenance is not None:
            maintenance.cancel()
            try:
                await maintenance
            except asyncio.CancelledError:
                pass
        await close_pool()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run_forever())
