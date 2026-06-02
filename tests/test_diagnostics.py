"""Unit tests for service/translation/diagnostics.py.

These tests verify that:
- Each surviving chart function returns a valid base64-encoded PNG data URI.
- Chart functions degrade gracefully when optional data is absent.
- The reasoning helpers (cycle detection, metrics, constraint/orderbook
  tables) return well-formed structures.
- The public entry points (build_scheduling_diagnostics,
  build_intraday_diagnostics) return ``(images, info, reasoning_markdown)``
  with the expected chart keys and a non-empty markdown document.
- No chart failure propagates as an exception — errors are captured in
  the _info list.

We avoid running the real RTC-Tools solver by constructing minimal stub
DataFrames and a lightweight mock problem object.
"""

from __future__ import annotations

import base64
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


def _committed_discharge(n: int) -> np.ndarray:
    """A committed discharge (export) obligation covering the second half."""
    prof = np.zeros(n)
    prof[n // 2 :] = 3.0
    return prof


def _make_intraday_output(n: int = 8, n_segs: int = 2) -> pd.DataFrame:
    """Return a minimal timeseries_export DataFrame for an intraday run.

    Gross flows respect the Modelica identity gross = committed + incremental:
    the second half carries a committed discharge obligation on top of the
    optimiser's (small) incremental trades.
    """
    t_start = pd.Timestamp("2025-08-01 00:00:00")
    times = pd.date_range(t_start, periods=n, freq="15min")
    committed_discharge = _committed_discharge(n)
    incr_charge = np.where(np.arange(n) < n // 2, 2.0, 0.0)
    incr_discharge = np.where(np.arange(n) >= n // 2, 1.0, 0.0)
    charge_power = incr_charge  # committed_charge is zero in this fixture
    discharge_power = committed_discharge + incr_discharge
    soc = np.clip(10.0 + np.cumsum(charge_power - discharge_power) * 0.25, 0, 20)
    df: dict[str, Any] = {
        "time": times,
        "soc": soc,
        "charge_power": charge_power,
        "discharge_power": discharge_power,
        "net_power": discharge_power - charge_power,
    }
    for seg in range(1, n_segs + 1):
        df[f"discharge_power_bids[{seg}]"] = incr_discharge / n_segs
        df[f"charge_power_asks[{seg}]"] = incr_charge / n_segs
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
        "committed_charge": np.zeros(n),
        "committed_discharge": _committed_discharge(n),
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
    prob.solver_stats = {"return_status": "Optimal", "t_wall_total": 0.42}
    rng = np.random.default_rng(0)
    prob.lagrange_multipliers = (None, rng.uniform(-0.5, 0.5, 120))
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

        fig = _chart_revenue_decomposition_scheduling(
            _make_scheduling_output(), _make_scheduling_input(), cycling_penalty=0.1
        )
        assert _is_valid_png_data_uri(_fig_to_b64(fig))

    def test_no_grid_fee_columns(self) -> None:
        from service.translation.diagnostics import (
            _chart_revenue_decomposition_scheduling,
            _fig_to_b64,
        )

        df_in = _make_scheduling_input().drop(columns=["grid_fee_in", "grid_fee_out"])
        fig = _chart_revenue_decomposition_scheduling(
            _make_scheduling_output(), df_in, cycling_penalty=0.1
        )
        assert _is_valid_png_data_uri(_fig_to_b64(fig))

    def test_single_interval(self) -> None:
        from service.translation.diagnostics import (
            _chart_revenue_decomposition_scheduling,
            _fig_to_b64,
        )

        fig = _chart_revenue_decomposition_scheduling(
            _make_scheduling_output(n=1), _make_scheduling_input(n=1), cycling_penalty=0.1
        )
        assert _is_valid_png_data_uri(_fig_to_b64(fig))


class TestRevenueDecompositionIntraday:
    def test_returns_valid_png(self) -> None:
        from service.translation.diagnostics import (
            _chart_revenue_decomposition_intraday,
            _fig_to_b64,
        )

        fig = _chart_revenue_decomposition_intraday(
            _make_intraday_output(), _make_intraday_input(),
            n_segments=2, cycling_penalty=0.1, transaction_cost=0.05,
        )
        assert _is_valid_png_data_uri(_fig_to_b64(fig))


class TestSocHeadroom:
    def test_returns_valid_png(self) -> None:
        from service.translation.diagnostics import _chart_soc_headroom, _fig_to_b64

        fig = _chart_soc_headroom(_make_scheduling_output(), _make_mock_prob())
        assert _is_valid_png_data_uri(_fig_to_b64(fig))

    def test_bad_prob_does_not_raise(self) -> None:
        from service.translation.diagnostics import _chart_soc_headroom, _fig_to_b64

        prob = MagicMock()
        prob.parameters.side_effect = RuntimeError("no params")
        fig = _chart_soc_headroom(_make_scheduling_output(), prob)
        assert _is_valid_png_data_uri(_fig_to_b64(fig))


class TestDecisionRationale:
    def test_returns_valid_png(self) -> None:
        from service.translation.diagnostics import (
            _chart_decision_rationale_scheduling,
            _fig_to_b64,
        )

        fig = _chart_decision_rationale_scheduling(
            _make_scheduling_output(), _make_scheduling_input(), 0.1, _make_mock_prob()
        )
        assert _is_valid_png_data_uri(_fig_to_b64(fig))

    def test_no_grid_fees(self) -> None:
        from service.translation.diagnostics import (
            _chart_decision_rationale_scheduling,
            _fig_to_b64,
        )

        df_in = _make_scheduling_input().drop(columns=["grid_fee_in", "grid_fee_out"])
        fig = _chart_decision_rationale_scheduling(
            _make_scheduling_output(), df_in, 0.1, _make_mock_prob()
        )
        assert _is_valid_png_data_uri(_fig_to_b64(fig))


class TestCommittedPosition:
    def test_returns_valid_png(self) -> None:
        from service.translation.diagnostics import (
            _chart_committed_position,
            _fig_to_b64,
        )

        fig = _chart_committed_position(
            _make_intraday_output(), _make_intraday_input(), _make_mock_prob()
        )
        assert _is_valid_png_data_uri(_fig_to_b64(fig))

    def test_missing_committed_columns(self) -> None:
        from service.translation.diagnostics import (
            _chart_committed_position,
            _fig_to_b64,
        )

        df_in = _make_intraday_input().drop(
            columns=["committed_charge", "committed_discharge"]
        )
        fig = _chart_committed_position(
            _make_intraday_output(), df_in, _make_mock_prob()
        )
        assert _is_valid_png_data_uri(_fig_to_b64(fig))


class TestSpreadDuration:
    def test_returns_valid_png(self) -> None:
        from service.translation.diagnostics import _chart_spread_duration, _fig_to_b64

        fig = _chart_spread_duration(
            _make_intraday_output(n_segs=3), _make_intraday_input(n_segs=3),
            n_segments=3, cycling_penalty=0.1, transaction_cost=0.05, efficiency=0.9,
        )
        assert _is_valid_png_data_uri(_fig_to_b64(fig))

    def test_missing_orderbook_columns(self) -> None:
        from service.translation.diagnostics import _chart_spread_duration, _fig_to_b64

        # df_in without any bid/ask columns — chart must degrade gracefully
        df_in = pd.DataFrame({"time": _make_intraday_output()["time"]})
        fig = _chart_spread_duration(
            _make_intraday_output(), df_in,
            n_segments=2, cycling_penalty=0.1, transaction_cost=0.05, efficiency=0.9,
        )
        assert _is_valid_png_data_uri(_fig_to_b64(fig))


# ── reasoning-helper unit tests ───────────────────────────────────────────────


class TestReasoningHelpers:
    def test_detect_episodes_alternating(self) -> None:
        from service.translation.diagnostics import _detect_episodes

        charge = np.array([5.0, 5.0, 0.0, 0.0, 0.0, 0.0])
        discharge = np.array([0.0, 0.0, 0.0, 4.0, 4.0, 0.0])
        episodes = _detect_episodes(charge, discharge)
        kinds = [e["kind"] for e in episodes]
        assert kinds == ["charge", "discharge"]
        assert episodes[0]["ptus"] == [0, 1]
        assert episodes[1]["ptus"] == [3, 4]

    def test_detect_episodes_empty(self) -> None:
        from service.translation.diagnostics import _detect_episodes

        assert _detect_episodes(np.zeros(5), np.zeros(5)) == []

    def test_collect_intraday_metrics(self) -> None:
        from service.translation.diagnostics import _collect_intraday_metrics

        metrics = _collect_intraday_metrics(
            _make_intraday_output(), _make_intraday_input(),
            n_segments=2, cycling_penalty=0.1, transaction_cost=0.05,
            prob=_make_mock_prob(),
        )
        assert metrics["solver_status"] == "Optimal"
        assert metrics["equivalent_full_cycles"] >= 0.0
        assert "net_profit_eur" in metrics
        assert metrics["horizon_intervals"] == 8
        # committed-position split must be present and consistent
        assert metrics["committed_discharged_mwh"] > 0.0
        assert metrics["incremental_discharged_mwh"] < metrics["total_discharged_mwh"]
        assert "forced_charge_mwh" in metrics
        assert 0.0 <= metrics["committed_share_of_throughput"] <= 1.0

    def test_committed_position_stats(self) -> None:
        from service.translation.diagnostics import _committed_position_stats

        df_out = _make_intraday_output()
        df_in = _make_intraday_input()
        st = _committed_position_stats(df_out, df_in, 0.25, 0.9, 20.0)
        assert st["committed_discharged_mwh"] > 0.0
        assert st["incremental_discharged_mwh"] >= 0.0
        assert len(st["committed_soc"]) == len(df_out) + 1
        assert st["forced_charge_mwh"] >= 0.0
        assert 0.0 <= st["committed_share_of_throughput"] <= 1.0

    def test_cycling_penalty_uses_incremental_not_gross(self) -> None:
        """Cycling penalty must be reconstructed on incremental trades only —
        the committed position carries no penalty (matches path_objective)."""
        from service.translation.diagnostics import _revenue_components_intraday

        df_out = _make_intraday_output()
        df_in = _make_intraday_input()
        comp = _revenue_components_intraday(df_out, df_in, 2, 0.1, 0.05, 0.25)
        incr = comp["incr_charge"] + comp["incr_discharge"]
        expected = float(np.sum(0.1 * incr * 0.25))
        assert comp["cycling"].sum() == pytest.approx(expected)
        # a gross-based reconstruction would be strictly larger, because the
        # committed discharge adds throughput the solver never penalised
        gross = (
            df_out["charge_power"].to_numpy(dtype=float)
            + df_out["discharge_power"].to_numpy(dtype=float)
        )
        assert comp["cycling"].sum() < float(np.sum(0.1 * gross * 0.25))

    def test_collect_scheduling_metrics(self) -> None:
        from service.translation.diagnostics import _collect_scheduling_metrics

        metrics = _collect_scheduling_metrics(
            _make_scheduling_output(), _make_scheduling_input(),
            cycling_penalty=0.1, prob=_make_mock_prob(),
        )
        assert "net_profit_eur" in metrics
        # scheduling has no transaction cost — that key is intraday-only
        assert "total_transaction_cost_eur" not in metrics

    def test_constraint_binding_stats(self) -> None:
        from service.translation.diagnostics import _constraint_binding_stats

        rows = _constraint_binding_stats(_make_scheduling_output(), _make_mock_prob())
        assert len(rows) == 5
        for r in rows:
            assert 0.0 <= r["pct"] <= 100.0

    def test_orderbook_depth_stats(self) -> None:
        from service.translation.diagnostics import _orderbook_depth_stats

        rows = _orderbook_depth_stats(
            _make_intraday_output(n_segs=3), _make_intraday_input(n_segs=3), 3
        )
        assert len(rows) == 3
        for r in rows:
            assert 0.0 <= r["charge_fill_pct"] <= 100.0

    def test_build_cycle_rows_ranked(self) -> None:
        from service.translation.diagnostics import (
            _build_cycle_rows,
            _detect_episodes,
            _revenue_components_intraday,
        )

        df_out = _make_intraday_output()
        df_in = _make_intraday_input()
        comp = _revenue_components_intraday(df_out, df_in, 2, 0.1, 0.05, 0.25)
        # cycles are detected on incremental flows (the optimiser's own trades)
        incr_charge = comp["incr_charge"]
        incr_discharge = comp["incr_discharge"]
        rows = _build_cycle_rows(
            _detect_episodes(incr_charge, incr_discharge),
            comp, incr_charge, incr_discharge, 0.25, 20.0,
        )
        assert rows  # at least one cycle detected
        nets = [r["net_eur"] for r in rows]
        assert nets == sorted(nets, reverse=True)  # merit order
        assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))


# ── public entry point tests ──────────────────────────────────────────────────


def _write_csvs(tmpdir: Path, df_out: pd.DataFrame, df_in: pd.DataFrame) -> Path:
    output_dir = tmpdir / "output"
    input_dir = tmpdir / "input"
    output_dir.mkdir()
    input_dir.mkdir()
    df_out.to_csv(output_dir / "timeseries_export.csv", index=False)
    df_in.to_csv(input_dir / "timeseries_import.csv", index=False)
    return output_dir


def _interval_start(n: int, *, minutes: int) -> list[str]:
    from datetime import datetime, timedelta, timezone

    base = datetime(2025, 8, 1, tzinfo=timezone.utc)
    return [
        (base + timedelta(minutes=i * minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(n)
    ]


class TestBuildSchedulingDiagnostics:
    def test_returns_expected_chart_keys_and_markdown(self) -> None:
        from service.translation.diagnostics import build_scheduling_diagnostics

        n = 24
        df_out = _make_scheduling_output(n)
        df_in = _make_scheduling_input(n)
        df_out_dummy = pd.concat([df_out.iloc[:1], df_out], ignore_index=True)
        df_in_dummy = pd.concat([df_in.iloc[:1], df_in], ignore_index=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = _write_csvs(Path(tmpdir), df_out_dummy, df_in_dummy)
            images, info, markdown = build_scheduling_diagnostics(
                output_dir,
                {"interval_start": _interval_start(n, minutes=60)},
                cycling_penalty=0.1,
                prob=_make_mock_prob(),
            )

        for key in ("revenue_decomposition", "soc_headroom", "decision_rationale"):
            assert key in images, f"Missing chart: {key}"
        # Retired charts must NOT be present
        assert "constraint_tightness" not in images
        assert "shadow_prices" not in images
        for key, uri in images.items():
            assert _is_valid_png_data_uri(uri), f"Invalid PNG URI for {key}"
        assert any("diagnostics:" in e for e in info)

        assert isinstance(markdown, str) and markdown
        assert "# Day-Ahead Scheduling" in markdown
        assert "## Results" in markdown
        assert "## Per-Cycle Merit Order" in markdown
        assert "## Full-Day Schedule" in markdown

    def test_missing_output_csv_returns_empty_and_info(self) -> None:
        from service.translation.diagnostics import build_scheduling_diagnostics

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "output"
            output_dir.mkdir()
            images, info, markdown = build_scheduling_diagnostics(
                output_dir, {}, cycling_penalty=0.1, prob=_make_mock_prob()
            )

        assert images == {}
        assert markdown == ""
        assert any("diagnostics: skipped" in e for e in info)

    def test_info_contains_timing(self) -> None:
        from service.translation.diagnostics import build_scheduling_diagnostics

        n = 4
        df_out = _make_scheduling_output(n)
        df_in = _make_scheduling_input(n)
        df_out_dummy = pd.concat([df_out.iloc[:1], df_out], ignore_index=True)
        df_in_dummy = pd.concat([df_in.iloc[:1], df_in], ignore_index=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = _write_csvs(Path(tmpdir), df_out_dummy, df_in_dummy)
            _, info, _ = build_scheduling_diagnostics(
                output_dir,
                {"interval_start": _interval_start(n, minutes=60)},
                cycling_penalty=0.1,
                prob=_make_mock_prob(),
            )

        timing = [e for e in info if "diagnostics:" in e and "ms" in e]
        assert len(timing) == 1
        assert "chart(s)" in timing[0]


class TestBuildIntradayDiagnostics:
    def test_returns_expected_chart_keys_and_markdown(self) -> None:
        from service.translation.diagnostics import build_intraday_diagnostics

        n, n_segs = 8, 2
        df_out = _make_intraday_output(n, n_segs)
        df_in = _make_intraday_input(n, n_segs)
        df_out_dummy = pd.concat([df_out.iloc[:1], df_out], ignore_index=True)
        df_in_dummy = pd.concat([df_in.iloc[:1], df_in], ignore_index=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = _write_csvs(Path(tmpdir), df_out_dummy, df_in_dummy)
            images, info, markdown = build_intraday_diagnostics(
                output_dir,
                {"interval_start": _interval_start(n, minutes=15)},
                n_segments=n_segs,
                cycling_penalty=0.1,
                transaction_cost=0.05,
                prob=_make_mock_prob(),
            )

        for key in (
            "revenue_decomposition",
            "soc_headroom",
            "spread_duration",
            "committed_position",
        ):
            assert key in images, f"Missing chart: {key}"
        assert "orderbook_utilisation" not in images
        assert "shadow_prices" not in images
        for key, uri in images.items():
            assert _is_valid_png_data_uri(uri), f"Invalid PNG URI for {key}"
        assert any("diagnostics:" in e for e in info)

        assert isinstance(markdown, str) and markdown
        assert "# Intraday Trading" in markdown
        assert "## Committed Position" in markdown
        assert "## Per-Cycle Merit Order" in markdown
        assert "## Orderbook Depth Utilisation" in markdown
