"""
Tests for the risk score calculation in the Guardrail Engine.

Covers:
- Single setting with small deviation → low score
- Multiple settings → score increases
- Primary vs replica → primary scores higher
- Score clamped to max 100
- Score exceeding threshold → blocked=True
- Score at or below threshold → not blocked
- Zero deviation → zero score
- Breakdown contains per-setting details

Requirements: 9.1, 9.2
"""

import pytest

from backend.services.guardrail_engine import calculate_risk_score


class TestRiskScoreBasicCalculation:
    """Test basic risk score calculation behavior."""

    def test_single_setting_small_deviation_low_score(self):
        """A single setting with a small deviation should produce a low risk score."""
        proposed_changes = [
            {"setting_name": "work_mem", "proposed_value": 5000, "current_value": 4096}
        ]
        current_settings = {}

        result = calculate_risk_score(
            proposed_changes=proposed_changes,
            host_role="replica",
            current_settings=current_settings,
            risk_threshold=70,
        )

        # Deviation: |5000 - 4096| / max(4096, 1) * 100 ≈ 22.07%
        # Risk contribution: 22.07 * 1.0 * 1.0 = 22.07 → score ~22
        assert result.score >= 0
        assert result.score <= 100
        assert result.score < 50  # Should be relatively low
        assert result.blocked is False

    def test_multiple_settings_increase_score(self):
        """More settings should produce a higher risk score than fewer settings."""
        single_change = [
            {"setting_name": "work_mem", "proposed_value": 4500, "current_value": 4096}
        ]
        multiple_changes = [
            {"setting_name": "work_mem", "proposed_value": 4500, "current_value": 4096},
            {"setting_name": "shared_buffers", "proposed_value": 280, "current_value": 256},
            {"setting_name": "effective_cache_size", "proposed_value": 1100, "current_value": 1024},
        ]

        single_result = calculate_risk_score(
            proposed_changes=single_change,
            host_role="replica",
            current_settings={},
            risk_threshold=70,
        )

        multi_result = calculate_risk_score(
            proposed_changes=multiple_changes,
            host_role="replica",
            current_settings={},
            risk_threshold=70,
        )

        assert multi_result.score > single_result.score

    def test_primary_scores_higher_than_replica(self):
        """A primary host should produce a higher risk score than an identical change on a replica."""
        proposed_changes = [
            {"setting_name": "work_mem", "proposed_value": 4500, "current_value": 4096}
        ]

        primary_result = calculate_risk_score(
            proposed_changes=proposed_changes,
            host_role="primary",
            current_settings={},
            risk_threshold=70,
        )

        replica_result = calculate_risk_score(
            proposed_changes=proposed_changes,
            host_role="replica",
            current_settings={},
            risk_threshold=70,
        )

        assert primary_result.score > replica_result.score
        assert primary_result.host_role_multiplier == 1.5
        assert replica_result.host_role_multiplier == 1.0


class TestRiskScoreClamping:
    """Test that risk scores are properly clamped to [0, 100]."""

    def test_score_clamped_to_max_100(self):
        """Even with extreme deviations, the score should not exceed 100."""
        # Create changes with very large deviations
        proposed_changes = [
            {"setting_name": "work_mem", "proposed_value": 100000, "current_value": 1},
            {"setting_name": "shared_buffers", "proposed_value": 100000, "current_value": 1},
            {"setting_name": "effective_cache_size", "proposed_value": 100000, "current_value": 1},
        ]

        result = calculate_risk_score(
            proposed_changes=proposed_changes,
            host_role="primary",
            current_settings={},
            risk_threshold=70,
        )

        assert result.score == 100

    def test_score_minimum_is_zero(self):
        """Score should never be below 0."""
        proposed_changes = [
            {"setting_name": "work_mem", "proposed_value": 100, "current_value": 100}
        ]

        result = calculate_risk_score(
            proposed_changes=proposed_changes,
            host_role="replica",
            current_settings={},
            risk_threshold=70,
        )

        assert result.score >= 0


class TestRiskScoreBlocking:
    """Test blocking behavior based on threshold."""

    def test_score_exceeding_threshold_is_blocked(self):
        """When risk score exceeds the threshold, blocked should be True."""
        # Create changes that will definitely exceed a threshold of 70
        proposed_changes = [
            {"setting_name": "work_mem", "proposed_value": 10000, "current_value": 1000},
            {"setting_name": "shared_buffers", "proposed_value": 10000, "current_value": 1000},
        ]

        result = calculate_risk_score(
            proposed_changes=proposed_changes,
            host_role="primary",
            current_settings={},
            risk_threshold=70,
        )

        # Deviation for each: |10000 - 1000| / 1000 * 100 = 900%
        # Per-setting risk: 900 * 1.5 * 1.0 = 1350 → clamped total to 100
        assert result.score > 70
        assert result.blocked is True
        assert result.block_reason is not None
        assert "exceeds threshold" in result.block_reason

    def test_score_at_threshold_is_not_blocked(self):
        """When risk score equals the threshold, it should NOT be blocked (strict >)."""
        # We need a score that equals exactly the threshold
        # Use a threshold that we can match
        # deviation = |proposed - current| / max(|current|, 1) * 100
        # For score = 70 on replica (mult=1.0):
        # We need total_risk = 70
        # With one setting: deviation_pct * 1.0 * 1.0 = 70 → deviation_pct = 70
        # |proposed - current| / max(|current|, 1) * 100 = 70
        # If current = 100: |proposed - 100| / 100 * 100 = 70 → |proposed - 100| = 70 → proposed = 170
        proposed_changes = [
            {"setting_name": "work_mem", "proposed_value": 170, "current_value": 100}
        ]

        result = calculate_risk_score(
            proposed_changes=proposed_changes,
            host_role="replica",
            current_settings={},
            risk_threshold=70,
        )

        assert result.score == 70
        assert result.blocked is False

    def test_score_below_threshold_is_not_blocked(self):
        """When risk score is below the threshold, blocked should be False."""
        proposed_changes = [
            {"setting_name": "work_mem", "proposed_value": 4200, "current_value": 4096}
        ]

        result = calculate_risk_score(
            proposed_changes=proposed_changes,
            host_role="replica",
            current_settings={},
            risk_threshold=70,
        )

        assert result.score < 70
        assert result.blocked is False
        assert result.block_reason is None

    def test_custom_threshold_respected(self):
        """A custom threshold value should be used for blocking decisions."""
        proposed_changes = [
            {"setting_name": "work_mem", "proposed_value": 200, "current_value": 100}
        ]

        # With threshold=30, a 100% deviation on replica (score=100) should block
        result = calculate_risk_score(
            proposed_changes=proposed_changes,
            host_role="replica",
            current_settings={},
            risk_threshold=30,
        )

        assert result.score > 30
        assert result.blocked is True


class TestRiskScoreZeroDeviation:
    """Test behavior when proposed values equal current values."""

    def test_zero_deviation_produces_zero_score(self):
        """When proposed values equal current values, the score should be 0."""
        proposed_changes = [
            {"setting_name": "work_mem", "proposed_value": 4096, "current_value": 4096},
            {"setting_name": "shared_buffers", "proposed_value": 256, "current_value": 256},
        ]

        result = calculate_risk_score(
            proposed_changes=proposed_changes,
            host_role="primary",
            current_settings={},
            risk_threshold=70,
        )

        assert result.score == 0
        assert result.blocked is False

    def test_zero_current_value_uses_denominator_of_one(self):
        """When current value is 0, the denominator should be 1 to avoid division by zero."""
        proposed_changes = [
            {"setting_name": "work_mem", "proposed_value": 5, "current_value": 0}
        ]

        result = calculate_risk_score(
            proposed_changes=proposed_changes,
            host_role="replica",
            current_settings={},
            risk_threshold=70,
        )

        # Deviation: |5 - 0| / max(0, 1) * 100 = 500%
        # Risk: 500 * 1.0 * 1.0 = 500 → clamped to 100
        assert result.score > 0
        assert result.score <= 100


class TestRiskScoreBreakdown:
    """Test that breakdown contains per-setting details."""

    def test_breakdown_contains_per_setting_details(self):
        """The breakdown list should contain one entry per proposed change."""
        proposed_changes = [
            {"setting_name": "work_mem", "proposed_value": 8192, "current_value": 4096},
            {"setting_name": "shared_buffers", "proposed_value": 512, "current_value": 256},
        ]

        result = calculate_risk_score(
            proposed_changes=proposed_changes,
            host_role="primary",
            current_settings={},
            risk_threshold=70,
        )

        assert len(result.breakdown) == 2

        # Check first entry has expected fields
        entry = result.breakdown[0]
        assert "setting_name" in entry
        assert "current_value" in entry
        assert "proposed_value" in entry
        assert "deviation_pct" in entry
        assert "host_role_multiplier" in entry
        assert "risk_contribution" in entry

        # Verify setting names match
        assert result.breakdown[0]["setting_name"] == "work_mem"
        assert result.breakdown[1]["setting_name"] == "shared_buffers"

    def test_breakdown_deviation_values_are_correct(self):
        """The deviation percentages in the breakdown should be calculated correctly."""
        proposed_changes = [
            {"setting_name": "work_mem", "proposed_value": 200, "current_value": 100}
        ]

        result = calculate_risk_score(
            proposed_changes=proposed_changes,
            host_role="replica",
            current_settings={},
            risk_threshold=70,
        )

        # Deviation: |200 - 100| / max(100, 1) * 100 = 100%
        assert result.breakdown[0]["deviation_pct"] == 100.0
        assert result.breakdown[0]["host_role_multiplier"] == 1.0
        assert result.breakdown[0]["risk_contribution"] == 100.0

    def test_empty_proposed_changes_produces_zero_score(self):
        """An empty list of proposed changes should produce a score of 0."""
        result = calculate_risk_score(
            proposed_changes=[],
            host_role="primary",
            current_settings={},
            risk_threshold=70,
        )

        assert result.score == 0
        assert result.blocked is False
        assert len(result.breakdown) == 0


class TestRiskScoreCurrentSettingsLookup:
    """Test that current_settings dict is used when current_value is not in the change."""

    def test_uses_current_settings_dict_when_no_current_value_in_change(self):
        """When current_value is not in the change dict, look it up from current_settings."""
        proposed_changes = [
            {"setting_name": "work_mem", "proposed_value": 8192}
        ]
        current_settings = {"work_mem": 4096}

        result = calculate_risk_score(
            proposed_changes=proposed_changes,
            host_role="replica",
            current_settings=current_settings,
            risk_threshold=70,
        )

        # Deviation: |8192 - 4096| / 4096 * 100 = 100%
        assert result.breakdown[0]["deviation_pct"] == 100.0
        assert result.score > 0

    def test_current_value_in_change_takes_precedence(self):
        """current_value in the change dict should override the current_settings lookup."""
        proposed_changes = [
            {"setting_name": "work_mem", "proposed_value": 8192, "current_value": 4096}
        ]
        # This different value in current_settings should NOT be used
        current_settings = {"work_mem": 2048}

        result = calculate_risk_score(
            proposed_changes=proposed_changes,
            host_role="replica",
            current_settings=current_settings,
            risk_threshold=70,
        )

        # Should use 4096 from change dict, not 2048 from current_settings
        # Deviation: |8192 - 4096| / 4096 * 100 = 100%
        assert result.breakdown[0]["deviation_pct"] == 100.0
        assert result.breakdown[0]["current_value"] == 4096
