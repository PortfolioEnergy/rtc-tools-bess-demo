"""Translate RTC-Tools CSV output into PE API response format.

Adds ``_info`` entries for any PE API output variables that are not
produced by the local solver.  When a solved ``OptimizationProblem``
instance is provided via the ``prob`` keyword argument, diagnostic
explainer charts are appended to ``_info`` as ``"image:<name>: <data URI>"``
entries so that the response shape (``members`` + ``_info``) stays unchanged.
"""

from __future__ import annotations

import math
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


def _shape_by_grid(
    values: list[float],
    grid: dict | None,
) -> dict[str, Any]:
    """Build the wire payload for one member.

    When *grid* is ``None`` the legacy single-key ``{"values": [...]}`` shape
    is preserved (caller did not declare a per-timeseries grid on the input
    that drives this member).  When *grid* is present the values are
    collapsed onto its blocks — one entry per block taken from the first
    PTU inside the block — and the block-spanning ``interval_start`` /
    ``interval_end`` arrays are attached.
    """
    if grid is None:
        return {"values": values}
    blocks: list[list[int]] = grid["blocks"]
    block_values = [values[b[0]] for b in blocks]
    return {
        "values": block_values,
        "interval_start": list(grid["interval_start"]),
        "interval_end": list(grid["interval_end"]),
    }


def _emit_reserve_members(
    members: dict[str, Any],
    df: pd.DataFrame,
    info: list[str],
    n_bands_per_product: dict[str, int],
    offer_prices_per_product: dict[str, list[float]],
    market_grids: dict[str, dict],
) -> None:
    """Append reserve-bid output members to *members*.

    Always emits:

    - ``bid_<p>_total`` for each product (MW per output bucket)
    - ``<p>_position`` echoing the committed input
    - ``total_<p>`` (committed + bid) for headroom-check transparency

    For each product whose ``n_bands_per_product`` is 1 (or missing), emits
    the single-band name (``fcr_power_out`` / ``afrr_<dir>_capacity``).
    For multi-band products, emits ``<p>_capacity_deltas[k]`` for k=1..N
    with the entire bid allocated to band 1 (cheapest offer price) and the
    rest zero — the solver's deterministic bid clears at band 1 anyway.

    When the caller supplied a per-timeseries ``interval_start`` /
    ``interval_end`` grid for a reserve product, every output member for
    that product is collapsed onto that grid (one value per input block)
    and carries the same ``interval_start`` / ``interval_end`` arrays.
    Products whose inputs were at PTU resolution keep the legacy shape.
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

        grid = market_grids.get(product)
        bid_payload = _shape_by_grid(bid_values, grid)
        members[f"bid_{product}_total"] = bid_payload
        members[f"total_{product}"] = _shape_by_grid(total_values, grid)
        members[f"{product}_position"] = _shape_by_grid(position_values, grid)

        n_bands = max(1, int(n_bands_per_product.get(product, 1)))
        if n_bands == 1:
            # Single-band wire shape aliases bid_<p>_total exactly, including
            # any block grid the caller declared on the input.
            members[_SINGLE_BAND_NAMES[product]] = dict(bid_payload)
            continue

        # Multi-band wire-shape compatibility: allocate the full bid to the
        # cheapest offer band (band 1).  The v1 solver is deterministic and
        # never benefits from spreading across bands, so this is exact.
        # When a future iteration models price uncertainty, the spread
        # logic moves here.
        prices = offer_prices_per_product.get(product) or []
        zeros = [0.0] * len(bid_payload["values"])
        zero_payload: dict[str, Any] = {"values": zeros}
        if grid is not None:
            zero_payload["interval_start"] = list(grid["interval_start"])
            zero_payload["interval_end"] = list(grid["interval_end"])
        members[f"{product}_capacity_deltas[1]"] = dict(bid_payload)
        for k in range(2, n_bands + 1):
            members[f"{product}_capacity_deltas[{k}]"] = {
                key: list(value) for key, value in zero_payload.items()
            }
        info.append(
            f"approximation: multi-band bid for '{product}' "
            f"(n_price_bands={n_bands}) collapsed to band 1 "
            f"(cheapest offer price = "
            f"{prices[0] if prices else 'unknown'}); v1 solver treats "
            f"clearing as deterministic so spreading bands adds no value"
        )


# ── aFRR energy bid pricing (post-solve) ────────────────────────────


def _compute_afrr_energy_bids(
    df: pd.DataFrame,
    df_input: pd.DataFrame,
    n_segments: int,
    obligation_up: list[float],
    obligation_down: list[float],
    open_mask: list[bool],
    n_bands: int,
    markup: float,
    cycling_penalty: float,
    stored_energy_value: float,
    efficiency: float,
    grid: dict | None,
    info: list[str],
) -> dict[str, Any]:
    """Compute aFRR energy bid prices from the solved intraday state.

    The energy bid price is derived from the marginal cost of activation:
    opportunity cost + cycling penalty + efficiency loss + grid fees + markup.
    All volume is allocated to band 1 (single-price strategy).

    Returns a dict of output members keyed by wire-format name.
    """
    n = len(df)
    members: dict[str, Any] = {}

    if not any(open_mask[:n]):
        return members

    sqrt_eff = math.sqrt(efficiency) if efficiency > 0.0 else 1.0

    # Reference price per PTU: mid-price of the orderbook (average of best
    # bid and best ask). Falls back to 0 if orderbook columns are missing.
    ref_prices = np.zeros(n)
    best_bid_col = "bid_prices[1]"
    best_ask_col = "ask_prices[1]"
    if best_bid_col in df_input.columns and best_ask_col in df_input.columns:
        bids = df_input[best_bid_col].to_numpy(dtype=float)[:n]
        asks = df_input[best_ask_col].to_numpy(dtype=float)[:n]
        ref_prices = (bids + asks) / 2.0

    # Grid fees from input
    fee_out = (
        df_input["grid_fee_out"].to_numpy(dtype=float)[:n]
        if "grid_fee_out" in df_input.columns
        else np.zeros(n)
    )
    fee_in = (
        df_input["grid_fee_in"].to_numpy(dtype=float)[:n]
        if "grid_fee_in" in df_input.columns
        else np.zeros(n)
    )

    # Compute per-PTU energy bid prices
    prices_up = np.zeros(n)
    prices_down = np.zeros(n)
    volumes_up = np.zeros(n)
    volumes_down = np.zeros(n)

    for t in range(n):
        if not open_mask[t]:
            continue

        eff_loss_up = (1.0 / sqrt_eff - 1.0) * ref_prices[t]
        eff_loss_down = (1.0 - sqrt_eff) * ref_prices[t]

        prices_up[t] = (
            stored_energy_value
            + cycling_penalty
            + eff_loss_up
            + fee_out[t]
            + markup
        )
        prices_down[t] = (
            -stored_energy_value
            + cycling_penalty
            - eff_loss_down
            + fee_in[t]
            + markup
        )

        volumes_up[t] = obligation_up[t]
        volumes_down[t] = obligation_down[t]

        info.append(
            f"afrr_energy_bid_up[{t}]: price={prices_up[t]:.2f} EUR/MWh = "
            f"opportunity_cost({stored_energy_value:.2f}) + "
            f"cycling({cycling_penalty:.2f}) + "
            f"efficiency_loss({eff_loss_up:.2f}) + "
            f"grid_fee({fee_out[t]:.2f}) + "
            f"markup({markup:.2f})"
        )
        info.append(
            f"afrr_energy_bid_down[{t}]: price={prices_down[t]:.2f} EUR/MWh = "
            f"-opportunity_cost({stored_energy_value:.2f}) + "
            f"cycling({cycling_penalty:.2f}) - "
            f"efficiency_loss({eff_loss_down:.2f}) + "
            f"grid_fee({fee_in[t]:.2f}) + "
            f"markup({markup:.2f})"
        )

    # Shape output onto the aFRR energy market grid
    up_price_values = prices_up.tolist()
    up_volume_values = volumes_up.tolist()
    down_price_values = prices_down.tolist()
    down_volume_values = volumes_down.tolist()

    # Band 1 gets all volume; remaining bands are zero
    members["afrr_energy_up_price[1]"] = _shape_by_grid(up_price_values, grid)
    members["afrr_energy_up_volume[1]"] = _shape_by_grid(up_volume_values, grid)
    members["afrr_energy_down_price[1]"] = _shape_by_grid(down_price_values, grid)
    members["afrr_energy_down_volume[1]"] = _shape_by_grid(down_volume_values, grid)

    if n_bands > 1:
        zeros = [0.0] * (len(grid["blocks"]) if grid and "blocks" in grid else n)
        zero_payload: dict[str, Any] = {"values": zeros}
        if grid is not None:
            zero_payload["interval_start"] = list(grid["interval_start"])
            zero_payload["interval_end"] = list(grid["interval_end"])
        for k in range(2, n_bands + 1):
            members[f"afrr_energy_up_price[{k}]"] = {
                key: list(val) for key, val in zero_payload.items()
            }
            members[f"afrr_energy_up_volume[{k}]"] = {
                key: list(val) for key, val in zero_payload.items()
            }
            members[f"afrr_energy_down_price[{k}]"] = {
                key: list(val) for key, val in zero_payload.items()
            }
            members[f"afrr_energy_down_volume[{k}]"] = {
                key: list(val) for key, val in zero_payload.items()
            }

    return members


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
    market_grids: dict[str, dict] | None = None,
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

    grids = market_grids or {}
    da_grid = grids.get("day_ahead")
    members: dict[str, Any] = {
        "day_ahead_power_in": _shape_by_grid(_safe_list(df["charge_power"]), da_grid),
        "day_ahead_power_out": _shape_by_grid(_safe_list(df["discharge_power"]), da_grid),
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
        grids,
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
    market_grids: dict[str, dict] | None = None,
    counterfactual_metrics: dict[str, Any] | None = None,
    skip_counterfactual_reserves: bool = False,
    afrr_energy_obligation_up: list[float] | None = None,
    afrr_energy_obligation_down: list[float] | None = None,
    afrr_energy_open_mask: list[bool] | None = None,
    afrr_energy_n_bands: int = 0,
    afrr_energy_markup: float = 0.0,
    afrr_energy_grid: dict | None = None,
    cycling_penalty_factor: float = 0.0,
    stored_energy_value: float = 0.0,
    efficiency: float = 0.9,
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
        market_grids or {},
    )

    # aFRR energy bid pricing — post-solve marginal-cost computation.
    if afrr_energy_open_mask and any(afrr_energy_open_mask):
        input_csv_path = output_dir.parent / "input" / "timeseries_import.csv"
        df_input = pd.read_csv(input_csv_path, parse_dates=["time"])
        if len(df_input) > n:
            df_input = df_input.iloc[1 : n + 1].reset_index(drop=True)
        energy_members = _compute_afrr_energy_bids(
            df,
            df_input,
            n_segments,
            afrr_energy_obligation_up or [],
            afrr_energy_obligation_down or [],
            afrr_energy_open_mask,
            afrr_energy_n_bands,
            afrr_energy_markup,
            cycling_penalty_factor,
            stored_energy_value,
            efficiency,
            afrr_energy_grid,
            info,
        )
        members.update(energy_members)

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
