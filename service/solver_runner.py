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
"""

from __future__ import annotations

import logging
import re
import tempfile
import threading
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

        result = translate_scheduling_result(
            base / "output",
            model_input,
            translation.info,
            prob=prob if include_diagnostics else None,
        )
        return {"result": result}


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

        result = translate_intraday_result(
            base / "output",
            model_input,
            translation.n_segments,
            translation.info,
            prob=prob if include_diagnostics else None,
        )
        return {"result": result}


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
