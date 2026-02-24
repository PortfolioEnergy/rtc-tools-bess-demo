"""Integration test: replay the DA scheduling debug dump through our service.

Uses the 11h00m00Z tick (bess_day_ahead, 24 hourly intervals) from the
poc-backtesting debug dumps as a real-world smoke test.

This test actually runs the RTC-Tools solver — it is NOT mocked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_DUMP_DIR = Path(
    r"C:\Code\poc-backtesting\debug_dumps"
    r"\2c64556c-c63d-48f2-8951-405aa543da45\2025-08-01"
)

_DA_INPUT = _DUMP_DIR / "11h00m00Z__optimiser_input__2e695898cf.json"
_DA_RESULT = _DUMP_DIR / "11h00m00Z__optimiser_result__0001f8d797.json"

pytestmark = pytest.mark.skipif(
    not _DA_INPUT.exists(),
    reason="Debug dump files not present",
)


@pytest.fixture()
def da_input() -> dict:
    return json.loads(_DA_INPUT.read_text(encoding="utf-8"))


@pytest.fixture()
def da_pe_result() -> dict:
    return json.loads(_DA_RESULT.read_text(encoding="utf-8"))


class TestDASchedulingFromDebugDump:
    """Replay the day-ahead scheduling dump through our service."""

    def test_solver_returns_200(self, client: TestClient, da_input: dict) -> None:
        resp = client.post(
            "/v1/models/bess_day_ahead/submit_sync",
            json={"model_input_data": da_input},
        )
        assert resp.status_code == 200, resp.text

    def test_response_has_required_members(
        self, client: TestClient, da_input: dict
    ) -> None:
        resp = client.post(
            "/v1/models/bess_day_ahead/submit_sync",
            json={"model_input_data": da_input},
        )
        result = resp.json()["result"]
        members = result["members"]["default"]

        assert "day_ahead_power_in" in members
        assert "day_ahead_power_out" in members
        assert "state_of_charge" in members

    def test_output_lengths_match_input_intervals(
        self, client: TestClient, da_input: dict
    ) -> None:
        n_intervals = len(da_input["interval_start"])
        resp = client.post(
            "/v1/models/bess_day_ahead/submit_sync",
            json={"model_input_data": da_input},
        )
        members = resp.json()["result"]["members"]["default"]

        assert len(members["day_ahead_power_in"]["values"]) == n_intervals
        assert len(members["day_ahead_power_out"]["values"]) == n_intervals
        assert len(members["state_of_charge"]["values"]) == n_intervals

    def test_info_documents_ignored_inputs(
        self, client: TestClient, da_input: dict
    ) -> None:
        resp = client.post(
            "/v1/models/bess_day_ahead/submit_sync",
            json={"model_input_data": da_input},
        )
        info = resp.json()["result"]["_info"]
        assert isinstance(info, list)
        assert len(info) > 0

        categories = {line.split(":")[0] for line in info}
        assert "solver" in categories

    def test_soc_within_bounds(self, client: TestClient, da_input: dict) -> None:
        """SoC should stay within [0, capacity]."""
        capacity = 20.0
        for p in da_input.get("parameters", []):
            if p["name"] == "battery_capacity":
                capacity = p["value"]

        resp = client.post(
            "/v1/models/bess_day_ahead/submit_sync",
            json={"model_input_data": da_input},
        )
        soc_values = resp.json()["result"]["members"]["default"]["state_of_charge"][
            "values"
        ]
        for v in soc_values:
            assert -0.01 <= v <= capacity + 0.01, f"SoC out of bounds: {v}"

    def test_power_within_bounds(self, client: TestClient, da_input: dict) -> None:
        """Charge/discharge should not exceed max_power."""
        max_power = 10.0
        for p in da_input.get("parameters", []):
            if p["name"] in ("max_charge_power", "max_discharge_power"):
                max_power = min(max_power, p["value"])

        resp = client.post(
            "/v1/models/bess_day_ahead/submit_sync",
            json={"model_input_data": da_input},
        )
        members = resp.json()["result"]["members"]["default"]
        for v in members["day_ahead_power_in"]["values"]:
            assert v <= max_power + 0.01, f"charge exceeds max: {v}"
        for v in members["day_ahead_power_out"]["values"]:
            assert v <= max_power + 0.01, f"discharge exceeds max: {v}"

    def test_pe_result_has_extra_keys_we_dont_produce(
        self, client: TestClient, da_input: dict, da_pe_result: dict
    ) -> None:
        """Document keys the PE API returns that we don't."""
        resp = client.post(
            "/v1/models/bess_day_ahead/submit_sync",
            json={"model_input_data": da_input},
        )
        our_keys = set(resp.json()["result"]["members"]["default"].keys())
        pe_keys = set(da_pe_result["members"]["default"].keys())

        missing = pe_keys - our_keys
        extra = our_keys - pe_keys

        # We expect these to be missing from our output
        expected_missing = {
            "pre_battery_power_out",
            "pre_battery_power_in",
            "pre_delta_power_in",
            "pre_delta_power_out",
            "delta_power_in",
            "delta_power_out",
            "interval_energy_segments[1]",
            "interval_energy_segments[2]",
            "battery_power_in",
            "battery_power_out",
            "imbalance_power_in",
            "imbalance_power_out",
            "cumulative_energy_loss",
            "cumulative_cycles",
            "day_ahead_power_in_deltas[1]",
            "day_ahead_power_out_deltas[1]",
        }

        # All missing keys should be in our expected list
        unexpected_missing = missing - expected_missing
        assert not unexpected_missing, f"Unexpected missing keys: {unexpected_missing}"

        # We should not produce extra keys beyond what PE returns
        # (except _info is at result level, not members level)
        assert not extra, f"Unexpected extra keys in our output: {extra}"
