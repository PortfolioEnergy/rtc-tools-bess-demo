"""Tests for aFRR energy bid pricing (post-solve marginal cost computation)."""

from __future__ import annotations

import copy
import math
from typing import Any

import numpy as np
import pandas as pd
import pytest

from service.translation.pe_to_rtc import (
    TranslationResult,
    _extract_afrr_energy_market,
    _parse_iso_utc,
    translate_intraday,
)
from service.translation.rtc_to_pe import _compute_afrr_energy_bids


# ── _extract_afrr_energy_market tests ─────────────────────────────────


class TestExtractAfrrEnergyMarket:
    """Input parsing for aFRR energy bid market."""

    def test_no_market_returns_defaults(self) -> None:
        """No afrr_energy market entry → all zeros, no open PTUs."""
        model_input: dict[str, Any] = {"markets": [], "timeseries": [], "parameters": []}
        ptu_starts = [_parse_iso_utc(f"2025-08-01T{h:02d}:00:00Z") for h in range(4)]
        info: list[str] = []

        up, down, mask, n_bands, markup, grid = _extract_afrr_energy_market(
            model_input, ptu_starts, info
        )

        assert up == [0.0] * 4
        assert down == [0.0] * 4
        assert mask == [False] * 4
        assert n_bands == 0
        assert markup == 0.0
        assert grid is None
        assert info == []

    def test_market_with_obligations_populates_fields(self) -> None:
        """afrr_energy market with obligation timeseries → correct extraction."""
        ptu_starts_iso = [f"2025-08-01T{h:02d}:00:00Z" for h in range(4)]
        ptu_starts = [_parse_iso_utc(t) for t in ptu_starts_iso]

        # Obligations on PTUs 1 and 2 only (their own grid)
        model_input: dict[str, Any] = {
            "markets": [
                {"name": "afrr_energy", "type": "afrr_energy_bid", "n_price_bands": 2}
            ],
            "timeseries": [
                {
                    "name": "afrr_energy_obligation_up",
                    "values": [10.0, 10.0],
                    "interval_start": ["2025-08-01T01:00:00Z", "2025-08-01T02:00:00Z"],
                    "interval_end": ["2025-08-01T02:00:00Z", "2025-08-01T03:00:00Z"],
                },
                {
                    "name": "afrr_energy_obligation_down",
                    "values": [5.0, 5.0],
                    "interval_start": ["2025-08-01T01:00:00Z", "2025-08-01T02:00:00Z"],
                    "interval_end": ["2025-08-01T02:00:00Z", "2025-08-01T03:00:00Z"],
                },
            ],
            "parameters": [
                {"name": "afrr_energy_markup", "value": 3.0},
            ],
        }
        info: list[str] = []

        up, down, mask, n_bands, markup, grid = _extract_afrr_energy_market(
            model_input, ptu_starts, info
        )

        assert up == [0.0, 10.0, 10.0, 0.0]
        assert down == [0.0, 5.0, 5.0, 0.0]
        assert mask == [False, True, True, False]
        assert n_bands == 2
        assert markup == 3.0
        assert grid is not None
        assert len(grid["interval_start"]) == 2

    def test_translate_intraday_includes_afrr_energy_fields(
        self, intraday_input: dict[str, Any]
    ) -> None:
        """translate_intraday populates aFRR energy fields on TranslationResult."""
        inp = copy.deepcopy(intraday_input)
        n = len(inp["interval_start"])

        inp["markets"].append(
            {"name": "afrr_energy", "type": "afrr_energy_bid", "n_price_bands": 1}
        )
        inp["timeseries"].append(
            {"name": "afrr_energy_obligation_up", "values": [8.0] * n}
        )
        inp["timeseries"].append(
            {"name": "afrr_energy_obligation_down", "values": [6.0] * n}
        )
        inp["parameters"].append({"name": "afrr_energy_markup", "value": 2.0})

        result = translate_intraday(inp)

        assert result.afrr_energy_obligation_up == [8.0] * n
        assert result.afrr_energy_obligation_down == [6.0] * n
        assert result.afrr_energy_open_mask == [True] * n
        assert result.afrr_energy_n_bands == 1
        assert result.afrr_energy_markup == 2.0


# ── _compute_afrr_energy_bids tests ──────────────────────────────────


class TestComputeAfrrEnergyBids:
    """Marginal cost computation for aFRR energy bid prices."""

    def _make_dfs(
        self, n: int, bid_price: float = 40.0, ask_price: float = 50.0
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Create minimal solver output and input DataFrames."""
        df_output = pd.DataFrame({
            "soc": [10.0] * n,
            "charge_power": [0.0] * n,
            "discharge_power": [0.0] * n,
        })
        df_input = pd.DataFrame({
            "bid_prices[1]": [bid_price] * n,
            "ask_prices[1]": [ask_price] * n,
            "grid_fee_in": [1.0] * n,
            "grid_fee_out": [2.0] * n,
        })
        return df_output, df_input

    def test_marginal_cost_formula_up(self) -> None:
        """Up-direction price follows the formula exactly."""
        n = 4
        df_out, df_in = self._make_dfs(n, bid_price=40.0, ask_price=50.0)
        efficiency = 0.9
        stored_energy_value = 30.0
        cycling_penalty = 0.5
        markup = 5.0

        obligation_up = [10.0] * n
        obligation_down = [0.0] * n
        open_mask = [True] * n
        info: list[str] = []

        members = _compute_afrr_energy_bids(
            df_out, df_in, 1,
            obligation_up, obligation_down, open_mask,
            1, markup, cycling_penalty, stored_energy_value, efficiency, None, info,
        )

        sqrt_eff = math.sqrt(efficiency)
        ref_price = (40.0 + 50.0) / 2.0  # 45.0
        expected_eff_loss_up = (1.0 / sqrt_eff - 1.0) * ref_price
        expected_price_up = (
            stored_energy_value + cycling_penalty + expected_eff_loss_up + 2.0 + markup
        )

        assert "afrr_energy_up_price[1]" in members
        actual_prices = members["afrr_energy_up_price[1]"]["values"]
        assert actual_prices[0] == pytest.approx(expected_price_up, rel=1e-6)

    def test_marginal_cost_formula_down(self) -> None:
        """Down-direction price follows the formula exactly."""
        n = 4
        df_out, df_in = self._make_dfs(n, bid_price=40.0, ask_price=50.0)
        efficiency = 0.9
        stored_energy_value = 30.0
        cycling_penalty = 0.5
        markup = 5.0

        obligation_up = [0.0] * n
        obligation_down = [8.0] * n
        open_mask = [True] * n
        info: list[str] = []

        members = _compute_afrr_energy_bids(
            df_out, df_in, 1,
            obligation_up, obligation_down, open_mask,
            1, markup, cycling_penalty, stored_energy_value, efficiency, None, info,
        )

        sqrt_eff = math.sqrt(efficiency)
        ref_price = (40.0 + 50.0) / 2.0  # 45.0
        expected_eff_loss_down = (1.0 - sqrt_eff) * ref_price
        expected_price_down = (
            -stored_energy_value + cycling_penalty - expected_eff_loss_down + 1.0 + markup
        )

        assert "afrr_energy_down_price[1]" in members
        actual_prices = members["afrr_energy_down_price[1]"]["values"]
        assert actual_prices[0] == pytest.approx(expected_price_down, rel=1e-6)

    def test_volumes_equal_obligations(self) -> None:
        """Output volumes match the obligation inputs exactly."""
        n = 3
        df_out, df_in = self._make_dfs(n)
        obligation_up = [10.0, 0.0, 5.0]
        obligation_down = [7.0, 3.0, 0.0]
        open_mask = [True, True, True]
        info: list[str] = []

        members = _compute_afrr_energy_bids(
            df_out, df_in, 1,
            obligation_up, obligation_down, open_mask,
            1, 0.0, 0.5, 30.0, 0.9, None, info,
        )

        assert members["afrr_energy_up_volume[1]"]["values"] == [10.0, 0.0, 5.0]
        assert members["afrr_energy_down_volume[1]"]["values"] == [7.0, 3.0, 0.0]

    def test_closed_ptus_have_zero_prices(self) -> None:
        """PTUs where open_mask is False have zero price and volume."""
        n = 4
        df_out, df_in = self._make_dfs(n)
        obligation_up = [10.0, 10.0, 10.0, 10.0]
        obligation_down = [5.0, 5.0, 5.0, 5.0]
        open_mask = [False, True, False, True]
        info: list[str] = []

        members = _compute_afrr_energy_bids(
            df_out, df_in, 1,
            obligation_up, obligation_down, open_mask,
            1, 0.0, 0.5, 30.0, 0.9, None, info,
        )

        prices_up = members["afrr_energy_up_price[1]"]["values"]
        assert prices_up[0] == 0.0
        assert prices_up[2] == 0.0
        assert prices_up[1] != 0.0
        assert prices_up[3] != 0.0

    def test_multi_band_fills_zeros_beyond_band_1(self) -> None:
        """Bands 2+ have zero price and zero volume."""
        n = 2
        df_out, df_in = self._make_dfs(n)
        info: list[str] = []

        members = _compute_afrr_energy_bids(
            df_out, df_in, 1,
            [10.0] * n, [5.0] * n, [True] * n,
            3, 0.0, 0.5, 30.0, 0.9, None, info,
        )

        assert "afrr_energy_up_price[2]" in members
        assert "afrr_energy_up_price[3]" in members
        assert members["afrr_energy_up_price[2]"]["values"] == [0.0, 0.0]
        assert members["afrr_energy_up_volume[2]"]["values"] == [0.0, 0.0]
        assert members["afrr_energy_down_price[3]"]["values"] == [0.0, 0.0]
        assert members["afrr_energy_down_volume[3]"]["values"] == [0.0, 0.0]

    def test_info_contains_decomposition(self) -> None:
        """_info entries contain the full price decomposition for traceability."""
        n = 2
        df_out, df_in = self._make_dfs(n)
        info: list[str] = []

        _compute_afrr_energy_bids(
            df_out, df_in, 1,
            [10.0] * n, [5.0] * n, [True, False],
            1, 5.0, 0.5, 30.0, 0.9, None, info,
        )

        # Only PTU 0 is open → should have exactly 2 info entries (up + down)
        bid_info = [i for i in info if i.startswith("afrr_energy_bid_")]
        assert len(bid_info) == 2
        assert "opportunity_cost(30.00)" in bid_info[0]
        assert "cycling(0.50)" in bid_info[0]
        assert "markup(5.00)" in bid_info[0]

    def test_no_open_ptus_returns_empty(self) -> None:
        """When no PTUs are open, returns empty dict."""
        n = 3
        df_out, df_in = self._make_dfs(n)
        info: list[str] = []

        members = _compute_afrr_energy_bids(
            df_out, df_in, 1,
            [0.0] * n, [0.0] * n, [False] * n,
            1, 0.0, 0.5, 30.0, 0.9, None, info,
        )

        assert members == {}

    def test_grid_shaping(self) -> None:
        """When a grid is provided, output values are collapsed onto it."""
        n = 4
        df_out, df_in = self._make_dfs(n)
        # Grid covers PTUs 1 and 2 (2 blocks)
        grid = {
            "interval_start": ["2025-08-01T01:00:00Z", "2025-08-01T02:00:00Z"],
            "interval_end": ["2025-08-01T02:00:00Z", "2025-08-01T03:00:00Z"],
            "blocks": [[1], [2]],
        }
        open_mask = [False, True, True, False]
        info: list[str] = []

        members = _compute_afrr_energy_bids(
            df_out, df_in, 1,
            [0.0, 10.0, 10.0, 0.0],
            [0.0, 5.0, 5.0, 0.0],
            open_mask,
            1, 0.0, 0.5, 30.0, 0.9, grid, info,
        )

        # Grid has 2 blocks → output should have 2 values
        assert len(members["afrr_energy_up_price[1]"]["values"]) == 2
        assert len(members["afrr_energy_up_volume[1]"]["values"]) == 2
        assert "interval_start" in members["afrr_energy_up_price[1]"]
        assert "interval_end" in members["afrr_energy_up_price[1]"]
