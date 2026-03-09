"""Unit tests for service/translation/diagnostics.py.

These tests verify that:
- Each chart function returns a valid base64-encoded PNG data URI.
- Chart functions degrade gracefully when optional data is absent
  (e.g. no grid-fee columns, no Lagrange multipliers available).
- The public entry points (build_scheduling_diagnostics,
  build_intraday_diagnostics) return the expected keys and _info entries.
- No chart failure propagates as an exception — errors are captured in
  the _info list.

We avoid running the real RTC-Tools solver by constructing minimal stub
DataFrames and a lightweight mock problem object.
"""

from __future__ import annotations

import base64
import io
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_scheduling_output(n: int = 24) -> pd.DataFrame:
    """Return a minimal timeseries_export DataFrame for a scheduling run."""
    t_start = pd.Timestamp("2025-08-01 00:00:00")
    times = pd.date_range(t_start, periods=n, freq="h")
    rng = np.random.default_rng(42)
    charge = np.where(np.arange(n) < 8, rng.uniform(0, 8, n), 0.0)
    discharge = np.where(np.arange(n) >= 16, rng.uniform(0, 8, n), 0.0)
    soc = 10.0 + np.cumsum(charge - discharge) * 0.5
    soc = np.clip(soc, 0, 20)
    return pd.DataFrame(
        {
            "time": times,
            "soc": soc,
            "charge_power": charge,
            "discharge_power": discharge,
            "net_power": discharge - charge,
        }
    )


def _make_scheduling_input(n: int = 24) -> pd.DataFrame:
    """Return a minimal timeseries_import DataFrame for a scheduling run."""
    t_start = pd.Timestamp("2025-08-01 00:00:00")
    times = pd.date_range(t_start, periods=n, freq="h")
    rng = np.random.default_rng(7)
    price = 30.0 + rng.uniform(-10, 60, n)
    return pd.DataFrame(
        {
            "time": times,
            "price": price,
            "grid_fee_in": np.zeros(n),
            "grid_fee_out": np.zeros(n),
        }
    )


def _make_intraday_output(n: int = 8, n_segs: int = 2) -> pd.DataFrame:
    """Return a minimal timeseries_export DataFrame for an intraday run."""
    t_start = pd.Timestamp("2025-08-01 00:00:00")
    times = pd.date_range(t_start, periods=n, freq="15min")
    rng = np.random.default_rng(99)
    charge = rng.uniform(0, 5, n)
    discharge = rng.uniform(0, 5, n)
    soc = 10.0 + np.cumsum(charge - discharge) * 0.25
    soc = np.clip(soc, 0, 20)
    df: dict[str, Any] = {
        "time": times,
        "soc": soc,
        "charge_power": charge,
        "discharge_power": discharge,
        "net_power": discharge - charge,
    }
    for seg in range(1, n_segs + 1):
        df[f"discharge_power_bids[{seg}]"] = discharge / n_segs
        df[f"charge_power_asks[{seg}]"] = charge / n_segs
    return pd.DataFrame(df)


def _make_intraday_input(n: int = 8, n_segs: int = 2) -> pd.DataFrame:
    """Return a minimal timeseries_import DataFrame for an intraday run."""
    t_start = pd.Timestamp("2025-08-01 00:00:00")
    times = pd.date_range(t_start, periods=n, freq="15min")
    rng = np.random.default_rng(13)
    df: dict[str, Any] = {
        "time": times,
        "grid_fee_in": np.zeros(n),
        "grid_fee_out": np.zeros(n),
        "committed_net_power": np.zeros(n),
    }
    for seg in range(1, n_segs + 1):
        df[f"bid_prices[{seg}]"] = 45.0 + rng.uniform(-5, 5, n)
        df[f"ask_prices[{seg}]"] = 55.0 + rng.uniform(-5, 5, n)
        df[f"bid_volumes[{seg}]"] = np.full(n, 10.0)
        df[f"ask_volumes[{seg}]"] = np.full(n, 10.0)
    return pd.DataFrame(df)


def _make_mock_prob(capacity: float = 20.0, max_power: float = 10.0) -> MagicMock:
    """Return a minimal mock of an OptimizationProblem post-solve."""
    prob = MagicMock()
    prob.parameters.return_value = {
        "capacity": capacity,
        "max_power": max_power,
        "efficiency": 0.9025,
    }
    prob.cycling_penalty_factor = 0.1
    prob.transaction_cost = 0.05
    prob.objective_value = -123.45
    prob.solver_stats = {
        "return_status": "Optimal",
        "t_wall_total": 0.42,
    }
    # Simulate Lagrange multipliers as a small random vector
    rng = np.random.default_rng(0)
    lam_x = rng.uniform(-0.5, 0.5, 120)
    prob.lagrange_multipliers = (None, lam_x)
    return prob


def _is_valid_png_data_uri(uri: str) -> bool:
    """Return True iff *uri* is a valid ``data:image/png;base64,…`` string."""
    if not uri.startswith("data:image/png;base64,"):
        return False
    encoded = uri[len("data:image/png;base64,") :]
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        return False
    return raw[:4] == b"\x89PNG"


# ── per-chart unit tests ──────────────────────────────────────────────────────


class TestRevenueDecompositionScheduling:
    def test_returns_valid_png(self) -> None:
        from service.translation.diagnostics import (
            _chart_revenue_decomposition_scheduling,
            _fig_to_b64,
        )

        df_out = _make_scheduling_output()
        df_in = _make_scheduling_input()
        fig = _chart_revenue_decomposition_scheduling(
            df_out, df_in, cycling_penalty=0.1
        )
        uri = _fig_to_b64(fig)
        assert _is_valid_png_data_uri(uri)

    def test_no_grid_fee_columns(self) -> None:
        from service.translation.diagnostics import (
            _chart_revenue_decomposition_scheduling,
            _fig_to_b64,
        )

        df_out = _make_scheduling_output()
        df_in = _make_scheduling_input().drop(columns=["grid_fee_in", "grid_fee_out"])
        fig = _chart_revenue_decomposition_scheduling(
            df_out, df_in, cycling_penalty=0.1
        )
        uri = _fig_to_b64(fig)
        assert _is_valid_png_data_uri(uri)

    def test_single_interval(self) -> None:
        from service.translation.diagnostics import (
            _chart_revenue_decomposition_scheduling,
            _fig_to_b64,
        )

        df_out = _make_scheduling_output(n=1)
        df_in = _make_scheduling_input(n=1)
        fig = _chart_revenue_decomposition_scheduling(
            df_out, df_in, cycling_penalty=0.1
        )
        assert _is_valid_png_data_uri(_fig_to_b64(fig))


class TestRevenueDecompositionIntraday:
    def test_returns_valid_png(self) -> None:
        from service.translation.diagnostics import (
            _chart_revenue_decomposition_intraday,
            _fig_to_b64,
        )

        df_out = _make_intraday_output()
        df_in = _make_intraday_input()
        fig = _chart_revenue_decomposition_intraday(
            df_out, df_in, n_segments=2, cycling_penalty=0.1, transaction_cost=0.05
        )
        assert _is_valid_png_data_uri(_fig_to_b64(fig))


class TestConstraintTightness:
    def test_returns_valid_png(self) -> None:
        from service.translation.diagnostics import (
            _chart_constraint_tightness,
            _fig_to_b64,
        )

        df_out = _make_scheduling_output()
        prob = _make_mock_prob()
        fig = _chart_constraint_tightness(df_out, prob)
        assert _is_valid_png_data_uri(_fig_to_b64(fig))

    def test_bad_prob_does_not_raise(self) -> None:
        from service.translation.diagnostics import (
            _chart_constraint_tightness,
            _fig_to_b64,
        )

        df_out = _make_scheduling_output()
        prob = MagicMock()
        prob.parameters.side_effect = RuntimeError("no params")
        # Should fall back to defaults and still produce a figure
        fig = _chart_constraint_tightness(df_out, prob)
        assert _is_valid_png_data_uri(_fig_to_b64(fig))


class TestSocHeadroom:
    def test_returns_valid_png(self) -> None:
        from service.translation.diagnostics import _chart_soc_headroom, _fig_to_b64

        df_out = _make_scheduling_output()
        prob = _make_mock_prob()
        fig = _chart_soc_headroom(df_out, prob)
        assert _is_valid_png_data_uri(_fig_to_b64(fig))


class TestShadowPrices:
    def test_returns_valid_png_when_duals_available(self) -> None:
        from service.translation.diagnostics import _chart_shadow_prices, _fig_to_b64

        df_out = _make_scheduling_output()
        prob = _make_mock_prob()
        fig = _chart_shadow_prices(df_out, prob)
        assert fig is not None
        assert _is_valid_png_data_uri(_fig_to_b64(fig))

    def test_returns_none_when_duals_missing(self) -> None:
        from service.translation.diagnostics import _chart_shadow_prices

        df_out = _make_scheduling_output()
        prob = MagicMock()
        prob.lagrange_multipliers = (None, None)
        result = _chart_shadow_prices(df_out, prob)
        assert result is None

    def test_returns_none_when_lam_x_empty(self) -> None:
        from service.translation.diagnostics import _chart_shadow_prices

        df_out = _make_scheduling_output()
        prob = MagicMock()
        prob.lagrange_multipliers = (None, np.array([]))
        result = _chart_shadow_prices(df_out, prob)
        assert result is None

    def test_returns_none_on_exception(self) -> None:
        from service.translation.diagnostics import _chart_shadow_prices

        df_out = _make_scheduling_output()
        prob = MagicMock()
        prob.lagrange_multipliers = PropertyError()
        result = _chart_shadow_prices(df_out, prob)
        assert result is None


class TestDecisionRationale:
    def test_returns_valid_png(self) -> None:
        from service.translation.diagnostics import (
            _chart_decision_rationale_scheduling,
            _fig_to_b64,
        )

        df_out = _make_scheduling_output()
        df_in = _make_scheduling_input()
        prob = _make_mock_prob()
        fig = _chart_decision_rationale_scheduling(df_out, df_in, 0.1, prob)
        assert _is_valid_png_data_uri(_fig_to_b64(fig))

    def test_no_grid_fees(self) -> None:
        from service.translation.diagnostics import (
            _chart_decision_rationale_scheduling,
            _fig_to_b64,
        )

        df_out = _make_scheduling_output()
        df_in = _make_scheduling_input().drop(columns=["grid_fee_in", "grid_fee_out"])
        prob = _make_mock_prob()
        fig = _chart_decision_rationale_scheduling(df_out, df_in, 0.1, prob)
        assert _is_valid_png_data_uri(_fig_to_b64(fig))


class TestOrderbookUtilisation:
    def test_returns_valid_png(self) -> None:
        from service.translation.diagnostics import (
            _chart_orderbook_utilisation,
            _fig_to_b64,
        )

        df_out = _make_intraday_output(n_segs=3)
        df_in = _make_intraday_input(n_segs=3)
        fig = _chart_orderbook_utilisation(df_out, df_in, n_segments=3)
        assert _is_valid_png_data_uri(_fig_to_b64(fig))

    def test_single_segment(self) -> None:
        from service.translation.diagnostics import (
            _chart_orderbook_utilisation,
            _fig_to_b64,
        )

        df_out = _make_intraday_output(n_segs=1)
        df_in = _make_intraday_input(n_segs=1)
        fig = _chart_orderbook_utilisation(df_out, df_in, n_segments=1)
        assert _is_valid_png_data_uri(_fig_to_b64(fig))


# ── public entry point tests ──────────────────────────────────────────────────


class TestBuildSchedulingDiagnostics:
    def _write_csvs(
        self,
        tmpdir: Path,
        df_out: pd.DataFrame,
        df_in: pd.DataFrame,
    ) -> Path:
        output_dir = tmpdir / "output"
        input_dir = tmpdir / "input"
        output_dir.mkdir()
        input_dir.mkdir()
        df_out.to_csv(output_dir / "timeseries_export.csv", index=False)
        df_in.to_csv(input_dir / "timeseries_import.csv", index=False)
        return output_dir

    def test_returns_expected_chart_keys(self) -> None:
        from service.translation.diagnostics import build_scheduling_diagnostics

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            n = 24
            df_out = _make_scheduling_output(n)
            # Prepend dummy row so the stripping logic leaves exactly n rows
            df_out_with_dummy = pd.concat([df_out.iloc[:1], df_out], ignore_index=True)
            df_in = _make_scheduling_input(n)
            df_in_with_dummy = pd.concat([df_in.iloc[:1], df_in], ignore_index=True)
            output_dir = self._write_csvs(base, df_out_with_dummy, df_in_with_dummy)

            from datetime import datetime, timezone, timedelta

            base_dt = datetime(2025, 8, 1, tzinfo=timezone.utc)
            interval_start = [
                (base_dt + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for h in range(n)
            ]
            model_input = {"interval_start": interval_start}
            prob = _make_mock_prob()

            images, info = build_scheduling_diagnostics(
                output_dir, model_input, cycling_penalty=0.1, prob=prob
            )

        # Expect these chart keys
        for key in (
            "revenue_decomposition",
            "constraint_tightness",
            "soc_headroom",
            "decision_rationale",
        ):
            assert key in images, f"Missing chart: {key}"

        # All images must be valid PNG data URIs
        for key, uri in images.items():
            assert _is_valid_png_data_uri(uri), f"Invalid PNG URI for {key}"

        # _info must contain a diagnostics timing entry
        assert any("diagnostics:" in entry for entry in info)

    def test_missing_output_csv_returns_empty_and_info(self) -> None:
        from service.translation.diagnostics import build_scheduling_diagnostics

        with tempfile.TemporaryDirectory() as tmpdir:
            # output dir exists but CSV is missing
            output_dir = Path(tmpdir) / "output"
            output_dir.mkdir()
            prob = _make_mock_prob()

            images, info = build_scheduling_diagnostics(
                output_dir, {}, cycling_penalty=0.1, prob=prob
            )

        assert images == {}
        assert any("diagnostics: skipped" in e for e in info)

    def test_info_contains_timing(self) -> None:
        from service.translation.diagnostics import build_scheduling_diagnostics

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            n = 4
            df_out = _make_scheduling_output(n)
            df_in = _make_scheduling_input(n)
            df_out_dummy = pd.concat([df_out.iloc[:1], df_out], ignore_index=True)
            df_in_dummy = pd.concat([df_in.iloc[:1], df_in], ignore_index=True)
            output_dir = self._write_csvs(base, df_out_dummy, df_in_dummy)

            from datetime import datetime, timezone, timedelta

            base_dt = datetime(2025, 8, 1, tzinfo=timezone.utc)
            interval_start = [
                (base_dt + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for h in range(n)
            ]

            _, info = build_scheduling_diagnostics(
                output_dir,
                {"interval_start": interval_start},
                cycling_penalty=0.1,
                prob=_make_mock_prob(),
            )

        timing_entries = [e for e in info if "diagnostics:" in e and "ms" in e]
        assert len(timing_entries) == 1
        assert "chart(s)" in timing_entries[0]


class TestBuildIntradayDiagnostics:
    def _write_csvs(
        self,
        tmpdir: Path,
        df_out: pd.DataFrame,
        df_in: pd.DataFrame,
    ) -> Path:
        output_dir = tmpdir / "output"
        input_dir = tmpdir / "input"
        output_dir.mkdir()
        input_dir.mkdir()
        df_out.to_csv(output_dir / "timeseries_export.csv", index=False)
        df_in.to_csv(input_dir / "timeseries_import.csv", index=False)
        return output_dir

    def test_returns_expected_chart_keys(self) -> None:
        from service.translation.diagnostics import build_intraday_diagnostics

        n = 8
        n_segs = 2
        df_out = _make_intraday_output(n, n_segs)
        df_in = _make_intraday_input(n, n_segs)
        df_out_dummy = pd.concat([df_out.iloc[:1], df_out], ignore_index=True)
        df_in_dummy = pd.concat([df_in.iloc[:1], df_in], ignore_index=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            output_dir = self._write_csvs(base, df_out_dummy, df_in_dummy)

            from datetime import datetime, timezone, timedelta

            base_dt = datetime(2025, 8, 1, tzinfo=timezone.utc)
            interval_start = [
                (base_dt + timedelta(minutes=i * 15)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for i in range(n)
            ]

            images, info = build_intraday_diagnostics(
                output_dir,
                {"interval_start": interval_start},
                n_segments=n_segs,
                cycling_penalty=0.1,
                transaction_cost=0.05,
                prob=_make_mock_prob(),
            )

        for key in (
            "revenue_decomposition",
            "constraint_tightness",
            "soc_headroom",
            "orderbook_utilisation",
        ):
            assert key in images, f"Missing chart: {key}"

        for key, uri in images.items():
            assert _is_valid_png_data_uri(uri), f"Invalid PNG URI for {key}"

        assert any("diagnostics:" in e for e in info)


# ── helper used by shadow_prices test ─────────────────────────────────────────


class PropertyError:
    """Raises RuntimeError when attribute is accessed (simulates broken prob)."""

    def __get__(self, obj: Any, objtype: Any = None) -> Any:
        raise RuntimeError("no duals")

    def __iter__(self) -> Any:
        raise RuntimeError("no duals")
