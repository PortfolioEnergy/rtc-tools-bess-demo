"""Tests for service.model_registry."""

from __future__ import annotations

import pytest

from service.model_registry import resolve_solver_type


class TestResolveSolverType:
    """Tests for resolve_solver_type."""

    @pytest.mark.parametrize(
        "model_name, expected",
        [
            # Day-ahead scheduling
            ("bess_day_ahead", "scheduling"),
            ("bess_scheduling", "scheduling"),
            ("my-dayahead-model", "scheduling"),
            ("da", "scheduling"),
            ("BESS_DAY_AHEAD", "scheduling"),
            # Intraday continuous
            ("bess_rolling", "intraday"),
            ("bess_intraday", "intraday"),
            ("bess_continuous", "intraday"),
            ("ic", "intraday"),
            ("BESS_ROLLING", "intraday"),
            # DA setpoints
            ("da_setpoints_from_positions", "da_setpoints"),
            ("bess_setpoints", "da_setpoints"),
            # Exact keyword matches
            ("scheduling", "scheduling"),
            ("rolling", "intraday"),
            ("setpoints", "da_setpoints"),
        ],
    )
    def test_known_models(self, model_name: str, expected: str) -> None:
        assert resolve_solver_type(model_name) == expected

    @pytest.mark.parametrize(
        "model_name",
        [
            "unknown_model",
            "bess",
            "fcr_model",
            "afrr_model",
            "",
        ],
    )
    def test_unknown_models_return_none(self, model_name: str) -> None:
        assert resolve_solver_type(model_name) is None

    def test_whitespace_is_stripped(self) -> None:
        assert resolve_solver_type("  bess_rolling  ") == "intraday"

    def test_hyphen_is_treated_as_underscore(self) -> None:
        assert resolve_solver_type("bess-day-ahead") == "scheduling"

    def test_longer_match_wins_over_shorter(self) -> None:
        """'day_ahead' (2 tokens) should match before 'da' (1 token)."""
        assert resolve_solver_type("bess_day_ahead") == "scheduling"
