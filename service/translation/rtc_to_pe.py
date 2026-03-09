"""Translate RTC-Tools CSV output into PE API response format.

Adds ``_info`` entries for any PE API output variables that are not
produced by the local solver.  When a solved ``OptimizationProblem``
instance is provided via the ``prob`` keyword argument, diagnostic
explainer charts are appended to ``_info`` as ``"image:<name>: <data URI>"``
entries so that the response shape (``members`` + ``_info``) stays unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from rtctools.optimization.optimization_problem import OptimizationProblem


def _read_output_csv(output_dir: Path) -> pd.DataFrame:
    """Read ``timeseries_export.csv`` from the solver output directory."""
    csv_path = output_dir / "timeseries_export.csv"
    return pd.read_csv(csv_path, parse_dates=["time"])


def _timestamps_to_iso(times: pd.Series) -> list[str]:
    """Convert pandas Timestamps to ISO 8601 UTC strings."""
    result: list[str] = []
    for t in times:
        if isinstance(t, pd.Timestamp):
            result.append(t.strftime("%Y-%m-%dT%H:%M:%SZ"))
        else:
            result.append(str(t))
    return result


def _safe_list(series: pd.Series) -> list[float]:
    """Convert a pandas Series to a plain list of Python floats."""
    return [
        float(v) if not (isinstance(v, float) and np.isnan(v)) else 0.0 for v in series
    ]


# ── scheduling ───────────────────────────────────────────────────────


def translate_scheduling_result(
    output_dir: Path,
    model_input: dict[str, Any],
    info: list[str],
    *,
    prob: "OptimizationProblem | None" = None,
) -> dict[str, Any]:
    """Build PE API response from scheduling solver output.

    Returns the ``result`` dict (goes inside ``{"result": ...}``).

    When *prob* is provided the solved ``OptimizationProblem`` instance is
    used to generate diagnostic explainer charts.  Each chart is appended to
    ``_info`` as ``"image:<name>: <data URI>"`` so the response shape
    (``members`` + ``_info``) remains unchanged.
    """
    df = _read_output_csv(output_dir)

    # Strip the prepended dummy row (front) and endpoint row (back).
    # The translation layer prepends one dummy timestep so that backward
    # Euler's blind-spot at t=0 falls outside the real trading window.
    interval_start = model_input.get("interval_start", [])
    n = len(interval_start)
    if len(df) > n:
        df = df.iloc[1 : n + 1].reset_index(drop=True)

    # Use the original interval_start timestamps for SOC times
    soc_times = list(interval_start)

    members: dict[str, Any] = {
        "day_ahead_power_in": {"values": _safe_list(df["charge_power"])},
        "day_ahead_power_out": {"values": _safe_list(df["discharge_power"])},
        "state_of_charge": {
            "values": _safe_list(df["soc"]),
            "times": soc_times,
        },
    }

    # Document outputs the PE API returns but we don't
    # Multi-band deltas — only relevant if the request had multi-band pricing
    for market in model_input.get("markets", []):
        if market.get("type") == "bid_offer_stack":
            n_bands = market.get("n_price_bands", 1)
            if n_bands > 1:
                info.append(
                    f"not_in_output: 'day_ahead_power_out_deltas[1..{n_bands}]' "
                    f"— multi-band pricing not supported"
                )
                info.append(
                    f"not_in_output: 'day_ahead_power_in_deltas[1..{n_bands}]' "
                    f"— multi-band pricing not supported"
                )

    if prob is not None:
        from service.translation.diagnostics import build_scheduling_diagnostics

        # Retrieve the cycling penalty used during this solve from the class
        # attribute stamped onto the dynamically-created solver subclass.
        cycling_penalty = float(getattr(prob, "cycling_penalty_factor", 0.0))
        images, diag_info = build_scheduling_diagnostics(
            output_dir, model_input, cycling_penalty, prob
        )
        info.extend(diag_info)
        for name, data_uri in images.items():
            info.append(f"image:{name}: {data_uri}")

    return {"members": {"default": members}, "_info": info}


# ── intraday ─────────────────────────────────────────────────────────


def translate_intraday_result(
    output_dir: Path,
    model_input: dict[str, Any],
    n_segments: int,
    info: list[str],
    *,
    prob: "OptimizationProblem | None" = None,
) -> dict[str, Any]:
    """Build PE API response from intraday solver output.

    Returns the ``result`` dict (goes inside ``{"result": ...}``).

    When *prob* is provided the solved ``OptimizationProblem`` instance is
    used to generate diagnostic explainer charts.  Each chart is appended to
    ``_info`` as ``"image:<name>: <data URI>"`` so the response shape
    (``members`` + ``_info``) remains unchanged.
    """
    df = _read_output_csv(output_dir)

    # Strip the prepended dummy row (front) and endpoint row (back).
    interval_start = model_input.get("interval_start", [])
    n = len(interval_start)
    if len(df) > n:
        df = df.iloc[1 : n + 1].reset_index(drop=True)

    soc_times = list(interval_start)

    members: dict[str, Any] = {}

    # Per-segment orderbook power
    for seg in range(1, n_segments + 1):
        charge_col = f"charge_power_asks[{seg}]"
        discharge_col = f"discharge_power_bids[{seg}]"

        if charge_col in df.columns:
            members[f"orderbook[{seg}]_power_in"] = {
                "values": _safe_list(df[charge_col])
            }
        else:
            members[f"orderbook[{seg}]_power_in"] = {"values": [0.0] * n}

        if discharge_col in df.columns:
            members[f"orderbook[{seg}]_power_out"] = {
                "values": _safe_list(df[discharge_col])
            }
        else:
            members[f"orderbook[{seg}]_power_out"] = {"values": [0.0] * n}

    # Aggregate battery power.
    # charge_power and discharge_power are GROSS flows: committed position plus
    # incremental trades.  Consumers who need only the incremental trades can
    # sum the per-segment orderbook[N]_power_in / _power_out fields.
    members["battery_power_in"] = {"values": _safe_list(df["charge_power"])}
    members["battery_power_out"] = {"values": _safe_list(df["discharge_power"])}
    members["state_of_charge"] = {
        "values": _safe_list(df["soc"]),
        "times": soc_times,
    }

    info.append(
        "applied: 'battery_power_in' and 'battery_power_out' reflect gross "
        "physical flows (committed position + incremental trades); use "
        "orderbook[N]_power_in/_out fields for incremental trades only"
    )

    if prob is not None:
        from service.translation.diagnostics import build_intraday_diagnostics

        cycling_penalty = float(getattr(prob, "cycling_penalty_factor", 0.0))
        transaction_cost = float(getattr(prob, "transaction_cost", 0.0))
        images, diag_info = build_intraday_diagnostics(
            output_dir,
            model_input,
            n_segments,
            cycling_penalty,
            transaction_cost,
            prob,
        )
        info.extend(diag_info)
        for name, data_uri in images.items():
            info.append(f"image:{name}: {data_uri}")

    return {"members": {"default": members}, "_info": info}
