"""Deterministic, bounded, measurement-first PostgreSQL candidate optimizer."""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional
from uuid import UUID

from backend.models.enums import PlanStatus
from backend.services.audit_logger import AuditLogger, get_audit_logger
from backend.services.baseline_measurement import build_baseline_measurement

DOMAIN_VERSION = "p0-bounded-v1"
PLANNER_KIND = "candidate_optimizer"
VALUE_PATTERN = re.compile(r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*([a-zA-Z]*)\s*$")
MEMORY_TO_KB = {
    "": 1.0,
    "b": 1.0 / 1024.0,
    "kb": 1.0,
    "mb": 1024.0,
    "gb": 1024.0 * 1024.0,
    "tb": 1024.0 * 1024.0 * 1024.0,
}


def _json(value: Any, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _setting_number(value: Any, value_kind: str) -> float:
    match = VALUE_PATTERN.fullmatch(str(value))
    if not match:
        raise ValueError(f"Unsupported PostgreSQL setting value {value!r}")
    number = float(match.group(1))
    suffix = match.group(2).lower()
    if value_kind == "memory_kb":
        if suffix not in MEMORY_TO_KB:
            raise ValueError(f"Unsupported memory unit {suffix!r}")
        return number * MEMORY_TO_KB[suffix]
    if suffix:
        raise ValueError(f"Unexpected unit {suffix!r} for {value_kind}")
    return number


def _format_setting_number(value: float, value_kind: str) -> str:
    if value_kind == "memory_kb":
        return f"{max(1, int(round(value)))}kB"
    if value_kind == "integer":
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _normalise_literal(value: Any) -> str:
    return str(value).strip().lower()


def generate_candidate_values(
    baseline_value: Any,
    value_kind: str,
    definition: Mapping[str, Any],
    max_deviation_pct: Optional[float],
) -> list[str]:
    """Expand one versioned domain while enforcing its allowlist deviation."""
    baseline = _setting_number(baseline_value, value_kind)
    strategy = str(definition.get("strategy"))
    raw_values = definition.get("values")
    if strategy not in {"multipliers", "absolute"} or not isinstance(raw_values, list):
        raise ValueError("Candidate domain must define absolute values or multipliers")
    minimum = float(definition.get("minimum", -math.inf))
    maximum = float(definition.get("maximum", math.inf))
    allowed_deviation = (
        float(max_deviation_pct)
        if max_deviation_pct is not None
        else float(definition.get("default_max_deviation_pct", 0))
    )
    values: list[str] = []
    for raw in raw_values:
        numeric = baseline * float(raw) if strategy == "multipliers" else float(raw)
        if numeric < minimum or numeric > maximum:
            continue
        deviation = abs(numeric - baseline) / max(abs(baseline), 1e-9) * 100
        if deviation > allowed_deviation + 1e-9:
            continue
        rendered = _format_setting_number(numeric, value_kind)
        if _normalise_literal(rendered) == _normalise_literal(baseline_value):
            continue
        if rendered not in values:
            values.append(rendered)
    return values


def _improvement_pct(reference: float, candidate: float, direction: str) -> float:
    denominator = max(abs(reference), 1e-9)
    if direction == "minimize":
        return (reference - candidate) / denominator * 100
    return (candidate - reference) / denominator * 100


def _delta_pct(before: Any, after: Any) -> Optional[float]:
    before_number = _number(before)
    after_number = _number(after)
    if before_number is None or after_number is None:
        return None
    if before_number == 0:
        return 0.0 if after_number == 0 else math.inf
    return (after_number - before_number) / abs(before_number) * 100


@dataclass(frozen=True)
class CandidateDecision:
    decision: str
    reason: str
    objective_score: Optional[float]
    baseline_delta_pct: Optional[float]
    best_delta_pct: Optional[float]
    safety_deltas: dict[str, Any]
    guardrail_violations: list[str]
    confidence_score: float


def evaluate_candidate(
    baseline: Mapping[str, Any],
    measurement: Mapping[str, Any],
    *,
    best_score_before: float,
    degradation_threshold_pct: float,
    objective_guardrails: Optional[Mapping[str, Any]] = None,
) -> CandidateDecision:
    """Keep only a comparable objective gain that passes every safety guardrail."""
    guardrails = dict(objective_guardrails or {})
    minimum_improvement = float(guardrails.get("minimum_improvement_pct", 1.0))
    max_coverage_drop = float(guardrails.get("max_coverage_drop_pct", 10.0))
    max_variance = float(guardrails.get("max_runtime_variance_pct", 50.0))
    score = _number(measurement.get("objective_score"))
    baseline_score = _number(baseline.get("objective_score"))
    direction = str(baseline.get("objective_direction"))
    comparable_errors: list[str] = []
    for field in ("objective_type", "objective_formula", "objective_direction"):
        if measurement.get(field) != baseline.get(field):
            comparable_errors.append(f"{field} changed from the immutable baseline")
    baseline_units = _json(baseline.get("metric_units"), {})
    measurement_units = _json(measurement.get("metric_units"), {})
    if measurement_units.get("objective_score") != baseline_units.get("objective_score"):
        comparable_errors.append("objective metric units changed")
    if score is None or baseline_score is None:
        comparable_errors.append("objective score is unavailable")

    requested = float(measurement.get("requested_measurement_window_seconds") or 0)
    observed = float(measurement.get("observed_measurement_window_seconds") or 0)
    if requested <= 0 or observed < requested * 0.8:
        comparable_errors.append("candidate measurement window is incomplete")
    baseline_coverage = float(baseline.get("workload_coverage_pct") or 0)
    candidate_coverage = float(measurement.get("workload_coverage_pct") or 0)
    if candidate_coverage < max(70.0, baseline_coverage - max_coverage_drop):
        comparable_errors.append("workload coverage no longer represents the baseline")
    variance = _number(measurement.get("runtime_variance_pct"))
    if variance is not None and variance > max_variance:
        comparable_errors.append("candidate runtime variance exceeds the noise limit")

    baseline_members = {
        str(item.get("query_id"))
        for item in _json(baseline.get("fingerprint_membership"), [])
    }
    candidate_members = {
        str(item.get("query_id"))
        for item in _json(measurement.get("fingerprint_membership"), [])
    }
    if baseline_members and candidate_members != baseline_members:
        comparable_errors.append("workload fingerprint membership changed")

    baseline_safety = _json(baseline.get("safety_metrics"), {})
    candidate_safety = _json(measurement.get("safety_metrics"), {})
    safety_deltas = {
        key: _delta_pct(baseline_safety.get(key), candidate_safety.get(key))
        for key in (
            "transaction_rate_per_second",
            "deadlocks_delta",
            "temp_files_delta",
            "temp_bytes_delta",
            "waiting_locks",
            "replication_lag_seconds",
            "cpu_utilization_pct",
            "memory_utilization_pct",
        )
    }
    violations: list[str] = []
    for key in ("deadlocks_delta", "waiting_locks"):
        before = float(baseline_safety.get(key) or 0)
        after = float(candidate_safety.get(key) or 0)
        if after > before:
            violations.append(f"{key} increased from {before:g} to {after:g}")
    for key in ("cpu_utilization_pct", "memory_utilization_pct", "replication_lag_seconds"):
        delta = safety_deltas.get(key)
        if delta is not None and delta > degradation_threshold_pct:
            violations.append(
                f"{key} degraded by {delta:.1f}% (limit {degradation_threshold_pct:.1f}%)"
            )
    if baseline.get("objective_type") != "transactions_per_second":
        tps_delta = safety_deltas.get("transaction_rate_per_second")
        if tps_delta is not None and tps_delta < -degradation_threshold_pct:
            violations.append(
                "transaction_rate_per_second degraded by "
                f"{abs(tps_delta):.1f}% (limit {degradation_threshold_pct:.1f}%)"
            )

    variance_penalty = min(0.5, (variance or 0) / 100)
    coverage_factor = min(1.0, candidate_coverage / 100)
    confidence = max(0.0, min(1.0, coverage_factor * (1 - variance_penalty)))
    if comparable_errors:
        return CandidateDecision(
            "inconclusive",
            "; ".join(comparable_errors),
            score,
            None,
            None,
            safety_deltas,
            violations,
            confidence,
        )

    assert score is not None and baseline_score is not None
    baseline_delta = _improvement_pct(baseline_score, score, direction)
    best_delta = _improvement_pct(float(best_score_before), score, direction)
    if violations:
        return CandidateDecision(
            "rolled_back",
            "; ".join(violations),
            score,
            baseline_delta,
            best_delta,
            safety_deltas,
            violations,
            confidence,
        )
    if baseline_delta < minimum_improvement or best_delta < minimum_improvement:
        return CandidateDecision(
            "rolled_back",
            (
                f"Objective improvement was {baseline_delta:.2f}% versus baseline and "
                f"{best_delta:.2f}% versus best; both must be at least "
                f"{minimum_improvement:.2f}%"
            ),
            score,
            baseline_delta,
            best_delta,
            safety_deltas,
            [],
            confidence,
        )
    return CandidateDecision(
        "kept",
        (
            f"Objective improved {baseline_delta:.2f}% versus baseline and "
            f"{best_delta:.2f}% versus best with all safety guardrails satisfied"
        ),
        score,
        baseline_delta,
        best_delta,
        safety_deltas,
        [],
        confidence,
    )


class CandidateOptimizer:
    """Persist and advance deterministic candidates behind an approval gate."""

    def __init__(
        self,
        pool,
        *,
        audit_logger: Optional[AuditLogger] = None,
        sleeper: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.pool = pool
        self.audit_logger = audit_logger or get_audit_logger()
        self.sleeper = sleeper

    async def propose_candidate(
        self,
        run: Mapping[str, Any],
        baseline: Mapping[str, Any],
        current_snapshot: Mapping[str, Mapping[str, Any]],
    ) -> Optional[dict[str, Any]]:
        run_id = run["id"]
        host_id = run["host_id"]
        selected_parameters = [
            str(item) for item in _json(run.get("selected_parameters"), [])
        ]
        if not selected_parameters:
            return None
        async with self.pool.acquire() as conn:
            history = await conn.fetch(
                """
                SELECT iteration, parameter_values, pre_change_snapshot
                FROM tuning_candidates WHERE run_id = $1 ORDER BY iteration
                """,
                run_id,
            )
            domains = await conn.fetch(
                """
                SELECT d.setting_name, d.value_kind, d.definition,
                       a.max_deviation_pct
                FROM candidate_parameter_domains d
                JOIN guardrail_allowlist a
                  ON a.host_id = $1
                 AND a.setting_name = d.setting_name
                 AND a.parameter_context = d.parameter_context
                WHERE d.version = $2 AND d.active = TRUE
                  AND d.setting_name = ANY($3::text[])
                ORDER BY array_position($3::text[], d.setting_name)
                """,
                host_id,
                DOMAIN_VERSION,
                selected_parameters,
            )
        iteration = len(history) + 1
        if iteration > int(run.get("max_iterations") or 1):
            return None
        baseline_snapshot = (
            _json(history[0]["pre_change_snapshot"], {}) if history else current_snapshot
        )
        tried = {
            (name, _normalise_literal(value))
            for row in history
            for name, value in _json(row["parameter_values"], {}).items()
        }
        proposal: Optional[tuple[str, str, Mapping[str, Any]]] = None
        for domain_row in domains:
            name = domain_row["setting_name"]
            if name not in current_snapshot or name not in baseline_snapshot:
                continue
            definition = _json(domain_row["definition"], {})
            values = generate_candidate_values(
                baseline_snapshot[name]["value"],
                domain_row["value_kind"],
                definition,
                float(domain_row["max_deviation_pct"])
                if domain_row["max_deviation_pct"] is not None
                else None,
            )
            current_value = _normalise_literal(current_snapshot[name]["value"])
            value = next(
                (
                    candidate
                    for candidate in values
                    if (name, _normalise_literal(candidate)) not in tried
                    and _normalise_literal(candidate) != current_value
                ),
                None,
            )
            if value is not None:
                proposal = (name, value, current_snapshot[name])
                break
        if proposal is None:
            return None

        name, value, current_state = proposal
        parameter_values = {name: value}
        pre_change_snapshot = {name: dict(current_state)}
        proposed_changes = [
            {
                "change_type": "setting",
                "setting_name": name,
                "current_value": current_state["value"],
                "proposed_value": value,
                "reason": (
                    f"Bounded candidate {iteration} from domain {DOMAIN_VERSION}; "
                    "benefit remains unproven until measurement"
                ),
            }
        ]
        rollback = [
            {
                "setting_name": name,
                "restore_value": current_state["value"],
                "reason": "Restore the last verified best configuration",
            }
        ]
        evidence_references = _json(baseline.get("evidence_references"), [])
        baseline_score = float(baseline["objective_score"])
        best_score = float(run.get("best_score") or baseline_score)
        confidence = float(baseline.get("root_cause_confidence") or 0)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                plan_id = await conn.fetchval(
                    """
                    INSERT INTO plans (
                        run_id, host_id, organization_id, status,
                        proposed_changes, evidence_references, risk_score,
                        confidence_score, uncertainty_explanation,
                        rollback_instructions, pre_change_snapshot,
                        planning_policy_version, planner_kind, configuration_backend
                    ) VALUES (
                        $1, $2, $3, $4, $5::jsonb, $6::jsonb, 25, $7,
                        $8, $9::jsonb, $10::jsonb, $11, $12,
                        (SELECT configuration_backend FROM hosts WHERE id = $2)
                    ) RETURNING id
                    """,
                    run_id,
                    host_id,
                    run["organization_id"],
                    PlanStatus.PENDING_APPROVAL.value,
                    json.dumps(proposed_changes),
                    json.dumps(evidence_references),
                    confidence,
                    "Deterministic bounded candidate; outcome requires measurement",
                    json.dumps(rollback),
                    json.dumps(pre_change_snapshot),
                    DOMAIN_VERSION,
                    PLANNER_KIND,
                )
                candidate_id = await conn.fetchval(
                    """
                    INSERT INTO tuning_candidates (
                        organization_id, run_id, host_id, plan_id, iteration,
                        domain_version, parameter_values, pre_change_snapshot,
                        baseline_score, best_score_before, objective_formula,
                        objective_direction, metric_units, warmup_window_seconds,
                        measurement_window_seconds, evidence_references,
                        confidence_score, decision
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb,
                        $9, $10, $11, $12, $13::jsonb, $14, $15,
                        $16::jsonb, $17, 'pending_approval'
                    ) RETURNING id
                    """,
                    run["organization_id"],
                    run_id,
                    host_id,
                    plan_id,
                    iteration,
                    DOMAIN_VERSION,
                    json.dumps(parameter_values),
                    json.dumps(pre_change_snapshot),
                    baseline_score,
                    best_score,
                    baseline["objective_formula"],
                    baseline["objective_direction"],
                    json.dumps(_json(baseline.get("metric_units"), {})),
                    int(run.get("warmup_window_seconds") or 0),
                    int(run.get("measurement_window_seconds") or 300),
                    json.dumps(evidence_references),
                    confidence,
                )
                await conn.execute(
                    "UPDATE loop_runs SET current_iteration = $2 WHERE id = $1",
                    run_id,
                    iteration,
                )
        await self.audit_logger.log(
            run_id=run_id,
            actor_type="system",
            actor_name=PLANNER_KIND,
            action_type="candidate_proposed",
            target_host_id=host_id,
            result="success",
            details={
                "candidate_id": str(candidate_id),
                "plan_id": str(plan_id),
                "iteration": iteration,
                "domain_version": DOMAIN_VERSION,
                "parameter_values": parameter_values,
                "baseline_score": baseline_score,
                "best_score_before": best_score,
            },
        )
        return {
            "candidate_id": candidate_id,
            "plan_id": plan_id,
            "iteration": iteration,
            "proposed_changes": proposed_changes,
            "rollback_instructions": rollback,
            "pre_change_snapshot": pre_change_snapshot,
            "parameter_values": parameter_values,
        }

    async def load_for_plan(self, plan_id: UUID) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tuning_candidates WHERE plan_id = $1", plan_id
            )
        return dict(row) if row else None

    async def mark_blocked(self, plan_id: UUID, reason: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE tuning_candidates SET decision = 'blocked',
                    decision_reason = $2, decided_at = NOW()
                WHERE plan_id = $1
                """,
                plan_id,
                reason,
            )

    async def measure_candidate(
        self,
        candidate: Mapping[str, Any],
        run: Mapping[str, Any],
    ) -> dict[str, Any]:
        persisted = _json(candidate.get("measurement_result"), None)
        if isinstance(persisted, dict):
            return persisted
        candidate_id = candidate["id"]
        warmup_seconds = int(candidate["warmup_window_seconds"])
        measurement_seconds = int(candidate["measurement_window_seconds"])
        now = datetime.now(timezone.utc)
        measurement_started = candidate.get("measurement_started_at")
        if measurement_started is None:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE tuning_candidates SET decision = 'measuring',
                        warmup_started_at = COALESCE(warmup_started_at, $2)
                    WHERE id = $1
                    """,
                    candidate_id,
                    now,
                )
            if warmup_seconds:
                await self.sleeper(warmup_seconds)
            measurement_started = datetime.now(timezone.utc)
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE tuning_candidates SET warmup_completed_at = $2,
                        measurement_started_at = $2
                    WHERE id = $1
                    """,
                    candidate_id,
                    measurement_started,
                )
        elif measurement_started.tzinfo is None:
            measurement_started = measurement_started.replace(tzinfo=timezone.utc)

        async with self.pool.acquire() as conn:
            interval = await conn.fetchval(
                "SELECT pg_stats_interval_sec FROM agent_config WHERE host_id = $1",
                run["host_id"],
            )
        collection_padding = min(60, max(5, int(interval or 30) + 5))
        target_end = measurement_started + timedelta(
            seconds=measurement_seconds + collection_padding
        )
        remaining = (target_end - datetime.now(timezone.utc)).total_seconds()
        if remaining > 0:
            await self.sleeper(remaining)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, evidence_type, collected_at, data, quality_score
                FROM evidence_snapshots
                WHERE host_id = $1
                  AND collected_at >= $2::timestamptz - INTERVAL '5 seconds'
                ORDER BY collected_at
                LIMIT 1000
                """,
                run["host_id"],
                measurement_started,
            )
            members = []
            if run.get("workload_fingerprint_id"):
                members = await conn.fetch(
                    """
                    SELECT query_id FROM workload_fingerprint_members
                    WHERE fingerprint_id = $1 ORDER BY ordinal
                    """,
                    run["workload_fingerprint_id"],
                )
        evidence = [{**dict(row), "data": _json(row["data"], {})} for row in rows]
        fingerprint = (
            {"members": [{"query_id": row["query_id"]} for row in members]}
            if members
            else None
        )
        measurement = build_baseline_measurement(run, evidence, fingerprint)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE tuning_candidates SET objective_score = $2,
                    observed_measurement_window_seconds = $3,
                    workload_coverage_pct = $4, runtime_variance_pct = $5,
                    safety_metrics = $6::jsonb, evidence_references = $7::jsonb,
                    measurement_result = $8::jsonb, measured_at = NOW()
                WHERE id = $1
                """,
                candidate_id,
                measurement["objective_score"],
                measurement["observed_measurement_window_seconds"],
                measurement["workload_coverage_pct"],
                measurement["runtime_variance_pct"],
                json.dumps(measurement["safety_metrics"]),
                json.dumps(measurement["evidence_references"]),
                json.dumps(measurement),
            )
        return measurement

    async def persist_decision(
        self,
        candidate: Mapping[str, Any],
        decision: CandidateDecision,
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE tuning_candidates SET decision = $2,
                        decision_reason = $3, objective_score = $4,
                        baseline_delta_pct = $5, best_delta_pct = $6,
                        safety_deltas = $7::jsonb,
                        guardrail_violations = $8::jsonb,
                        confidence_score = $9, decided_at = NOW()
                    WHERE id = $1
                    """,
                    candidate["id"],
                    decision.decision,
                    decision.reason,
                    decision.objective_score,
                    decision.baseline_delta_pct,
                    decision.best_delta_pct,
                    json.dumps(decision.safety_deltas),
                    json.dumps(decision.guardrail_violations),
                    decision.confidence_score,
                )
                if decision.decision == "kept":
                    await conn.execute(
                        """
                        UPDATE loop_runs SET best_score = $2,
                            current_iteration = GREATEST(current_iteration, $3)
                        WHERE id = $1
                        """,
                        candidate["run_id"],
                        decision.objective_score,
                        candidate["iteration"],
                    )
        await self.audit_logger.log(
            run_id=candidate["run_id"],
            actor_type="system",
            actor_name=PLANNER_KIND,
            action_type=f"candidate_{decision.decision}",
            target_host_id=candidate["host_id"],
            result="success" if decision.decision == "kept" else "blocked",
            result_reason=decision.reason,
            details={
                "candidate_id": str(candidate["id"]),
                "plan_id": str(candidate["plan_id"]),
                "iteration": candidate["iteration"],
                "objective_score": decision.objective_score,
                "baseline_delta_pct": decision.baseline_delta_pct,
                "best_delta_pct": decision.best_delta_pct,
                "safety_deltas": decision.safety_deltas,
                "guardrail_violations": decision.guardrail_violations,
            },
        )
