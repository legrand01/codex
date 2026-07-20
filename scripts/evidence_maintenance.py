#!/usr/bin/env python3
"""Preview or execute evidence lifecycle maintenance from the command line."""

import argparse
import asyncio
import json
from datetime import datetime
from uuid import UUID

from backend.db.pool import close_pool, create_pool
from backend.services.evidence_lifecycle import EvidenceLifecycleManager


def _json_default(value):
    if isinstance(value, (datetime, UUID)):
        return str(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


async def _run(args: argparse.Namespace) -> None:
    pool = await create_pool()
    manager = EvidenceLifecycleManager(pool)
    try:
        organizations = [UUID(args.organization_id)] if args.organization_id else (
            await manager.list_organizations()
        )
        results = []
        for organization_id in organizations:
            if args.execute:
                payload = await manager.run_cleanup(
                    organization_id,
                    triggered_by="cli:operator",
                    max_batches=args.max_batches,
                )
            else:
                payload = await manager.status(organization_id)
            results.append({"organization_id": organization_id, "result": payload})
        print(json.dumps(results, indent=2, default=_json_default))
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview evidence retention or execute bounded cleanup"
    )
    parser.add_argument("--organization-id", help="Limit the operation to one tenant UUID")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write rollups and delete eligible raw payloads (default is preview only)",
    )
    parser.add_argument("--max-batches", type=int, help="Override the per-run batch limit")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
