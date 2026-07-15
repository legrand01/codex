"""Exercise the control-plane managed backend against the disposable tuning lab."""

import asyncio
import json
import os
from uuid import UUID

import asyncpg

from backend.services.configuration_backends import ManagedConfFileBackend

HOST_ID = UUID("fa4c33a1-2359-4539-9ca3-3f1761e28aae")


async def main() -> None:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3)
    backend = ManagedConfFileBackend(pool)
    try:
        async with pool.acquire() as conn:
            plan_id = await conn.fetchval(
                "SELECT id FROM plans WHERE host_id=$1 ORDER BY created_at DESC LIMIT 1",
                HOST_ID,
            )
        snapshot = await backend.capture_snapshot(HOST_ID, ["work_mem"])
        changes = [{"setting_name": "work_mem", "proposed_value": "128kB"}]
        dry_run = await backend.dry_run(HOST_ID, changes)
        if not dry_run.passed:
            raise RuntimeError(f"Managed backend dry-run failed: {dry_run.errors}")
        applied = await backend.apply(
            HOST_ID,
            changes,
            snapshot,
            plan_id=plan_id,
        )
        # Exercise the same redacted snapshot the API persists. The backend must
        # recover exact bytes from the private configuration version.
        rollback_snapshot = dict(applied.to_dict().get("backend_snapshot") or {})
        rollback_snapshot["configuration_version_id"] = applied.configuration_version_id
        rolled_back = await backend.rollback(
            HOST_ID,
            snapshot,
            rollback_snapshot,
            plan_id=plan_id,
        )
        async with pool.acquire() as conn:
            version = await conn.fetchrow(
                """
                SELECT id, status, managed_conf_path, backend_snapshot,
                       apply_result, rollback_result
                FROM configuration_versions WHERE id=$1
                """,
                UUID(str(applied.configuration_version_id)),
            )
        stored_snapshot = version["backend_snapshot"] or {}
        if isinstance(stored_snapshot, str):
            stored_snapshot = json.loads(stored_snapshot)
        print(
            json.dumps(
                {
                    "dry_run": {"passed": dry_run.passed, "errors": dry_run.errors},
                    "applied": applied.to_dict(),
                    "rolled_back": rolled_back.to_dict(),
                    "configuration_version": {
                        "id": str(version["id"]),
                        "status": version["status"],
                        "managed_conf_path": version["managed_conf_path"],
                        "has_exact_previous_bytes": "bytes_b64"
                        in stored_snapshot.get("file", {}),
                        "apply_recorded": bool(version["apply_result"]),
                        "rollback_recorded": bool(version["rollback_result"]),
                    },
                },
                default=str,
                sort_keys=True,
            )
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
