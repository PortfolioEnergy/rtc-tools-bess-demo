"""Translate PE API model_input_data into RTC-Tools CSV input files.

For every PE API field that is present but not used, an entry is appended
to the ``_info`` list so the caller can surface it in the response.
"""

from __future__ import annotations

import io
import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass
class TranslationResult:
    """Artefacts produced by the PE-to-RTC translation."""

    timeseries_csv: str
    initial_state_csv: str
    parameters_csv: str | None
    cycling_penalty: float
    transaction_cost: float
    n_segments: int
    stored_energy_value: float = 0.0
    info: list[str] = field(default_factory=list)
    # Reserve config keyed by product ("fcr", "afrr_up", "afrr_down").
    # Per-product shape: {"open": bool, "t_min_hours": float,
    #                     "blocks": list[list[int]]}
    reserve_config: dict[str, dict] = field(default_factory=dict)
    # Per-product bid-band metadata so the rtc_to_pe layer can reshape the
    # solver's single bid quantity back into the caller's multi-band format.
    n_bands_per_product: dict[str, int] = field(default_factory=dict)
    offer_prices_per_product: dict[str, list[float]] = field(default_factory=dict)
    # Counterfactual ("no reserves") re-solve toggle.  True = skip the second
    # solve in the diagnostics layer.  Default False = always run it when
    # diagnostics are requested (added overhead documented in the markdown).
    skip_counterfactual_reserves: bool = False
    # Per-market output grid keyed by market identifier ("fcr", "afrr_up",
    # "afrr_down", "day_ahead").  Populated only when the matching input
    # timeseries declared its own ``interval_start`` / ``interval_end``.
    # Each entry carries ``interval_start``, ``interval_end`` and the
    # ``blocks`` PTU-index partition; rtc_to_pe collapses output members
    # onto this grid by taking the first PTU value of each block.
    market_grids: dict[str, dict] = field(default_factory=dict)


# ── helpers ──────────────────────────────────────────────────────────


def _find_timeseries(model_input: dict[str, Any], name: str) -> dict[str, Any] | None:
    for ts in model_input.get("timeseries", []):
        if ts.get("name") == name:
            return ts
    return None


def _find_parameter(
    model_input: dict[str, Any], name: str, default: float | None = None
) -> float | None:
    for p in model_input.get("parameters", []):
        if p.get("name") == name:
            return float(p["value"])
    return default


def _iso_to_csv_time(iso_str: str) -> str:
    """Convert ``2025-08-01T00:00:00Z`` to ``2025-08-01 00:00:00``."""
    cleaned = iso_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    # RTC-Tools expects naive timestamps — strip tz
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _compute_interval_step(interval_start: list[str]) -> timedelta:
    """Infer the interval duration from the first two ``interval_start`` entries."""
    if len(interval_start) >= 2:
        t0 = datetime.fromisoformat(interval_start[0].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(interval_start[1].replace("Z", "+00:00"))
        return t1 - t0
    return timedelta(minutes=15)


def _prepend_dummy_time(interval_start: list[str]) -> str:
    """Return a CSV-format timestamp one interval step before the first entry.

    RTC-Tools' backward Euler (theta=1) leaves controls at the first
    collocation point decoupled from the SoC dynamics.  Prepending one
    dummy timestep shifts the initial-state boundary to *before* the
    trading window so that every real PTU has a proper SoC transition.
    """
    step = _compute_interval_step(interval_start)
    first = datetime.fromisoformat(interval_start[0].replace("Z", "+00:00"))
    dummy = first - step
    return dummy.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _has_nonzero(values: list[float] | None) -> bool:
    if not values:
        return False
    return any(v != 0.0 for v in values)


def _write_csv(header: list[str], rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


# ── reserve markets (FCR / aFRR) ─────────────────────────────────────

# Product identifiers in the canonical order used everywhere downstream.
_RESERVE_PRODUCTS: tuple[str, ...] = ("fcr", "afrr_up", "afrr_down")

# PE-wire timeseries names per product.  Two of them — the activation prices
# and the activation fraction — feed the SoC drift and activation-revenue
# logic in the solver and are required whenever the matching market is open.
_RESERVE_TS_NAMES: dict[str, dict[str, str]] = {
    "fcr": {
        "position": "fcr_position",
        "standby_price": "fcr_standby_price",
        "price": "fcr_price",
    },
    "afrr_up": {
        "position": "afrr_up_position",
        "standby_price": "afrr_up_standby_price",
        "price": "afrr_up_price",
    },
    "afrr_down": {
        "position": "afrr_down_position",
        "standby_price": "afrr_down_standby_price",
        "price": "afrr_down_price",
    },
}

# CSV columns for the per-product reserve timeseries.  These align 1:1 with
# the ``input Real`` declarations in BESS.mo / BESSIntraday.mo.
_RESERVE_CSV_COLUMNS: tuple[str, ...] = (
    "fcr_position", "afrr_up_position", "afrr_down_position",
    "fcr_standby_price", "fcr_price",
    "afrr_up_standby_price", "afrr_up_price",
    "afrr_down_standby_price", "afrr_down_price",
    "fcr_activation_fraction", "afrr_activation_fraction",
)


def _pad_to(values: list[float], length: int) -> list[float]:
    """Pad *values* with zeros up to *length* (no-op if already long enough)."""
    out = [float(v) for v in (values or [])][:length]
    if len(out) < length:
        out.extend([0.0] * (length - len(out)))
    return out


def _parse_iso_utc(iso: str) -> datetime:
    """Parse an ISO-8601 timestamp into a tz-aware UTC ``datetime``."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def _ptu_block_membership(
    ts_starts: list[str],
    ts_ends: list[str],
    ptu_starts_dt: list[datetime],
    ts_name: str,
) -> list[int]:
    """For each PTU, return the index of the input block that contains it.

    Raises ``ValueError`` (→ HTTP 422) when the input grid is malformed or
    fails to cover every PTU exactly once.  Block coverage uses
    ``[start, end)`` semantics so contiguous blocks share boundaries without
    overlap.
    """
    if len(ts_starts) != len(ts_ends):
        raise ValueError(
            f"timeseries '{ts_name}': interval_start ({len(ts_starts)}) and "
            f"interval_end ({len(ts_ends)}) lengths differ"
        )
    if not ts_starts:
        raise ValueError(
            f"timeseries '{ts_name}': interval_start/interval_end are empty "
            f"but a grid was declared"
        )
    starts_dt = [_parse_iso_utc(t) for t in ts_starts]
    ends_dt = [_parse_iso_utc(t) for t in ts_ends]
    membership: list[int] = []
    for ptu_idx, ptu_start in enumerate(ptu_starts_dt):
        block = next(
            (
                k
                for k, (s, e) in enumerate(zip(starts_dt, ends_dt))
                if s <= ptu_start < e
            ),
            None,
        )
        if block is None:
            raise ValueError(
                f"timeseries '{ts_name}': PTU {ptu_idx} "
                f"({ptu_start.isoformat()}) is not covered by any input block"
            )
        membership.append(block)
    return membership


def _expand_timeseries_to_ptu(
    ts: dict[str, Any] | None,
    ptu_starts_dt: list[datetime],
    ts_name: str,
) -> tuple[list[float] | None, dict[str, list[str]] | None]:
    """Expand a timeseries onto the PTU grid using its own grid (when present).

    Returns ``(values_per_ptu, grid)``:

    - ``values_per_ptu`` carries one value per PTU; ``None`` when ``ts`` is
      absent or empty.
    - ``grid`` echoes the input's ``interval_start`` / ``interval_end`` when
      it declared its own; ``None`` otherwise (the input was already at PTU
      resolution and the top-level grid governs).
    """
    if ts is None:
        return None, None
    values = ts.get("values")
    if not values:
        return None, None
    starts_in = ts.get("interval_start") or []
    ends_in = ts.get("interval_end") or []
    if not starts_in and not ends_in:
        return [float(v) for v in values], None
    if len(values) != len(starts_in):
        raise ValueError(
            f"timeseries '{ts_name}': values ({len(values)}) and "
            f"interval_start ({len(starts_in)}) lengths differ"
        )
    membership = _ptu_block_membership(starts_in, ends_in, ptu_starts_dt, ts_name)
    return (
        [float(values[k]) for k in membership],
        {"interval_start": list(starts_in), "interval_end": list(ends_in)},
    )


def _blocks_from_grid(
    grid_starts: list[str],
    grid_ends: list[str],
    ptu_starts_dt: list[datetime],
    ts_name: str,
) -> list[list[int]]:
    """Return PTU indices grouped by their containing input block."""
    membership = _ptu_block_membership(grid_starts, grid_ends, ptu_starts_dt, ts_name)
    bucket: dict[int, list[int]] = {}
    for ptu_idx, block in enumerate(membership):
        bucket.setdefault(block, []).append(ptu_idx)
    return [bucket[k] for k in sorted(bucket)]


def _extract_reserves(
    model_input: dict[str, Any],
    ptu_starts_dt: list[datetime],
    info: list[str],
) -> tuple[
    dict[str, dict],
    dict[str, list[float]],
    dict[str, int],
    dict[str, list[float]],
    dict[str, dict],
]:
    """Pull all reserve-market state out of the PE request.

    Returns ``(reserve_config, reserve_columns, n_bands_per_product,
    offer_prices_per_product, market_grids)``:

    - ``reserve_config`` — per-product ``{"open": bool, "t_min_hours": float,
      "blocks": list[list[int]]}``; consumed by the solver class to add LER
      and block-equality constraints.  ``blocks`` is empty when the caller
      passed reserve inputs at PTU resolution (no per-timeseries
      ``interval_start`` / ``interval_end``).
    - ``reserve_columns`` — column-name -> per-PTU values for every
      Modelica ``input Real`` reserve variable, defaulted to zero when the
      caller omitted a timeseries.  Block-shaped inputs are pre-expanded
      onto the PTU grid here so the solver always sees PTU-resolution data.
    - ``n_bands_per_product`` — used by rtc_to_pe to reshape the solver's
      scalar bid into the caller's multi-band wire format.
    - ``offer_prices_per_product`` — ditto, holds the offer-price list per
      product so the reshape can fill cheapest-band-first.
    - ``market_grids`` — per-product ``{"interval_start", "interval_end"}``
      grids derived from the explicit input timeseries grids.  Used by the
      output translation to echo the same grid back to the caller.
      ``standby_price`` wins when several reserve inputs for the same
      product declare a grid (the bid block structure follows the standby
      revenue stream).

    Raises ``ValueError`` (mapped to HTTP 422 in the route) when the caller
    opened an aFRR market without supplying the matching activation-fraction
    timeseries.  FCR has the same requirement for its activation cycling
    cost.
    """
    n_intervals = len(ptu_starts_dt)
    reserve_config: dict[str, dict] = {}
    reserve_columns: dict[str, list[float]] = {}
    n_bands_per_product: dict[str, int] = {}
    offer_prices_per_product: dict[str, list[float]] = {}
    market_grids: dict[str, dict] = {}

    # Default every reserve column to zeros so the CSV always has the
    # right shape — solvers never see undefined columns.
    for col in _RESERVE_CSV_COLUMNS:
        reserve_columns[col] = [0.0] * n_intervals

    markets_by_name = {
        m.get("name"): m
        for m in model_input.get("markets", [])
        if m.get("name") in _RESERVE_PRODUCTS
    }

    # Per-product cache of the grid attached to each input timeseries; the
    # priority order ``standby_price > price > position`` picks the product
    # grid below.
    product_input_grids: dict[str, dict[str, dict[str, list[str]] | None]] = {
        p: {} for p in _RESERVE_PRODUCTS
    }

    def _ingest(product: str, ts_key: str, ts_name: str) -> None:
        ts = _find_timeseries(model_input, ts_name)
        values, grid = _expand_timeseries_to_ptu(ts, ptu_starts_dt, ts_name)
        if values is not None:
            reserve_columns[ts_name] = _pad_to(values, n_intervals)
        product_input_grids[product][ts_key] = grid
        if (
            ts_key == "position"
            and values is not None
            and _has_nonzero(values)
        ):
            info.append(
                f"applied: committed '{ts_name}' ({len(values)} values) "
                f"— tightens LER and power headroom"
            )

    for product in _RESERVE_PRODUCTS:
        _ingest(product, "position", _RESERVE_TS_NAMES[product]["position"])
        _ingest(product, "standby_price", _RESERVE_TS_NAMES[product]["standby_price"])
        _ingest(product, "price", _RESERVE_TS_NAMES[product]["price"])

    # Activation-fraction series (shared across FCR / aFRR products).  These
    # may carry their own grid too; expand consistently.
    for fraction_name in ("fcr_activation_fraction", "afrr_activation_fraction"):
        fraction_ts = _find_timeseries(model_input, fraction_name)
        fraction_values, _ = _expand_timeseries_to_ptu(
            fraction_ts, ptu_starts_dt, fraction_name
        )
        if fraction_values is not None:
            reserve_columns[fraction_name] = _pad_to(fraction_values, n_intervals)

    # Walk every open market and stamp its config, validate required series,
    # capture multi-band metadata for the output-reshape layer.
    for product in _RESERVE_PRODUCTS:
        market = markets_by_name.get(product)
        if market is None:
            reserve_config[product] = {
                "open": False, "t_min_hours": 0.0, "blocks": [],
            }
            continue

        # LER duration.  Accept either ``activation_duration`` (seconds, the
        # canonical poc-backtesting field) or a legacy ``t_min_minutes``.
        if "activation_duration" in market:
            t_min_hours = float(market["activation_duration"]) / 3600.0
        elif "t_min_minutes" in market:
            t_min_hours = float(market["t_min_minutes"]) / 60.0
        else:
            t_min_hours = 0.25  # 15-min default (ACER-aligned)
            info.append(
                f"approximation: market '{product}' missing "
                "'activation_duration' / 't_min_minutes' — "
                "defaulting LER duration to 15 minutes"
            )

        # Required activation-fraction timeseries for any open product.
        fraction_name = (
            "fcr_activation_fraction"
            if product == "fcr"
            else "afrr_activation_fraction"
        )
        fraction_ts = _find_timeseries(model_input, fraction_name)
        if fraction_ts is None or not fraction_ts.get("values"):
            raise ValueError(
                f"Open market '{product}' requires timeseries "
                f"'{fraction_name}' — none supplied"
            )

        # Bid-block structure is the standby_price grid (it drives the
        # standby revenue stream).  ``price`` and ``position`` grids serve
        # as fallbacks for callers that didn't price-bid on a block.
        grids = product_input_grids[product]
        product_grid = grids.get("standby_price") or grids.get("price") or grids.get("position")
        if product_grid is not None:
            blocks = _blocks_from_grid(
                product_grid["interval_start"],
                product_grid["interval_end"],
                ptu_starts_dt,
                _RESERVE_TS_NAMES[product]["standby_price"],
            )
            market_grids[product] = {**product_grid, "blocks": blocks}
        else:
            blocks = []

        reserve_config[product] = {
            "open": True,
            "t_min_hours": t_min_hours,
            "blocks": blocks,
        }

        n_bands = int(market.get("n_price_bands", 1) or 1)
        n_bands_per_product[product] = n_bands
        offer_prices = market.get("offer_prices") or []
        offer_prices_per_product[product] = [float(p) for p in offer_prices]

        if market.get("service_activation_constraints"):
            info.append(
                f"approximation: market '{product}' set "
                "service_activation_constraints=true — "
                "downgraded to expected-value modelling for v1"
            )

        info.append(
            f"applied: market '{product}' open — bid as decision variable, "
            f"t_min={t_min_hours * 60:.0f} min, "
            f"{len(blocks)} block(s), n_price_bands={n_bands}"
        )

    return (
        reserve_config,
        reserve_columns,
        n_bands_per_product,
        offer_prices_per_product,
        market_grids,
    )


# ── scheduling ───────────────────────────────────────────────────────


def translate_scheduling(model_input: dict[str, Any]) -> TranslationResult:
    """Translate a day-ahead PE ``model_input_data`` to RTC-Tools CSVs."""
    info: list[str] = []

    interval_start: list[str] = model_input.get("interval_start", [])
    interval_end: list[str] = model_input.get("interval_end", [])
    ptu_starts_dt = [_parse_iso_utc(t) for t in interval_start]

    market_grids: dict[str, dict] = {}

    # ── timeseries ──
    # day_ahead_price drives the day-ahead output grid; its native grid (if
    # the caller supplied one) is echoed onto day_ahead_power_in / _out.
    price_ts = _find_timeseries(model_input, "day_ahead_price")
    prices, da_grid = _expand_timeseries_to_ptu(
        price_ts, ptu_starts_dt, "day_ahead_price"
    )
    if prices is None:
        prices = []
    if da_grid is not None:
        da_blocks = _blocks_from_grid(
            da_grid["interval_start"],
            da_grid["interval_end"],
            ptu_starts_dt,
            "day_ahead_price",
        )
        market_grids["day_ahead"] = {**da_grid, "blocks": da_blocks}

    grid_fee_in_ts = _find_timeseries(model_input, "grid_fee_in")
    grid_fee_in_values, _ = _expand_timeseries_to_ptu(
        grid_fee_in_ts, ptu_starts_dt, "grid_fee_in"
    )
    grid_fee_in_values = grid_fee_in_values or []

    grid_fee_out_ts = _find_timeseries(model_input, "grid_fee_out")
    grid_fee_out_values, _ = _expand_timeseries_to_ptu(
        grid_fee_out_ts, ptu_starts_dt, "grid_fee_out"
    )
    grid_fee_out_values = grid_fee_out_values or []

    soc_ts = _find_timeseries(model_input, "state_of_charge")
    initial_soc = soc_ts["values"][0] if soc_ts and soc_ts.get("values") else 0.0

    # ignored timeseries
    for ts_name in ("market_position", "imbalance_price_in", "imbalance_price_out"):
        ts = _find_timeseries(model_input, ts_name)
        if ts and _has_nonzero(ts.get("values")):
            info.append(
                f"ignored_input: timeseries '{ts_name}' — "
                f"not supported by local scheduling solver"
            )

    # ── parameters ──
    capacity = _find_parameter(model_input, "battery_capacity")
    max_charge = _find_parameter(model_input, "max_charge_power")
    max_discharge = _find_parameter(model_input, "max_discharge_power")
    eff_in = _find_parameter(model_input, "efficiency_in")
    eff_out = _find_parameter(model_input, "efficiency_out")
    cost_per_cycle = _find_parameter(model_input, "cost_per_cycle", default=2.0)
    stored_energy_value = _find_parameter(model_input, "stored_energy_value")
    epsilon = _find_parameter(model_input, "epsilon")
    skip_counterfactual = bool(
        _find_parameter(model_input, "skip_counterfactual_reserves", default=0.0)
    )

    max_power: float | None = None
    if max_charge is not None and max_discharge is not None:
        max_power = min(max_charge, max_discharge)
        info.append(
            f"approximation: 'max_charge_power' ({max_charge}) and "
            f"'max_discharge_power' ({max_discharge}) merged to single "
            f"'max_power' = {max_power} using min()"
        )

    efficiency: float | None = None
    if eff_in is not None and eff_out is not None:
        efficiency = eff_in * eff_out
        info.append(
            f"approximation: 'efficiency_in' ({eff_in}) and "
            f"'efficiency_out' ({eff_out}) merged to round-trip "
            f"'efficiency' = {efficiency}"
        )

    sev = stored_energy_value if stored_energy_value is not None else 0.0
    if sev != 0.0:
        info.append(
            f"approximation: 'stored_energy_value' ({sev} EUR/MWh) applied as "
            f"terminal SoC valuation in objective"
        )

    if epsilon is not None:
        info.append(
            "ignored_input: parameter 'epsilon' "
            "— local solver uses HiGHS default tolerances"
        )

    # ignored market configs
    for market in model_input.get("markets", []):
        mname = market.get("name", "unknown")
        mtype = market.get("type", "unknown")
        if mname in _RESERVE_PRODUCTS:
            # Reserve markets handled by _extract_reserves below
            continue
        if mtype == "imbalance":
            info.append(
                f"ignored_input: market config '{mname}' (type={mtype}) "
                f"— imbalance market not modeled"
            )
        elif mtype == "bid_offer_stack":
            n_bands = market.get("n_price_bands", 1)
            if n_bands > 1:
                info.append(
                    f"ignored_input: market config '{mname}' "
                    f"(n_price_bands={n_bands}) — single-band only"
                )
            ignored_keys = [
                k for k in ("min_price", "max_price", "bid_offer_prices") if k in market
            ]
            if ignored_keys:
                info.append(
                    f"ignored_input: market config '{mname}' "
                    f"keys {ignored_keys} — not used by local solver"
                )

    # ── reserves ──
    (
        reserve_config,
        reserve_columns,
        n_bands_per_product,
        offer_prices_per_product,
        reserve_market_grids,
    ) = _extract_reserves(model_input, ptu_starts_dt, info)
    market_grids.update(reserve_market_grids)
    n_intervals = len(interval_start)

    # ── build CSVs ──

    # timeseries_import.csv — time,price
    #
    # RTC-Tools needs one extra row at the end (the endpoint) and we
    # prepend one dummy row so that the initial SoC sits one interval
    # *before* the trading window.  With backward Euler the first
    # collocation point's controls are decoupled from SoC dynamics;
    # the dummy row absorbs that blind-spot harmlessly.
    times = [_iso_to_csv_time(t) for t in interval_start]
    if interval_end:
        times.append(_iso_to_csv_time(interval_end[-1]))

    # Pad prices to match times length (repeat last value for endpoint)
    padded_prices = list(prices)
    if padded_prices and len(padded_prices) < len(times):
        padded_prices.append(padded_prices[-1])

    # Pad grid fees to match times length, defaulting to 0.0

    padded_fee_in = (
        list(grid_fee_in_values) if grid_fee_in_values else [0.0] * n_intervals
    )
    while len(padded_fee_in) < n_intervals:
        padded_fee_in.append(0.0)
    padded_fee_in.append(padded_fee_in[-1] if padded_fee_in else 0.0)

    padded_fee_out = (
        list(grid_fee_out_values) if grid_fee_out_values else [0.0] * n_intervals
    )
    while len(padded_fee_out) < n_intervals:
        padded_fee_out.append(0.0)
    padded_fee_out.append(padded_fee_out[-1] if padded_fee_out else 0.0)

    if _has_nonzero(grid_fee_in_values):
        info.append(
            f"applied: 'grid_fee_in' timeseries ({len(grid_fee_in_values)} values) "
            f"— subtracted from charging revenue in objective"
        )
    if _has_nonzero(grid_fee_out_values):
        info.append(
            f"applied: 'grid_fee_out' timeseries ({len(grid_fee_out_values)} values) "
            f"— subtracted from discharging revenue in objective"
        )

    # Append endpoint rows to reserve columns (repeat last value).
    for col in _RESERVE_CSV_COLUMNS:
        vals = reserve_columns[col]
        vals.append(vals[-1] if vals else 0.0)

    # Prepend dummy row — price=0, fees=0, all reserves=0 so the optimizer
    # earns nothing and is constrained to nothing on the dummy timestep.
    if interval_start:
        times.insert(0, _prepend_dummy_time(interval_start))
        padded_prices.insert(0, 0.0)
        padded_fee_in.insert(0, 0.0)
        padded_fee_out.insert(0, 0.0)
        for col in _RESERVE_CSV_COLUMNS:
            reserve_columns[col].insert(0, 0.0)

    header = ["time", "price", "grid_fee_in", "grid_fee_out", *_RESERVE_CSV_COLUMNS]
    rows = [
        [
            times[i],
            padded_prices[i] if i < len(padded_prices) else 0.0,
            padded_fee_in[i],
            padded_fee_out[i],
            *[reserve_columns[col][i] for col in _RESERVE_CSV_COLUMNS],
        ]
        for i in range(len(times))
    ]
    timeseries_csv = _write_csv(header, rows)

    # initial_state.csv
    initial_state_csv = _write_csv(["soc"], [[initial_soc]])

    # parameters.csv (only if we have overrides)
    param_header: list[str] = []
    param_row: list[float] = []
    if capacity is not None:
        param_header.append("capacity")
        param_row.append(capacity)
    if max_power is not None:
        param_header.append("max_power")
        param_row.append(max_power)
    if efficiency is not None:
        param_header.append("efficiency")
        param_row.append(efficiency)

    parameters_csv = _write_csv(param_header, [param_row]) if param_header else None

    # Convert cost_per_cycle (EUR/cycle) to cycling_penalty_factor (EUR/MWh throughput).
    # A full cycle = charge capacity + discharge capacity = 2 * capacity MWh throughput.
    if cost_per_cycle is not None and capacity is not None and capacity > 0:
        cycling_penalty_factor = cost_per_cycle / (2.0 * capacity)
        info.append(
            f"approximation: 'cost_per_cycle' ({cost_per_cycle} EUR/cycle) converted to "
            f"cycling_penalty_factor = {cycling_penalty_factor:.4f} EUR/MWh "
            f"using cost_per_cycle / (2 * capacity)"
        )
    else:
        cycling_penalty_factor = cost_per_cycle if cost_per_cycle is not None else 2.0

    info.append("solver: using HiGHS MILP via RTC-Tools (PE API solver may differ)")

    return TranslationResult(
        timeseries_csv=timeseries_csv,
        initial_state_csv=initial_state_csv,
        parameters_csv=parameters_csv,
        cycling_penalty=cycling_penalty_factor,
        transaction_cost=0.0,
        n_segments=0,
        stored_energy_value=sev,
        info=info,
        reserve_config=reserve_config,
        n_bands_per_product=n_bands_per_product,
        offer_prices_per_product=offer_prices_per_product,
        skip_counterfactual_reserves=skip_counterfactual,
        market_grids=market_grids,
    )


# ── intraday ─────────────────────────────────────────────────────────


def translate_intraday(model_input: dict[str, Any]) -> TranslationResult:
    """Translate an IC-trading PE ``model_input_data`` to RTC-Tools CSVs."""
    info: list[str] = []

    interval_start: list[str] = model_input.get("interval_start", [])
    interval_end: list[str] = model_input.get("interval_end", [])
    ptu_starts_dt = [_parse_iso_utc(t) for t in interval_start]
    market_grids: dict[str, dict] = {}

    # ── detect n_segments ──
    n_segments = 0
    for market in model_input.get("markets", []):
        if market.get("name") == "orderbook":
            n_segments = market.get("n_orderbook_segments", 0)
            break

    # Fallback: count orderbook timeseries
    if n_segments == 0:
        seg = 1
        while _find_timeseries(model_input, f"orderbook[{seg}]_price_in"):
            seg += 1
        n_segments = seg - 1

    if n_segments == 0:
        n_segments = 1
        info.append(
            "approximation: could not detect n_orderbook_segments, defaulting to 1"
        )

    info.append(f"solver: n_orderbook_entries set to {n_segments} based on request")

    # ── timeseries ──
    market_pos_ts = _find_timeseries(model_input, "market_position")
    market_position, _ = _expand_timeseries_to_ptu(
        market_pos_ts, ptu_starts_dt, "market_position"
    )
    market_position = market_position or []

    grid_fee_in_ts = _find_timeseries(model_input, "grid_fee_in")
    grid_fee_in_values, _ = _expand_timeseries_to_ptu(
        grid_fee_in_ts, ptu_starts_dt, "grid_fee_in"
    )
    grid_fee_in_values = grid_fee_in_values or []

    grid_fee_out_ts = _find_timeseries(model_input, "grid_fee_out")
    grid_fee_out_values, _ = _expand_timeseries_to_ptu(
        grid_fee_out_ts, ptu_starts_dt, "grid_fee_out"
    )
    grid_fee_out_values = grid_fee_out_values or []

    soc_ts = _find_timeseries(model_input, "state_of_charge")
    initial_soc = soc_ts["values"][0] if soc_ts and soc_ts.get("values") else 0.0

    # ignored timeseries
    for ts_name in ("imbalance_price_in", "imbalance_price_out"):
        ts = _find_timeseries(model_input, ts_name)
        if ts and _has_nonzero(ts.get("values")):
            info.append(
                f"ignored_input: timeseries '{ts_name}' — "
                f"not supported by local intraday solver"
            )

    # ── parameters (same logic as scheduling) ──
    capacity = _find_parameter(model_input, "battery_capacity")
    max_charge = _find_parameter(model_input, "max_charge_power")
    max_discharge = _find_parameter(model_input, "max_discharge_power")
    eff_in = _find_parameter(model_input, "efficiency_in")
    eff_out = _find_parameter(model_input, "efficiency_out")
    cost_per_cycle = _find_parameter(model_input, "cost_per_cycle", default=2.0)
    stored_energy_value = _find_parameter(model_input, "stored_energy_value")
    epsilon = _find_parameter(model_input, "epsilon")
    skip_counterfactual = bool(
        _find_parameter(model_input, "skip_counterfactual_reserves", default=0.0)
    )

    max_power: float | None = None
    if max_charge is not None and max_discharge is not None:
        max_power = min(max_charge, max_discharge)
        info.append(
            f"approximation: 'max_charge_power' ({max_charge}) and "
            f"'max_discharge_power' ({max_discharge}) merged to single "
            f"'max_power' = {max_power} using min()"
        )

    efficiency: float | None = None
    if eff_in is not None and eff_out is not None:
        efficiency = eff_in * eff_out
        info.append(
            f"approximation: 'efficiency_in' ({eff_in}) and "
            f"'efficiency_out' ({eff_out}) merged to round-trip "
            f"'efficiency' = {efficiency}"
        )

    sev = stored_energy_value if stored_energy_value is not None else 0.0
    if sev != 0.0:
        info.append(
            f"approximation: 'stored_energy_value' ({sev} EUR/MWh) applied as "
            f"terminal SoC valuation in objective"
        )

    if epsilon is not None:
        info.append(
            "ignored_input: parameter 'epsilon' "
            "— local solver uses HiGHS default tolerances"
        )

    # ignored market configs
    for market in model_input.get("markets", []):
        mtype = market.get("type", "unknown")
        mname = market.get("name", "unknown")
        if mname in _RESERVE_PRODUCTS:
            # Reserve markets handled by _extract_reserves below.  The intraday
            # solver never bids reserves but still consumes their LER and
            # headroom impact via the committed_<p> timeseries.
            continue
        if mtype == "imbalance":
            info.append(
                f"ignored_input: market config '{mname}' (type={mtype}) "
                f"— imbalance market not modeled"
            )

    # ── reserves ──
    # Extract reserve config before CSV building so the columns can be
    # appended to the row layout below.
    (
        reserve_config,
        reserve_columns,
        n_bands_per_product,
        offer_prices_per_product,
        reserve_market_grids,
    ) = _extract_reserves(model_input, ptu_starts_dt, info)
    market_grids.update(reserve_market_grids)
    n_intervals_for_reserves = len(interval_start)

    # Intraday never *bids* reserves: any market entry the caller included
    # is treated as committed-only, so flip ``open`` to False here.  The
    # solver class also pins bid totals to 0 as belt-and-braces.
    for product in _RESERVE_PRODUCTS:
        if reserve_config.get(product, {}).get("open"):
            reserve_config[product]["open"] = False
            info.append(
                f"approximation: intraday solver ignores '{product}' bid "
                "decision variables — only the committed position is honoured"
            )

    # ── build CSVs ──

    # timeseries_import.csv
    # Columns: time, committed_net_power, bid_prices[1..N], ask_prices[1..N],
    #          bid_volumes[1..N], ask_volumes[1..N]
    #
    # We prepend one dummy row (see translate_scheduling for rationale).
    times = [_iso_to_csv_time(t) for t in interval_start]
    if interval_end:
        times.append(_iso_to_csv_time(interval_end[-1]))

    n_intervals = len(interval_start)

    # Decompose committed_net_power (market_position) into two non-negative
    # components so the Modelica model can apply efficiency losses to each
    # gross power flow independently.
    #
    # When the committed position and incremental intraday trades partially
    # offset (e.g. committed discharge + new charge order), computing SoC
    # dynamics from the net would underestimate efficiency losses because:
    #   net_efficiency_loss(P_net) < gross_loss(P_discharge) + gross_loss(P_charge)
    #
    # By splitting here (fixed input, no solver non-linearity) the Modelica
    # equations receive:
    #   charge_power   = committed_charge   + sum(charge_power_asks)
    #   discharge_power = committed_discharge + sum(discharge_power_bids)
    # and der(soc) is computed on those gross values.
    raw_pos = list(market_position) if market_position else [0.0] * n_intervals
    if len(raw_pos) < n_intervals:
        raw_pos.extend([0.0] * (n_intervals - len(raw_pos)))
    # Add endpoint row (repeat last value)
    raw_pos.append(raw_pos[-1] if raw_pos else 0.0)

    # Non-negative committed flows
    padded_committed_charge = [max(0.0, -v) for v in raw_pos]  # net < 0 → charging
    padded_committed_discharge = [max(0.0, v) for v in raw_pos]  # net > 0 → discharging

    if any(v != 0.0 for v in raw_pos):
        info.append(
            "applied: 'market_position' decomposed into 'committed_charge' and "
            "'committed_discharge' for gross-flow SoC tracking — prevents "
            "underestimating efficiency losses when committed position and "
            "incremental trades partially offset"
        )

    # Pad grid fees to match intervals, defaulting to 0.0
    padded_fee_in = (
        list(grid_fee_in_values) if grid_fee_in_values else [0.0] * n_intervals
    )
    while len(padded_fee_in) < n_intervals:
        padded_fee_in.append(0.0)
    padded_fee_in.append(padded_fee_in[-1] if padded_fee_in else 0.0)

    padded_fee_out = (
        list(grid_fee_out_values) if grid_fee_out_values else [0.0] * n_intervals
    )
    while len(padded_fee_out) < n_intervals:
        padded_fee_out.append(0.0)
    padded_fee_out.append(padded_fee_out[-1] if padded_fee_out else 0.0)

    if _has_nonzero(grid_fee_in_values):
        info.append(
            f"applied: 'grid_fee_in' timeseries ({len(grid_fee_in_values)} values) "
            f"— subtracted from charging revenue in objective"
        )
    if _has_nonzero(grid_fee_out_values):
        info.append(
            f"applied: 'grid_fee_out' timeseries ({len(grid_fee_out_values)} values) "
            f"— subtracted from discharging revenue in objective"
        )

    # Collect orderbook columns
    orderbook_columns: dict[str, list[float]] = {}
    for seg in range(1, n_segments + 1):
        for pe_name, csv_name in (
            (f"orderbook[{seg}]_price_out", f"bid_prices[{seg}]"),
            (f"orderbook[{seg}]_price_in", f"ask_prices[{seg}]"),
            (f"orderbook[{seg}]_max_power_out", f"bid_volumes[{seg}]"),
            (f"orderbook[{seg}]_max_power_in", f"ask_volumes[{seg}]"),
        ):
            ts = _find_timeseries(model_input, pe_name)
            values = (
                list(ts["values"]) if ts and ts.get("values") else [0.0] * n_intervals
            )
            # Pad to n_intervals
            while len(values) < n_intervals:
                values.append(0.0)
            # Endpoint row — repeat last
            values.append(values[-1])
            orderbook_columns[csv_name] = values

    # Append endpoint rows for reserve columns (repeat last value).
    for col in _RESERVE_CSV_COLUMNS:
        vals = reserve_columns[col]
        vals.append(vals[-1] if vals else 0.0)

    # Prepend dummy row — zero volumes so no trading is possible there
    if interval_start:
        times.insert(0, _prepend_dummy_time(interval_start))
        padded_committed_charge.insert(0, 0.0)
        padded_committed_discharge.insert(0, 0.0)
        padded_fee_in.insert(0, 0.0)
        padded_fee_out.insert(0, 0.0)
        for csv_name, values in orderbook_columns.items():
            values.insert(0, 0.0)
        for col in _RESERVE_CSV_COLUMNS:
            reserve_columns[col].insert(0, 0.0)

    # Build header and rows
    header = [
        "time",
        "committed_charge",
        "committed_discharge",
        "grid_fee_in",
        "grid_fee_out",
    ]
    for seg in range(1, n_segments + 1):
        header.extend(
            [
                f"bid_prices[{seg}]",
                f"ask_prices[{seg}]",
                f"bid_volumes[{seg}]",
                f"ask_volumes[{seg}]",
            ]
        )
    header.extend(_RESERVE_CSV_COLUMNS)

    rows: list[list[Any]] = []
    for i in range(len(times)):
        row: list[Any] = [
            times[i],
            padded_committed_charge[i],
            padded_committed_discharge[i],
            padded_fee_in[i],
            padded_fee_out[i],
        ]
        for seg in range(1, n_segments + 1):
            row.append(orderbook_columns[f"bid_prices[{seg}]"][i])
            row.append(orderbook_columns[f"ask_prices[{seg}]"][i])
            row.append(orderbook_columns[f"bid_volumes[{seg}]"][i])
            row.append(orderbook_columns[f"ask_volumes[{seg}]"][i])
        for col in _RESERVE_CSV_COLUMNS:
            row.append(reserve_columns[col][i])
        rows.append(row)

    timeseries_csv = _write_csv(header, rows)

    # initial_state.csv
    initial_state_csv = _write_csv(["soc"], [[initial_soc]])

    # parameters.csv
    param_header: list[str] = []
    param_row: list[float] = []
    if capacity is not None:
        param_header.append("capacity")
        param_row.append(capacity)
    if max_power is not None:
        param_header.append("max_power")
        param_row.append(max_power)
    if efficiency is not None:
        param_header.append("efficiency")
        param_row.append(efficiency)

    parameters_csv = _write_csv(param_header, [param_row]) if param_header else None

    # Convert cost_per_cycle (EUR/cycle) to cycling_penalty_factor (EUR/MWh throughput).
    # A full cycle = charge capacity + discharge capacity = 2 * capacity MWh throughput.
    if cost_per_cycle is not None and capacity is not None and capacity > 0:
        cycling_penalty_factor = cost_per_cycle / (2.0 * capacity)
        info.append(
            f"approximation: 'cost_per_cycle' ({cost_per_cycle} EUR/cycle) converted to "
            f"cycling_penalty_factor = {cycling_penalty_factor:.4f} EUR/MWh "
            f"using cost_per_cycle / (2 * capacity)"
        )
    else:
        cycling_penalty_factor = cost_per_cycle if cost_per_cycle is not None else 2.0

    info.append("solver: using HiGHS MILP via RTC-Tools (PE API solver may differ)")

    return TranslationResult(
        timeseries_csv=timeseries_csv,
        initial_state_csv=initial_state_csv,
        parameters_csv=parameters_csv,
        cycling_penalty=cycling_penalty_factor,
        transaction_cost=0.05,
        n_segments=n_segments,
        stored_energy_value=sev,
        info=info,
        reserve_config=reserve_config,
        n_bands_per_product=n_bands_per_product,
        offer_prices_per_product=offer_prices_per_product,
        skip_counterfactual_reserves=skip_counterfactual,
        market_grids=market_grids,
    )
