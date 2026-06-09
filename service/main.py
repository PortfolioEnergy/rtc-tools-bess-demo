"""RTC-Tools BESS Optimizer Service — PE API-compatible wrapper."""

from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI

from service.otel_setup import setup_otel
from service.routes import router

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

# pymoca uses its own top-level logger ("pymoca") that is independent of the
# "rtctools" logger.  It inherits from root (INFO by default), which produces
# ~15 noisy compilation lines per model build even when the pymoca disk cache
# is warm.  Limit it to warnings only.
logging.getLogger("pymoca").setLevel(logging.WARNING)

# rtctools adds its own StreamHandler to the "rtctools" logger on the first
# run_optimization_problem() call.  Without propagate=False that handler would
# fire in addition to the root handler configured by basicConfig above,
# causing every rtctools WARNING/ERROR to appear twice in the output.
logging.getLogger("rtctools").propagate = False

_log = logging.getLogger(__name__)

app = FastAPI(
    title="RTC-Tools BESS Optimizer Service",
    version="0.1.0",
    description=(
        "PE API-compatible wrapper around the RTC-Tools BESS demo optimizer. "
        "Supports day-ahead scheduling, intraday continuous trading, and "
        "DA-setpoints-from-positions. Returns an _info field documenting "
        "all ignored inputs, approximations, and shortcuts taken."
    ),
)

app.include_router(router)
setup_otel(app)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    try:
        import rtctools  # noqa: F401

        return {"status": "ready"}
    except ImportError:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "detail": "rtctools not installed"},
        )


def start() -> None:
    """Entry point for ``bess-service`` console script."""
    _log.info("Starting RTC-Tools BESS Optimizer Service on port 8010")
    uvicorn.run("service.main:app", host="0.0.0.0", port=8010, access_log=False)
