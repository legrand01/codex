"""Versioned PostgreSQL parameter catalog and complete run dispositions."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID

RELOAD_ONLY_PARAMETERS = (
    "work_mem",
    "random_page_cost",
    "seq_page_cost",
    "checkpoint_completion_target",
    "effective_io_concurrency",
    "max_parallel_workers_per_gather",
    "max_parallel_workers",
    "max_wal_size",
    "min_wal_size",
    "bgwriter_lru_maxpages",
    "bgwriter_delay",
    "effective_cache_size",
    "maintenance_work_mem",
    "default_statistics_target",
    "max_parallel_maintenance_workers",
)
RESTART_PARAMETERS = (
    "shared_buffers",
    "max_worker_processes",
    "wal_buffers",
    "huge_pages",
)
SUPPORTED_PARAMETERS = frozenset(RELOAD_ONLY_PARAMETERS + RESTART_PARAMETERS)
SUPPORTED_PG_MAJORS = frozenset({15, 16, 17, 18})
TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "manually_halted", "timed_out"}
)
FINAL_DISPOSITIONS = frozenset(
    {
        "changed_and_verified",
        "retained_at_baseline",
        "blocked_by_policy",
        "restart_required",
        "unsupported_on_target",
        "not_applicable_to_objective",
        "inconclusive_insufficient_evidence",
    }
)


def _json(value: Any, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def parse_pg_major(version: Any) -> Optional[int]:
    """Extract a supported PostgreSQL major from agent or fleet version text."""
    match = re.search(r"(?:postgresql\s+)?(\d+)", str(version or ""), re.IGNORECASE)
    if not match:
        return None
    major = int(match.group(1))
    return major if major in SUPPORTED_PG_MAJORS else None


def catalog_version_name(pg_major: int, platform_type: str) -> str:
    return f"pg{pg_major}-{platform_type.replace('_', '-')}-v1"


def _settings_map(payload: Any) -> dict[str, dict[str, Any]]:
    data = _json(payload, {})
    raw = data.get("settings", []) if isinstance(data, dict) else []
    if isinstance(raw, dict):
        return {
            str(name): value if isinstance(value, dict) else {"setting": value}
            for name, value in raw.items()
        }
    return {
        str(item["name"]): dict(item)
        for item in raw
        if isinstance(item, dict) and item.get("name")
    }


def _normalise_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value).strip().lower()


def derive_parameter_dispositions(
    *,
    run: Mapping[str, Any],
    catalog_entries: Sequence[Mapping[str, Any]],
    allowlist: Mapping[str, str],
    baseline_settings: Mapping[str, Mapping[str, Any]],
    current_settings: Mapping[str, Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    baseline_status: Optional[str],
    root_cause_category: Optional[str],
) -> list[dict[str, Any]]:
    """Derive exactly one honest row for every catalog entry in the run mode."""
    selected = {str(item) for item in _json(run.get("selected_parameters"), [])}
    terminal = str(run.get("status")) in TERMINAL_RUN_STATUSES
    platform_type = str(run.get("platform_type") or "self_managed")
    by_setting: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        for name, value in _json(candidate.get("parameter_values"), {}).items():
            by_setting.setdefault(str(name), []).append(
                {
                    "value": str(value),
                    "decision": str(candidate.get("decision") or ""),
                    "iteration": int(candidate.get("iteration") or 0),
                }
            )

    result: list[dict[str, Any]] = []
    for entry in sorted(catalog_entries, key=lambda item: int(item["display_order"])):
        name = str(entry["setting_name"])
        apply_context = str(entry["apply_context"])
        before = dict(baseline_settings.get(name, {}))
        current = dict(current_settings.get(name, before))
        history = sorted(by_setting.get(name, []), key=lambda item: item["iteration"])
        kept = [item for item in history if item["decision"] == "kept"]
        pending = [
            item
            for item in history
            if item["decision"] in {"pending_approval", "measuring"}
        ]
        baseline_value = before.get("setting", before.get("value"))
        current_value = current.get("setting", current.get("value", baseline_value))
        best_value = kept[-1]["value"] if kept else baseline_value
        pending_value = pending[-1]["value"] if pending else None
        supported = bool(before or current)
        allowlisted = allowlist.get(name) == apply_context
        is_selected = name in selected
        pending_restart = bool(current.get("pending_restart", False))
        final: Optional[str] = None
        reason: Optional[str] = None

        if terminal:
            if not supported:
                final = "unsupported_on_target"
                reason = "The setting was absent from the target pg_settings snapshots."
            elif not allowlisted:
                final = "blocked_by_policy"
                reason = "The host guardrail allowlist does not permit this setting."
            elif not is_selected:
                final = "not_applicable_to_objective"
                reason = "The setting was not included in this session objective."
            elif baseline_status != "ready":
                final = "inconclusive_insufficient_evidence"
                reason = "The immutable baseline was not measurement-ready."
            elif pending_restart:
                final = "restart_required"
                reason = "PostgreSQL reports a pending restart for this setting."
            elif kept and _normalise_value(best_value) != _normalise_value(baseline_value):
                if apply_context == "restart":
                    final = "restart_required"
                    reason = "The verified candidate is staged but requires a restart."
                else:
                    final = "changed_and_verified"
                    reason = "A measured candidate beat baseline and best-so-far safely."
            elif any(item["decision"] == "inconclusive" for item in history):
                final = "inconclusive_insufficient_evidence"
                reason = "The candidate measurement was not comparable to baseline."
            elif any(item["decision"] == "blocked" for item in history):
                final = "blocked_by_policy"
                reason = "The candidate was blocked by a safety or policy guardrail."
            elif history:
                final = "retained_at_baseline"
                reason = "No candidate safely beat the immutable baseline."
            elif root_cause_category and root_cause_category != "configuration":
                final = "not_applicable_to_objective"
                reason = "The root-cause gate did not identify configuration as the lever."
            elif not bool(entry.get("bounded_domain_available")):
                final = "inconclusive_insufficient_evidence"
                reason = "No bounded candidate domain was available for this setting."
            else:
                final = "inconclusive_insufficient_evidence"
                reason = "The session ended before this setting received a measurement."
        elif not supported and (baseline_settings or current_settings):
            final = "unsupported_on_target"
            reason = "The setting is absent from the observed target catalog."
        elif not allowlisted:
            final = "blocked_by_policy"
            reason = "The host guardrail allowlist does not permit this setting."
        elif not is_selected:
            final = "not_applicable_to_objective"
            reason = "The setting is outside the selected session scope."

        sourcefile = current.get("sourcefile")
        if not sourcefile and platform_type != "self_managed":
            sourcefile = platform_type
        result.append(
            {
                "setting_name": name,
                "display_order": int(entry["display_order"]),
                "apply_context": apply_context,
                "bounded_domain_available": bool(
                    entry.get("bounded_domain_available", False)
                ),
                "selected": is_selected,
                "supported_on_target": supported,
                "allowlisted": allowlisted,
                "current_value": str(current_value) if current_value is not None else None,
                "unit": current.get("unit", before.get("unit")),
                "source": current.get("source", before.get("source")),
                "sourcefile_or_provider": sourcefile or before.get("sourcefile"),
                "setting_context": current.get("context", before.get("context")),
                "pending_restart": pending_restart,
                "baseline_value": (
                    str(baseline_value) if baseline_value is not None else None
                ),
                "best_verified_value": (
                    str(best_value) if best_value is not None else None
                ),
                "pending_candidate_value": pending_value,
                "final_disposition": final,
                "disposition_reason": reason,
            }
        )
    return result


async def load_catalog_entries(
    conn, pg_version: Any, platform_type: str, tuning_mode: str
) -> tuple[Optional[str], list[dict[str, Any]]]:
    major = parse_pg_major(pg_version)
    if major is None:
        return None, []
    version = catalog_version_name(major, platform_type)
    rows = await conn.fetch(
        """
        SELECT setting_name, apply_context, display_order,
               bounded_domain_available, description
        FROM parameter_catalog_entries
        WHERE catalog_version = $1
          AND (apply_context = 'reload' OR $2 = 'restart_enabled')
        ORDER BY display_order
        """,
        version,
        tuning_mode,
    )
    return version, [dict(row) for row in rows]


async def refresh_parameter_dispositions(conn, run_id: UUID) -> list[dict[str, Any]]:
    """Reconcile the durable disposition snapshot for one tuning session."""
    run_row = await conn.fetchrow(
        """
        SELECT r.*, h.platform_type, h.pg_version
        FROM loop_runs r
        JOIN hosts h ON h.id = r.host_id
        WHERE r.id = $1
        """,
        run_id,
    )
    if run_row is None:
        return []
    run = dict(run_row)
    version, entries = await load_catalog_entries(
        conn,
        run.get("pg_version"),
        str(run.get("platform_type") or "self_managed"),
        str(run.get("tuning_mode") or "reload_only"),
    )
    if version is None or not entries:
        return []

    allowlist_rows = await conn.fetch(
        """
        SELECT setting_name, parameter_context
        FROM guardrail_allowlist WHERE host_id = $1
        """,
        run["host_id"],
    )
    allowlist = {
        str(row["setting_name"]): str(row["parameter_context"])
        for row in allowlist_rows
    }
    first = await conn.fetchrow(
        """
        SELECT data FROM evidence_snapshots
        WHERE run_id = $1 AND evidence_type = 'pg_settings'
        ORDER BY collected_at, id LIMIT 1
        """,
        run_id,
    )
    last = await conn.fetchrow(
        """
        SELECT data FROM evidence_snapshots
        WHERE run_id = $1 AND evidence_type = 'pg_settings'
        ORDER BY collected_at DESC, id DESC LIMIT 1
        """,
        run_id,
    )
    baseline = await conn.fetchrow(
        """
        SELECT status, root_cause_category
        FROM baseline_measurements WHERE run_id = $1
        """,
        run_id,
    )
    candidate_rows = await conn.fetch(
        """
        SELECT iteration, parameter_values, decision
        FROM tuning_candidates WHERE run_id = $1 ORDER BY iteration
        """,
        run_id,
    )
    dispositions = derive_parameter_dispositions(
        run=run,
        catalog_entries=entries,
        allowlist=allowlist,
        baseline_settings=_settings_map(first["data"] if first else None),
        current_settings=_settings_map(last["data"] if last else None),
        candidates=[dict(row) for row in candidate_rows],
        baseline_status=str(baseline["status"]) if baseline else None,
        root_cause_category=(
            str(baseline["root_cause_category"])
            if baseline and baseline["root_cause_category"]
            else None
        ),
    )
    await conn.execute(
        "UPDATE loop_runs SET parameter_catalog_version = $2 WHERE id = $1",
        run_id,
        version,
    )
    for item in dispositions:
        await conn.execute(
            """
            INSERT INTO run_parameter_dispositions (
                organization_id, run_id, host_id, catalog_version,
                setting_name, display_order, apply_context,
                bounded_domain_available, selected, supported_on_target,
                allowlisted, current_value, unit, source,
                sourcefile_or_provider, setting_context, pending_restart,
                baseline_value, best_verified_value, pending_candidate_value,
                final_disposition, disposition_reason, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, NOW()
            )
            ON CONFLICT (run_id, setting_name) DO UPDATE SET
                catalog_version = EXCLUDED.catalog_version,
                display_order = EXCLUDED.display_order,
                apply_context = EXCLUDED.apply_context,
                bounded_domain_available = EXCLUDED.bounded_domain_available,
                selected = EXCLUDED.selected,
                supported_on_target = EXCLUDED.supported_on_target,
                allowlisted = EXCLUDED.allowlisted,
                current_value = EXCLUDED.current_value,
                unit = EXCLUDED.unit,
                source = EXCLUDED.source,
                sourcefile_or_provider = EXCLUDED.sourcefile_or_provider,
                setting_context = EXCLUDED.setting_context,
                pending_restart = EXCLUDED.pending_restart,
                baseline_value = EXCLUDED.baseline_value,
                best_verified_value = EXCLUDED.best_verified_value,
                pending_candidate_value = EXCLUDED.pending_candidate_value,
                final_disposition = EXCLUDED.final_disposition,
                disposition_reason = EXCLUDED.disposition_reason,
                updated_at = NOW()
            """,
            run["organization_id"],
            run_id,
            run["host_id"],
            version,
            item["setting_name"],
            item["display_order"],
            item["apply_context"],
            item["bounded_domain_available"],
            item["selected"],
            item["supported_on_target"],
            item["allowlisted"],
            item["current_value"],
            item["unit"],
            item["source"],
            item["sourcefile_or_provider"],
            item["setting_context"],
            item["pending_restart"],
            item["baseline_value"],
            item["best_verified_value"],
            item["pending_candidate_value"],
            item["final_disposition"],
            item["disposition_reason"],
        )
    await conn.execute(
        """
        DELETE FROM run_parameter_dispositions
        WHERE run_id = $1 AND catalog_version <> $2
        """,
        run_id,
        version,
    )
    return dispositions
