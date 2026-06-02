"""Optimizer explainer diagrams and reasoning data for the BESS service.

Generates a *small, deliberately curated* set of diagnostic charts from
RTC-Tools solver internals, plus the structured tables and metrics that feed
the deterministic reasoning-markdown document (see ``reasoning.py``).

Design choice — charts vs. tables
---------------------------------
A heatmap that only rescales primal output, or a bar chart of a handful of
discrete cycles, transfers far more bytes than the few numbers it conveys.
Such views are emitted as **markdown tables** instead.  Only genuinely visual
charts (per-interval shapes, duration curves) are rendered as PNGs:

- ``revenue_decomposition`` — per-interval value breakdown + cumulative P&L
- ``soc_headroom`` — SoC trajectory and capacity utilisation
- ``decision_rationale`` — scheduling: why-trade-here threshold overlay
- ``spread_duration`` — intraday: how much profitable spread the market offered

Everything else (per-cycle merit order, constraint binding, orderbook depth,
solver statistics) is returned as table/metric data for the markdown builder.

All functions accept the ``prob`` object returned by
``rtctools.util.run_optimization_problem`` and the input/output DataFrames the
service already reads from CSV.  No re-solving occurs.

Public entry points return ``(images, info, reasoning_markdown)``:
- ``build_scheduling_diagnostics`` — day-ahead scheduling model
- ``build_intraday_diagnostics`` — continuous intraday trading model
"""

from __future__ import annotations

import base64
import io
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Force non-interactive backend before any figure is created.
matplotlib.use("Agg")

if TYPE_CHECKING:
    from rtctools.optimization.optimization_problem import OptimizationProblem

_log = logging.getLogger(__name__)

# ── colour palette (matches the project's existing plot_results scripts) ───────

_C = {
    "bg": "#ffffff",
    "grid": "#dddddd",
    "charge": "#e74c3c",  # red  — buying / charging
    "discharge": "#ced73e",  # yellow-green — selling / discharging
    "neutral": "#3498db",  # blue — neutral / SoC
    "warn": "#e67e22",  # orange — constraint tightness warning
    "idle": "#95a5a6",  # grey  — idle / zero activity
    "shadow": "#8e44ad",  # purple — shadow prices / duals
    "text": "#2c3e50",
}

_FIG_W = 9  # inches — wide enough to show 24–96 timesteps clearly
_FIG_H = 3.2  # inches per sub-axes
_DPI = 120


# ── helpers ────────────────────────────────────────────────────────────────────


def _fig_to_b64(fig: plt.Figure) -> str:
    """Render *fig* to a PNG byte buffer and return a data URI string."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=_DPI, bbox_inches="tight")
    buf.seek(0)
    encoded = base64.b64encode(buf.read()).decode("ascii")
    plt.close(fig)
    return f"data:image/png;base64,{encoded}"


def _time_axis(df: pd.DataFrame) -> np.ndarray:
    """Return a numeric x-axis (hours from first timestamp) from *df*."""
    if "time" in df.columns:
        t = pd.to_datetime(df["time"])
        return (t - t.iloc[0]).dt.total_seconds().to_numpy() / 3600.0
    return np.arange(len(df), dtype=float)


def _safe_float(value: Any) -> float | None:
    """Convert *value* to float, returning None if not possible."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _make_fig(n_rows: int, title: str) -> tuple[plt.Figure, list[plt.Axes]]:
    fig, axes = plt.subplots(
        n_rows,
        1,
        figsize=(_FIG_W, _FIG_H * n_rows),
        facecolor=_C["bg"],
        sharex=True,
    )
    fig.suptitle(title, color=_C["text"], fontsize=11, fontweight="bold", y=1.01)
    if n_rows == 1:
        axes = [axes]
    for ax in axes:
        ax.set_facecolor(_C["bg"])
        ax.tick_params(colors=_C["text"])
        ax.spines[:].set_color(_C["grid"])
        for label in ax.get_xticklabels() + ax.get_yticklabels():
            label.set_color(_C["text"])
    return fig, axes


def _label_axes(ax: plt.Axes, ylabel: str, xlabel: str = "Time (hours)") -> None:
    ax.set_ylabel(ylabel, color=_C["text"], fontsize=9)
    ax.set_xlabel(xlabel, color=_C["text"], fontsize=9)
    ax.grid(True, color=_C["grid"], linewidth=0.5, alpha=0.7)
    ax.yaxis.label.set_color(_C["text"])


def _place_label(
    ax: plt.Axes,
    x: float,
    y: float,
    text: str,
    *,
    placed: list[tuple[float, float, float, float]],
    fontsize: float = 7,
    color: str = "#2c3e50",
    ec: str | None = None,
    fontweight: str = "normal",
    ha: str = "center",
    va: str = "center",
    zorder: int = 6,
    x_range: tuple[float, float] = (0.0, 24.0),
    y_range: tuple[float, float] | None = None,
) -> None:
    """Place a text label, nudging it to avoid overlapping already-placed labels.

    ``placed`` is a mutable list of (x0, y0, x1, y1) data-coordinate bounding
    boxes, updated in-place so later calls step away from earlier labels.
    Ported from the poc-backtesting local DA-spread adapter.
    """
    renderer = ax.get_figure().canvas.get_renderer()
    tmp = ax.text(x, y, text, fontsize=fontsize, ha=ha, va=va, visible=False)
    try:
        bbox_disp = tmp.get_window_extent(renderer=renderer)
        inv = ax.transData.inverted()
        lo = inv.transform((bbox_disp.x0, bbox_disp.y0))
        hi = inv.transform((bbox_disp.x1, bbox_disp.y1))
        w = abs(hi[0] - lo[0])
        h = abs(hi[1] - lo[1])
    except Exception:
        w, h = 1.5, 5.0
    finally:
        tmp.remove()

    step_x, step_y = max(w * 0.6, 0.5), max(h * 1.1, 1.0)
    candidates: list[tuple[float, float]] = [(0.0, 0.0)]
    for ring in range(1, 10):
        for dx in range(-ring, ring + 1):
            for dy in range(-ring, ring + 1):
                if abs(dx) == ring or abs(dy) == ring:
                    candidates.append((dx * step_x, dy * step_y))

    xl, xr = x_range
    for dx, dy in candidates:
        cx, cy = x + dx, y + dy
        cx = max(xl + w / 2, min(xr - w / 2, cx))
        if y_range:
            yl_r, yr_r = y_range
            cy = max(yl_r + h / 2, min(yr_r - h / 2, cy))
        bx0, by0 = cx - w / 2, cy - h / 2
        bx1, by1 = cx + w / 2, cy + h / 2
        overlap = any(
            bx0 < px1 and bx1 > px0 and by0 < py1 and by1 > py0
            for px0, py0, px1, py1 in placed
        )
        if not overlap:
            bbox_kw = (
                dict(boxstyle="round,pad=0.3", fc="white", ec=ec or color, alpha=0.92)
                if ec is not None
                else None
            )
            ax.text(
                cx,
                cy,
                text,
                ha=ha,
                va=va,
                fontsize=fontsize,
                fontweight=fontweight,
                color=color,
                zorder=zorder,
                **({"bbox": bbox_kw} if bbox_kw else {}),
            )
            placed.append((bx0, by0, bx1, by1))
            return

    ax.text(
        x, y, text, ha=ha, va=va, fontsize=fontsize, fontweight=fontweight,
        color=color, zorder=zorder,
    )
    placed.append((x - w / 2, y - h / 2, x + w / 2, y + h / 2))


def _param(prob: "OptimizationProblem", name: str, default: float) -> float:
    """Read a numeric model parameter, falling back to *default* on any error."""
    try:
        return float(prob.parameters(0).get(name, default))
    except Exception:
        return default


# ══════════════════════════════════════════════════════════════════════════════
# Chart 1 — Revenue decomposition waterfall
# ══════════════════════════════════════════════════════════════════════════════


def _chart_revenue_decomposition_scheduling(
    df_out: pd.DataFrame,
    df_in: pd.DataFrame,
    cycling_penalty: float,
) -> plt.Figure:
    """Waterfall of gross revenue, costs, and net profit over the horizon."""
    t = _time_axis(df_out)
    n = len(df_out)
    price = df_in["price"].values[:n]
    charge = df_out["charge_power"].values
    discharge = df_out["discharge_power"].values
    net_power = df_out["net_power"].values

    dt = (t[1] - t[0]) if len(t) > 1 else 1.0

    gross_rev_ts = net_power * price * dt
    grid_fee_in_ts = (
        df_in["grid_fee_in"].values[:n] * charge * dt
        if "grid_fee_in" in df_in.columns
        else np.zeros(n)
    )
    grid_fee_out_ts = (
        df_in["grid_fee_out"].values[:n] * discharge * dt
        if "grid_fee_out" in df_in.columns
        else np.zeros(n)
    )
    cycling_ts = cycling_penalty * (charge + discharge) * dt

    net_ts = gross_rev_ts - grid_fee_in_ts - grid_fee_out_ts - cycling_ts

    fig, axes = _make_fig(2, "Revenue Decomposition — Scheduling")

    ax = axes[0]
    positive_rev = np.maximum(gross_rev_ts, 0)
    negative_rev = np.minimum(gross_rev_ts, 0)
    ax.bar(
        t, positive_rev, color=_C["discharge"],
        label="Gross revenue (discharge)", width=dt * 0.8,
    )
    ax.bar(
        t, negative_rev, color=_C["charge"], label="Gross cost (charge)", width=dt * 0.8
    )
    ax.bar(
        t, -grid_fee_in_ts - grid_fee_out_ts, bottom=negative_rev,
        color=_C["warn"], label="Grid fees", width=dt * 0.8, alpha=0.8,
    )
    ax.bar(
        t, -cycling_ts, bottom=negative_rev - grid_fee_in_ts - grid_fee_out_ts,
        color=_C["idle"], label="Cycling penalty", width=dt * 0.8, alpha=0.8,
    )
    ax.axhline(0, color=_C["text"], linewidth=0.8)
    ax.legend(fontsize=7, loc="upper left")
    _label_axes(ax, "EUR per interval", xlabel="")

    ax2 = axes[1]
    cumulative_net = np.cumsum(net_ts)
    ax2.plot(
        t, cumulative_net, color=_C["neutral"], linewidth=1.8,
        label="Cumulative net profit",
    )
    ax2.fill_between(
        t, 0, cumulative_net, where=cumulative_net >= 0,
        color=_C["discharge"], alpha=0.15,
    )
    ax2.fill_between(
        t, 0, cumulative_net, where=cumulative_net < 0, color=_C["charge"], alpha=0.15
    )
    ax2.axhline(0, color=_C["text"], linewidth=0.8)
    ax2.legend(fontsize=7)
    _label_axes(ax2, "Cumulative EUR")

    total = float(cumulative_net[-1]) if len(cumulative_net) else 0.0
    color = _C["discharge"] if total >= 0 else _C["charge"]
    ax2.annotate(
        f"Net: {total:.0f} EUR",
        xy=(t[-1], total),
        xytext=(-60, 10),
        textcoords="offset points",
        fontsize=8,
        color=color,
        arrowprops=dict(arrowstyle="->", color=color),
    )

    fig.tight_layout()
    return fig


def _chart_revenue_decomposition_intraday(
    df_out: pd.DataFrame,
    df_in: pd.DataFrame,
    n_segments: int,
    cycling_penalty: float,
    transaction_cost: float,
) -> plt.Figure:
    """Per-level revenue attribution for intraday orderbook trading."""
    t = _time_axis(df_out)
    n = len(df_out)
    dt = (t[1] - t[0]) if len(t) > 1 else 1.0

    total_discharge_rev = np.zeros(n)
    total_charge_cost = np.zeros(n)
    for seg in range(1, n_segments + 1):
        bid_col = f"discharge_power_bids[{seg}]"
        ask_col = f"charge_power_asks[{seg}]"
        bp_col = f"bid_prices[{seg}]"
        ap_col = f"ask_prices[{seg}]"
        if bid_col in df_out.columns and bp_col in df_in.columns:
            total_discharge_rev += df_out[bid_col].values[:n] * df_in[bp_col].values[:n]
        if ask_col in df_out.columns and ap_col in df_in.columns:
            total_charge_cost += df_out[ask_col].values[:n] * df_in[ap_col].values[:n]

    charge = df_out["charge_power"].values[:n]
    discharge = df_out["discharge_power"].values[:n]

    grid_fee_in_ts = (
        df_in["grid_fee_in"].values[:n] * charge
        if "grid_fee_in" in df_in.columns
        else np.zeros(n)
    )
    grid_fee_out_ts = (
        df_in["grid_fee_out"].values[:n] * discharge
        if "grid_fee_out" in df_in.columns
        else np.zeros(n)
    )
    cycling_ts = cycling_penalty * (charge + discharge) * dt
    transaction_ts = transaction_cost * (charge + discharge) * dt

    gross_rev_ts = (total_discharge_rev - total_charge_cost) * dt
    net_ts = (
        gross_rev_ts - grid_fee_in_ts - grid_fee_out_ts - cycling_ts - transaction_ts
    )

    fig, axes = _make_fig(2, "Revenue Decomposition — Intraday")

    ax = axes[0]
    ax.bar(
        t, np.maximum(gross_rev_ts, 0), color=_C["discharge"],
        label="Trading revenue", width=dt * 0.8,
    )
    ax.bar(
        t, np.minimum(gross_rev_ts, 0), color=_C["charge"],
        label="Trading cost", width=dt * 0.8,
    )
    ax.bar(
        t, -cycling_ts - transaction_ts, bottom=np.minimum(gross_rev_ts, 0),
        color=_C["idle"], label="Cycling + transaction costs", width=dt * 0.8,
        alpha=0.8,
    )
    ax.axhline(0, color=_C["text"], linewidth=0.8)
    ax.legend(fontsize=7, loc="upper left")
    _label_axes(ax, "EUR per interval", xlabel="")

    ax2 = axes[1]
    cumulative_net = np.cumsum(net_ts)
    ax2.plot(t, cumulative_net, color=_C["neutral"], linewidth=1.8)
    ax2.fill_between(
        t, 0, cumulative_net, where=cumulative_net >= 0,
        color=_C["discharge"], alpha=0.15,
    )
    ax2.fill_between(
        t, 0, cumulative_net, where=cumulative_net < 0, color=_C["charge"], alpha=0.15
    )
    ax2.axhline(0, color=_C["text"], linewidth=0.8)
    _label_axes(ax2, "Cumulative EUR")

    fig.tight_layout()
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# Chart 2 — SoC headroom and utilisation
# ══════════════════════════════════════════════════════════════════════════════


def _chart_soc_headroom(
    df_out: pd.DataFrame,
    prob: "OptimizationProblem",
) -> plt.Figure:
    """SoC trajectory with capacity bounds and utilisation fraction."""
    t = _time_axis(df_out)
    n = len(df_out)

    capacity = _param(prob, "capacity", 100.0)
    soc_min = 0.0
    soc_max = capacity

    soc = df_out["soc"].values[:n]
    utilisation = (
        (soc - soc_min) / (soc_max - soc_min)
        if (soc_max - soc_min) > 0
        else np.zeros(n)
    )

    fig, axes = _make_fig(2, "SoC Headroom and Capacity Utilisation")

    ax = axes[0]
    ax.fill_between(
        t, soc_min, soc_max, color=_C["neutral"], alpha=0.08, label="Available range"
    )
    ax.fill_between(
        t, soc_min, soc, color=_C["neutral"], alpha=0.35, label="Stored energy"
    )
    ax.plot(t, soc, color=_C["neutral"], linewidth=1.8, label="SoC")
    ax.axhline(
        soc_max, color=_C["warn"], linewidth=1.0, linestyle="--",
        label=f"Max ({soc_max:.0f} MWh)",
    )
    ax.axhline(
        soc_min, color=_C["charge"], linewidth=1.0, linestyle="--",
        label=f"Min ({soc_min:.0f} MWh)",
    )
    ax.legend(fontsize=7, loc="upper right")
    _label_axes(ax, "State of Charge (MWh)", xlabel="")

    ax2 = axes[1]
    ax2.fill_between(t, 0, utilisation * 100, color=_C["neutral"], alpha=0.3)
    ax2.plot(t, utilisation * 100, color=_C["neutral"], linewidth=1.5)
    ax2.axhline(
        90, color=_C["warn"], linewidth=0.8, linestyle=":", label="90% threshold"
    )
    ax2.axhline(
        10, color=_C["charge"], linewidth=0.8, linestyle=":", label="10% threshold"
    )
    ax2.set_ylim(0, 105)
    ax2.legend(fontsize=7)
    _label_axes(ax2, "Utilisation (%)")

    fig.tight_layout()
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# Chart 3 — Decision rationale overlay (scheduling only)
# ══════════════════════════════════════════════════════════════════════════════


def _chart_decision_rationale_scheduling(
    df_out: pd.DataFrame,
    df_in: pd.DataFrame,
    cycling_penalty: float,
    prob: "OptimizationProblem",
) -> plt.Figure:
    """Annotated price chart explaining charge / discharge / idle decisions."""
    t = _time_axis(df_out)
    n = len(df_out)
    dt = (t[1] - t[0]) if len(t) > 1 else 1.0

    price = df_in["price"].values[:n]
    charge = df_out["charge_power"].values[:n]
    discharge = df_out["discharge_power"].values[:n]

    efficiency = _param(prob, "efficiency", 0.81)
    sqrt_eff = efficiency**0.5 if efficiency > 0 else 1.0

    fee_in = (
        df_in["grid_fee_in"].values[:n]
        if "grid_fee_in" in df_in.columns
        else np.zeros(n)
    )
    fee_out = (
        df_in["grid_fee_out"].values[:n]
        if "grid_fee_out" in df_in.columns
        else np.zeros(n)
    )

    # Round-trip efficiency split per leg: charging stores only sqrt_eff of
    # each MWh bought, so the effective cost per *stored* MWh is price/sqrt_eff;
    # discharging delivers only sqrt_eff of each MWh drawn, so the effective
    # revenue per *stored* MWh is price*sqrt_eff.
    effective_charge_price = price / sqrt_eff + cycling_penalty + fee_in
    effective_discharge_price = price * sqrt_eff - cycling_penalty - fee_out

    breakeven_spread = 2.0 * cycling_penalty / sqrt_eff + (fee_in + fee_out) / sqrt_eff

    is_charging = charge > 0.01
    is_discharging = discharge > 0.01

    fig, axes = _make_fig(2, "Decision Rationale — Why Did the Optimiser Trade Here?")

    ax = axes[0]

    for i in range(n):
        x0 = t[i]
        x1 = t[i] + dt
        if is_charging[i]:
            ax.axvspan(x0, x1, color=_C["charge"], alpha=0.15)
        elif is_discharging[i]:
            ax.axvspan(x0, x1, color=_C["discharge"], alpha=0.15)
        else:
            ax.axvspan(x0, x1, color=_C["idle"], alpha=0.05)

    ax.step(
        t, price, where="post", color=_C["text"], linewidth=2.0, label="Market price"
    )
    ax.step(
        t, effective_charge_price, where="post", color=_C["charge"],
        linewidth=1.2, linestyle="--", label="Effective charge threshold",
    )
    ax.step(
        t, effective_discharge_price, where="post", color=_C["discharge"],
        linewidth=1.2, linestyle="--", label="Effective discharge threshold",
    )

    _label_axes(ax, "Price (EUR/MWh)", xlabel="")

    from matplotlib.patches import Patch

    legend_patches = [
        Patch(color=_C["charge"], alpha=0.3, label="Charging"),
        Patch(color=_C["discharge"], alpha=0.3, label="Discharging"),
        Patch(color=_C["idle"], alpha=0.2, label="Idle"),
    ]
    ax.legend(
        handles=legend_patches + ax.get_legend_handles_labels()[0][:3],
        fontsize=7,
        loc="upper left",
    )

    ax2 = axes[1]
    realised_spread = effective_discharge_price - effective_charge_price
    ax2.fill_between(
        t, 0, breakeven_spread, color=_C["warn"], alpha=0.2,
        label=f"Break-even spread ({breakeven_spread.mean():.2f} EUR/MWh avg)",
    )
    ax2.step(
        t, np.maximum(realised_spread, 0), where="post", color=_C["discharge"],
        linewidth=1.5, label="Profitable spread (effective)",
    )
    ax2.step(
        t, np.minimum(realised_spread, 0), where="post", color=_C["charge"],
        linewidth=1.0, label="Unprofitable region",
    )
    ax2.axhline(0, color=_C["text"], linewidth=0.8)
    ax2.legend(fontsize=7, loc="upper right")
    _label_axes(ax2, "Spread (EUR/MWh)")

    fig.tight_layout()
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# Chart 4 — Spread duration (intraday only)
# ══════════════════════════════════════════════════════════════════════════════


def _chart_spread_duration(
    df_out: pd.DataFrame,
    df_in: pd.DataFrame,
    n_segments: int,
    cycling_penalty: float,
    transaction_cost: float,
    efficiency: float,
) -> plt.Figure:
    """How much profitable round-trip spread the orderbook offered.

    Top: best bid/ask price-duration curves (sorted), with the volume-weighted
    prices the battery actually charged/discharged at.  Bottom: the sorted
    round-trip gross spread against the break-even threshold — the shaded area
    is every interval where cycling cleared its cost.
    """
    n = len(df_out)
    if (
        n == 0
        or "bid_prices[1]" not in df_in.columns
        or "ask_prices[1]" not in df_in.columns
    ):
        fig, axes = _make_fig(1, "Spread Duration — no orderbook data")
        axes[0].text(
            0.5, 0.5, "No orderbook price data available",
            ha="center", va="center", transform=axes[0].transAxes, color=_C["text"],
        )
        fig.tight_layout()
        return fig

    best_bid = df_in["bid_prices[1]"].to_numpy(dtype=float)[:n]
    best_ask = df_in["ask_prices[1]"].to_numpy(dtype=float)[:n]

    charge_mw = 0.0
    charge_val = 0.0
    discharge_mw = 0.0
    discharge_val = 0.0
    for seg in range(1, n_segments + 1):
        ask_pw, ap = f"charge_power_asks[{seg}]", f"ask_prices[{seg}]"
        bid_pw, bp = f"discharge_power_bids[{seg}]", f"bid_prices[{seg}]"
        if ask_pw in df_out.columns and ap in df_in.columns:
            pw = df_out[ask_pw].to_numpy(dtype=float)[:n]
            charge_mw += float(pw.sum())
            charge_val += float((pw * df_in[ap].to_numpy(dtype=float)[:n]).sum())
        if bid_pw in df_out.columns and bp in df_in.columns:
            pw = df_out[bid_pw].to_numpy(dtype=float)[:n]
            discharge_mw += float(pw.sum())
            discharge_val += float((pw * df_in[bp].to_numpy(dtype=float)[:n]).sum())
    vwap_charge = charge_val / charge_mw if charge_mw > 1e-9 else None
    vwap_discharge = discharge_val / discharge_mw if discharge_mw > 1e-9 else None

    ask_sorted = np.sort(best_ask)  # cheapest charging opportunities first
    bid_sorted = np.sort(best_bid)[::-1]  # best discharge opportunities first
    rank = np.arange(1, n + 1)

    sqrt_eff = efficiency**0.5 if efficiency > 0 else 1.0
    spread_curve = bid_sorted - ask_sorted
    mid = float(np.median(np.concatenate([best_bid, best_ask])))
    breakeven = 2.0 * (cycling_penalty + transaction_cost) + mid * (
        1.0 / sqrt_eff - sqrt_eff
    )
    n_profitable = int(np.sum(spread_curve > breakeven))

    fig, axes = _make_fig(
        2, "Spread Duration — How Much Profitable Spread the Market Offered"
    )

    ax = axes[0]
    ax.plot(
        rank, ask_sorted, color=_C["charge"], linewidth=1.8,
        label="Ask price (sorted cheap → dear)",
    )
    ax.plot(
        rank, bid_sorted, color=_C["discharge"], linewidth=1.8,
        label="Bid price (sorted high → low)",
    )
    if vwap_charge is not None:
        ax.axhline(
            vwap_charge, color=_C["charge"], linestyle="--", linewidth=1.0,
            label=f"Avg charge price ({vwap_charge:.1f})",
        )
    if vwap_discharge is not None:
        ax.axhline(
            vwap_discharge, color=_C["discharge"], linestyle="--", linewidth=1.0,
            label=f"Avg discharge price ({vwap_discharge:.1f})",
        )
    ax.legend(fontsize=7, loc="upper right")
    ax.set_title("Best bid / ask price-duration curves", color=_C["text"], fontsize=9)
    _label_axes(ax, "Price (EUR/MWh)", xlabel="")

    ax2 = axes[1]
    ax2.fill_between(
        rank, breakeven, spread_curve, where=spread_curve > breakeven,
        color=_C["discharge"], alpha=0.25,
    )
    ax2.plot(
        rank, spread_curve, color=_C["neutral"], linewidth=1.8,
        label="Round-trip gross spread (sorted)",
    )
    ax2.axhline(
        breakeven, color=_C["warn"], linestyle="--", linewidth=1.2,
        label=f"Break-even spread ({breakeven:.1f} EUR/MWh)",
    )
    ax2.axhline(0, color=_C["text"], linewidth=0.8)
    ax2.legend(fontsize=7, loc="upper right")
    _label_axes(ax2, "Spread (EUR/MWh)", xlabel="Interval rank")

    fig.canvas.draw()
    if n_profitable > 0 and spread_curve.size:
        placed: list[tuple[float, float, float, float]] = []
        _place_label(
            ax2,
            max(1.0, n_profitable / 2.0),
            breakeven + (float(spread_curve.max()) - breakeven) * 0.4,
            f"{n_profitable} of {n} intervals\nclear break-even",
            placed=placed,
            fontsize=8,
            color=_C["text"],
            ec=_C["neutral"],
            x_range=(1.0, float(n)),
        )

    fig.tight_layout()
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# Chart 5 — Committed position (intraday only)
# ══════════════════════════════════════════════════════════════════════════════


def _chart_committed_position(
    df_out: pd.DataFrame,
    df_in: pd.DataFrame,
    prob: "OptimizationProblem",
) -> plt.Figure:
    """Committed obligation vs discretionary trades, and the feasibility it forces.

    Top: per-interval committed power (the inherited obligation) with the
    optimiser's incremental trades stacked on top.  Bottom: the actual SoC
    against the SoC the committed schedule alone would produce — where the
    latter dips below zero, the SoC ≥ 0 constraint forces compensating charging.
    """
    n = len(df_out)
    dt = _dt_hours(df_out)
    efficiency = _param(prob, "efficiency", 0.81)
    capacity = _param(prob, "capacity", 100.0)
    st = _committed_position_stats(df_out, df_in, dt, efficiency, capacity)

    t = _time_axis(df_out)
    width = (t[1] - t[0]) * 0.8 if len(t) > 1 else 0.8

    fig, axes = _make_fig(
        2, "Committed Position — Inherited Obligation vs Discretionary Trades"
    )

    ax = axes[0]
    ax.bar(
        t, st["committed_discharge"], width=width, color=_C["discharge"],
        label="Committed discharge (obligation)",
    )
    ax.bar(
        t, -st["committed_charge"], width=width, color=_C["charge"],
        label="Committed charge (obligation)",
    )
    ax.bar(
        t, st["incr_discharge"], width=width, bottom=st["committed_discharge"],
        color=_C["neutral"], alpha=0.75, label="Incremental discharge (traded)",
    )
    ax.bar(
        t, -st["incr_charge"], width=width, bottom=-st["committed_charge"],
        color=_C["warn"], alpha=0.75, label="Incremental charge (traded)",
    )
    ax.axhline(0, color=_C["text"], linewidth=0.8)
    ax.legend(fontsize=7, loc="upper right")
    ax.set_title(
        "Power per interval — discharge positive, charge negative",
        color=_C["text"], fontsize=9,
    )
    _label_axes(ax, "Power (MW)", xlabel="")

    ax2 = axes[1]
    actual = st["actual_soc"][:n]
    committed_soc = st["committed_soc"][:n]
    ax2.plot(t, actual, color=_C["neutral"], linewidth=1.8, label="Actual SoC")
    ax2.plot(
        t, committed_soc, color=_C["charge"], linewidth=1.5, linestyle="--",
        label="SoC if only the committed schedule ran",
    )
    ax2.fill_between(
        t, 0, np.minimum(committed_soc, 0.0), color=_C["charge"], alpha=0.2
    )
    ax2.axhline(0, color=_C["text"], linewidth=0.8)
    ax2.axhline(
        capacity, color=_C["warn"], linewidth=1.0, linestyle=":",
        label=f"Capacity ({capacity:.0f} MWh)",
    )
    ax2.legend(fontsize=7, loc="upper right")
    ax2.set_title(
        "Where the committed-only SoC breaches 0, charging is required for "
        "feasibility",
        color=_C["text"], fontsize=9,
    )
    _label_axes(ax2, "State of Charge (MWh)")

    fig.tight_layout()
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# Reasoning data — components, cycles, tables, metrics (no rendering)
# ══════════════════════════════════════════════════════════════════════════════


def _dt_hours(df: pd.DataFrame, default: float = 0.25) -> float:
    t = _time_axis(df)
    return float(t[1] - t[0]) if len(t) > 1 else default


def _revenue_components_intraday(
    df_out: pd.DataFrame,
    df_in: pd.DataFrame,
    n_segments: int,
    cycling_penalty: float,
    transaction_cost: float,
    dt: float,
) -> dict[str, np.ndarray]:
    """Per-interval cashflow components (EUR) for the intraday solver."""
    n = len(df_out)
    # The intraday objective (bess_intraday.py:path_objective) scores only
    # INCREMENTAL trades — the per-segment charge_power_asks / discharge_power_bids
    # decision variables.  The committed position enters the model purely as a
    # fixed input to the SoC dynamics and carries no price, so it incurs neither
    # revenue nor any penalty.  Every term below is therefore computed on
    # incremental flows, mirroring the solver's own objective exactly.
    incr_charge = np.zeros(n)
    incr_discharge = np.zeros(n)
    discharge_rev = np.zeros(n)
    charge_cost = np.zeros(n)
    for seg in range(1, n_segments + 1):
        bid_col, bp = f"discharge_power_bids[{seg}]", f"bid_prices[{seg}]"
        ask_col, ap = f"charge_power_asks[{seg}]", f"ask_prices[{seg}]"
        if bid_col in df_out.columns:
            bid_pw = df_out[bid_col].to_numpy(dtype=float)[:n]
            incr_discharge += bid_pw
            if bp in df_in.columns:
                discharge_rev += bid_pw * df_in[bp].to_numpy(dtype=float)[:n]
        if ask_col in df_out.columns:
            ask_pw = df_out[ask_col].to_numpy(dtype=float)[:n]
            incr_charge += ask_pw
            if ap in df_in.columns:
                charge_cost += ask_pw * df_in[ap].to_numpy(dtype=float)[:n]
    discharge_rev = discharge_rev * dt
    charge_cost = charge_cost * dt
    fee_in = (
        df_in["grid_fee_in"].to_numpy(dtype=float)[:n]
        if "grid_fee_in" in df_in.columns
        else np.zeros(n)
    )
    fee_out = (
        df_in["grid_fee_out"].to_numpy(dtype=float)[:n]
        if "grid_fee_out" in df_in.columns
        else np.zeros(n)
    )
    cycling = cycling_penalty * (incr_charge + incr_discharge) * dt
    transaction = transaction_cost * (incr_charge + incr_discharge) * dt
    grid_fee = (fee_in * incr_charge + fee_out * incr_discharge) * dt
    gross = discharge_rev - charge_cost
    return {
        "discharge_rev": discharge_rev,
        "charge_cost": charge_cost,
        "gross_rev": gross,
        "cycling": cycling,
        "transaction": transaction,
        "grid_fee": grid_fee,
        "net": gross - cycling - transaction - grid_fee,
        "incr_charge": incr_charge,
        "incr_discharge": incr_discharge,
    }


def _revenue_components_scheduling(
    df_out: pd.DataFrame,
    df_in: pd.DataFrame,
    cycling_penalty: float,
    dt: float,
) -> dict[str, np.ndarray]:
    """Per-interval cashflow components (EUR) for the scheduling solver."""
    n = len(df_out)
    charge = df_out["charge_power"].to_numpy(dtype=float)
    discharge = df_out["discharge_power"].to_numpy(dtype=float)
    price = (
        df_in["price"].to_numpy(dtype=float)[:n]
        if "price" in df_in.columns
        else np.zeros(n)
    )
    fee_in = (
        df_in["grid_fee_in"].to_numpy(dtype=float)[:n]
        if "grid_fee_in" in df_in.columns
        else np.zeros(n)
    )
    fee_out = (
        df_in["grid_fee_out"].to_numpy(dtype=float)[:n]
        if "grid_fee_out" in df_in.columns
        else np.zeros(n)
    )
    discharge_rev = discharge * price * dt
    charge_cost = charge * price * dt
    cycling = cycling_penalty * (charge + discharge) * dt
    grid_fee = (fee_in * charge + fee_out * discharge) * dt
    gross = discharge_rev - charge_cost
    return {
        "discharge_rev": discharge_rev,
        "charge_cost": charge_cost,
        "gross_rev": gross,
        "cycling": cycling,
        "transaction": np.zeros(n),
        "grid_fee": grid_fee,
        "net": gross - cycling - grid_fee,
    }


def _simulate_soc(
    charge: np.ndarray,
    discharge: np.ndarray,
    soc0: float,
    efficiency: float,
    dt_hours: float,
) -> np.ndarray:
    """Integrate the model's SoC equation for a given charge/discharge profile.

    Mirrors ``BESSIntraday.mo``: ``3600*der(soc) = charge*sqrt(eff)
    - discharge/sqrt(eff)``.  Returns SoC at each interval boundary
    (length ``len(charge) + 1``).
    """
    sqrt_eff = efficiency**0.5 if efficiency > 0 else 1.0
    soc = np.empty(len(charge) + 1)
    soc[0] = soc0
    s = soc0
    for i in range(len(charge)):
        s += (charge[i] * sqrt_eff - discharge[i] / sqrt_eff) * dt_hours
        soc[i + 1] = s
    return soc


def _committed_position_stats(
    df_out: pd.DataFrame,
    df_in: pd.DataFrame,
    dt_hours: float,
    efficiency: float,
    capacity: float,
) -> dict[str, Any]:
    """Split battery activity into the inherited committed obligation and the
    optimiser's discretionary incremental trades.

    The committed position (``committed_charge`` / ``committed_discharge``) is a
    fixed model input; ``charge_power`` / ``discharge_power`` are the gross
    flows, and the Modelica model guarantees gross = committed + incremental, so
    incremental = gross − committed.

    ``forced_charge_mwh`` is derived purely from the optimiser's own constraint
    set: simulating the committed schedule alone against the SoC dynamics, the
    depth it breaches below the SoC ≥ 0 floor is the charging any feasible
    solution must add — a property of the constraints, independent of price.
    """
    n = len(df_out)
    charge = df_out["charge_power"].to_numpy(dtype=float)
    discharge = df_out["discharge_power"].to_numpy(dtype=float)
    soc = df_out["soc"].to_numpy(dtype=float)
    committed_charge = (
        df_in["committed_charge"].to_numpy(dtype=float)[:n]
        if "committed_charge" in df_in.columns
        else np.zeros(n)
    )
    committed_discharge = (
        df_in["committed_discharge"].to_numpy(dtype=float)[:n]
        if "committed_discharge" in df_in.columns
        else np.zeros(n)
    )
    incr_charge = np.clip(charge - committed_charge, 0.0, None)
    incr_discharge = np.clip(discharge - committed_discharge, 0.0, None)
    soc0 = float(soc[0]) if n else 0.0
    committed_soc = _simulate_soc(
        committed_charge, committed_discharge, soc0, efficiency, dt_hours
    )
    trough = float(np.min(committed_soc)) if committed_soc.size else soc0
    trough_idx = int(np.argmin(committed_soc)) if committed_soc.size else 0
    committed_charged = float(np.sum(committed_charge)) * dt_hours
    committed_discharged = float(np.sum(committed_discharge)) * dt_hours
    gross_throughput = float(np.sum(charge + discharge)) * dt_hours
    committed_throughput = committed_charged + committed_discharged
    return {
        "committed_charge": committed_charge,
        "committed_discharge": committed_discharge,
        "incr_charge": incr_charge,
        "incr_discharge": incr_discharge,
        "committed_soc": committed_soc,
        "actual_soc": soc,
        "committed_charged_mwh": committed_charged,
        "committed_discharged_mwh": committed_discharged,
        "committed_net_mwh": committed_discharged - committed_charged,
        "incremental_charged_mwh": float(np.sum(incr_charge)) * dt_hours,
        "incremental_discharged_mwh": float(np.sum(incr_discharge)) * dt_hours,
        "n_committed_intervals": int(
            np.sum((committed_charge > 0.01) | (committed_discharge > 0.01))
        ),
        "peak_committed_charge_mw": float(np.max(committed_charge)) if n else 0.0,
        "peak_committed_discharge_mw": (
            float(np.max(committed_discharge)) if n else 0.0
        ),
        "committed_share_of_throughput": (
            committed_throughput / gross_throughput
            if gross_throughput > 1e-9
            else 0.0
        ),
        "committed_soc_trough_mwh": trough,
        "committed_soc_trough_interval": trough_idx,
        "forced_charge_mwh": max(0.0, -trough),
    }


def _detect_episodes(
    charge: np.ndarray, discharge: np.ndarray, min_power: float = 0.01
) -> list[dict[str, Any]]:
    """Group consecutive intervals into charge / discharge episodes.

    Idle intervals (both flows below *min_power*) break episodes.  When both
    flows are active in one interval the dominant one classifies it.
    """
    episodes: list[dict[str, Any]] = []
    cur_kind: str | None = None
    cur: list[int] = []
    for i in range(len(charge)):
        c = charge[i] > min_power
        d = discharge[i] > min_power
        if c and d:
            kind: str | None = "discharge" if discharge[i] >= charge[i] else "charge"
        elif c:
            kind = "charge"
        elif d:
            kind = "discharge"
        else:
            kind = None
        if kind != cur_kind:
            if cur and cur_kind is not None:
                episodes.append({"kind": cur_kind, "ptus": cur})
            cur = []
            cur_kind = kind
        if kind is not None:
            cur.append(i)
    if cur and cur_kind is not None:
        episodes.append({"kind": cur_kind, "ptus": cur})
    return episodes


def _ptu_label(episode: dict[str, Any] | None) -> str:
    if not episode or not episode["ptus"]:
        return "—"
    ptus = episode["ptus"]
    if ptus[0] == ptus[-1]:
        return f"#{ptus[0]}"
    return f"#{ptus[0]}–#{ptus[-1]}"


def _build_cycle_rows(
    episodes: list[dict[str, Any]],
    comp: dict[str, np.ndarray],
    charge: np.ndarray,
    discharge: np.ndarray,
    dt: float,
    capacity: float,
) -> list[dict[str, Any]]:
    """Pair charge/discharge episodes into cycles and rank them by net margin.

    A cycle is a charge episode paired with a later discharge episode.
    Pairing is FIFO — the earliest unconsumed charge is matched to the next
    discharge — which mirrors the natural buy-then-sell arbitrage order.
    Unpaired episodes (discharging the initial SoC, or charging for the next
    horizon) are kept as standalone rows.  Rows are sorted descending by net
    margin — the merit order — so diminishing returns of successive cycles
    are visible.
    """
    pending: list[dict[str, Any]] = []
    pairs: list[tuple[dict | None, dict | None]] = []
    for ep in episodes:
        if ep["kind"] == "charge":
            pending.append(ep)
        else:  # discharge — FIFO: match the earliest unconsumed charge
            pairs.append((pending.pop(0) if pending else None, ep))
    for ep in pending:
        pairs.append((ep, None))

    rows: list[dict[str, Any]] = []
    for ch, dis in pairs:
        ptus = (ch["ptus"] if ch else []) + (dis["ptus"] if dis else [])
        charge_eur = sum(comp["charge_cost"][i] for i in (ch["ptus"] if ch else []))
        discharge_eur = sum(
            comp["discharge_rev"][i] for i in (dis["ptus"] if dis else [])
        )
        cycling_eur = sum(comp["cycling"][i] for i in ptus)
        transaction_eur = sum(comp["transaction"][i] for i in ptus)
        fee_eur = sum(comp["grid_fee"][i] for i in ptus)
        net = discharge_eur - charge_eur - cycling_eur - transaction_eur - fee_eur
        throughput = sum(charge[i] + discharge[i] for i in ptus) * dt
        rows.append(
            {
                "kind": "cycle"
                if ch and dis
                else ("charge-only" if ch else "discharge-only"),
                "charge_label": _ptu_label(ch),
                "discharge_label": _ptu_label(dis),
                "charge_eur": float(charge_eur),
                "discharge_eur": float(discharge_eur),
                "cycling_eur": float(cycling_eur),
                "transaction_eur": float(transaction_eur),
                "grid_fee_eur": float(fee_eur),
                "net_eur": float(net),
                "equiv_cycles": float(throughput / (2.0 * capacity))
                if capacity > 0
                else 0.0,
            }
        )

    rows.sort(key=lambda r: r["net_eur"], reverse=True)
    cumulative = 0.0
    for rank, row in enumerate(rows, start=1):
        cumulative += row["net_eur"]
        row["rank"] = rank
        row["cumulative_eur"] = cumulative
    return rows


def _solver_stats(prob: "OptimizationProblem") -> dict[str, Any]:
    """Extract solver status / objective / timing — formerly the shadow_prices panel."""
    out: dict[str, Any] = {}
    try:
        stats = prob.solver_stats
        out["solver_status"] = stats.get("return_status", "unknown")
        out["solver_wall_time"] = _safe_float(
            stats.get("t_wall_total") or stats.get("t_wall_solver")
        )
    except Exception:
        out["solver_status"] = "unknown"
        out["solver_wall_time"] = None
    out["objective_value"] = _safe_float(getattr(prob, "objective_value", None))
    try:
        _, lam_x = prob.lagrange_multipliers
        out["n_nlp_variables"] = (
            int(len(np.array(lam_x).ravel())) if lam_x is not None else None
        )
    except Exception:
        out["n_nlp_variables"] = None
    return out


def _constraint_binding_stats(
    df_out: pd.DataFrame, prob: "OptimizationProblem"
) -> list[dict[str, Any]]:
    """How often each physical limit is binding — replaces the heatmap chart."""
    n = len(df_out)
    capacity = _param(prob, "capacity", 100.0)
    max_power = _param(prob, "max_power", 50.0)
    soc = df_out["soc"].to_numpy(dtype=float)
    charge = df_out["charge_power"].to_numpy(dtype=float)
    discharge = df_out["discharge_power"].to_numpy(dtype=float)

    rows: list[dict[str, Any]] = []

    def _row(label: str, mask: np.ndarray) -> None:
        count = int(np.sum(mask))
        rows.append(
            {
                "constraint": label,
                "count": count,
                "intervals": n,
                "pct": (100.0 * count / n) if n else 0.0,
            }
        )

    _row("SoC at full capacity", soc >= capacity * 0.99)
    _row("SoC fully drained", soc <= capacity * 0.01)
    _row("Charge power at limit", charge >= max_power * 0.99)
    _row("Discharge power at limit", discharge >= max_power * 0.99)
    _row("Battery active (charging or discharging)", (charge > 0.01) | (discharge > 0.01))
    return rows


def _orderbook_depth_stats(
    df_out: pd.DataFrame, df_in: pd.DataFrame, n_segments: int
) -> list[dict[str, Any]]:
    """Average fill per orderbook level — replaces the two utilisation heatmaps."""
    n = len(df_out)
    rows: list[dict[str, Any]] = []
    for seg in range(1, n_segments + 1):
        ask_pw, ask_vol = f"charge_power_asks[{seg}]", f"ask_volumes[{seg}]"
        bid_pw, bid_vol = f"discharge_power_bids[{seg}]", f"bid_volumes[{seg}]"
        charge_fill = discharge_fill = 0.0
        charge_sum = discharge_sum = 0.0
        if ask_pw in df_out.columns:
            pw = df_out[ask_pw].to_numpy(dtype=float)
            charge_sum = float(pw.sum())
            if ask_vol in df_in.columns:
                vol = df_in[ask_vol].to_numpy(dtype=float)[:n]
                with np.errstate(divide="ignore", invalid="ignore"):
                    frac = np.where(vol > 0, np.clip(pw / vol, 0.0, 1.0), 0.0)
                charge_fill = float(np.mean(frac)) * 100.0 if n else 0.0
        if bid_pw in df_out.columns:
            pw = df_out[bid_pw].to_numpy(dtype=float)
            discharge_sum = float(pw.sum())
            if bid_vol in df_in.columns:
                vol = df_in[bid_vol].to_numpy(dtype=float)[:n]
                with np.errstate(divide="ignore", invalid="ignore"):
                    frac = np.where(vol > 0, np.clip(pw / vol, 0.0, 1.0), 0.0)
                discharge_fill = float(np.mean(frac)) * 100.0 if n else 0.0
        rows.append(
            {
                "level": seg,
                "charge_fill_pct": charge_fill,
                "discharge_fill_pct": discharge_fill,
                "charge_mw_sum": charge_sum,
                "discharge_mw_sum": discharge_sum,
            }
        )
    return rows


def _schedule_rows_scheduling(
    df_out: pd.DataFrame, df_in: pd.DataFrame
) -> list[dict[str, Any]]:
    """Per-interval action table for the scheduling full-day schedule."""
    n = len(df_out)
    price = (
        df_in["price"].to_numpy(dtype=float)[:n]
        if "price" in df_in.columns
        else np.zeros(n)
    )
    soc = df_out["soc"].to_numpy(dtype=float)
    charge = df_out["charge_power"].to_numpy(dtype=float)
    discharge = df_out["discharge_power"].to_numpy(dtype=float)
    rows: list[dict[str, Any]] = []
    for i in range(n):
        if charge[i] > 0.01:
            action, mw = "Charge", float(charge[i])
        elif discharge[i] > 0.01:
            action, mw = "Discharge", float(discharge[i])
        else:
            action, mw = "Idle", 0.0
        rows.append(
            {
                "interval": i,
                "price": float(price[i]),
                "action": action,
                "mw": mw,
                "soc": float(soc[i]),
            }
        )
    return rows


def _collect_intraday_metrics(
    df_out: pd.DataFrame,
    df_in: pd.DataFrame,
    n_segments: int,
    cycling_penalty: float,
    transaction_cost: float,
    prob: "OptimizationProblem",
) -> dict[str, Any]:
    """Scalar KPIs for the intraday run — the structured backtest record."""
    n = len(df_out)
    dt = _dt_hours(df_out)
    charge = df_out["charge_power"].to_numpy(dtype=float)
    discharge = df_out["discharge_power"].to_numpy(dtype=float)
    soc = df_out["soc"].to_numpy(dtype=float)
    capacity = _param(prob, "capacity", 100.0)
    comp = _revenue_components_intraday(
        df_out, df_in, n_segments, cycling_penalty, transaction_cost, dt
    )
    total_charged = float(np.sum(charge)) * dt
    total_discharged = float(np.sum(discharge)) * dt
    throughput = total_charged + total_discharged
    metrics: dict[str, Any] = {
        "horizon_intervals": n,
        "interval_minutes": dt * 60.0,
        "cycling_penalty_factor": cycling_penalty,
        "transaction_cost": transaction_cost,
        "capacity_mwh": capacity,
        "total_charged_mwh": total_charged,
        "total_discharged_mwh": total_discharged,
        "throughput_mwh": throughput,
        "equivalent_full_cycles": throughput / (2.0 * capacity)
        if capacity > 0
        else 0.0,
        "total_revenue_eur": float(np.sum(comp["gross_rev"])),
        "total_cycling_penalty_eur": float(np.sum(comp["cycling"])),
        "total_transaction_cost_eur": float(np.sum(comp["transaction"])),
        "total_grid_fee_eur": float(np.sum(comp["grid_fee"])),
        "n_charge_intervals": int(np.sum(charge > 0.01)),
        "n_discharge_intervals": int(np.sum(discharge > 0.01)),
        "initial_soc_mwh": float(soc[0]) if n else 0.0,
        "final_soc_mwh": float(soc[-1]) if n else 0.0,
    }
    metrics["net_profit_eur"] = (
        metrics["total_revenue_eur"]
        - metrics["total_cycling_penalty_eur"]
        - metrics["total_transaction_cost_eur"]
        - metrics["total_grid_fee_eur"]
    )
    # Committed-position breakdown: scalar keys only (drop the array entries).
    efficiency = _param(prob, "efficiency", 0.81)
    for key, val in _committed_position_stats(
        df_out, df_in, dt, efficiency, capacity
    ).items():
        if not isinstance(val, np.ndarray):
            metrics[key] = val
    metrics.update(_solver_stats(prob))
    return metrics


def _collect_scheduling_metrics(
    df_out: pd.DataFrame,
    df_in: pd.DataFrame,
    cycling_penalty: float,
    prob: "OptimizationProblem",
) -> dict[str, Any]:
    """Scalar KPIs for the scheduling run."""
    n = len(df_out)
    dt = _dt_hours(df_out, default=1.0)
    charge = df_out["charge_power"].to_numpy(dtype=float)
    discharge = df_out["discharge_power"].to_numpy(dtype=float)
    soc = df_out["soc"].to_numpy(dtype=float)
    capacity = _param(prob, "capacity", 100.0)
    comp = _revenue_components_scheduling(df_out, df_in, cycling_penalty, dt)
    total_charged = float(np.sum(charge)) * dt
    total_discharged = float(np.sum(discharge)) * dt
    throughput = total_charged + total_discharged
    metrics: dict[str, Any] = {
        "horizon_intervals": n,
        "interval_minutes": dt * 60.0,
        "cycling_penalty_factor": cycling_penalty,
        "capacity_mwh": capacity,
        "total_charged_mwh": total_charged,
        "total_discharged_mwh": total_discharged,
        "throughput_mwh": throughput,
        "equivalent_full_cycles": throughput / (2.0 * capacity)
        if capacity > 0
        else 0.0,
        "total_revenue_eur": float(np.sum(comp["gross_rev"])),
        "total_cycling_penalty_eur": float(np.sum(comp["cycling"])),
        "total_grid_fee_eur": float(np.sum(comp["grid_fee"])),
        "n_charge_intervals": int(np.sum(charge > 0.01)),
        "n_discharge_intervals": int(np.sum(discharge > 0.01)),
        "initial_soc_mwh": float(soc[0]) if n else 0.0,
        "final_soc_mwh": float(soc[-1]) if n else 0.0,
    }
    metrics["net_profit_eur"] = (
        metrics["total_revenue_eur"]
        - metrics["total_cycling_penalty_eur"]
        - metrics["total_grid_fee_eur"]
    )
    metrics.update(_solver_stats(prob))
    return metrics


# ══════════════════════════════════════════════════════════════════════════════
# Public entry points
# ══════════════════════════════════════════════════════════════════════════════


def _strip_dummy(df_full: pd.DataFrame, n: int) -> pd.DataFrame:
    """Strip the prepended dummy row + endpoint row (same logic as rtc_to_pe)."""
    if len(df_full) > n:
        return df_full.iloc[1 : n + 1].reset_index(drop=True)
    return df_full.copy()


def build_scheduling_diagnostics(
    output_dir: Any,
    model_input: dict[str, Any],
    cycling_penalty: float,
    prob: "OptimizationProblem",
) -> tuple[dict[str, str], list[str], str]:
    """Generate scheduling explainer charts, table data, and reasoning markdown.

    Returns ``(images, info_entries, reasoning_markdown)``.
    """
    from service.translation.reasoning import generate_scheduling_markdown

    t_start = time.monotonic()
    images: dict[str, str] = {}
    info: list[str] = []

    output_dir = Path(output_dir)
    csv_path = output_dir / "timeseries_export.csv"
    try:
        df_out_full = pd.read_csv(csv_path, parse_dates=["time"])
    except Exception as exc:
        info.append(f"diagnostics: skipped — could not read output CSV: {exc}")
        return images, info, ""

    interval_start = model_input.get("interval_start", [])
    n = len(interval_start)
    df_out = _strip_dummy(df_out_full, n)

    input_dir = output_dir.parent / "input"
    try:
        df_in_full = pd.read_csv(
            input_dir / "timeseries_import.csv", parse_dates=["time"]
        )
        df_in = _strip_dummy(df_in_full, n)
    except Exception:
        df_in = pd.DataFrame({"price": np.zeros(len(df_out))})

    chart_fns = [
        (
            "revenue_decomposition",
            lambda: _chart_revenue_decomposition_scheduling(
                df_out, df_in, cycling_penalty
            ),
        ),
        ("soc_headroom", lambda: _chart_soc_headroom(df_out, prob)),
        (
            "decision_rationale",
            lambda: _chart_decision_rationale_scheduling(
                df_out, df_in, cycling_penalty, prob
            ),
        ),
    ]
    for name, fn in chart_fns:
        try:
            fig = fn()
            if fig is None:
                continue
            images[name] = _fig_to_b64(fig)
        except Exception as exc:
            _log.warning("Diagnostic chart '%s' failed: %s", name, exc, exc_info=True)
            info.append(f"diagnostics: '{name}' failed — {exc}")

    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    info.append(
        f"diagnostics: generated {len(images)} scheduling chart(s) in {elapsed_ms}ms"
    )

    reasoning_markdown = ""
    try:
        dt = _dt_hours(df_out, default=1.0)
        comp = _revenue_components_scheduling(df_out, df_in, cycling_penalty, dt)
        charge = df_out["charge_power"].to_numpy(dtype=float)
        discharge = df_out["discharge_power"].to_numpy(dtype=float)
        episodes = _detect_episodes(charge, discharge)
        cycle_rows = _build_cycle_rows(
            episodes, comp, charge, discharge, dt, _param(prob, "capacity", 100.0)
        )
        reasoning_markdown = generate_scheduling_markdown(
            metrics=_collect_scheduling_metrics(df_out, df_in, cycling_penalty, prob),
            cycle_rows=cycle_rows,
            constraint_rows=_constraint_binding_stats(df_out, prob),
            schedule_rows=_schedule_rows_scheduling(df_out, df_in),
            images=images,
            info=info,
            model_input=model_input,
        )
    except Exception as exc:
        _log.warning("Reasoning markdown failed: %s", exc, exc_info=True)
        info.append(f"diagnostics: reasoning markdown failed — {exc}")

    return images, info, reasoning_markdown


def build_intraday_diagnostics(
    output_dir: Any,
    model_input: dict[str, Any],
    n_segments: int,
    cycling_penalty: float,
    transaction_cost: float,
    prob: "OptimizationProblem",
) -> tuple[dict[str, str], list[str], str]:
    """Generate intraday explainer charts, table data, and reasoning markdown.

    Returns ``(images, info_entries, reasoning_markdown)``.
    """
    from service.translation.reasoning import generate_intraday_markdown

    t_start = time.monotonic()
    images: dict[str, str] = {}
    info: list[str] = []

    output_dir = Path(output_dir)
    csv_path = output_dir / "timeseries_export.csv"
    try:
        df_out_full = pd.read_csv(csv_path, parse_dates=["time"])
    except Exception as exc:
        info.append(f"diagnostics: skipped — could not read output CSV: {exc}")
        return images, info, ""

    interval_start = model_input.get("interval_start", [])
    n = len(interval_start)
    df_out = _strip_dummy(df_out_full, n)

    input_dir = output_dir.parent / "input"
    try:
        df_in_full = pd.read_csv(
            input_dir / "timeseries_import.csv", parse_dates=["time"]
        )
        df_in = _strip_dummy(df_in_full, n)
    except Exception:
        df_in = pd.DataFrame()

    efficiency = _param(prob, "efficiency", 0.81)
    chart_fns = [
        (
            "revenue_decomposition",
            lambda: _chart_revenue_decomposition_intraday(
                df_out, df_in, n_segments, cycling_penalty, transaction_cost
            ),
        ),
        ("soc_headroom", lambda: _chart_soc_headroom(df_out, prob)),
        (
            "spread_duration",
            lambda: _chart_spread_duration(
                df_out, df_in, n_segments, cycling_penalty, transaction_cost, efficiency
            ),
        ),
        (
            "committed_position",
            lambda: _chart_committed_position(df_out, df_in, prob),
        ),
    ]
    for name, fn in chart_fns:
        try:
            fig = fn()
            if fig is None:
                continue
            images[name] = _fig_to_b64(fig)
        except Exception as exc:
            _log.warning("Diagnostic chart '%s' failed: %s", name, exc, exc_info=True)
            info.append(f"diagnostics: '{name}' failed — {exc}")

    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    info.append(
        f"diagnostics: generated {len(images)} intraday chart(s) in {elapsed_ms}ms"
    )

    reasoning_markdown = ""
    try:
        dt = _dt_hours(df_out)
        comp = _revenue_components_intraday(
            df_out, df_in, n_segments, cycling_penalty, transaction_cost, dt
        )
        # Detect cycles on INCREMENTAL flows so the merit order reflects the
        # optimiser's own trades, not the committed obligation it must deliver.
        incr_charge = comp["incr_charge"]
        incr_discharge = comp["incr_discharge"]
        episodes = _detect_episodes(incr_charge, incr_discharge)
        cycle_rows = _build_cycle_rows(
            episodes, comp, incr_charge, incr_discharge, dt,
            _param(prob, "capacity", 100.0),
        )
        reasoning_markdown = generate_intraday_markdown(
            metrics=_collect_intraday_metrics(
                df_out, df_in, n_segments, cycling_penalty, transaction_cost, prob
            ),
            cycle_rows=cycle_rows,
            constraint_rows=_constraint_binding_stats(df_out, prob),
            orderbook_rows=_orderbook_depth_stats(df_out, df_in, n_segments),
            images=images,
            info=info,
            model_input=model_input,
        )
    except Exception as exc:
        _log.warning("Reasoning markdown failed: %s", exc, exc_info=True)
        info.append(f"diagnostics: reasoning markdown failed — {exc}")

    return images, info, reasoning_markdown
