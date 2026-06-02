"""Tests for service.translation modules."""

from __future__ import annotations

import copy
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
        cols = result.timeseries_csv.splitlines()[0].split(",")
        # Energy market columns must come first in this order.
        assert cols[:4] == ["time", "price", "grid_fee_in", "grid_fee_out"]
        # Reserve columns must all be present (order matters for CSV alignment
        # with the Modelica input variables).
        for required in (
            "fcr_position", "afrr_up_position", "afrr_down_position",
            "fcr_standby_price", "fcr_price",
            "afrr_up_standby_price", "afrr_up_price",
            "afrr_down_standby_price", "afrr_down_price",
            "fcr_activation_fraction", "afrr_activation_fraction",
        ):
            assert required in cols

    def test_csv_row_count_includes_endpoint(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """24 intervals → 26 rows (1 dummy + 24 + endpoint) + 1 header = 27 lines."""
        result = translate_scheduling(scheduling_input)
        lines = result.timeseries_csv.strip().splitlines()
        assert len(lines) == 27

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

    def test_stored_energy_value_propagated(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        scheduling_input["parameters"].append(
            {"name": "stored_energy_value", "value": 50.0}
        )
        result = translate_scheduling(scheduling_input)
        assert result.stored_energy_value == 50.0
        approx_msgs = [
            i
            for i in result.info
            if "stored_energy_value" in i and "approximation:" in i
        ]
        assert len(approx_msgs) == 1

    def test_stored_energy_value_defaults_to_zero(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        result = translate_scheduling(scheduling_input)
        assert result.stored_energy_value == 0.0

    def test_epsilon_ignored(self, scheduling_input: dict[str, Any]) -> None:
        scheduling_input["parameters"].append({"name": "epsilon", "value": 0.001})
        result = translate_scheduling(scheduling_input)
        ignored = [i for i in result.info if "epsilon" in i]
        assert len(ignored) == 1

    def test_solver_info_always_present(self, scheduling_input: dict[str, Any]) -> None:
        result = translate_scheduling(scheduling_input)
        solver_lines = [i for i in result.info if "solver:" in i]
        assert len(solver_lines) >= 1

    def test_cycling_penalty_converted_from_cost_per_cycle(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """cost_per_cycle / (2 * capacity) = 2.0 / (2 * 20.0) = 0.05."""
        result = translate_scheduling(scheduling_input)
        assert result.cycling_penalty == pytest.approx(0.05)
        conversion_msgs = [
            i
            for i in result.info
            if "cost_per_cycle" in i and "cycling_penalty_factor" in i
        ]
        assert len(conversion_msgs) == 1

    def test_no_parameters_csv_when_no_overrides(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        scheduling_input["parameters"] = []
        result = translate_scheduling(scheduling_input)
        assert result.parameters_csv is None

    def test_grid_fees_default_to_zero(self, scheduling_input: dict[str, Any]) -> None:
        """When grid_fee_in/grid_fee_out timeseries are absent, columns default to 0.0."""
        result = translate_scheduling(scheduling_input)
        lines = result.timeseries_csv.strip().splitlines()
        # Skip header, check all data rows have 0.0 for grid fees
        for line in lines[1:]:
            parts = line.split(",")
            assert parts[2] == "0.0"
            assert parts[3] == "0.0"

    def test_grid_fees_propagated(self, scheduling_input: dict[str, Any]) -> None:
        """When grid_fee_in/grid_fee_out are provided, values appear in CSV."""
        scheduling_input["timeseries"].extend(
            [
                {"name": "grid_fee_in", "values": [5.0] * 24},
                {"name": "grid_fee_out", "values": [3.0] * 24},
            ]
        )
        result = translate_scheduling(scheduling_input)
        lines = result.timeseries_csv.strip().splitlines()
        # Skip header and dummy row (index 0,1), check real data rows
        for line in lines[2:-1]:  # exclude endpoint row too
            parts = line.split(",")
            assert parts[2] == "5.0"
            assert parts[3] == "3.0"

    def test_grid_fees_info_when_present(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        scheduling_input["timeseries"].extend(
            [
                {"name": "grid_fee_in", "values": [5.0] * 24},
                {"name": "grid_fee_out", "values": [3.0] * 24},
            ]
        )
        result = translate_scheduling(scheduling_input)
        fee_in_msgs = [i for i in result.info if "grid_fee_in" in i and "applied:" in i]
        fee_out_msgs = [
            i for i in result.info if "grid_fee_out" in i and "applied:" in i
        ]
        assert len(fee_in_msgs) == 1
        assert len(fee_out_msgs) == 1

    def test_grid_fees_no_info_when_absent(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        result = translate_scheduling(scheduling_input)
        fee_msgs = [i for i in result.info if "grid_fee" in i and "applied:" in i]
        assert len(fee_msgs) == 0


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
        # committed_net_power has been split into two non-negative columns
        assert "committed_charge" in header
        assert "committed_discharge" in header
        assert "committed_net_power" not in header
        assert "grid_fee_in" in header
        assert "grid_fee_out" in header
        assert "bid_prices[1]" in header
        assert "ask_prices[1]" in header
        assert "bid_volumes[1]" in header
        assert "ask_volumes[1]" in header

    def test_committed_position_decomposed_correctly(
        self, intraday_input: dict[str, Any]
    ) -> None:
        """market_position values are split into non-negative charge/discharge columns.

        Positive net  = discharging: committed_discharge > 0, committed_charge = 0
        Negative net  = charging:    committed_charge > 0,    committed_discharge = 0
        Zero net      = idle:        both are 0
        """
        n = 8
        # Mix of positive (discharge), negative (charge), and zero positions
        positions = [5.0, -3.0, 0.0, 10.0, -7.5, 0.0, 2.0, -1.0]
        intraday_input["timeseries"] = [
            ts
            for ts in intraday_input["timeseries"]
            if ts.get("name") != "market_position"
        ]
        intraday_input["timeseries"].append(
            {"name": "market_position", "values": positions}
        )
        result = translate_intraday(intraday_input)

        lines = result.timeseries_csv.strip().splitlines()
        header = lines[0].split(",")
        ch_idx = header.index("committed_charge")
        dis_idx = header.index("committed_discharge")

        # Row 0 is the prepended dummy — both should be 0
        dummy = lines[1].split(",")
        assert float(dummy[ch_idx]) == 0.0
        assert float(dummy[dis_idx]) == 0.0

        # Rows 1..n are the real intervals
        for row_line, pos in zip(lines[2 : n + 2], positions):
            parts = row_line.split(",")
            committed_charge = float(parts[ch_idx])
            committed_discharge = float(parts[dis_idx])
            expected_charge = max(0.0, -pos)
            expected_discharge = max(0.0, pos)
            assert committed_charge == pytest.approx(expected_charge), (
                f"pos={pos}: expected committed_charge={expected_charge}, "
                f"got {committed_charge}"
            )
            assert committed_discharge == pytest.approx(expected_discharge), (
                f"pos={pos}: expected committed_discharge={expected_discharge}, "
                f"got {committed_discharge}"
            )

    def test_committed_position_decomposition_info_present(
        self, intraday_input: dict[str, Any]
    ) -> None:
        """An _info entry is emitted when market_position is non-zero."""
        result = translate_intraday(intraday_input)
        # The fixture has market_position = [5.0] * n (non-zero)
        decomp_msgs = [
            i for i in result.info if "committed_charge" in i and "applied:" in i
        ]
        assert len(decomp_msgs) == 1

    def test_zero_market_position_no_decomposition_info(self) -> None:
        """No _info entry emitted when market_position is all zeros."""
        from tests.conftest import _make_qh_timestamps

        n = 4
        starts, ends = _make_qh_timestamps(n)
        model_input = {
            "interval_start": starts,
            "interval_end": ends,
            "timeseries": [
                {"name": "market_position", "values": [0.0] * n},
                {"name": "state_of_charge", "values": [10.0]},
                {"name": "orderbook[1]_price_in", "values": [50.0] * n},
                {"name": "orderbook[1]_price_out", "values": [40.0] * n},
                {"name": "orderbook[1]_max_power_in", "values": [10.0] * n},
                {"name": "orderbook[1]_max_power_out", "values": [10.0] * n},
            ],
            "parameters": [{"name": "battery_capacity", "value": 20.0}],
            "markets": [{"name": "orderbook", "n_orderbook_segments": 1}],
        }
        result = translate_intraday(model_input)
        decomp_msgs = [i for i in result.info if "committed_charge" in i]
        assert len(decomp_msgs) == 0

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

    def test_cycling_penalty_converted_from_cost_per_cycle(
        self, intraday_input: dict[str, Any]
    ) -> None:
        """cost_per_cycle / (2 * capacity) = 2.0 / (2 * 20.0) = 0.05."""
        result = translate_intraday(intraday_input)
        assert result.cycling_penalty == pytest.approx(0.05)
        conversion_msgs = [
            i
            for i in result.info
            if "cost_per_cycle" in i and "cycling_penalty_factor" in i
        ]
        assert len(conversion_msgs) == 1

    def test_cycling_penalty_passthrough_without_capacity(
        self, intraday_input: dict[str, Any]
    ) -> None:
        """Without capacity, cost_per_cycle is passed through unchanged."""
        intraday_input["parameters"] = [
            {"name": "cost_per_cycle", "value": 5.0},
        ]
        result = translate_intraday(intraday_input)
        assert result.cycling_penalty == 5.0

    def test_stored_energy_value_propagated(
        self, intraday_input: dict[str, Any]
    ) -> None:
        intraday_input["parameters"].append(
            {"name": "stored_energy_value", "value": 70.0}
        )
        result = translate_intraday(intraday_input)
        assert result.stored_energy_value == 70.0
        approx_msgs = [
            i
            for i in result.info
            if "stored_energy_value" in i and "approximation:" in i
        ]
        assert len(approx_msgs) == 1

    def test_stored_energy_value_defaults_to_zero(
        self, intraday_input: dict[str, Any]
    ) -> None:
        result = translate_intraday(intraday_input)
        assert result.stored_energy_value == 0.0

    def test_grid_fees_default_to_zero(self, intraday_input: dict[str, Any]) -> None:
        """When grid_fee_in/grid_fee_out timeseries are absent, columns default to 0.0."""
        result = translate_intraday(intraday_input)
        lines = result.timeseries_csv.strip().splitlines()
        header = lines[0].split(",")
        fee_in_idx = header.index("grid_fee_in")
        fee_out_idx = header.index("grid_fee_out")
        for line in lines[1:]:
            parts = line.split(",")
            assert parts[fee_in_idx] == "0.0"
            assert parts[fee_out_idx] == "0.0"

    def test_grid_fees_propagated(self, intraday_input: dict[str, Any]) -> None:
        """When grid_fee_in/grid_fee_out are provided, values appear in CSV."""
        n = 8
        intraday_input["timeseries"].extend(
            [
                {"name": "grid_fee_in", "values": [7.0] * n},
                {"name": "grid_fee_out", "values": [4.0] * n},
            ]
        )
        result = translate_intraday(intraday_input)
        lines = result.timeseries_csv.strip().splitlines()
        header = lines[0].split(",")
        fee_in_idx = header.index("grid_fee_in")
        fee_out_idx = header.index("grid_fee_out")
        # Skip header and dummy row, check real data rows
        for line in lines[2:-1]:
            parts = line.split(",")
            assert parts[fee_in_idx] == "7.0"
            assert parts[fee_out_idx] == "4.0"

    def test_grid_fees_info_when_present(self, intraday_input: dict[str, Any]) -> None:
        n = 8
        intraday_input["timeseries"].extend(
            [
                {"name": "grid_fee_in", "values": [7.0] * n},
                {"name": "grid_fee_out", "values": [4.0] * n},
            ]
        )
        result = translate_intraday(intraday_input)
        fee_in_msgs = [i for i in result.info if "grid_fee_in" in i and "applied:" in i]
        fee_out_msgs = [
            i for i in result.info if "grid_fee_out" in i and "applied:" in i
        ]
        assert len(fee_in_msgs) == 1
        assert len(fee_out_msgs) == 1

    def test_grid_fees_no_info_when_absent(
        self, intraday_input: dict[str, Any]
    ) -> None:
        result = translate_intraday(intraday_input)
        fee_msgs = [i for i in result.info if "grid_fee" in i and "applied:" in i]
        assert len(fee_msgs) == 0


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


class TestGridFeeSolverIntegration:
    """Integration tests that run the actual RTC-Tools solver to verify grid fees
    influence the optimization result.

    These tests are NOT mocked — they execute the full solver pipeline.
    """

    def test_high_grid_fees_suppress_trading(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """With grid fees exceeding the max price spread, the battery should not trade.

        Price range in fixture is 22–90 EUR/MWh (spread=68).  Setting
        grid_fee_in=500 and grid_fee_out=500 makes every possible trade
        unprofitable, so charge and discharge power should be zero everywhere.
        """
        from service.solver_runner import run_solver

        scheduling_input = copy.deepcopy(scheduling_input)
        n = len(scheduling_input["interval_start"])
        scheduling_input["timeseries"].extend(
            [
                {"name": "grid_fee_in", "values": [500.0] * n},
                {"name": "grid_fee_out", "values": [500.0] * n},
            ]
        )

        result = run_solver("scheduling", scheduling_input)
        members = result["result"]["members"]["default"]

        charge_values = members["day_ahead_power_in"]["values"]
        discharge_values = members["day_ahead_power_out"]["values"]

        for v in charge_values:
            assert v == pytest.approx(0.0, abs=0.01), (
                f"Expected no charging with prohibitive grid fees, got {v}"
            )
        for v in discharge_values:
            assert v == pytest.approx(0.0, abs=0.01), (
                f"Expected no discharging with prohibitive grid fees, got {v}"
            )

    def test_zero_grid_fees_allow_trading(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """With zero grid fees the battery should actively trade the price spread."""
        from service.solver_runner import run_solver

        scheduling_input = copy.deepcopy(scheduling_input)
        n = len(scheduling_input["interval_start"])
        scheduling_input["timeseries"].extend(
            [
                {"name": "grid_fee_in", "values": [0.0] * n},
                {"name": "grid_fee_out", "values": [0.0] * n},
            ]
        )

        result = run_solver("scheduling", scheduling_input)
        members = result["result"]["members"]["default"]

        charge_values = members["day_ahead_power_in"]["values"]
        discharge_values = members["day_ahead_power_out"]["values"]

        total_charge = sum(charge_values)
        total_discharge = sum(discharge_values)

        assert total_charge > 1.0, (
            f"Expected active charging with zero grid fees, got total={total_charge}"
        )
        assert total_discharge > 1.0, (
            f"Expected active discharging with zero grid fees, got total={total_discharge}"
        )

    def test_grid_fees_reduce_trading_volume(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """Moderate grid fees should reduce total traded volume compared to zero fees."""
        from service.solver_runner import run_solver

        base_input = copy.deepcopy(scheduling_input)
        n = len(base_input["interval_start"])

        # Run without grid fees
        no_fee_input = copy.deepcopy(base_input)
        no_fee_input["timeseries"].extend(
            [
                {"name": "grid_fee_in", "values": [0.0] * n},
                {"name": "grid_fee_out", "values": [0.0] * n},
            ]
        )
        no_fee_result = run_solver("scheduling", no_fee_input)
        no_fee_members = no_fee_result["result"]["members"]["default"]
        no_fee_volume = sum(no_fee_members["day_ahead_power_in"]["values"]) + sum(
            no_fee_members["day_ahead_power_out"]["values"]
        )

        # Run with moderate grid fees (20 EUR/MWh each direction)
        fee_input = copy.deepcopy(base_input)
        fee_input["timeseries"].extend(
            [
                {"name": "grid_fee_in", "values": [20.0] * n},
                {"name": "grid_fee_out", "values": [20.0] * n},
            ]
        )
        fee_result = run_solver("scheduling", fee_input)
        fee_members = fee_result["result"]["members"]["default"]
        fee_volume = sum(fee_members["day_ahead_power_in"]["values"]) + sum(
            fee_members["day_ahead_power_out"]["values"]
        )

        assert fee_volume < no_fee_volume, (
            f"Expected grid fees to reduce trading volume: "
            f"with_fees={fee_volume:.1f}, without_fees={no_fee_volume:.1f}"
        )
