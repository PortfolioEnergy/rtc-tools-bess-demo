"""Orchestrates RTC-Tools solver execution in isolated temp directories.

Each request gets its own temp directory with ``input/``, ``output/``,
and ``model/`` subdirectories.  The solver class is dynamically subclassed
per request to avoid shared mutable state between concurrent calls.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

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

_log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEDULING_MODEL = _REPO_ROOT / "scheduling" / "model"
_INTRADAY_MODEL = _REPO_ROOT / "continuous_intraday" / "model"
_INTRADAY_MO = _INTRADAY_MODEL / "BESSIntraday.mo"


def run_solver(solver_type: str, model_input: dict[str, Any]) -> dict[str, Any]:
    """Run the appropriate solver and return a PE-API-shaped result dict.

    The returned dict already contains the ``_info`` list.
    """
    if solver_type == "da_setpoints":
        return translate_setpoints(model_input)

    if solver_type == "scheduling":
        return _run_scheduling(model_input)

    if solver_type == "intraday":
        return _run_intraday(model_input)

    raise ValueError(f"Unknown solver_type: {solver_type}")


# ── scheduling ───────────────────────────────────────────────────────


def _run_scheduling(model_input: dict[str, Any]) -> dict[str, Any]:
    translation = translate_scheduling(model_input)

    with tempfile.TemporaryDirectory(prefix="bess_sched_") as tmpdir:
        base = Path(tmpdir)
        _prepare_dirs(base)

        # Copy Modelica model
        shutil.copytree(_SCHEDULING_MODEL, base / "model", dirs_exist_ok=True)

        # Write input files
        _write_inputs(base, translation)

        # Build a per-request solver class to avoid shared state
        from service.solvers.scheduling import ConfigurableBESS

        klass = type(
            f"BESS_{uuid4().hex[:8]}",
            (ConfigurableBESS,),
            {
                "_cycling_penalty": translation.cycling_penalty,
                "model_name": "BESS",
            },
        )

        _log.info(
            "Running scheduling solver (cycling_penalty=%.4f)",
            translation.cycling_penalty,
        )
        run_optimization_problem(
            klass,
            base_folder=str(base),
            log_level=logging.WARNING,
        )

        return translate_scheduling_result(
            base / "output", model_input, translation.info
        )


# ── intraday ─────────────────────────────────────────────────────────


def _run_intraday(model_input: dict[str, Any]) -> dict[str, Any]:
    translation = translate_intraday(model_input)

    with tempfile.TemporaryDirectory(prefix="bess_id_") as tmpdir:
        base = Path(tmpdir)
        _prepare_dirs(base)

        # Write a modified .mo file with the correct n_orderbook_entries
        _write_intraday_model(base / "model", translation.n_segments)

        # Write input files
        _write_inputs(base, translation)

        # Build a per-request solver class
        from service.solvers.intraday import ConfigurableBESSIntraday

        klass = type(
            f"BESSIntraday_{uuid4().hex[:8]}",
            (ConfigurableBESSIntraday,),
            {
                "_cycling_penalty": translation.cycling_penalty,
                "_transaction_cost": translation.transaction_cost,
                "model_name": "BESSIntraday",
            },
        )

        _log.info(
            "Running intraday solver (n_segments=%d, cycling_penalty=%.4f, "
            "transaction_cost=%.4f)",
            translation.n_segments,
            translation.cycling_penalty,
            translation.transaction_cost,
        )
        run_optimization_problem(
            klass,
            base_folder=str(base),
            log_level=logging.WARNING,
        )

        return translate_intraday_result(
            base / "output",
            model_input,
            translation.n_segments,
            translation.info,
        )


# ── helpers ──────────────────────────────────────────────────────────


def _prepare_dirs(base: Path) -> None:
    (base / "input").mkdir(exist_ok=True)
    (base / "output").mkdir(exist_ok=True)
    (base / "model").mkdir(exist_ok=True)


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


def _write_intraday_model(model_dir: Path, n_segments: int) -> None:
    """Write ``BESSIntraday.mo`` with the correct ``n_orderbook_entries``."""
    mo_content = _INTRADAY_MO.read_text(encoding="utf-8")

    # Replace the default n_orderbook_entries value
    mo_content = re.sub(
        r"parameter\s+Integer\s+n_orderbook_entries\s*=\s*\d+",
        f"parameter Integer n_orderbook_entries = {n_segments}",
        mo_content,
    )

    (model_dir / "BESSIntraday.mo").write_text(mo_content, encoding="utf-8")
