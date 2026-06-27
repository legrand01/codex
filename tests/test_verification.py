"""
Tests for the post-apply verification and rollback decision service.

Covers:
- Metric delta computation (positive change, negative change, zero denominator)
- Comparison with all metrics within threshold → kept
- Comparison with one metric exceeding threshold → rollback
- Collection failure → rollback
- Custom threshold values

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from backend.models.config import LoopConfig
from backend.services.audit_logger import AuditLogger
from backend.services.verification import (
    METRIC_CATEGORIES,
    collect_verification_evidence,
    compare_evidence,
    compute_metric_delta,
    verify_and_decide,
)


# ============================================================
# Tests for compute_metric_delta
# ============================================================


class TestComputeMetricDelta:
    """Tests for the compute_metric_delta function."""

    def test_positive_change(self):
        """Metric increased: (110 - 100) / 100 * 100 = 10%"""
        result = compute_metric_delta(100.0, 110.0)
        assert result == pytest.approx(10.0)

    def test_negative_change(self):
        """Metric decreased: (90 - 100) / 100 * 100 = -10%"""
        result = compute_metric_delta(100.0, 90.0)
        assert result == pytest.approx(-10.0)

    def test_no_change(self):
        """Metric unchanged: (100 - 100) / 100 * 100 = 0%"""
        result = compute_metric_delta(100.0, 100.0)
        assert result == pytest.approx(0.0)

    def test_large_positive_change(self):
        """Metric doubled: (200 - 100) / 100 * 100 = 100%"""
        result = compute_metric_delta(100.0, 200.0)
        assert result == pytest.approx(100.0)

    def test_large_negative_change(self):
        """Metric halved: (50 - 100) / 100 * 100 = -50%"""
        result = compute_metric_delta(100.0, 50.0)
        assert result == pytest.approx(-50.0)

    def test_zero_pre_value_zero_post_value(self):
        """Both zero: no change, returns 0.0"""
        result = compute_metric_delta(0.0, 0.0)
        assert result == 0.0

    def test_zero_pre_value_positive_post_value(self):
        """Pre is zero, post is positive: returns infinity"""
        result = compute_metric_delta(0.0, 5.0)
        assert result == float("inf")

    def test_zero_pre_value_negative_post_value(self):
        """Pre is zero, post is negative: returns negative infinity"""
        result = compute_metric_delta(0.0, -5.0)
        assert result == float("-inf")

    def test_small_values(self):
        """Works with small decimal values"""
        result = compute_metric_delta(0.001, 0.002)
        assert result == pytest.approx(100.0)

    def test_fractional_change(self):
        """Fractional percentage change: (105 - 100) / 100 * 100 = 5%"""
        result = compute_metric_delta(100.0, 105.0)
        assert result == pytest.approx(5.0)


# ============================================================
# Tests for compare_evidence
# ============================================================


class TestCompareEvidence:
    """Tests for the compare_evidence function."""

    def test_same_evidence_returns_zero_deltas(self):
        """Identical pre and post evidence should yield zero deltas."""
        evidence = {
            "pg_stat_database": {"xact_commit": 1000, "xact_rollback": 5},
            "os_metrics": {"cpu_percent": 45.0, "memory_percent": 60.0},
        }
        deltas = compare_evidence(evidence, evidence)
        for category, delta in deltas.items():
            assert delta == pytest.approx(0.0)

    def test_positive_delta_detected(self):
        """Post values higher than pre should yield positive deltas."""
        pre = {"os_metrics": {"cpu_percent": 50.0, "memory_percent": 60.0}}
        post = {"os_metrics": {"cpu_percent": 60.0, "memory_percent": 66.0}}
        deltas = compare_evidence(pre, post)
        # cpu went from 50 to 60 = 20%, memory went from 60 to 66 = 10%
        # max abs delta should be 20.0
        assert "os_metrics" in deltas
        assert deltas["os_metrics"] == pytest.approx(20.0)

    def test_negative_delta_detected(self):
        """Post values lower than pre should yield negative deltas."""
        pre = {"pg_stat_database": {"xact_commit": 1000}}
        post = {"pg_stat_database": {"xact_commit": 800}}
        deltas = compare_evidence(pre, post)
        assert "pg_stat_database" in deltas
        assert deltas["pg_stat_database"] == pytest.approx(-20.0)

    def test_empty_evidence_returns_empty(self):
        """Empty evidence on both sides returns empty deltas."""
        deltas = compare_evidence({}, {})
        assert deltas == {}

    def test_mixed_metric_types_ignored(self):
        """Non-numeric values in evidence are ignored during comparison."""
        pre = {
            "pg_stat_database": {
                "xact_commit": 100,
                "datname": "mydb",
            }
        }
        post = {
            "pg_stat_database": {
                "xact_commit": 110,
                "datname": "mydb",
            }
        }
        deltas = compare_evidence(pre, post)
        assert "pg_stat_database" in deltas
        assert deltas["pg_stat_database"] == pytest.approx(10.0)

    def test_only_common_categories_compared(self):
        """Only categories present in both pre and post are compared."""
        pre = {"os_metrics": {"cpu_percent": 50.0}}
        post = {"locks": {"lock_count": 10}}
        deltas = compare_evidence(pre, post)
        # os_metrics only in pre (post has empty), locks only in post (pre has empty)
        # os_metrics: pre has cpu=50, post has nothing -> no numeric comparison
        # locks: pre has nothing, post has lock_count=10 -> no numeric in pre
        assert "os_metrics" not in deltas or deltas.get("os_metrics", 0) == 0
        assert "locks" not in deltas or deltas.get("locks", 0) == 0

    def test_multiple_categories_with_deltas(self):
        """Multiple categories each have their own delta."""
        pre = {
            "os_metrics": {"cpu_percent": 50.0},
            "pg_stat_database": {"xact_commit": 1000},
        }
        post = {
            "os_metrics": {"cpu_percent": 55.0},
            "pg_stat_database": {"xact_commit": 1050},
        }
        deltas = compare_evidence(pre, post)
        assert "os_metrics" in deltas
        assert deltas["os_metrics"] == pytest.approx(10.0)
        assert "pg_stat_database" in deltas
        assert deltas["pg_stat_database"] == pytest.approx(5.0)


# ============================================================
# Tests for collect_verification_evidence
# ============================================================


class TestCollectVerificationEvidence:
    """Tests for the collect_verification_evidence function."""

    @pytest.mark.asyncio
    async def test_invalid_observation_window_too_low(self):
        """Window below 10s returns None."""
        result = await collect_verification_evidence(uuid4(), observation_window_seconds=5)
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_observation_window_too_high(self):
        """Window above 600s returns None."""
        result = await collect_verification_evidence(
            uuid4(), observation_window_seconds=601
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_collection_with_no_pool_returns_none(self):
        """If pool is None and get_pool() returns None, returns None."""
        with patch(
            "backend.services.verification.asyncio.sleep", new_callable=AsyncMock
        ):
            with patch("backend.db.pool.get_pool", return_value=None):
                result = await collect_verification_evidence(
                    uuid4(), observation_window_seconds=10, pool=None
                )
        # Since pool=None and get_pool() returns None, it should return None
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_collection_with_mock_pool(self):
        """Successful evidence collection returns dict with metric categories."""
        host_id = uuid4()

        # Create mock pool and connection
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={"data": {"cpu_percent": 55.0, "memory_percent": 70.0}}
        )

        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "backend.services.verification.asyncio.sleep", new_callable=AsyncMock
        ):
            result = await collect_verification_evidence(
                host_id, observation_window_seconds=10, pool=mock_pool
            )

        assert result is not None
        assert isinstance(result, dict)
        # Should have entries for each metric category
        for category in METRIC_CATEGORIES:
            assert category in result

    @pytest.mark.asyncio
    async def test_collection_failure_returns_none(self):
        """Database error during collection returns None."""
        host_id = uuid4()

        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(
            side_effect=Exception("DB connection failed")
        )
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "backend.services.verification.asyncio.sleep", new_callable=AsyncMock
        ):
            result = await collect_verification_evidence(
                host_id, observation_window_seconds=10, pool=mock_pool
            )

        assert result is None


# ============================================================
# Tests for verify_and_decide
# ============================================================


class TestVerifyAndDecide:
    """Tests for the verify_and_decide function."""

    def _make_config(self, threshold=10.0, window=60):
        """Create a LoopConfig with custom verification parameters."""
        return LoopConfig(
            verification_window_seconds=window,
            degradation_threshold_pct=threshold,
        )

    def _make_audit_logger(self):
        """Create a mock audit logger."""
        mock_logger = AsyncMock(spec=AuditLogger)
        mock_logger.log = AsyncMock()
        return mock_logger

    @pytest.mark.asyncio
    async def test_all_metrics_within_threshold_kept(self):
        """When all metrics are within threshold, decision is 'kept'."""
        run_id = uuid4()
        host_id = uuid4()
        plan_id = uuid4()
        config = self._make_config(threshold=10.0, window=10)
        audit_logger = self._make_audit_logger()

        pre_evidence = {
            "os_metrics": {"cpu_percent": 50.0, "memory_percent": 60.0},
            "pg_stat_database": {"xact_commit": 1000},
        }
        # Post evidence: small changes within 10% threshold
        post_evidence = {
            "os_metrics": {"cpu_percent": 52.0, "memory_percent": 62.0},
            "pg_stat_database": {"xact_commit": 1050},
        }

        with patch(
            "backend.services.verification.collect_verification_evidence",
            new_callable=AsyncMock,
            return_value=post_evidence,
        ):
            result = await verify_and_decide(
                run_id=run_id,
                host_id=host_id,
                plan_id=plan_id,
                pre_evidence=pre_evidence,
                config=config,
                pool=AsyncMock(),
                audit_logger=audit_logger,
            )

        assert result["decision"] == "kept"
        assert result["triggering_metric"] is None
        assert result["triggering_delta"] is None
        assert result["threshold"] == 10.0
        assert result["failure_reason"] is None

        # Verify audit logger was called with success
        audit_logger.log.assert_called()
        last_call = audit_logger.log.call_args
        assert last_call.kwargs["action_type"] == "verification_passed"
        assert last_call.kwargs["result"] == "success"

    @pytest.mark.asyncio
    async def test_metric_exceeds_threshold_rollback(self):
        """When a metric exceeds threshold, decision is 'rolled_back'."""
        run_id = uuid4()
        host_id = uuid4()
        plan_id = uuid4()
        config = self._make_config(threshold=10.0, window=10)
        audit_logger = self._make_audit_logger()

        pre_evidence = {
            "os_metrics": {"cpu_percent": 50.0},
        }
        # Post evidence: CPU jumped 30% - exceeds 10% threshold
        post_evidence = {
            "os_metrics": {"cpu_percent": 65.0},
        }

        with patch(
            "backend.services.verification.collect_verification_evidence",
            new_callable=AsyncMock,
            return_value=post_evidence,
        ), patch(
            "backend.services.verification._initiate_rollback",
            new_callable=AsyncMock,
        ) as mock_rollback:
            result = await verify_and_decide(
                run_id=run_id,
                host_id=host_id,
                plan_id=plan_id,
                pre_evidence=pre_evidence,
                config=config,
                pool=AsyncMock(),
                audit_logger=audit_logger,
            )

        assert result["decision"] == "rolled_back"
        assert result["triggering_metric"] == "os_metrics"
        assert result["triggering_delta"] == pytest.approx(30.0)
        assert result["threshold"] == 10.0
        assert result["failure_reason"] is None
        mock_rollback.assert_called_once_with(plan_id, mock_rollback.call_args[0][1])

    @pytest.mark.asyncio
    async def test_collection_failure_triggers_rollback(self):
        """When evidence collection fails, decision is 'rolled_back'."""
        run_id = uuid4()
        host_id = uuid4()
        plan_id = uuid4()
        config = self._make_config(threshold=10.0, window=10)
        audit_logger = self._make_audit_logger()

        pre_evidence = {"os_metrics": {"cpu_percent": 50.0}}

        with patch(
            "backend.services.verification.collect_verification_evidence",
            new_callable=AsyncMock,
            return_value=None,  # Collection failed
        ), patch(
            "backend.services.verification._initiate_rollback",
            new_callable=AsyncMock,
        ) as mock_rollback:
            result = await verify_and_decide(
                run_id=run_id,
                host_id=host_id,
                plan_id=plan_id,
                pre_evidence=pre_evidence,
                config=config,
                pool=AsyncMock(),
                audit_logger=audit_logger,
            )

        assert result["decision"] == "rolled_back"
        assert result["failure_reason"] is not None
        assert "Failed to collect" in result["failure_reason"]
        assert result["deltas"] == {}
        mock_rollback.assert_called_once()

        # Verify audit logged the failure
        audit_logger.log.assert_called()
        failure_call = audit_logger.log.call_args
        assert failure_call.kwargs["action_type"] == "verification_collection_failed"
        assert failure_call.kwargs["result"] == "failure"

    @pytest.mark.asyncio
    async def test_custom_threshold_higher(self):
        """With higher threshold, same delta is within bounds → kept."""
        run_id = uuid4()
        host_id = uuid4()
        plan_id = uuid4()
        config = self._make_config(threshold=50.0, window=10)  # 50% threshold
        audit_logger = self._make_audit_logger()

        pre_evidence = {"os_metrics": {"cpu_percent": 50.0}}
        # 30% change - exceeds 10% but not 50%
        post_evidence = {"os_metrics": {"cpu_percent": 65.0}}

        with patch(
            "backend.services.verification.collect_verification_evidence",
            new_callable=AsyncMock,
            return_value=post_evidence,
        ):
            result = await verify_and_decide(
                run_id=run_id,
                host_id=host_id,
                plan_id=plan_id,
                pre_evidence=pre_evidence,
                config=config,
                pool=AsyncMock(),
                audit_logger=audit_logger,
            )

        assert result["decision"] == "kept"
        assert result["threshold"] == 50.0

    @pytest.mark.asyncio
    async def test_custom_threshold_lower(self):
        """With lower threshold, a small delta triggers rollback."""
        run_id = uuid4()
        host_id = uuid4()
        plan_id = uuid4()
        config = self._make_config(threshold=2.0, window=10)  # 2% threshold
        audit_logger = self._make_audit_logger()

        pre_evidence = {"os_metrics": {"cpu_percent": 50.0}}
        # 4% change - within 10% but exceeds 2%
        post_evidence = {"os_metrics": {"cpu_percent": 52.0}}

        with patch(
            "backend.services.verification.collect_verification_evidence",
            new_callable=AsyncMock,
            return_value=post_evidence,
        ), patch(
            "backend.services.verification._initiate_rollback",
            new_callable=AsyncMock,
        ):
            result = await verify_and_decide(
                run_id=run_id,
                host_id=host_id,
                plan_id=plan_id,
                pre_evidence=pre_evidence,
                config=config,
                pool=AsyncMock(),
                audit_logger=audit_logger,
            )

        assert result["decision"] == "rolled_back"
        assert result["threshold"] == 2.0

    @pytest.mark.asyncio
    async def test_negative_degradation_triggers_rollback(self):
        """A large negative change (metric drop) can also trigger rollback."""
        run_id = uuid4()
        host_id = uuid4()
        plan_id = uuid4()
        config = self._make_config(threshold=10.0, window=10)
        audit_logger = self._make_audit_logger()

        pre_evidence = {"pg_stat_database": {"xact_commit": 1000}}
        # 50% drop in commits - exceeds 10% threshold
        post_evidence = {"pg_stat_database": {"xact_commit": 500}}

        with patch(
            "backend.services.verification.collect_verification_evidence",
            new_callable=AsyncMock,
            return_value=post_evidence,
        ), patch(
            "backend.services.verification._initiate_rollback",
            new_callable=AsyncMock,
        ):
            result = await verify_and_decide(
                run_id=run_id,
                host_id=host_id,
                plan_id=plan_id,
                pre_evidence=pre_evidence,
                config=config,
                pool=AsyncMock(),
                audit_logger=audit_logger,
            )

        assert result["decision"] == "rolled_back"
        assert result["triggering_metric"] == "pg_stat_database"
        assert result["triggering_delta"] == pytest.approx(-50.0)

    @pytest.mark.asyncio
    async def test_empty_pre_evidence_no_comparison(self):
        """With empty pre-evidence and post-evidence, all kept (nothing to compare)."""
        run_id = uuid4()
        host_id = uuid4()
        plan_id = uuid4()
        config = self._make_config(threshold=10.0, window=10)
        audit_logger = self._make_audit_logger()

        pre_evidence = {}
        post_evidence = {}

        with patch(
            "backend.services.verification.collect_verification_evidence",
            new_callable=AsyncMock,
            return_value=post_evidence,
        ):
            result = await verify_and_decide(
                run_id=run_id,
                host_id=host_id,
                plan_id=plan_id,
                pre_evidence=pre_evidence,
                config=config,
                pool=AsyncMock(),
                audit_logger=audit_logger,
            )

        assert result["decision"] == "kept"
        assert result["deltas"] == {}

    @pytest.mark.asyncio
    async def test_exactly_at_threshold_is_kept(self):
        """Metric delta exactly at threshold (not exceeding) results in kept."""
        run_id = uuid4()
        host_id = uuid4()
        plan_id = uuid4()
        config = self._make_config(threshold=10.0, window=10)
        audit_logger = self._make_audit_logger()

        pre_evidence = {"os_metrics": {"cpu_percent": 100.0}}
        # Exactly 10% change - at threshold, not exceeding
        post_evidence = {"os_metrics": {"cpu_percent": 110.0}}

        with patch(
            "backend.services.verification.collect_verification_evidence",
            new_callable=AsyncMock,
            return_value=post_evidence,
        ):
            result = await verify_and_decide(
                run_id=run_id,
                host_id=host_id,
                plan_id=plan_id,
                pre_evidence=pre_evidence,
                config=config,
                pool=AsyncMock(),
                audit_logger=audit_logger,
            )

        assert result["decision"] == "kept"

    @pytest.mark.asyncio
    async def test_multiple_metrics_one_exceeds(self):
        """Only one metric needs to exceed threshold to trigger rollback."""
        run_id = uuid4()
        host_id = uuid4()
        plan_id = uuid4()
        config = self._make_config(threshold=10.0, window=10)
        audit_logger = self._make_audit_logger()

        pre_evidence = {
            "os_metrics": {"cpu_percent": 50.0},
            "pg_stat_database": {"xact_commit": 1000},
        }
        # os_metrics: 4% change (within threshold)
        # pg_stat_database: -20% change (exceeds threshold)
        post_evidence = {
            "os_metrics": {"cpu_percent": 52.0},
            "pg_stat_database": {"xact_commit": 800},
        }

        with patch(
            "backend.services.verification.collect_verification_evidence",
            new_callable=AsyncMock,
            return_value=post_evidence,
        ), patch(
            "backend.services.verification._initiate_rollback",
            new_callable=AsyncMock,
        ):
            result = await verify_and_decide(
                run_id=run_id,
                host_id=host_id,
                plan_id=plan_id,
                pre_evidence=pre_evidence,
                config=config,
                pool=AsyncMock(),
                audit_logger=audit_logger,
            )

        assert result["decision"] == "rolled_back"
        # The triggering metric should be one that exceeded threshold
        assert result["triggering_metric"] is not None
        assert abs(result["triggering_delta"]) > 10.0
