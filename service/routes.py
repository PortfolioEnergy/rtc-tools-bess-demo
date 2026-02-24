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


@router.post("/v1/models/{model_name}/submit_sync")
def submit_sync(model_name: str, body: SubmitRequest) -> dict[str, Any]:
    """PE API-compatible endpoint.

    Resolves ``model_name`` to a local solver type, runs the solver,
    and returns the result wrapped in ``{"result": ...}``.
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

    _log.info("submit_sync: model_name=%s -> solver_type=%s", model_name, solver_type)

    try:
        result = run_solver(solver_type, body.model_input_data)
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

    return {"result": result}
