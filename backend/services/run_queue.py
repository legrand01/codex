"""PostgreSQL-backed durable queue for DBA loop runs."""

from dataclasses import dataclass
from typing import Optional
from uuid import UUID


@dataclass(frozen=True)
class ClaimedRunJob:
    job_id: UUID
    run_id: UUID
    organization_id: UUID
    claimed_by: str
    attempts: int


class RunQueue:
    def __init__(self, pool, *, lease_seconds: int = 60, max_attempts: int = 5) -> None:
        self.pool = pool
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    async def claim(self, worker_id: str) -> Optional[ClaimedRunJob]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE run_jobs
                    SET status = 'claimed', claimed_by = $2, claimed_at = NOW(),
                        heartbeat_at = NOW(),
                        lease_expires_at = NOW() + ($3 * INTERVAL '1 second'),
                        attempts = attempts + 1, updated_at = NOW()
                    WHERE id = (
                        SELECT candidate.id
                        FROM run_jobs AS candidate
                        WHERE candidate.attempts < $1
                          AND (
                              (candidate.status = 'queued' AND candidate.available_at <= NOW())
                              OR (
                                  candidate.status = 'claimed'
                                  AND candidate.lease_expires_at < NOW()
                              )
                          )
                        ORDER BY candidate.available_at, candidate.created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    RETURNING id, run_id, organization_id, attempts
                    """,
                    self.max_attempts,
                    worker_id,
                    self.lease_seconds,
                )
        if row is None:
            return None
        return ClaimedRunJob(
            job_id=row["id"],
            run_id=row["run_id"],
            organization_id=row["organization_id"],
            claimed_by=worker_id,
            attempts=row["attempts"],
        )

    async def heartbeat(self, job: ClaimedRunJob) -> bool:
        async with self.pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE run_jobs
                SET heartbeat_at = NOW(),
                    lease_expires_at = NOW() + ($3 * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE id = $1 AND status = 'claimed' AND claimed_by = $2
                RETURNING id
                """,
                job.job_id,
                job.claimed_by,
                self.lease_seconds,
            )
        return updated is not None

    async def mark_waiting_approval(self, job: ClaimedRunJob) -> None:
        await self._finish_claim(job, "waiting_approval")

    async def complete(self, job: ClaimedRunJob) -> None:
        await self._finish_claim(job, "succeeded", completed=True)

    async def fail(self, job: ClaimedRunJob, error: str, *, retry: bool) -> None:
        status = "queued" if retry and job.attempts < self.max_attempts else "failed"
        delay = min(60, 2 ** max(0, job.attempts - 1))
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE run_jobs
                SET status = $3, last_error = $4,
                    available_at = NOW() + ($5 * INTERVAL '1 second'),
                    claimed_by = NULL, claimed_at = NULL, lease_expires_at = NULL,
                    updated_at = NOW(),
                    completed_at = CASE WHEN $3 = 'failed' THEN NOW() ELSE NULL END
                WHERE id = $1 AND claimed_by = $2
                """,
                job.job_id,
                job.claimed_by,
                status,
                error,
                delay,
            )

    async def cancellation_requested(self, job: ClaimedRunJob) -> bool:
        async with self.pool.acquire() as conn:
            status = await conn.fetchval("SELECT status FROM run_jobs WHERE id = $1", job.job_id)
        return status == "cancel_requested"

    async def cancel(self, job: ClaimedRunJob) -> None:
        await self._finish_claim(job, "cancelled", completed=True, allow_cancel=True)

    async def _finish_claim(
        self,
        job: ClaimedRunJob,
        status: str,
        *,
        completed: bool = False,
        allow_cancel: bool = False,
    ) -> None:
        statuses = ["claimed"]
        if allow_cancel:
            statuses.append("cancel_requested")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE run_jobs
                SET status = $3, claimed_by = NULL, claimed_at = NULL,
                    lease_expires_at = NULL, updated_at = NOW(),
                    completed_at = CASE WHEN $4 THEN NOW() ELSE NULL END
                WHERE id = $1 AND claimed_by = $2 AND status = ANY($5::text[])
                """,
                job.job_id,
                job.claimed_by,
                status,
                completed,
                statuses,
            )
