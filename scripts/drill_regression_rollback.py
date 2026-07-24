"""Prove measured regression detection and exact managed-file rollback."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
from benchmark_tuning_lab import benchmark
from staging_preflight import (
    host_runtime_database_url,
    host_target_workload_database_url,
    load_env,
)

from backend.config import settings
from backend.services.configuration_backends import ManagedConfFileBackend

ROOT = Path(__file__).resolve().parents[1]
def control_dsn(values: dict[str, str]) -> str:
    return host_runtime_database_url(values)


def regressed(baseline: dict[str, Any], candidate: dict[str, Any]) -> bool:
    baseline_temp = float(baseline["median_temp_written_blocks"])
    candidate_temp = float(candidate["median_temp_written_blocks"])
    baseline_ms = float(baseline["median_execution_ms"])
    candidate_ms = float(candidate["median_execution_ms"])
    return (
        candidate_temp > max(baseline_temp * 1.2, baseline_temp + 100)
        or candidate_ms > baseline_ms * 1.05
    )


async def run_drill(values: dict[str, str], runs: int) -> dict[str, Any]:
    target_dsn = host_target_workload_database_url(values)
    host_id = UUID(values["TARGET_AGENT_HOST_ID"])
    pool = await asyncpg.create_pool(control_dsn(values), min_size=1, max_size=4)
    backend = ManagedConfFileBackend(pool)
    baseline_applied = None
    degraded_applied = None
    degraded_rolled_back = False
    original_restored = False
    previous_write_switch = settings.write_execution_enabled
    previous_target_dsn = os.environ.get("DBTUNE_LAB_TARGET_DSN")
    allowlist_existed = False
    original_host_writes = False
    original_snapshot: dict[str, dict[str, Any]] | None = None
    baseline_snapshot: dict[str, dict[str, Any]] | None = None
    try:
        async with pool.acquire() as conn:
            original_host_writes = bool(
                await conn.fetchval("SELECT writes_enabled FROM hosts WHERE id=$1", host_id)
            )
            allowlist_existed = bool(
                await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM guardrail_allowlist "
                    "WHERE host_id=$1 AND setting_name='work_mem')",
                    host_id,
                )
            )
            await conn.execute("UPDATE hosts SET writes_enabled=TRUE WHERE id=$1", host_id)
            await conn.execute(
                """
                INSERT INTO guardrail_allowlist (
                    host_id, setting_name, parameter_context, max_deviation_pct
                ) VALUES ($1, 'work_mem', 'reload', NULL)
                ON CONFLICT (host_id, setting_name) DO NOTHING
                """,
                host_id,
            )
        settings.write_execution_enabled = True
        os.environ["DBTUNE_LAB_TARGET_DSN"] = target_dsn

        original_snapshot = await backend.capture_snapshot(host_id, ["work_mem"])
        baseline_change = [{"setting_name": "work_mem", "proposed_value": "4MB"}]
        baseline_dry_run = await backend.dry_run(host_id, baseline_change)
        if not baseline_dry_run.passed:
            raise RuntimeError(
                f"managed-file baseline dry-run failed: {baseline_dry_run.errors}"
            )
        baseline_applied = await backend.apply(
            host_id,
            baseline_change,
            original_snapshot,
        )
        baseline = await benchmark(target_dsn, runs)
        if str(baseline["work_mem"]).lower() not in {"4mb", "4096kb"}:
            raise RuntimeError(
                f"controlled baseline did not activate 4MB work_mem: {baseline['work_mem']}"
            )

        baseline_snapshot = await backend.capture_snapshot(host_id, ["work_mem"])
        degraded_change = [{"setting_name": "work_mem", "proposed_value": "64kB"}]
        dry_run = await backend.dry_run(host_id, degraded_change)
        if not dry_run.passed:
            raise RuntimeError(f"managed-file dry-run failed: {dry_run.errors}")
        degraded_applied = await backend.apply(
            host_id,
            degraded_change,
            baseline_snapshot,
        )
        degraded = await benchmark(target_dsn, runs)
        if str(degraded["work_mem"]).lower() != "64kb":
            raise RuntimeError(
                "degraded candidate did not activate 64kB work_mem: "
                f"{degraded['work_mem']}"
            )
        regression_detected = regressed(baseline, degraded)
        if not regression_detected:
            raise RuntimeError(
                "64kB work_mem did not produce a measurable runtime or temp-I/O regression"
            )

        degraded_backend_snapshot = dict(degraded_applied.backend_snapshot or {})
        degraded_backend_snapshot["configuration_version_id"] = (
            degraded_applied.configuration_version_id
        )
        rollback = await backend.rollback(
            host_id,
            baseline_snapshot,
            degraded_backend_snapshot,
        )
        if not rollback.succeeded or not rollback.rolled_back:
            raise RuntimeError("managed-file regression rollback did not report success")
        degraded_rolled_back = True
        restored = await benchmark(target_dsn, runs)
        restored_snapshot = await backend.capture_snapshot(host_id, ["work_mem"])
        expected_value = str(baseline_snapshot["work_mem"]["value"]).lower()
        restored_value = str(restored_snapshot["work_mem"]["value"]).lower()
        if restored_value != expected_value:
            raise RuntimeError(
                f"rollback restored {restored_value!r}, expected {expected_value!r}"
            )

        baseline_backend_snapshot = dict(baseline_applied.backend_snapshot or {})
        baseline_backend_snapshot["configuration_version_id"] = (
            baseline_applied.configuration_version_id
        )
        original_restore = await backend.rollback(
            host_id,
            original_snapshot,
            baseline_backend_snapshot,
        )
        if not original_restore.succeeded or not original_restore.rolled_back:
            raise RuntimeError("pre-drill managed-file state was not restored")
        original_restored = True
        final_snapshot = await backend.capture_snapshot(host_id, ["work_mem"])
        original_value = str(original_snapshot["work_mem"]["value"]).lower()
        final_value = str(final_snapshot["work_mem"]["value"]).lower()
        if final_value != original_value:
            raise RuntimeError(
                f"drill cleanup restored {final_value!r}, expected {original_value!r}"
            )
        for field in ("source", "sourcefile"):
            if final_snapshot["work_mem"][field] != original_snapshot["work_mem"][field]:
                raise RuntimeError(
                    f"drill cleanup changed work_mem {field}: "
                    f"{original_snapshot['work_mem'][field]!r} -> "
                    f"{final_snapshot['work_mem'][field]!r}"
                )
        return {
            "baseline": baseline,
            "degraded": degraded,
            "restored": restored,
            "regression_detected": regression_detected,
            "rollback_succeeded": rollback.succeeded,
            "restored_value": restored_value,
            "restored_source": restored_snapshot["work_mem"]["source"],
            "restored_sourcefile": restored_snapshot["work_mem"]["sourcefile"],
            "degraded_configuration_version_id": (
                degraded_applied.configuration_version_id
            ),
            "baseline_configuration_version_id": (
                baseline_applied.configuration_version_id
            ),
            "pre_drill_value": original_value,
            "final_value": final_value,
            "pre_drill_source": original_snapshot["work_mem"]["source"],
            "final_source": final_snapshot["work_mem"]["source"],
            "pre_drill_sourcefile": original_snapshot["work_mem"]["sourcefile"],
            "final_sourcefile": final_snapshot["work_mem"]["sourcefile"],
        }
    finally:
        if (
            degraded_applied is not None
            and not degraded_rolled_back
            and baseline_snapshot is not None
        ):
            rollback_snapshot = dict(degraded_applied.backend_snapshot or {})
            rollback_snapshot["configuration_version_id"] = (
                degraded_applied.configuration_version_id
            )
            try:
                await backend.rollback(host_id, baseline_snapshot, rollback_snapshot)
            except Exception:
                pass
        if (
            baseline_applied is not None
            and not original_restored
            and original_snapshot is not None
        ):
            rollback_snapshot = dict(baseline_applied.backend_snapshot or {})
            rollback_snapshot["configuration_version_id"] = (
                baseline_applied.configuration_version_id
            )
            try:
                await backend.rollback(host_id, original_snapshot, rollback_snapshot)
            except Exception:
                pass
        settings.write_execution_enabled = previous_write_switch
        if previous_target_dsn is None:
            os.environ.pop("DBTUNE_LAB_TARGET_DSN", None)
        else:
            os.environ["DBTUNE_LAB_TARGET_DSN"] = previous_target_dsn
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE hosts SET writes_enabled=$2 WHERE id=$1",
                host_id,
                original_host_writes,
            )
            if not allowlist_existed:
                await conn.execute(
                    "DELETE FROM guardrail_allowlist "
                    "WHERE host_id=$1 AND setting_name='work_mem'",
                    host_id,
                )
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.staging")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.runs < 2:
        raise SystemExit("--runs must be at least 2")
    result = asyncio.run(run_drill(load_env(args.env_file), args.runs))
    print(json.dumps({"drill": "regression_rollback", "passed": True, **result}, sort_keys=True))


if __name__ == "__main__":
    main()
