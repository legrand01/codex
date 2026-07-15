"""One-shot live verification for the disposable managed-file tuning lab."""

import asyncio
import json
import os

import asyncpg
from managed_conf import ManagedPostgresConf


async def main() -> None:
    dsn = os.environ["PG_CONNECTION_STRING"]
    path = os.environ["MANAGED_CONF_PATH"]
    conn = await asyncpg.connect(dsn)
    manager = ManagedPostgresConf(conn, path)
    try:
        row = await conn.fetchrow(
            """
            SELECT name, current_setting(name) AS value, unit, context, source,
                   sourcefile, pending_restart
            FROM pg_settings WHERE name = 'work_mem'
            """
        )
        snapshot = {
            "work_mem": {
                "value": row["value"],
                "unit": row["unit"],
                "context": row["context"],
                "source": row["source"],
                "sourcefile": row["sourcefile"],
                "pending_restart": row["pending_restart"],
            }
        }
        applied = await manager.apply(
            [{"setting_name": "work_mem", "proposed_value": "128kB"}],
            snapshot,
        )
        after_apply = await conn.fetchrow(
            "SELECT current_setting(name) AS current_value, setting, sourcefile, "
            "pending_restart FROM pg_settings WHERE name='work_mem'"
        )
        rolled_back = await manager.rollback(applied["backend_snapshot"], snapshot)
        after_rollback = await conn.fetchrow(
            "SELECT current_setting(name) AS current_value, setting, sourcefile, "
            "pending_restart FROM pg_settings WHERE name='work_mem'"
        )
        if after_apply["current_value"] != "128kB":
            raise RuntimeError(f"Managed apply did not activate 128kB: {dict(after_apply)}")
        if after_apply["sourcefile"] != path:
            raise RuntimeError(f"Managed sourcefile was not active: {dict(after_apply)}")
        if after_rollback["current_value"] != row["value"]:
            raise RuntimeError(
                f"Rollback did not restore {row['value']}: {dict(after_rollback)}"
            )
        if after_rollback["sourcefile"] != row["sourcefile"]:
            raise RuntimeError(
                f"Rollback provenance differs from baseline: {dict(after_rollback)}"
            )
        print(
            json.dumps(
                {
                    "baseline": snapshot["work_mem"],
                    "applied": dict(after_apply),
                    "apply_checksum": applied["backend_snapshot"]["applied_checksum"],
                    "rolled_back": rolled_back,
                    "after_rollback": dict(after_rollback),
                },
                default=str,
                sort_keys=True,
            )
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
