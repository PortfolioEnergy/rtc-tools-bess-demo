"""Shared fixtures for BESS service tests."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from service.main import app


@pytest.fixture()
def client() -> TestClient:
    """FastAPI test client (no real HTTP, no real server)."""
    return TestClient(app)


def _make_timestamps(n: int) -> tuple[list[str], list[str]]:
    """Generate ``n`` hourly interval_start / interval_end pairs."""
    from datetime import datetime, timedelta, timezone

    base = datetime(2025, 8, 1, tzinfo=timezone.utc)
    starts = [
        (base + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ") for h in range(n)
    ]
    ends = [
        (base + timedelta(hours=h + 1)).strftime("%Y-%m-%dT%H:%M:%SZ") for h in range(n)
    ]
    return starts, ends


@pytest.fixture()
def scheduling_input() -> dict[str, Any]:
    """Minimal valid day-ahead scheduling ``model_input_data``."""
    starts, ends = _make_timestamps(24)
    return {
        "interval_start": starts,
        "interval_end": ends,
        "timeseries": [
            {
                "name": "day_ahead_price",
                "values": [
                    30.0,
                    28.0,
                    25.0,
                    23.0,
                    22.0,
                    24.0,
                    35.0,
                    50.0,
                    65.0,
                    70.0,
                    60.0,
                    55.0,
                    50.0,
                    48.0,
                    45.0,
                    42.0,
                    40.0,
                    55.0,
                    80.0,
                    90.0,
                    75.0,
                    60.0,
                    45.0,
                    35.0,
                ],
            },
            {"name": "state_of_charge", "values": [10.0]},
        ],
        "parameters": [
            {"name": "battery_capacity", "value": 20.0},
            {"name": "max_charge_power", "value": 10.0},
            {"name": "max_discharge_power", "value": 10.0},
            {"name": "efficiency_in", "value": 0.95},
            {"name": "efficiency_out", "value": 0.95},
            {"name": "cost_per_cycle", "value": 2.0},
        ],
        "markets": [],
    }


def _make_qh_timestamps(n: int) -> tuple[list[str], list[str]]:
    """Generate ``n`` quarter-hourly interval pairs starting at 00:00."""
    starts: list[str] = []
    ends: list[str] = []
    for i in range(n):
        h, m = divmod(i * 15, 60)
        starts.append(f"2025-08-01T{h:02d}:{m:02d}:00Z")
        h2, m2 = divmod((i + 1) * 15, 60)
        ends.append(f"2025-08-01T{h2:02d}:{m2:02d}:00Z")
    return starts, ends


@pytest.fixture()
def intraday_input() -> dict[str, Any]:
    """Minimal valid intraday ``model_input_data`` with 1 orderbook segment."""
    n = 8
    starts, ends = _make_qh_timestamps(n)
    return {
        "interval_start": starts,
        "interval_end": ends,
        "timeseries": [
            {"name": "market_position", "values": [5.0] * n},
            {"name": "state_of_charge", "values": [10.0]},
            {"name": "orderbook[1]_price_in", "values": [50.0] * n},
            {"name": "orderbook[1]_price_out", "values": [40.0] * n},
            {"name": "orderbook[1]_max_power_in", "values": [10.0] * n},
            {"name": "orderbook[1]_max_power_out", "values": [10.0] * n},
        ],
        "parameters": [
            {"name": "battery_capacity", "value": 20.0},
            {"name": "max_charge_power", "value": 10.0},
            {"name": "max_discharge_power", "value": 10.0},
            {"name": "efficiency_in", "value": 0.95},
            {"name": "efficiency_out", "value": 0.95},
            {"name": "cost_per_cycle", "value": 2.0},
        ],
        "markets": [
            {"name": "orderbook", "n_orderbook_segments": 1},
        ],
    }


@pytest.fixture()
def setpoints_input() -> dict[str, Any]:
    """Minimal valid setpoints ``model_input_data``."""
    starts, ends = _make_qh_timestamps(4)
    return {
        "interval_start": starts,
        "interval_end": ends,
        "timeseries": [
            {"name": "market_position", "values": [5.0, -3.0, 0.0, 7.5]},
        ],
        "parameters": [],
        "markets": [],
    }
