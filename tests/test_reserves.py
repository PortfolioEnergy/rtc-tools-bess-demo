"""Tests for FCR / aFRR reserve-market integration."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from service.solver_runner import run_solver
from service.translation.pe_to_rtc import (
    _detect_blocks_from_runs,
    translate_scheduling,
)


# ── pe_to_rtc tests ───────────────────────────────────────────────────


class TestReserveTranslation:
    """Pure translation-layer behaviour: no solver invocations."""

    def test_no_reserve_markets_leaves_all_closed(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """Existing inputs without reserve markets keep every product closed."""
        result = translate_scheduling(scheduling_input)
        for product in ("fcr", "afrr_up", "afrr_down"):
            assert result.reserve_config[product]["open"] is False
            assert result.reserve_config[product]["blocks"] == []
        assert result.skip_counterfactual_reserves is False

    def test_open_fcr_market_requires_activation_fraction(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """An open FCR market without an activation_fraction timeseries 422s."""
        scheduling_input = copy.deepcopy(scheduling_input)
        n = len(scheduling_input["interval_start"])
        scheduling_input["timeseries"].append(
            {"name": "fcr_standby_price", "values": [10.0] * n}
        )
        scheduling_input["markets"].append(
            {
                "name": "fcr",
                "type": "ancillary_offer_stack",
                "activation_duration": 900,
            }
        )
        with pytest.raises(ValueError, match="fcr_activation_fraction"):
            translate_scheduling(scheduling_input)

    def test_open_afrr_up_market_requires_activation_fraction(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """An open aFRR up market without afrr_activation_fraction 422s."""
        scheduling_input = copy.deepcopy(scheduling_input)
        n = len(scheduling_input["interval_start"])
        scheduling_input["timeseries"].extend(
            [
                {"name": "afrr_up_standby_price", "values": [15.0] * n},
                {"name": "afrr_up_price", "values": [80.0] * n},
            ]
        )
        scheduling_input["markets"].append(
            {
                "name": "afrr_up",
                "type": "afrr_capacity",
                "activation_duration": 900,
            }
        )
        with pytest.raises(ValueError, match="afrr_activation_fraction"):
            translate_scheduling(scheduling_input)

    def test_open_fcr_market_populates_reserve_config(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """Standby-price runs become bid blocks; activation_duration → t_min_hours."""
        scheduling_input = copy.deepcopy(scheduling_input)
        n = len(scheduling_input["interval_start"])
        # 24 hourly PTUs → 4h blocks → 6 blocks of identical standby price
        block_prices = [10.0] * 4 + [12.0] * 4 + [8.0] * 4 + [11.0] * 4 + [9.0] * 4 + [7.0] * 4
        scheduling_input["timeseries"].extend(
            [
                {"name": "fcr_standby_price", "values": block_prices},
                {"name": "fcr_activation_fraction", "values": [0.10] * n},
            ]
        )
        scheduling_input["markets"].append(
            {
                "name": "fcr",
                "type": "ancillary_offer_stack",
                "activation_duration": 900,  # 15 min
                "n_price_bands": 1,
            }
        )
        result = translate_scheduling(scheduling_input)
        assert result.reserve_config["fcr"]["open"] is True
        assert result.reserve_config["fcr"]["t_min_hours"] == pytest.approx(0.25)
        # Six runs of length 4 → six blocks
        blocks = result.reserve_config["fcr"]["blocks"]
        assert len(blocks) == 6
        for blk in blocks:
            assert len(blk) == 4

    def test_block_detection_from_runs(self) -> None:
        """_detect_blocks_from_runs groups consecutive identical values."""
        assert _detect_blocks_from_runs([]) == []
        assert _detect_blocks_from_runs([5.0]) == [[0]]
        assert _detect_blocks_from_runs([1.0, 1.0, 2.0, 2.0]) == [[0, 1], [2, 3]]
        # Single-PTU blocks when every value differs
        out = _detect_blocks_from_runs([1.0, 2.0, 3.0])
        assert out == [[0], [1], [2]]

    def test_committed_fcr_position_populates_csv_column(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """fcr_position timeseries → fcr_position column with the same values."""
        scheduling_input = copy.deepcopy(scheduling_input)
        n = len(scheduling_input["interval_start"])
        scheduling_input["timeseries"].append(
            {"name": "fcr_position", "values": [2.5] * n}
        )
        result = translate_scheduling(scheduling_input)
        lines = result.timeseries_csv.strip().splitlines()
        header = lines[0].split(",")
        idx = header.index("fcr_position")
        # Dummy row at lines[1] is zero
        assert float(lines[1].split(",")[idx]) == 0.0
        # Real rows carry 2.5
        for line in lines[2:-1]:
            assert float(line.split(",")[idx]) == 2.5

    def test_skip_counterfactual_parameter_recognised(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """skip_counterfactual_reserves parameter is captured on the result."""
        scheduling_input = copy.deepcopy(scheduling_input)
        scheduling_input["parameters"].append(
            {"name": "skip_counterfactual_reserves", "value": 1.0}
        )
        result = translate_scheduling(scheduling_input)
        assert result.skip_counterfactual_reserves is True


# ── solver tests ──────────────────────────────────────────────────────


class TestReserveSolver:
    """Integration tests that run the actual scheduling solver."""

    def _open_fcr_input(
        self, scheduling_input: dict[str, Any], *,
        standby_price: float = 10.0,
        activation_fraction: float = 0.05,
        activation_duration_s: int = 900,
        committed_mw: float = 0.0,
    ) -> dict[str, Any]:
        """Build a scheduling input with FCR open and standard parameters."""
        cfg = copy.deepcopy(scheduling_input)
        n = len(cfg["interval_start"])
        cfg["timeseries"].extend(
            [
                {"name": "fcr_standby_price", "values": [standby_price] * n},
                {"name": "fcr_activation_fraction", "values": [activation_fraction] * n},
                {"name": "fcr_position", "values": [committed_mw] * n},
            ]
        )
        cfg["markets"].append(
            {
                "name": "fcr",
                "type": "ancillary_offer_stack",
                "activation_duration": activation_duration_s,
                "n_price_bands": 1,
            }
        )
        return cfg

    def test_open_fcr_market_with_high_standby_price_yields_nonzero_bid(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """High standby price + slack SoC → solver should bid some FCR MW."""
        cfg = self._open_fcr_input(
            scheduling_input, standby_price=200.0, activation_fraction=0.05
        )
        out = run_solver("scheduling", cfg)["result"]["members"]["default"]
        total_bid = sum(out["bid_fcr_total"]["values"])
        assert total_bid > 0.1, (
            f"Expected non-zero FCR bid with 200 EUR/MW/h standby, got {total_bid}"
        )

    def test_closed_fcr_market_yields_zero_bid(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """No fcr market in markets[] → bid_fcr_total is zero everywhere."""
        # Note: no fcr market added; existing scheduling_input has none
        out = run_solver("scheduling", scheduling_input)["result"]["members"]["default"]
        assert all(v == 0.0 for v in out["bid_fcr_total"]["values"])

    def test_bid_constant_within_blocks(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """Block-equality keeps the bid value constant inside each block."""
        cfg = copy.deepcopy(scheduling_input)
        n = len(cfg["interval_start"])
        # Two blocks of 12 PTUs each with different standby prices
        prices = [100.0] * 12 + [200.0] * 12
        cfg["timeseries"].extend(
            [
                {"name": "fcr_standby_price", "values": prices},
                {"name": "fcr_activation_fraction", "values": [0.05] * n},
            ]
        )
        cfg["markets"].append(
            {
                "name": "fcr",
                "type": "ancillary_offer_stack",
                "activation_duration": 900,
            }
        )
        out = run_solver("scheduling", cfg)["result"]["members"]["default"]
        bid = out["bid_fcr_total"]["values"]
        block1 = bid[:12]
        block2 = bid[12:24]
        # All values within a block must match (block equality)
        for v in block1:
            assert v == pytest.approx(block1[0], abs=1e-6)
        for v in block2:
            assert v == pytest.approx(block2[0], abs=1e-6)

    def test_committed_fcr_reduces_inverter_headroom(
        self, scheduling_input: dict[str, Any]
    ) -> None:
        """Committed FCR caps the max charge / discharge power for arbitrage."""
        # Without any reserves
        base_out = run_solver("scheduling", scheduling_input)["result"]["members"][
            "default"
        ]
        peak_charge = max(base_out["day_ahead_power_in"]["values"])
        peak_discharge = max(base_out["day_ahead_power_out"]["values"])

        # With 5 MW committed FCR (battery max_power = 10 MW)
        cfg = self._open_fcr_input(
            scheduling_input, standby_price=0.0, committed_mw=5.0,
        )
        with_commit = run_solver("scheduling", cfg)["result"]["members"]["default"]
        peak_charge_c = max(with_commit["day_ahead_power_in"]["values"])
        peak_discharge_c = max(with_commit["day_ahead_power_out"]["values"])
        # Inverter headroom should drop by ~5 MW (constrained at 10 - 5 = 5 MW)
        assert peak_charge_c <= 5.0 + 1e-3, (
            f"Charge headroom should be capped at 5 MW, got {peak_charge_c}"
        )
        assert peak_discharge_c <= 5.0 + 1e-3, (
            f"Discharge headroom should be capped at 5 MW, got {peak_discharge_c}"
        )
        # Total energy moved should also drop versus the no-reserves case
        assert peak_charge_c < peak_charge + 1e-3
        assert peak_discharge_c < peak_discharge + 1e-3


# ── end-to-end API test ──────────────────────────────────────────────


class TestReserveAPI:
    """End-to-end FastAPI surface tests."""

    def test_open_fcr_via_api(
        self, client, scheduling_input: dict[str, Any]
    ) -> None:
        """POSTing a scheduling request with open FCR returns reserve outputs."""
        cfg = copy.deepcopy(scheduling_input)
        n = len(cfg["interval_start"])
        cfg["timeseries"].extend(
            [
                {"name": "fcr_standby_price", "values": [150.0] * n},
                {"name": "fcr_activation_fraction", "values": [0.05] * n},
            ]
        )
        cfg["markets"].append(
            {
                "name": "fcr",
                "type": "ancillary_offer_stack",
                "activation_duration": 900,
                "n_price_bands": 1,
            }
        )
        resp = client.post(
            "/v1/models/bess_day_ahead/submit_sync",
            json={"model_input_data": cfg, "include_diagnostics": False},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        members = body["result"]["members"]["default"]
        # New reserve outputs must be present
        for key in (
            "bid_fcr_total", "total_fcr", "fcr_position", "fcr_power_out",
            "bid_afrr_up_total", "bid_afrr_down_total",
        ):
            assert key in members, f"missing output member: {key}"
        # The single-band wire shape aliases bid_fcr_total
        assert members["fcr_power_out"]["values"] == members["bid_fcr_total"]["values"]

    def test_missing_activation_fraction_yields_422(
        self, client, scheduling_input: dict[str, Any]
    ) -> None:
        """ValueError from translation surfaces as HTTP 422."""
        cfg = copy.deepcopy(scheduling_input)
        n = len(cfg["interval_start"])
        cfg["timeseries"].append(
            {"name": "fcr_standby_price", "values": [100.0] * n}
        )
        cfg["markets"].append(
            {
                "name": "fcr",
                "type": "ancillary_offer_stack",
                "activation_duration": 900,
            }
        )
        resp = client.post(
            "/v1/models/bess_day_ahead/submit_sync",
            json={"model_input_data": cfg, "include_diagnostics": False},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "fcr_activation_fraction" in detail["message"]

    def test_reasoning_markdown_includes_reserve_sections(
        self, client, scheduling_input: dict[str, Any]
    ) -> None:
        """When diagnostics enabled, reserve sections appear in markdown."""
        cfg = copy.deepcopy(scheduling_input)
        n = len(cfg["interval_start"])
        cfg["timeseries"].extend(
            [
                {"name": "fcr_standby_price", "values": [200.0] * n},
                {"name": "fcr_activation_fraction", "values": [0.05] * n},
                # Skip counterfactual to keep the test fast
            ]
        )
        cfg["parameters"].append(
            {"name": "skip_counterfactual_reserves", "value": 1.0}
        )
        cfg["markets"].append(
            {
                "name": "fcr",
                "type": "ancillary_offer_stack",
                "activation_duration": 900,
                "n_price_bands": 1,
            }
        )
        resp = client.post(
            "/v1/models/bess_day_ahead/submit_sync",
            json={"model_input_data": cfg, "include_diagnostics": True},
        )
        assert resp.status_code == 200, resp.text
        markdown = resp.json().get("reasoning_markdown", "")
        # Reserve sections must appear when there are open markets with bids
        assert "## Reserve Bids" in markdown
        # Counterfactual section says it was skipped
        assert "skip_counterfactual_reserves" in markdown

    def test_counterfactual_section_when_enabled(
        self, client, scheduling_input: dict[str, Any]
    ) -> None:
        """When skip flag is 0 (default), the counterfactual re-solve runs and
        produces a comparison table in the markdown."""
        cfg = copy.deepcopy(scheduling_input)
        n = len(cfg["interval_start"])
        cfg["timeseries"].extend(
            [
                {"name": "fcr_standby_price", "values": [200.0] * n},
                {"name": "fcr_activation_fraction", "values": [0.05] * n},
            ]
        )
        cfg["markets"].append(
            {
                "name": "fcr",
                "type": "ancillary_offer_stack",
                "activation_duration": 900,
                "n_price_bands": 1,
            }
        )
        resp = client.post(
            "/v1/models/bess_day_ahead/submit_sync",
            json={"model_input_data": cfg, "include_diagnostics": True},
        )
        assert resp.status_code == 200, resp.text
        markdown = resp.json().get("reasoning_markdown", "")
        assert "## Counterfactual" in markdown
        assert "Without reserves" in markdown
        assert "Δ" in markdown


# ── intraday-side smoke test for committed reserves ──────────────────


class TestReserveIntraday:
    """Intraday consumes committed reserves but never bids."""

    def test_committed_fcr_emitted_in_intraday_output(
        self, client, intraday_input: dict[str, Any]
    ) -> None:
        """Intraday output exposes fcr_position passthrough + zero bids."""
        cfg = copy.deepcopy(intraday_input)
        n = len(cfg["interval_start"])
        cfg["timeseries"].append({"name": "fcr_position", "values": [2.0] * n})
        resp = client.post(
            "/v1/models/bess_rolling/submit_sync",
            json={"model_input_data": cfg, "include_diagnostics": False},
        )
        assert resp.status_code == 200, resp.text
        members = resp.json()["result"]["members"]["default"]
        # Position passes through unchanged
        assert all(
            v == pytest.approx(2.0) for v in members["fcr_position"]["values"]
        )
        # Bid total is always zero in intraday (solver pins it)
        assert all(v == pytest.approx(0.0, abs=1e-6) for v in members["bid_fcr_total"]["values"])
        # Total reflects committed + bid
        assert all(
            v == pytest.approx(2.0) for v in members["total_fcr"]["values"]
        )
