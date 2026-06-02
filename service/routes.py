"""PE API-compatible HTTP endpoints."""

from __future__ import annotations

import logging
import traceback
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from service.model_registry import SUPPORTED_KEYWORDS, resolve_solver_type
from service.solver_runner import run_solver

_log = logging.getLogger(__name__)

router = APIRouter()


class SubmitRequest(BaseModel):
    model_input_data: dict[str, Any]
    include_diagnostics: bool = False


@router.post("/v1/models/{model_name}/submit_sync")
def submit_sync(model_name: str, body: SubmitRequest) -> dict[str, Any]:
    """PE API-compatible endpoint.

    Resolves ``model_name`` to a local solver type, runs the solver, and
    returns a body shaped as ``{"result": {...}}`` — or, when diagnostics are
    enabled, ``{"result": {...}, "reasoning_markdown": "..."}``.

    ``result`` always contains ``members`` and ``_info``, preserving the PE
    API contract on the ``result`` shape.

    Set ``include_diagnostics: true`` in the request body to generate
    explainer output: base64-encoded charts are embedded in ``result._info``
    as ``"image:<name>: <data URI>"`` entries, and a deterministic
    ``reasoning_markdown`` document (KPI/cycle/constraint tables plus the
    embedded charts) is returned as a top-level key. This adds roughly
    100–300 ms of post-processing overhead.
    """
    solver_type = resolve_solver_type(model_name)
    if solver_type is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unsupported model '{model_name}'. "
                f"Supported keywords: {SUPPORTED_KEYWORDS}"
            ),
        )

    _log.info(
        "submit_sync: model_name=%s -> solver_type=%s include_diagnostics=%s",
        model_name,
        solver_type,
        body.include_diagnostics,
    )

    try:
        result = run_solver(
            solver_type,
            body.model_input_data,
            include_diagnostics=body.include_diagnostics,
        )
    except ValueError as exc:
        # Translation-layer validation errors (e.g. open aFRR market without
        # the required activation_fraction timeseries) — surface as 422 so
        # callers can distinguish bad input from genuine solver failure.
        _log.warning(
            "Validation failed for model_name=%s: %s", model_name, exc
        )
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Invalid model_input_data",
                "model_name": model_name,
                "solver_type": solver_type,
                "message": str(exc),
            },
        ) from exc
    except Exception as exc:
        _log.exception("Solver failed for model_name=%s", model_name)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Solver execution failed",
                "model_name": model_name,
                "solver_type": solver_type,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        ) from exc

    return result
