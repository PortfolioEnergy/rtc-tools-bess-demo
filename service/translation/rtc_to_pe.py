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


# ── reserve member shaping ───────────────────────────────────────────

# Single-band output names per product, in priority order matching
# poc-backtesting's bid extractors (fcr_helpers / afrr_helpers).
_SINGLE_BAND_NAMES: dict[str, str] = {
    "fcr": "fcr_power_out",
    "afrr_up": "afrr_up_capacity",
    "afrr_down": "afrr_down_capacity",
}


def _emit_reserve_members(
    members: dict[str, Any],
    df: pd.DataFrame,
    info: list[str],
    n_bands_per_product: dict[str, int],
    offer_prices_per_product: dict[str, list[float]],
) -> None:
    """Append reserve-bid output members to *members*.

    Always emits:

    - ``bid_<p>_total`` for each product (scalar MW per PTU)
    - ``<p>_position`` echoing the committed input (for caller-side accounting)
    - ``total_<p>`` (committed + bid) for headroom-check transparency

    For each product whose ``n_bands_per_product`` is 1 (or missing), emits
    the single-band name (``fcr_power_out`` / ``afrr_<dir>_capacity``).
    For multi-band products, emits ``<p>_capacity_deltas[k]`` for k=1..N
    with the entire bid allocated to band 1 (cheapest offer price) and the
    rest zero — the solver's deterministic bid clears at band 1 anyway.
    """
    n = len(df)
    for product in ("fcr", "afrr_up", "afrr_down"):
        bid_col = f"bid_{product}_total"
        total_col = f"total_{product}"
        bid_values = _safe_list(df[bid_col]) if bid_col in df.columns else [0.0] * n
        total_values = (
            _safe_list(df[total_col]) if total_col in df.columns else [0.0] * n
        )
        # Position (cleared input) is derivable from total - bid; emit it
        # so callers can pin one number per product without re-reading the
        # request body.  RTC-Tools doesn't echo input variables back through
        # the export CSV, so reconstructing here is the only way.
        position_values = [
            float(t) - float(b) for t, b in zip(total_values, bid_values)
        ]

        members[f"bid_{product}_total"] = {"values": bid_values}
        members[f"total_{product}"]     = {"values": total_values}
        members[f"{product}_position"]  = {"values": position_values}

        n_bands = max(1, int(n_bands_per_product.get(product, 1)))
        if n_bands == 1:
            members[_SINGLE_BAND_NAMES[product]] = {"values": bid_values}
            continue

        # Multi-band wire-shape compatibility: allocate the full bid to the
        # cheapest offer band (band 1).  The v1 solver is deterministic and
        # never benefits from spreading across bands, so this is exact.
        # When a future iteration models price uncertainty, the spread
        # logic moves here.
        prices = offer_prices_per_product.get(product) or []
        members[f"{product}_capacity_deltas[1]"] = {"values": list(bid_values)}
        for k in range(2, n_bands + 1):
            members[f"{product}_capacity_deltas[{k}]"] = {"values": [0.0] * n}
        info.append(
            f"approximation: multi-band bid for '{product}' "
            f"(n_price_bands={n_bands}) collapsed to band 1 "
            f"(cheapest offer price = "
            f"{prices[0] if prices else 'unknown'}); v1 solver treats "
            f"clearing as deterministic so spreading bands adds no value"
        )


# ── scheduling ───────────────────────────────────────────────────────


def translate_scheduling_result(
    output_dir: Path,
    model_input: dict[str, Any],
    info: list[str],
    *,
    prob: "OptimizationProblem | None" = None,
    n_bands_per_product: dict[str, int] | None = None,
    offer_prices_per_product: dict[str, list[float]] | None = None,
    reserve_config: dict[str, dict] | None = None,
    counterfactual_metrics: dict[str, Any] | None = None,
    skip_counterfactual_reserves: bool = False,
) -> tuple[dict[str, Any], str]:
    """Build PE API response from scheduling solver output.

    Returns ``(result, reasoning_markdown)``. ``result`` goes inside
    ``{"result": ...}``; ``reasoning_markdown`` is a top-level response key
    (empty string when diagnostics are not requested).

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

    # Reserve outputs (always emit so callers see zero-bids explicitly).
    _emit_reserve_members(
        members,
        df,
        info,
        n_bands_per_product or {},
        offer_prices_per_product or {},
    )

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

    reasoning_markdown = ""
    if prob is not None:
        from service.translation.diagnostics import build_scheduling_diagnostics

        # Retrieve the cycling penalty used during this solve from the class
        # attribute stamped onto the dynamically-created solver subclass.
        cycling_penalty = float(getattr(prob, "cycling_penalty_factor", 0.0))
        images, diag_info, reasoning_markdown = build_scheduling_diagnostics(
            output_dir,
            model_input,
            cycling_penalty,
            prob,
            reserve_config=reserve_config or {},
            counterfactual_metrics=counterfactual_metrics,
            skip_counterfactual_reserves=skip_counterfactual_reserves,
        )
        info.extend(diag_info)
        for name, data_uri in images.items():
            info.append(f"image:{name}: {data_uri}")

    return {"members": {"default": members}, "_info": info}, reasoning_markdown


# ── intraday ─────────────────────────────────────────────────────────


def translate_intraday_result(
    output_dir: Path,
    model_input: dict[str, Any],
    n_segments: int,
    info: list[str],
    *,
    prob: "OptimizationProblem | None" = None,
    n_bands_per_product: dict[str, int] | None = None,
    offer_prices_per_product: dict[str, list[float]] | None = None,
    reserve_config: dict[str, dict] | None = None,
    counterfactual_metrics: dict[str, Any] | None = None,
    skip_counterfactual_reserves: bool = False,
) -> tuple[dict[str, Any], str]:
    """Build PE API response from intraday solver output.

    Returns ``(result, reasoning_markdown)``. ``result`` goes inside
    ``{"result": ...}``; ``reasoning_markdown`` is a top-level response key
    (empty string when diagnostics are not requested).

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

    # Reserve outputs — intraday bids are always zero (solver pins them) but
    # emitting them keeps the wire shape uniform with the scheduling solver,
    # and the committed-position passthrough is useful for caller-side accounting.
    _emit_reserve_members(
        members,
        df,
        info,
        n_bands_per_product or {},
        offer_prices_per_product or {},
    )

    reasoning_markdown = ""
    if prob is not None:
        from service.translation.diagnostics import build_intraday_diagnostics

        cycling_penalty = float(getattr(prob, "cycling_penalty_factor", 0.0))
        transaction_cost = float(getattr(prob, "transaction_cost", 0.0))
        images, diag_info, reasoning_markdown = build_intraday_diagnostics(
            output_dir,
            model_input,
            n_segments,
            cycling_penalty,
            transaction_cost,
            prob,
            reserve_config=reserve_config or {},
            counterfactual_metrics=counterfactual_metrics,
            skip_counterfactual_reserves=skip_counterfactual_reserves,
        )
        info.extend(diag_info)
        for name, data_uri in images.items():
            info.append(f"image:{name}: {data_uri}")

    return {"members": {"default": members}, "_info": info}, reasoning_markdown
