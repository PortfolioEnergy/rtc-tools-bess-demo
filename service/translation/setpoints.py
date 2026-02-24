"""Shortcut handler for DA-setpoints-from-positions.

Instead of running the solver, this extracts ``market_position`` from the
PE API input and returns it directly as ``setpoints``.
"""

from __future__ import annotations

from typing import Any


def _find_timeseries(model_input: dict[str, Any], name: str) -> dict[str, Any] | None:
    for ts in model_input.get("timeseries", []):
        if ts.get("name") == name:
            return ts
    return None


def translate_setpoints(model_input: dict[str, Any]) -> dict[str, Any]:
    """Return ``market_position`` values as ``setpoints``.

    Returns the ``result`` dict (goes inside ``{"result": ...}``).
    """
    info: list[str] = [
        "shortcut: 'da_setpoints_from_positions' — returning market_position "
        "directly as setpoints without running solver; does not account for "
        "SoC constraints or efficiency losses",
    ]

    market_pos_ts = _find_timeseries(model_input, "market_position")
    if market_pos_ts and market_pos_ts.get("values"):
        values = [float(v) for v in market_pos_ts["values"]]
    else:
        values = []
        info.append(
            "ignored_input: no 'market_position' timeseries found in request "
            "— returning empty setpoints"
        )

    members: dict[str, Any] = {
        "setpoints": {"values": values},
    }

    return {"members": {"default": members}, "_info": info}
