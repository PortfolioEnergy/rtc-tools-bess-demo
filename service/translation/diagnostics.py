"""Optimizer explainer diagrams for the BESS service.

Generates a set of diagnostic charts from RTC-Tools solver internals that
explain *why* the optimizer made each decision.  Charts are returned as a
``dict[str, str]`` mapping a chart name to a ``data:image/png;base64,…``
URI suitable for embedding directly in JSON responses.

All functions accept the ``prob`` object returned by
``rtctools.util.run_optimization_problem`` and the relevant input/output
DataFrames that the service already reads from CSV.  No re-solving occurs —
every computation is a cheap post-processing step on data that is already
available.

Performance is managed by:
- Using the ``Agg`` backend (no GUI, no display required).
- Rendering at 100 DPI with compact figure sizes.
- Calling ``plt.close(fig)`` immediately after encoding to release memory.
- Wrapping each chart in a try/except so a single broken chart never
  crashes the full diagnostic pass.

Two public entry points are provided:
- ``build_scheduling_diagnostics`` — day-ahead scheduling model
- ``build_intraday_diagnostics`` — continuous intraday trading model
"""

from __future__ import annotations

import base64
import io
import logging
import time
from typing import TYPE_CHECKING, Any

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Force non-interactive backend before any figure is created.
# This must happen before the first import of pyplot's display machinery.
matplotlib.use("Agg")

if TYPE_CHECKING:
    # Avoid a hard import of rtctools at module level so that the module can
    # be imported (and unit-tested) without a full RTC-Tools installation.
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
_DPI = 100


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


# ══════════════════════════════════════════════════════════════════════════════
# Chart 1 — Revenue decomposition waterfall
# ══════════════════════════════════════════════════════════════════════════════


def _chart_revenue_decomposition_scheduling(
    df_out: pd.DataFrame,
    df_in: pd.DataFrame,
    cycling_penalty: float,
) -> plt.Figure:
    """Waterfall of gross revenue, costs, and net profit over the horizon.

    Shows how each cost component erodes gross revenue so the operator can
    see which term most constrains optimiser aggressiveness.
    """
    t = _time_axis(df_out)
    # Align input timeseries length to output (output may have been trimmed)
    n = len(df_out)
    price = df_in["price"].values[:n]
    charge = df_out["charge_power"].values
    discharge = df_out["discharge_power"].values
    net_power = df_out["net_power"].values

    # dt in hours (uniform grid assumed)
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

    # Top: per-interval stacked bars showing value breakdown
    ax = axes[0]
    positive_rev = np.maximum(gross_rev_ts, 0)
    negative_rev = np.minimum(gross_rev_ts, 0)
    ax.bar(
        t,
        positive_rev,
        color=_C["discharge"],
        label="Gross revenue (discharge)",
        width=dt * 0.8,
    )
    ax.bar(
        t, negative_rev, color=_C["charge"], label="Gross cost (charge)", width=dt * 0.8
    )
    ax.bar(
        t,
        -grid_fee_in_ts - grid_fee_out_ts,
        bottom=negative_rev,
        color=_C["warn"],
        label="Grid fees",
        width=dt * 0.8,
        alpha=0.8,
    )
    ax.bar(
        t,
        -cycling_ts,
        bottom=negative_rev - grid_fee_in_ts - grid_fee_out_ts,
        color=_C["idle"],
        label="Cycling penalty",
        width=dt * 0.8,
        alpha=0.8,
    )
    ax.axhline(0, color=_C["text"], linewidth=0.8)
    ax.legend(fontsize=7, loc="upper left")
    _label_axes(ax, "EUR per interval", xlabel="")

    # Bottom: cumulative net profit
    ax2 = axes[1]
    cumulative_net = np.cumsum(net_ts)
    ax2.plot(
        t,
        cumulative_net,
        color=_C["neutral"],
        linewidth=1.8,
        label="Cumulative net profit",
    )
    ax2.fill_between(
        t,
        0,
        cumulative_net,
        where=cumulative_net >= 0,
        color=_C["discharge"],
        alpha=0.15,
    )
    ax2.fill_between(
        t, 0, cumulative_net, where=cumulative_net < 0, color=_C["charge"], alpha=0.15
    )
    ax2.axhline(0, color=_C["text"], linewidth=0.8)
    ax2.legend(fontsize=7)
    _label_axes(ax2, "Cumulative EUR")

    # Annotate final total
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

    # Aggregate revenue per segment and direction
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
        t,
        np.maximum(gross_rev_ts, 0),
        color=_C["discharge"],
        label="Trading revenue",
        width=dt * 0.8,
    )
    ax.bar(
        t,
        np.minimum(gross_rev_ts, 0),
        color=_C["charge"],
        label="Trading cost",
        width=dt * 0.8,
    )
    ax.bar(
        t,
        -cycling_ts - transaction_ts,
        bottom=np.minimum(gross_rev_ts, 0),
        color=_C["idle"],
        label="Cycling + transaction costs",
        width=dt * 0.8,
        alpha=0.8,
    )
    ax.axhline(0, color=_C["text"], linewidth=0.8)
    ax.legend(fontsize=7, loc="upper left")
    _label_axes(ax, "EUR per interval", xlabel="")

    ax2 = axes[1]
    cumulative_net = np.cumsum(net_ts)
    ax2.plot(t, cumulative_net, color=_C["neutral"], linewidth=1.8)
    ax2.fill_between(
        t,
        0,
        cumulative_net,
        where=cumulative_net >= 0,
        color=_C["discharge"],
        alpha=0.15,
    )
    ax2.fill_between(
        t, 0, cumulative_net, where=cumulative_net < 0, color=_C["charge"], alpha=0.15
    )
    ax2.axhline(0, color=_C["text"], linewidth=0.8)
    _label_axes(ax2, "Cumulative EUR")

    fig.tight_layout()
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# Chart 2 — Constraint tightness heatmap
# ══════════════════════════════════════════════════════════════════════════════


def _chart_constraint_tightness(
    df_out: pd.DataFrame,
    prob: "OptimizationProblem",
) -> plt.Figure:
    """How close each constraint is to its bound at every timestep.

    A value of 1.0 (red) means the constraint is at its limit; 0.0 (white)
    means it is completely loose.  This reveals where the optimiser is
    "pinned" against physical or operational limits.
    """
    t = _time_axis(df_out)
    n = len(df_out)

    try:
        params = prob.parameters(0)
        capacity = float(params.get("capacity", 100.0))
        max_power = float(params.get("max_power", 50.0))
    except Exception:
        capacity = 100.0
        max_power = 50.0

    soc = df_out["soc"].values[:n]
    charge = df_out["charge_power"].values[:n]
    discharge = df_out["discharge_power"].values[:n]

    # Tightness = how close to the binding limit, in [0, 1]
    # SoC upper bound (capacity)
    soc_upper = np.clip(soc / capacity, 0.0, 1.0)
    # SoC lower bound (zero) — closeness to draining
    soc_lower = np.clip(1.0 - soc / capacity, 0.0, 1.0)
    # Charge power limit
    charge_tight = np.clip(
        charge / max_power if max_power > 0 else np.zeros(n), 0.0, 1.0
    )
    # Discharge power limit
    discharge_tight = np.clip(
        discharge / max_power if max_power > 0 else np.zeros(n), 0.0, 1.0
    )
    # Complementarity: is_charging + is_discharging <= 1
    # Approximate as: how far from both simultaneously being active
    # (1.0 when one mode fully used, 0.0 when idle)
    mode_usage = np.clip(
        (charge + discharge) / max_power if max_power > 0 else np.zeros(n), 0.0, 1.0
    )

    # Build matrix: rows = constraint types, cols = timesteps
    matrix = np.vstack(
        [
            soc_upper,
            soc_lower,
            charge_tight,
            discharge_tight,
            mode_usage,
        ]
    )

    row_labels = [
        "SoC → full",
        "SoC → empty",
        "Charge at limit",
        "Discharge at limit",
        "Mode usage",
    ]

    fig, ax = plt.subplots(1, 1, figsize=(_FIG_W, 2.5), facecolor=_C["bg"])
    fig.suptitle(
        "Constraint Tightness (0 = loose, 1 = binding)",
        color=_C["text"],
        fontsize=11,
        fontweight="bold",
    )

    im = ax.imshow(
        matrix,
        aspect="auto",
        cmap="RdYlGn_r",
        vmin=0.0,
        vmax=1.0,
        interpolation="nearest",
        extent=[
            t[0] if len(t) else 0,
            t[-1] if len(t) else n,
            -0.5,
            len(row_labels) - 0.5,
        ],
    )
    ax.set_yticks(range(len(row_labels)))
    ax.set_yticklabels(row_labels, fontsize=8, color=_C["text"])
    ax.set_xlabel("Time (hours)", color=_C["text"], fontsize=9)
    ax.tick_params(colors=_C["text"])

    cbar = fig.colorbar(im, ax=ax, fraction=0.02, pad=0.02)
    cbar.ax.tick_params(colors=_C["text"])
    cbar.set_label("Tightness", color=_C["text"], fontsize=8)

    fig.tight_layout()
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# Chart 3 — SoC headroom and utilisation
# ══════════════════════════════════════════════════════════════════════════════


def _chart_soc_headroom(
    df_out: pd.DataFrame,
    prob: "OptimizationProblem",
) -> plt.Figure:
    """SoC trajectory with capacity bounds and utilisation fraction.

    The shaded band between min and max SoC reveals how much headroom the
    optimiser chose to keep and where SoC limits genuinely constrained
    the strategy.
    """
    t = _time_axis(df_out)
    n = len(df_out)

    try:
        params = prob.parameters(0)
        capacity = float(params.get("capacity", 100.0))
        soc_min = 0.0
        soc_max = capacity
    except Exception:
        capacity = 100.0
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
        soc_max,
        color=_C["warn"],
        linewidth=1.0,
        linestyle="--",
        label=f"Max ({soc_max:.0f} MWh)",
    )
    ax.axhline(
        soc_min,
        color=_C["charge"],
        linewidth=1.0,
        linestyle="--",
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
# Chart 4 — Shadow prices / Lagrange multipliers on SoC bounds
# ══════════════════════════════════════════════════════════════════════════════


def _chart_shadow_prices(
    df_out: pd.DataFrame,
    prob: "OptimizationProblem",
) -> plt.Figure | None:
    """Lagrange multipliers on the variable bounds, indicating opportunity cost.

    A non-zero shadow price on the SoC upper bound at time t means the
    optimiser would earn more if capacity were larger at that moment.  A
    non-zero value on the lower bound means it would earn more if the battery
    could discharge further.

    For MILP problems HiGHS may not provide duals.  When duals are
    unavailable this function returns ``None`` and the caller omits the chart.
    """
    try:
        lam_g, lam_x = prob.lagrange_multipliers
        if lam_x is None:
            return None
        lam_x_arr = np.array(lam_x).ravel()
        if len(lam_x_arr) == 0:
            return None
    except Exception:
        return None

    t = _time_axis(df_out)
    n = len(df_out)

    # lam_x is a vector over all decision variables in the collocation NLP.
    # We can't easily demultiplex which entries correspond to SoC without
    # detailed knowledge of the collocation structure, so we show the full
    # lam_x magnitude distribution over time as a heatmap — still informative
    # as it shows when the solver is near active bounds.
    try:
        solver_stats = prob.solver_stats
        obj_val = _safe_float(prob.objective_value)
        return_status = solver_stats.get("return_status", "unknown")
        wall_time = _safe_float(
            solver_stats.get("t_wall_total") or solver_stats.get("t_wall_solver")
        )
        n_vars = len(lam_x_arr)
    except Exception:
        return None

    # Reshape lam_x into a time×variable grid if possible.
    # For a uniform collocation problem the NLP variable vector is arranged
    # as [x_0, x_1, …, x_T] where each x_t contains all variables at t.
    # If it divides evenly we use that; otherwise show the full vector.
    vars_per_step = max(1, n_vars // max(n, 1))
    n_steps = n_vars // vars_per_step
    reshaped = lam_x_arr[: n_steps * vars_per_step].reshape(n_steps, vars_per_step)
    magnitude = np.abs(reshaped)

    fig, axes = _make_fig(2, "Solver Internals — Shadow Prices & Statistics")

    # Top: lam_x magnitude heatmap
    ax = axes[0]
    im = ax.imshow(
        magnitude.T,
        aspect="auto",
        cmap="plasma",
        interpolation="nearest",
    )
    ax.set_xlabel("Collocation step", color=_C["text"], fontsize=9)
    ax.set_ylabel("Variable index", color=_C["text"], fontsize=9)
    ax.tick_params(colors=_C["text"])
    cbar = fig.colorbar(im, ax=ax, fraction=0.02, pad=0.02)
    cbar.ax.tick_params(colors=_C["text"])
    cbar.set_label("|λ|", color=_C["text"], fontsize=8)
    ax.set_title(
        "Bound shadow prices |λₓ| — non-zero = active bound",
        color=_C["text"],
        fontsize=9,
    )

    # Bottom: solver statistics as a text panel
    ax2 = axes[1]
    ax2.axis("off")
    stats_lines = [
        f"Solver status:  {return_status}",
        f"Objective value:  {obj_val:.4f}"
        if obj_val is not None
        else "Objective value: n/a",
        f"Wall-clock time:  {wall_time:.3f}s"
        if wall_time is not None
        else "Wall-clock time: n/a",
        f"NLP variables:  {n_vars}",
        f"Collocation steps:  {n_steps}",
    ]
    ax2.text(
        0.05,
        0.95,
        "\n".join(stats_lines),
        transform=ax2.transAxes,
        verticalalignment="top",
        fontsize=9,
        color=_C["text"],
        family="monospace",
        bbox=dict(facecolor=_C["bg"], edgecolor=_C["grid"], boxstyle="round,pad=0.4"),
    )

    fig.tight_layout()
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# Chart 5 — Decision rationale overlay (scheduling only)
# ══════════════════════════════════════════════════════════════════════════════


def _chart_decision_rationale_scheduling(
    df_out: pd.DataFrame,
    df_in: pd.DataFrame,
    cycling_penalty: float,
    prob: "OptimizationProblem",
) -> plt.Figure:
    """Annotated price chart explaining charge / discharge / idle decisions.

    Overlays the price curve with:
    - The effective charge price threshold (ask_price + cycling_penalty + grid_fee_in)
    - The effective discharge price threshold (bid_price − cycling_penalty − grid_fee_out)
    - The break-even spread: the minimum price gap required to trade profitably
    - Colour-coded background bands showing the decision at each interval

    This directly answers: "Why didn't the optimiser trade at hour X?"
    """
    t = _time_axis(df_out)
    n = len(df_out)
    dt = (t[1] - t[0]) if len(t) > 1 else 1.0

    price = df_in["price"].values[:n]
    charge = df_out["charge_power"].values[:n]
    discharge = df_out["discharge_power"].values[:n]

    try:
        params = prob.parameters(0)
        efficiency = float(params.get("efficiency", 0.81))
    except Exception:
        efficiency = 0.81

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

    # Effective charge cost: you pay price/sqrt_eff per MWh stored + cycling + grid fee
    # (round-trip: charge at eff_in, discharge at eff_out; single sqrt per leg)
    effective_charge_price = price / sqrt_eff + cycling_penalty + fee_in
    effective_discharge_price = price * sqrt_eff - cycling_penalty - fee_out

    # Break-even spread: how large the sell/buy price difference must be
    breakeven_spread = 2.0 * cycling_penalty / sqrt_eff + (fee_in + fee_out) / sqrt_eff

    # Classify each interval
    is_charging = charge > 0.01
    is_discharging = discharge > 0.01

    fig, axes = _make_fig(2, "Decision Rationale — Why Did the Optimiser Trade Here?")

    ax = axes[0]

    # Decision background bands
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
        t,
        effective_charge_price,
        where="post",
        color=_C["charge"],
        linewidth=1.2,
        linestyle="--",
        label="Effective charge threshold",
    )
    ax.step(
        t,
        effective_discharge_price,
        where="post",
        color=_C["discharge"],
        linewidth=1.2,
        linestyle="--",
        label="Effective discharge threshold",
    )

    ax.legend(fontsize=7, loc="upper right")
    _label_axes(ax, "Price (EUR/MWh)", xlabel="")

    # Custom legend for decision background
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

    # Bottom: break-even spread and realised spread
    ax2 = axes[1]
    realised_spread = effective_discharge_price - effective_charge_price
    ax2.fill_between(
        t,
        0,
        breakeven_spread,
        color=_C["warn"],
        alpha=0.2,
        label=f"Break-even spread ({breakeven_spread.mean():.2f} EUR/MWh avg)",
    )
    ax2.step(
        t,
        np.maximum(realised_spread, 0),
        where="post",
        color=_C["discharge"],
        linewidth=1.5,
        label="Profitable spread (effective)",
    )
    ax2.step(
        t,
        np.minimum(realised_spread, 0),
        where="post",
        color=_C["charge"],
        linewidth=1.0,
        label="Unprofitable region",
    )
    ax2.axhline(0, color=_C["text"], linewidth=0.8)
    ax2.legend(fontsize=7, loc="upper right")
    _label_axes(ax2, "Spread (EUR/MWh)")

    fig.tight_layout()
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# Chart 6 — Orderbook depth utilisation (intraday only)
# ══════════════════════════════════════════════════════════════════════════════


def _chart_orderbook_utilisation(
    df_out: pd.DataFrame,
    df_in: pd.DataFrame,
    n_segments: int,
) -> plt.Figure:
    """What fraction of each orderbook level's available volume was traded.

    Reveals whether the optimiser is volume-constrained at the best prices
    (level 1) or has headroom, and how aggressively it reaches into deeper,
    less favourable levels.
    """
    t = _time_axis(df_out)
    n = len(df_out)

    fig, axes = _make_fig(2, f"Orderbook Depth Utilisation ({n_segments} levels)")

    # Build utilisation matrices: shape (n_segments, n)
    bid_util = np.zeros((n_segments, n))
    ask_util = np.zeros((n_segments, n))

    for seg in range(1, n_segments + 1):
        bid_vol_col = f"bid_volumes[{seg}]"
        ask_vol_col = f"ask_volumes[{seg}]"
        bid_pw_col = f"discharge_power_bids[{seg}]"
        ask_pw_col = f"charge_power_asks[{seg}]"

        if bid_vol_col in df_in.columns and bid_pw_col in df_out.columns:
            vol = df_in[bid_vol_col].values[:n]
            pw = df_out[bid_pw_col].values[:n]
            with np.errstate(divide="ignore", invalid="ignore"):
                bid_util[seg - 1] = np.where(vol > 0, np.clip(pw / vol, 0.0, 1.0), 0.0)

        if ask_vol_col in df_in.columns and ask_pw_col in df_out.columns:
            vol = df_in[ask_vol_col].values[:n]
            pw = df_out[ask_pw_col].values[:n]
            with np.errstate(divide="ignore", invalid="ignore"):
                ask_util[seg - 1] = np.where(vol > 0, np.clip(pw / vol, 0.0, 1.0), 0.0)

    # Show as heatmaps
    ax = axes[0]
    if n_segments > 0:
        im = ax.imshow(
            bid_util,
            aspect="auto",
            cmap="Greens",
            vmin=0.0,
            vmax=1.0,
            interpolation="nearest",
            extent=[
                t[0] if len(t) else 0,
                t[-1] if len(t) else n,
                n_segments + 0.5,
                0.5,
            ],
        )
        ax.set_yticks(range(1, n_segments + 1))
        ax.set_yticklabels(
            [f"Bid L{i}" for i in range(1, n_segments + 1)],
            fontsize=8,
            color=_C["text"],
        )
        ax.tick_params(colors=_C["text"])
        ax.set_title(
            "Discharge bid utilisation (0 = unused, 1 = full)",
            color=_C["text"],
            fontsize=9,
        )
        cbar = fig.colorbar(im, ax=ax, fraction=0.02, pad=0.02)
        cbar.ax.tick_params(colors=_C["text"])

    ax2 = axes[1]
    if n_segments > 0:
        im2 = ax2.imshow(
            ask_util,
            aspect="auto",
            cmap="Reds",
            vmin=0.0,
            vmax=1.0,
            interpolation="nearest",
            extent=[
                t[0] if len(t) else 0,
                t[-1] if len(t) else n,
                n_segments + 0.5,
                0.5,
            ],
        )
        ax2.set_yticks(range(1, n_segments + 1))
        ax2.set_yticklabels(
            [f"Ask L{i}" for i in range(1, n_segments + 1)],
            fontsize=8,
            color=_C["text"],
        )
        ax2.tick_params(colors=_C["text"])
        ax2.set_title(
            "Charge ask utilisation (0 = unused, 1 = full)",
            color=_C["text"],
            fontsize=9,
        )
        ax2.set_xlabel("Time (hours)", color=_C["text"], fontsize=9)
        cbar2 = fig.colorbar(im2, ax=ax2, fraction=0.02, pad=0.02)
        cbar2.ax.tick_params(colors=_C["text"])

    fig.tight_layout()
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# Public entry points
# ══════════════════════════════════════════════════════════════════════════════


def build_scheduling_diagnostics(
    output_dir: Any,  # pathlib.Path — kept as Any to avoid circular import
    model_input: dict[str, Any],
    cycling_penalty: float,
    prob: "OptimizationProblem",
) -> tuple[dict[str, str], list[str]]:
    """Generate all scheduling explainer charts.

    Returns a tuple of:
    - ``images``: dict mapping chart name to ``data:image/png;base64,…`` URI
    - ``info_entries``: list of ``_info`` strings to append to the response
    """
    import pandas as pd
    from pathlib import Path

    t_start = time.monotonic()
    images: dict[str, str] = {}
    info: list[str] = []

    output_dir = Path(output_dir)
    csv_path = output_dir / "timeseries_export.csv"

    try:
        df_out_full = pd.read_csv(csv_path, parse_dates=["time"])
    except Exception as exc:
        info.append(f"diagnostics: skipped — could not read output CSV: {exc}")
        return images, info

    # Strip dummy row + endpoint row (same logic as rtc_to_pe.py)
    interval_start = model_input.get("interval_start", [])
    n = len(interval_start)
    if len(df_out_full) > n:
        df_out = df_out_full.iloc[1 : n + 1].reset_index(drop=True)
    else:
        df_out = df_out_full.copy()

    # Read the input CSV to get price and fee timeseries
    input_dir = output_dir.parent / "input"
    try:
        df_in_full = pd.read_csv(
            input_dir / "timeseries_import.csv", parse_dates=["time"]
        )
        # The input CSV also has the prepended dummy row; strip it consistently
        if len(df_in_full) > n:
            df_in = df_in_full.iloc[1 : n + 1].reset_index(drop=True)
        else:
            df_in = df_in_full.copy()
    except Exception:
        # Fall back to zeros if input CSV is not available
        df_in = pd.DataFrame({"price": np.zeros(n)})

    chart_fns = [
        (
            "revenue_decomposition",
            lambda: _chart_revenue_decomposition_scheduling(
                df_out, df_in, cycling_penalty
            ),
        ),
        (
            "constraint_tightness",
            lambda: _chart_constraint_tightness(df_out, prob),
        ),
        (
            "soc_headroom",
            lambda: _chart_soc_headroom(df_out, prob),
        ),
        (
            "shadow_prices",
            lambda: _chart_shadow_prices(df_out, prob),
        ),
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
                info.append(
                    f"diagnostics: '{name}' skipped — duals not available for MILP"
                )
                continue
            images[name] = _fig_to_b64(fig)
        except Exception as exc:
            _log.warning("Diagnostic chart '%s' failed: %s", name, exc, exc_info=True)
            info.append(f"diagnostics: '{name}' failed — {exc}")

    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    info.append(
        f"diagnostics: generated {len(images)} scheduling chart(s) in {elapsed_ms}ms"
    )
    return images, info


def build_intraday_diagnostics(
    output_dir: Any,  # pathlib.Path
    model_input: dict[str, Any],
    n_segments: int,
    cycling_penalty: float,
    transaction_cost: float,
    prob: "OptimizationProblem",
) -> tuple[dict[str, str], list[str]]:
    """Generate all intraday explainer charts.

    Returns a tuple of:
    - ``images``: dict mapping chart name to ``data:image/png;base64,…`` URI
    - ``info_entries``: list of ``_info`` strings to append to the response
    """
    import pandas as pd
    from pathlib import Path

    t_start = time.monotonic()
    images: dict[str, str] = {}
    info: list[str] = []

    output_dir = Path(output_dir)
    csv_path = output_dir / "timeseries_export.csv"

    try:
        df_out_full = pd.read_csv(csv_path, parse_dates=["time"])
    except Exception as exc:
        info.append(f"diagnostics: skipped — could not read output CSV: {exc}")
        return images, info

    interval_start = model_input.get("interval_start", [])
    n = len(interval_start)
    if len(df_out_full) > n:
        df_out = df_out_full.iloc[1 : n + 1].reset_index(drop=True)
    else:
        df_out = df_out_full.copy()

    input_dir = output_dir.parent / "input"
    try:
        df_in_full = pd.read_csv(
            input_dir / "timeseries_import.csv", parse_dates=["time"]
        )
        if len(df_in_full) > n:
            df_in = df_in_full.iloc[1 : n + 1].reset_index(drop=True)
        else:
            df_in = df_in_full.copy()
    except Exception:
        df_in = pd.DataFrame()

    chart_fns = [
        (
            "revenue_decomposition",
            lambda: _chart_revenue_decomposition_intraday(
                df_out, df_in, n_segments, cycling_penalty, transaction_cost
            ),
        ),
        (
            "constraint_tightness",
            lambda: _chart_constraint_tightness(df_out, prob),
        ),
        (
            "soc_headroom",
            lambda: _chart_soc_headroom(df_out, prob),
        ),
        (
            "shadow_prices",
            lambda: _chart_shadow_prices(df_out, prob),
        ),
        (
            "orderbook_utilisation",
            lambda: _chart_orderbook_utilisation(df_out, df_in, n_segments),
        ),
    ]

    for name, fn in chart_fns:
        try:
            fig = fn()
            if fig is None:
                info.append(
                    f"diagnostics: '{name}' skipped — duals not available for MILP"
                )
                continue
            images[name] = _fig_to_b64(fig)
        except Exception as exc:
            _log.warning("Diagnostic chart '%s' failed: %s", name, exc, exc_info=True)
            info.append(f"diagnostics: '{name}' failed — {exc}")

    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    info.append(
        f"diagnostics: generated {len(images)} intraday chart(s) in {elapsed_ms}ms"
    )
    return images, info
