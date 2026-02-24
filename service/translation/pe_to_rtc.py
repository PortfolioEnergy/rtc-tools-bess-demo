"""Translate PE API model_input_data into RTC-Tools CSV input files.

For every PE API field that is present but not used, an entry is appended
to the ``_info`` list so the caller can surface it in the response.
"""

from __future__ import annotations

import io
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    info: list[str] = field(default_factory=list)


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


# ── scheduling ───────────────────────────────────────────────────────


def translate_scheduling(model_input: dict[str, Any]) -> TranslationResult:
    """Translate a day-ahead PE ``model_input_data`` to RTC-Tools CSVs."""
    info: list[str] = []

    interval_start: list[str] = model_input.get("interval_start", [])
    interval_end: list[str] = model_input.get("interval_end", [])

    # ── timeseries ──
    price_ts = _find_timeseries(model_input, "day_ahead_price")
    prices = price_ts["values"] if price_ts else []

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

    if stored_energy_value is not None and stored_energy_value != 0.0:
        info.append(
            f"ignored_input: parameter 'stored_energy_value' ({stored_energy_value}) "
            f"— no terminal energy value in local solver objective"
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

    # ── build CSVs ──

    # timeseries_import.csv — time,price
    # RTC-Tools needs one extra row at the end (the endpoint)
    times = [_iso_to_csv_time(t) for t in interval_start]
    if interval_end:
        times.append(_iso_to_csv_time(interval_end[-1]))

    # Pad prices to match times length (repeat last value for endpoint)
    padded_prices = list(prices)
    if padded_prices and len(padded_prices) < len(times):
        padded_prices.append(padded_prices[-1])

    rows = [
        [times[i], padded_prices[i] if i < len(padded_prices) else 0.0]
        for i in range(len(times))
    ]
    timeseries_csv = _write_csv(["time", "price"], rows)

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

    info.append("solver: using HiGHS MILP via RTC-Tools (PE API solver may differ)")

    return TranslationResult(
        timeseries_csv=timeseries_csv,
        initial_state_csv=initial_state_csv,
        parameters_csv=parameters_csv,
        cycling_penalty=cost_per_cycle if cost_per_cycle is not None else 2.0,
        transaction_cost=0.0,
        n_segments=0,
        info=info,
    )


# ── intraday ─────────────────────────────────────────────────────────


def translate_intraday(model_input: dict[str, Any]) -> TranslationResult:
    """Translate an IC-trading PE ``model_input_data`` to RTC-Tools CSVs."""
    info: list[str] = []

    interval_start: list[str] = model_input.get("interval_start", [])
    interval_end: list[str] = model_input.get("interval_end", [])

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
    market_position = market_pos_ts["values"] if market_pos_ts else []

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

    if stored_energy_value is not None and stored_energy_value != 0.0:
        info.append(
            f"ignored_input: parameter 'stored_energy_value' ({stored_energy_value}) "
            f"— no terminal energy value in local solver objective"
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
        if mtype == "imbalance":
            info.append(
                f"ignored_input: market config '{mname}' (type={mtype}) "
                f"— imbalance market not modeled"
            )

    # ── build CSVs ──

    # timeseries_import.csv
    # Columns: time, committed_net_power, bid_prices[1..N], ask_prices[1..N],
    #          bid_volumes[1..N], ask_volumes[1..N]
    times = [_iso_to_csv_time(t) for t in interval_start]
    if interval_end:
        times.append(_iso_to_csv_time(interval_end[-1]))

    n_intervals = len(interval_start)

    # Pad market_position
    padded_pos = list(market_position) if market_position else [0.0] * n_intervals
    if len(padded_pos) < n_intervals:
        padded_pos.extend([0.0] * (n_intervals - len(padded_pos)))
    # Add endpoint row
    padded_pos.append(padded_pos[-1] if padded_pos else 0.0)

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

    # Build header and rows
    header = ["time", "committed_net_power"]
    for seg in range(1, n_segments + 1):
        header.extend(
            [
                f"bid_prices[{seg}]",
                f"ask_prices[{seg}]",
                f"bid_volumes[{seg}]",
                f"ask_volumes[{seg}]",
            ]
        )

    rows: list[list[Any]] = []
    for i in range(len(times)):
        row: list[Any] = [times[i], padded_pos[i]]
        for seg in range(1, n_segments + 1):
            row.append(orderbook_columns[f"bid_prices[{seg}]"][i])
            row.append(orderbook_columns[f"ask_prices[{seg}]"][i])
            row.append(orderbook_columns[f"bid_volumes[{seg}]"][i])
            row.append(orderbook_columns[f"ask_volumes[{seg}]"][i])
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

    info.append("solver: using HiGHS MILP via RTC-Tools (PE API solver may differ)")

    return TranslationResult(
        timeseries_csv=timeseries_csv,
        initial_state_csv=initial_state_csv,
        parameters_csv=parameters_csv,
        cycling_penalty=cost_per_cycle if cost_per_cycle is not None else 2.0,
        transaction_cost=0.05,
        n_segments=n_segments,
        info=info,
    )
