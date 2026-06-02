"""Orchestrates RTC-Tools solver execution in isolated temp directories.

Each request gets its own temp directory with ``input/`` and ``output/``
subdirectories.  The model directory is kept stable across requests so that
pymoca's on-disk cache (``*.pymoca_cache``) is reused between runs:

- **Scheduling**: the permanent ``scheduling/model/`` directory is used
  directly as ``model_folder``.  It is read-only from the solver's
  perspective; pymoca only updates the cache file there.

- **Intraday**: the ``.mo`` file must be patched per ``n_segments`` value.
  A stable per-``n_segments`` directory is created once (lazily, with a
  write-lock to guard concurrent first-time compilations) and reused on
  every subsequent request with the same segment count.

The solver class is still dynamically subclassed per request to avoid shared
mutable state between concurrent calls.  Input data and output results remain
fully isolated in per-run temp directories — cross-run contamination is not
possible.

When diagnostics are enabled, the runner additionally executes a
*counterfactual* re-solve with all reserve markets stripped from the input.
The resulting "no reserves" metrics flow to the reasoning-markdown builder
to quantify the EUR delta reserves contributed to (or cost) the portfolio
this horizon.  This roughly doubles solver wall time; callers can pass
``parameters[skip_counterfactual_reserves] = 1`` to suppress it.
"""

from __future__ import annotations

import copy
import logging
import re
import tempfile
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
from rtctools.util import run_optimization_problem

from service.translation.pe_to_rtc import (
    TranslationResult,
    translate_intraday,
    translate_scheduling,
)
from service.translation.rtc_to_pe import (
    translate_intraday_result,
    translate_scheduling_result,
)
from service.translation.setpoints import translate_setpoints

# Reserve products handled by the counterfactual stripper.  Kept inline
# rather than imported to avoid a cycle with the translation module.
_RESERVE_PRODUCTS: tuple[str, ...] = ("fcr", "afrr_up", "afrr_down")
_RESERVE_TIMESERIES_NAMES: tuple[str, ...] = (
    "fcr_position", "afrr_up_position", "afrr_down_position",
    "fcr_standby_price", "fcr_price",
    "afrr_up_standby_price", "afrr_up_price",
    "afrr_down_standby_price", "afrr_down_price",
    "fcr_activation_fraction", "afrr_activation_fraction",
)

_log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEDULING_MODEL = _REPO_ROOT / "scheduling" / "model"
_INTRADAY_MODEL = _REPO_ROOT / "continuous_intraday" / "model"
_INTRADAY_MO = _INTRADAY_MODEL / "BESSIntraday.mo"

# Intraday model directory cache keyed by n_segments.  Entries are created
# lazily on the first request for a given segment count and then reused for
# the lifetime of the process.  The directories are created via
# tempfile.mkdtemp() so the OS cleans them up on next boot if the process
# exits without doing so explicitly.
_intraday_model_cache: dict[int, Path] = {}
_intraday_model_lock = threading.Lock()


def run_solver(
    solver_type: str,
    model_input: dict[str, Any],
    include_diagnostics: bool = False,
) -> dict[str, Any]:
    """Run the appropriate solver and return the full HTTP response body.

    Always returns ``{"result": {...}}`` where ``result`` contains ``members``
    and ``_info``.  When diagnostics are enabled, chart data URIs are appended
    to ``_info`` as ``"image:<name>: <data URI>"`` entries so the response
    shape stays compatible with the PE API.
    """
    if solver_type == "da_setpoints":
        return {"result": translate_setpoints(model_input)}

    if solver_type == "scheduling":
        return _run_scheduling(model_input, include_diagnostics=include_diagnostics)

    if solver_type == "intraday":
        return _run_intraday(model_input, include_diagnostics=include_diagnostics)

    raise ValueError(f"Unknown solver_type: {solver_type}")


# ── scheduling ───────────────────────────────────────────────────────


def _run_scheduling(
    model_input: dict[str, Any],
    include_diagnostics: bool = False,
) -> dict[str, Any]:
    translation = translate_scheduling(model_input)

    with tempfile.TemporaryDirectory(prefix="bess_sched_") as tmpdir:
        base = Path(tmpdir)
        _prepare_io_dirs(base)

        # Write input files
        _write_inputs(base, translation)

        # Build a per-request solver class to avoid shared state
        from service.solvers.scheduling import ConfigurableBESS

        klass = type(
            f"BESS_{uuid4().hex[:8]}",
            (ConfigurableBESS,),
            {
                "_cycling_penalty": translation.cycling_penalty,
                "_stored_energy_value": translation.stored_energy_value,
                "_reserve_config": translation.reserve_config,
                "model_name": "BESS",
            },
        )

        _log.info(
            "Running scheduling solver (cycling_penalty=%.4f, "
            "stored_energy_value=%.4f)",
            translation.cycling_penalty,
            translation.stored_energy_value,
        )
        # model_folder points at the stable repo directory so pymoca's
        # .pymoca_cache is preserved across requests.  input/output remain
        # in the per-run temp directory.
        # The returned problem instance exposes solver internals (objective
        # value, solver stats, Lagrange multipliers) used for diagnostics.
        prob = run_optimization_problem(
            klass,
            model_folder=str(_SCHEDULING_MODEL),
            input_folder=str(base / "input"),
            output_folder=str(base / "output"),
            log_level=logging.WARNING,
        )

        counterfactual_metrics = None
        if include_diagnostics and _has_active_reserves(translation.reserve_config):
            if translation.skip_counterfactual_reserves:
                translation.info.append(
                    "diagnostics: counterfactual 'no reserves' re-solve "
                    "skipped via skip_counterfactual_reserves=1 parameter"
                )
            else:
                counterfactual_metrics = _counterfactual_metrics_scheduling(
                    model_input
                )
                translation.info.append(
                    "diagnostics: counterfactual 'no reserves' re-solve "
                    "completed — set parameter "
                    "skip_counterfactual_reserves=1 to disable"
                )

        result, reasoning_markdown = translate_scheduling_result(
            base / "output",
            model_input,
            translation.info,
            prob=prob if include_diagnostics else None,
            n_bands_per_product=translation.n_bands_per_product,
            offer_prices_per_product=translation.offer_prices_per_product,
            reserve_config=translation.reserve_config,
            counterfactual_metrics=counterfactual_metrics,
            skip_counterfactual_reserves=translation.skip_counterfactual_reserves,
        )
        response: dict[str, Any] = {"result": result}
        if reasoning_markdown:
            response["reasoning_markdown"] = reasoning_markdown
        return response


# ── intraday ─────────────────────────────────────────────────────────


def _run_intraday(
    model_input: dict[str, Any],
    include_diagnostics: bool = False,
) -> dict[str, Any]:
    translation = translate_intraday(model_input)
    model_dir = _get_intraday_model_dir(translation.n_segments)

    with tempfile.TemporaryDirectory(prefix="bess_id_") as tmpdir:
        base = Path(tmpdir)
        _prepare_io_dirs(base)

        # Write input files — model is handled by the stable model_dir
        _write_inputs(base, translation)

        # Build a per-request solver class
        from service.solvers.intraday import ConfigurableBESSIntraday

        klass = type(
            f"BESSIntraday_{uuid4().hex[:8]}",
            (ConfigurableBESSIntraday,),
            {
                "_cycling_penalty": translation.cycling_penalty,
                "_transaction_cost": translation.transaction_cost,
                "_stored_energy_value": translation.stored_energy_value,
                "_reserve_config": translation.reserve_config,
                "model_name": "BESSIntraday",
            },
        )

        _log.info(
            "Running intraday solver (n_segments=%d, cycling_penalty=%.4f, "
            "transaction_cost=%.4f, stored_energy_value=%.4f)",
            translation.n_segments,
            translation.cycling_penalty,
            translation.transaction_cost,
            translation.stored_energy_value,
        )
        # model_folder points at the stable per-n_segments directory so
        # pymoca's .pymoca_cache survives between requests.
        # The returned problem instance exposes solver internals (objective
        # value, solver stats, Lagrange multipliers) used for diagnostics.
        prob = run_optimization_problem(
            klass,
            model_folder=str(model_dir),
            input_folder=str(base / "input"),
            output_folder=str(base / "output"),
            log_level=logging.WARNING,
        )

        counterfactual_metrics = None
        # Intraday counterfactual fires whenever there are committed reserve
        # positions to strip, even though the solver itself never bids.
        if include_diagnostics and _has_any_reserve_inputs(model_input):
            if translation.skip_counterfactual_reserves:
                translation.info.append(
                    "diagnostics: counterfactual 'no reserves' re-solve "
                    "skipped via skip_counterfactual_reserves=1 parameter"
                )
            else:
                counterfactual_metrics = _counterfactual_metrics_intraday(
                    model_input
                )
                translation.info.append(
                    "diagnostics: counterfactual 'no reserves' re-solve "
                    "completed — set parameter "
                    "skip_counterfactual_reserves=1 to disable"
                )

        result, reasoning_markdown = translate_intraday_result(
            base / "output",
            model_input,
            translation.n_segments,
            translation.info,
            prob=prob if include_diagnostics else None,
            n_bands_per_product=translation.n_bands_per_product,
            offer_prices_per_product=translation.offer_prices_per_product,
            reserve_config=translation.reserve_config,
            counterfactual_metrics=counterfactual_metrics,
            skip_counterfactual_reserves=translation.skip_counterfactual_reserves,
        )
        response: dict[str, Any] = {"result": result}
        if reasoning_markdown:
            response["reasoning_markdown"] = reasoning_markdown
        return response


# ── helpers ──────────────────────────────────────────────────────────


def _prepare_io_dirs(base: Path) -> None:
    """Create the per-run input and output directories inside *base*."""
    (base / "input").mkdir(exist_ok=True)
    (base / "output").mkdir(exist_ok=True)


def _write_inputs(base: Path, translation: TranslationResult) -> None:
    (base / "input" / "timeseries_import.csv").write_text(
        translation.timeseries_csv, encoding="utf-8"
    )
    (base / "input" / "initial_state.csv").write_text(
        translation.initial_state_csv, encoding="utf-8"
    )
    if translation.parameters_csv:
        (base / "input" / "parameters.csv").write_text(
            translation.parameters_csv, encoding="utf-8"
        )


def _get_intraday_model_dir(n_segments: int) -> Path:
    """Return a stable model directory for *n_segments*, creating it on first use.

    The directory contains a ``BESSIntraday.mo`` patched with the correct
    ``n_orderbook_entries`` value.  pymoca will compile the model on the first
    request for a given segment count and write a ``BESSIntraday.pymoca_cache``
    file alongside it; subsequent requests reuse that cache.

    A per-process write-lock prevents two concurrent first-time requests for
    the same ``n_segments`` from writing the ``.mo`` file simultaneously.
    """
    if n_segments in _intraday_model_cache:
        return _intraday_model_cache[n_segments]

    with _intraday_model_lock:
        # Double-checked locking: another thread may have populated the cache
        # while we were waiting for the lock.
        if n_segments in _intraday_model_cache:
            return _intraday_model_cache[n_segments]

        model_dir = Path(tempfile.mkdtemp(prefix=f"bess_id_model_{n_segments}_"))
        _write_intraday_model(model_dir, n_segments)
        _intraday_model_cache[n_segments] = model_dir
        _log.info(
            "Intraday model directory created for n_segments=%d at %s",
            n_segments,
            model_dir,
        )
        return model_dir


def _write_intraday_model(model_dir: Path, n_segments: int) -> None:
    """Write ``BESSIntraday.mo`` with ``n_orderbook_entries`` set to *n_segments*."""
    mo_content = _INTRADAY_MO.read_text(encoding="utf-8")

    mo_content = re.sub(
        r"parameter\s+Integer\s+n_orderbook_entries\s*=\s*\d+",
        f"parameter Integer n_orderbook_entries = {n_segments}",
        mo_content,
    )

    (model_dir / "BESSIntraday.mo").write_text(mo_content, encoding="utf-8")


# ── counterfactual ("no reserves") re-solve support ─────────────────


def _has_active_reserves(reserve_config: dict[str, dict]) -> bool:
    """True if any product is open for bidding this run."""
    return any(
        (cfg or {}).get("open") for cfg in (reserve_config or {}).values()
    )


def _has_any_reserve_inputs(model_input: dict[str, Any]) -> bool:
    """True if any reserve market entry or non-zero reserve timeseries exists.

    Used by intraday — there are no bid decisions to roll back, but cleared
    positions and activation prices still tightened the LER constraints
    and contributed to objective terms, so the counterfactual is meaningful.
    """
    for market in model_input.get("markets", []):
        if market.get("name") in _RESERVE_PRODUCTS:
            return True
    for ts in model_input.get("timeseries", []):
        name = ts.get("name")
        if name in _RESERVE_TIMESERIES_NAMES and any(
            float(v) != 0.0 for v in (ts.get("values") or [])
        ):
            return True
    return False


def _strip_reserves_from_input(model_input: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of *model_input* with every reserve trace removed.

    The counterfactual run must see exactly the input the caller would have
    submitted if reserves did not exist: no reserve market entries, no
    committed positions, no standby/activation prices, no activation
    fractions.  Everything else (energy market, orderbook, grid fees,
    initial SoC, parameters) stays intact.
    """
    stripped = copy.deepcopy(model_input)
    stripped["markets"] = [
        m for m in stripped.get("markets", []) if m.get("name") not in _RESERVE_PRODUCTS
    ]
    stripped["timeseries"] = [
        ts
        for ts in stripped.get("timeseries", [])
        if ts.get("name") not in _RESERVE_TIMESERIES_NAMES
    ]
    # Force the second solve to skip its own counterfactual (no recursion).
    new_params = []
    saw_skip = False
    for p in stripped.get("parameters", []) or []:
        if p.get("name") == "skip_counterfactual_reserves":
            new_params.append({"name": "skip_counterfactual_reserves", "value": 1.0})
            saw_skip = True
        else:
            new_params.append(p)
    if not saw_skip:
        new_params.append({"name": "skip_counterfactual_reserves", "value": 1.0})
    stripped["parameters"] = new_params
    return stripped


def _metrics_from_output_csv(
    df: pd.DataFrame, df_in: pd.DataFrame, dt_hours: float
) -> dict[str, Any]:
    """Compute reserve-agnostic profit numbers from a solved CSV.

    Reads the same canonical columns both solvers produce.  Returned dict
    keys mirror the actual-run metrics so the reasoning markdown can diff
    them line-by-line.
    """
    charge = df["charge_power"].to_numpy(dtype=float)
    discharge = df["discharge_power"].to_numpy(dtype=float)
    soc = df["soc"].to_numpy(dtype=float)
    n = len(df)
    price = (
        df_in["price"].to_numpy(dtype=float)[:n]
        if "price" in df_in.columns
        else None
    )
    fee_in = (
        df_in["grid_fee_in"].to_numpy(dtype=float)[:n]
        if "grid_fee_in" in df_in.columns
        else None
    )
    fee_out = (
        df_in["grid_fee_out"].to_numpy(dtype=float)[:n]
        if "grid_fee_out" in df_in.columns
        else None
    )
    arbitrage_revenue = 0.0
    if price is not None:
        arbitrage_revenue = float(((discharge - charge) * price).sum()) * dt_hours
    grid_fee_cost = 0.0
    if fee_in is not None:
        grid_fee_cost += float((fee_in * charge).sum()) * dt_hours
    if fee_out is not None:
        grid_fee_cost += float((fee_out * discharge).sum()) * dt_hours
    return {
        "arbitrage_revenue_eur": arbitrage_revenue,
        "grid_fee_cost_eur": grid_fee_cost,
        "total_charged_mwh": float(charge.sum()) * dt_hours,
        "total_discharged_mwh": float(discharge.sum()) * dt_hours,
        "throughput_mwh": float((charge + discharge).sum()) * dt_hours,
        "initial_soc_mwh": float(soc[0]) if n else 0.0,
        "final_soc_mwh": float(soc[-1]) if n else 0.0,
        "horizon_intervals": n,
    }


def _counterfactual_metrics_scheduling(
    model_input: dict[str, Any],
) -> dict[str, Any] | None:
    """Re-solve scheduling without any reserves and return the metric dict.

    Returns ``None`` on any failure — the markdown builder defensively skips
    the comparison section in that case rather than failing the whole run.
    """
    try:
        stripped = _strip_reserves_from_input(model_input)
        translation = translate_scheduling(stripped)
        with tempfile.TemporaryDirectory(prefix="bess_sched_cf_") as tmpdir:
            base = Path(tmpdir)
            _prepare_io_dirs(base)
            _write_inputs(base, translation)

            from service.solvers.scheduling import ConfigurableBESS

            klass = type(
                f"BESS_CF_{uuid4().hex[:8]}",
                (ConfigurableBESS,),
                {
                    "_cycling_penalty": translation.cycling_penalty,
                    "_stored_energy_value": translation.stored_energy_value,
                    "_reserve_config": translation.reserve_config,
                    "model_name": "BESS",
                },
            )
            run_optimization_problem(
                klass,
                model_folder=str(_SCHEDULING_MODEL),
                input_folder=str(base / "input"),
                output_folder=str(base / "output"),
                log_level=logging.WARNING,
            )
            df_out_full = pd.read_csv(
                base / "output" / "timeseries_export.csv", parse_dates=["time"]
            )
            df_in_full = pd.read_csv(
                base / "input" / "timeseries_import.csv", parse_dates=["time"]
            )
            n_intervals = len(model_input.get("interval_start", []))
            df_out = (
                df_out_full.iloc[1 : n_intervals + 1].reset_index(drop=True)
                if len(df_out_full) > n_intervals
                else df_out_full
            )
            df_in = (
                df_in_full.iloc[1 : n_intervals + 1].reset_index(drop=True)
                if len(df_in_full) > n_intervals
                else df_in_full
            )
            t = pd.to_datetime(df_out["time"])
            dt_hours = (
                (t.iloc[1] - t.iloc[0]).total_seconds() / 3600.0
                if len(t) > 1
                else 1.0
            )
            metrics = _metrics_from_output_csv(df_out, df_in, dt_hours)
            metrics["cycling_penalty_eur"] = (
                translation.cycling_penalty * metrics["throughput_mwh"]
            )
            metrics["net_profit_eur"] = (
                metrics["arbitrage_revenue_eur"]
                - metrics["grid_fee_cost_eur"]
                - metrics["cycling_penalty_eur"]
            )
            return metrics
    except Exception as exc:
        _log.warning("Counterfactual scheduling solve failed: %s", exc, exc_info=True)
        return None


def _counterfactual_metrics_intraday(
    model_input: dict[str, Any],
) -> dict[str, Any] | None:
    """Re-solve intraday without any reserves and return the metric dict."""
    try:
        stripped = _strip_reserves_from_input(model_input)
        translation = translate_intraday(stripped)
        model_dir = _get_intraday_model_dir(translation.n_segments)
        with tempfile.TemporaryDirectory(prefix="bess_id_cf_") as tmpdir:
            base = Path(tmpdir)
            _prepare_io_dirs(base)
            _write_inputs(base, translation)

            from service.solvers.intraday import ConfigurableBESSIntraday

            klass = type(
                f"BESSIntraday_CF_{uuid4().hex[:8]}",
                (ConfigurableBESSIntraday,),
                {
                    "_cycling_penalty": translation.cycling_penalty,
                    "_transaction_cost": translation.transaction_cost,
                    "_stored_energy_value": translation.stored_energy_value,
                    "_reserve_config": translation.reserve_config,
                    "model_name": "BESSIntraday",
                },
            )
            run_optimization_problem(
                klass,
                model_folder=str(model_dir),
                input_folder=str(base / "input"),
                output_folder=str(base / "output"),
                log_level=logging.WARNING,
            )
            df_out_full = pd.read_csv(
                base / "output" / "timeseries_export.csv", parse_dates=["time"]
            )
            df_in_full = pd.read_csv(
                base / "input" / "timeseries_import.csv", parse_dates=["time"]
            )
            n_intervals = len(model_input.get("interval_start", []))
            df_out = (
                df_out_full.iloc[1 : n_intervals + 1].reset_index(drop=True)
                if len(df_out_full) > n_intervals
                else df_out_full
            )
            df_in = (
                df_in_full.iloc[1 : n_intervals + 1].reset_index(drop=True)
                if len(df_in_full) > n_intervals
                else df_in_full
            )
            t = pd.to_datetime(df_out["time"])
            dt_hours = (
                (t.iloc[1] - t.iloc[0]).total_seconds() / 3600.0
                if len(t) > 1
                else 0.25
            )
            # Intraday revenue is from the orderbook, not a single price.
            metrics = _metrics_from_output_csv(df_out, df_in, dt_hours)
            n = len(df_out)
            discharge_rev = 0.0
            charge_cost = 0.0
            for seg in range(1, translation.n_segments + 1):
                bid_col = f"discharge_power_bids[{seg}]"
                ask_col = f"charge_power_asks[{seg}]"
                bp = f"bid_prices[{seg}]"
                ap = f"ask_prices[{seg}]"
                if bid_col in df_out.columns and bp in df_in.columns:
                    discharge_rev += float(
                        (
                            df_out[bid_col].to_numpy(dtype=float)[:n]
                            * df_in[bp].to_numpy(dtype=float)[:n]
                        ).sum()
                    )
                if ask_col in df_out.columns and ap in df_in.columns:
                    charge_cost += float(
                        (
                            df_out[ask_col].to_numpy(dtype=float)[:n]
                            * df_in[ap].to_numpy(dtype=float)[:n]
                        ).sum()
                    )
            metrics["arbitrage_revenue_eur"] = (discharge_rev - charge_cost) * dt_hours
            metrics["cycling_penalty_eur"] = (
                translation.cycling_penalty * metrics["throughput_mwh"]
            )
            metrics["transaction_cost_eur"] = (
                translation.transaction_cost * metrics["throughput_mwh"]
            )
            metrics["net_profit_eur"] = (
                metrics["arbitrage_revenue_eur"]
                - metrics["grid_fee_cost_eur"]
                - metrics["cycling_penalty_eur"]
                - metrics["transaction_cost_eur"]
            )
            return metrics
    except Exception as exc:
        _log.warning("Counterfactual intraday solve failed: %s", exc, exc_info=True)
        return None
