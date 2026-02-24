"""Tests for service.translation modules."""

from __future__ import annotations

from typing import Any

import pytest

from service.translation.pe_to_rtc import (
    TranslationResult,
    translate_intraday,
    translate_scheduling,
)
from service.translation.setpoints import translate_setpoints


class TestTranslateScheduling:
    """Tests for translate_scheduling."""

    def test_basic_output_shape(self, scheduling_input: dict[str, Any]) -> None:
        result = translate_scheduling(scheduling_input)
        assert isinstance(result, TranslationResult)
        assert result.timeseries_csv
        assert result.initial_state_csv
        assert result.n_segments == 0

    def test_csv_has_correct_columns(self, scheduling_input: dict[str, Any]) -> None:
        result = translate_scheduling(scheduling_input)
        first_line = result.timeseries_csv.splitlines()[0]
        assert first_line == "time,price"

    def test_csv_row_count_includes_endpoint(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """24 intervals → 25 rows (24 + endpoint) + 1 header = 26 lines."""
        result = translate_scheduling(scheduling_input)
        lines = result.timeseries_csv.strip().splitlines()
        assert len(lines) == 26

    def test_initial_soc_extracted(self, scheduling_input: dict[str, Any]) -> None:
        result = translate_scheduling(scheduling_input)
        assert "10.0" in result.initial_state_csv

    def test_parameters_csv_generated(self, scheduling_input: dict[str, Any]) -> None:
        result = translate_scheduling(scheduling_input)
        assert result.parameters_csv is not None
        assert "capacity" in result.parameters_csv
        assert "max_power" in result.parameters_csv
        assert "efficiency" in result.parameters_csv

    def test_efficiency_merged_to_roundtrip(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        result = translate_scheduling(scheduling_input)
        assert result.parameters_csv is not None
        # 0.95 * 0.95 = 0.9025
        assert "0.9025" in result.parameters_csv

    def test_max_power_uses_min(self, scheduling_input: dict[str, Any]) -> None:
        """When charge != discharge, min() should be used."""
        scheduling_input["parameters"] = [
            {"name": "max_charge_power", "value": 8.0},
            {"name": "max_discharge_power", "value": 12.0},
        ]
        result = translate_scheduling(scheduling_input)
        assert result.parameters_csv is not None
        assert "8.0" in result.parameters_csv
        approx_msg = [
            i for i in result.info if "approximation:" in i and "max_power" in i
        ]
        assert len(approx_msg) == 1

    def test_ignored_timeseries_reported(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        scheduling_input["timeseries"].append(
            {"name": "imbalance_price_in", "values": [1.0] * 24}
        )
        result = translate_scheduling(scheduling_input)
        ignored = [
            i
            for i in result.info
            if "ignored_input:" in i and "imbalance_price_in" in i
        ]
        assert len(ignored) == 1

    def test_stored_energy_value_ignored(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        scheduling_input["parameters"].append(
            {"name": "stored_energy_value", "value": 50.0}
        )
        result = translate_scheduling(scheduling_input)
        ignored = [i for i in result.info if "stored_energy_value" in i]
        assert len(ignored) == 1

    def test_epsilon_ignored(self, scheduling_input: dict[str, Any]) -> None:
        scheduling_input["parameters"].append({"name": "epsilon", "value": 0.001})
        result = translate_scheduling(scheduling_input)
        ignored = [i for i in result.info if "epsilon" in i]
        assert len(ignored) == 1

    def test_solver_info_always_present(self, scheduling_input: dict[str, Any]) -> None:
        result = translate_scheduling(scheduling_input)
        solver_lines = [i for i in result.info if "solver:" in i]
        assert len(solver_lines) >= 1

    def test_no_parameters_csv_when_no_overrides(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        scheduling_input["parameters"] = []
        result = translate_scheduling(scheduling_input)
        assert result.parameters_csv is None


class TestTranslateIntraday:
    """Tests for translate_intraday."""

    def test_basic_output_shape(self, intraday_input: dict[str, Any]) -> None:
        result = translate_intraday(intraday_input)
        assert isinstance(result, TranslationResult)
        assert result.timeseries_csv
        assert result.n_segments == 1

    def test_csv_has_orderbook_columns(self, intraday_input: dict[str, Any]) -> None:
        result = translate_intraday(intraday_input)
        header = result.timeseries_csv.splitlines()[0]
        assert "committed_net_power" in header
        assert "bid_prices[1]" in header
        assert "ask_prices[1]" in header
        assert "bid_volumes[1]" in header
        assert "ask_volumes[1]" in header

    def test_segment_detection_from_market_config(
        self, intraday_input: dict[str, Any]
    ) -> None:
        intraday_input["markets"] = [{"name": "orderbook", "n_orderbook_segments": 3}]
        result = translate_intraday(intraday_input)
        assert result.n_segments == 3

    def test_segment_detection_fallback_to_timeseries(
        self, intraday_input: dict[str, Any]
    ) -> None:
        """When market config has no n_orderbook_segments, count timeseries."""
        intraday_input["markets"] = []
        result = translate_intraday(intraday_input)
        # Has orderbook[1]_price_in → should detect 1 segment
        assert result.n_segments == 1

    def test_transaction_cost_default(self, intraday_input: dict[str, Any]) -> None:
        result = translate_intraday(intraday_input)
        assert result.transaction_cost == 0.05

    def test_cycling_penalty_from_input(self, intraday_input: dict[str, Any]) -> None:
        intraday_input["parameters"] = [
            {"name": "cost_per_cycle", "value": 5.0},
        ]
        result = translate_intraday(intraday_input)
        assert result.cycling_penalty == 5.0


class TestTranslateSetpoints:
    """Tests for translate_setpoints."""

    def test_returns_market_position_as_setpoints(
        self, setpoints_input: dict[str, Any]
    ) -> None:
        result = translate_setpoints(setpoints_input)
        values = result["members"]["default"]["setpoints"]["values"]
        assert values == [5.0, -3.0, 0.0, 7.5]

    def test_shortcut_info_present(self, setpoints_input: dict[str, Any]) -> None:
        result = translate_setpoints(setpoints_input)
        info = result["_info"]
        shortcut_lines = [i for i in info if "shortcut:" in i]
        assert len(shortcut_lines) >= 1

    def test_empty_market_position(self) -> None:
        result = translate_setpoints(
            {"timeseries": [], "parameters": [], "markets": []}
        )
        values = result["members"]["default"]["setpoints"]["values"]
        assert values == []
        info = result["_info"]
        missing = [i for i in info if "no 'market_position'" in i]
        assert len(missing) == 1

    def test_info_inside_result(self, setpoints_input: dict[str, Any]) -> None:
        """_info must be inside the result dict (alongside members)."""
        result = translate_setpoints(setpoints_input)
        assert "members" in result
        assert "_info" in result
