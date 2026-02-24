"""End-to-end test: start bess-service as a live HTTP server and replay debug dumps.

This validates the full stack: uvicorn, FastAPI routing, solver, translation — all
running as a real HTTP server on port 8010.

Usage:
    uv run python scripts/e2e_test.py

Requires the debug dump directory from poc-backtesting to be present.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

SERVICE_PORT = 8010
SERVICE_URL = f"http://localhost:{SERVICE_PORT}"
STARTUP_TIMEOUT_S = 30
REQUEST_TIMEOUT_S = 60

DUMP_DIR = Path(
    r"C:\Code\poc-backtesting\debug_dumps"
    r"\2c64556c-c63d-48f2-8951-405aa543da45\2025-08-01"
)

MODEL_MAP = {
    "pe/v1/ic_trading": "bess_rolling",
    "pe/v1/day_ahead": "bess_day_ahead",
    "pe/v1/da_setpoints_from_positions": "bess_day_ahead",
}


def _wait_for_service(client: httpx.Client) -> bool:
    """Poll the health endpoint until the service is ready."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            resp = client.get(f"{SERVICE_URL}/health", timeout=2)
            if resp.status_code == 200:
                return True
        except httpx.ConnectError:
            pass
        time.sleep(0.5)
    return False


def _discover_ticks() -> list[str]:
    """Return sorted list of tick prefixes (e.g. '00h00m00Z')."""
    ticks: set[str] = set()
    for f in DUMP_DIR.glob("*__optimiser_input__*"):
        ticks.add(f.name[:9])
    return sorted(ticks)


def _load_tick_files(tick: str) -> dict[str, Path]:
    """Load all dump files for a tick."""
    files: dict[str, Path] = {}
    for f in DUMP_DIR.glob(f"{tick}__*"):
        parts = f.stem.split("__")
        if len(parts) >= 2:
            files[parts[1]] = f
    return files


def _validate_response(
    tick: str,
    model_name: str,
    template_ref: str,
    resp: httpx.Response,
    pe_result: dict[str, Any],
    optimiser_input: dict[str, Any],
) -> list[str]:
    """Validate one tick's response. Returns list of failure messages (empty = pass)."""
    failures: list[str] = []

    # HTTP status
    if resp.status_code != 200:
        failures.append(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return failures

    body = resp.json()

    # Response shape
    if "result" not in body:
        failures.append("Missing 'result' key in response")
        return failures

    result = body["result"]
    if "members" not in result:
        failures.append("Missing 'members' in result")
    if "_info" not in result:
        failures.append("Missing '_info' in result")

    members = result.get("members", {})
    if "default" not in members:
        failures.append("Missing 'default' in members")
        return failures

    default = members["default"]

    # For scheduling/intraday, check key output variables exist
    # DA uses day_ahead_power_*, IC uses battery_power_*
    if template_ref in ("pe/v1/day_ahead", "pe/v1/ic_trading"):
        if template_ref == "pe/v1/day_ahead":
            expected_keys = {"state_of_charge", "day_ahead_power_out"}
        else:
            expected_keys = {"state_of_charge", "battery_power_out"}
        missing = expected_keys - set(default.keys())
        if missing:
            failures.append(f"Missing expected output keys: {missing}")

        # Validate SoC within bounds
        soc_values = default.get("state_of_charge", {}).get("values", [])
        if soc_values:
            # Get capacity from input params
            params = optimiser_input.get("parameters", {})
            capacity = None
            for p in params if isinstance(params, list) else [params]:
                if isinstance(p, dict) and p.get("name") == "battery_capacity":
                    capacity = p["value"]
                    break
            if capacity is None:
                # Try timeseries parameters
                for ts in optimiser_input.get("timeseries", []):
                    if ts.get("name") == "battery_capacity":
                        vals = ts.get("values", [])
                        if vals:
                            capacity = vals[0]
                            break

            if capacity is not None:
                min_soc = min(soc_values)
                max_soc = max(soc_values)
                if min_soc < -0.01:
                    failures.append(f"SoC below 0: {min_soc:.4f}")
                if max_soc > capacity + 0.01:
                    failures.append(f"SoC above capacity ({capacity}): {max_soc:.4f}")

        # Value arrays should match interval count
        n_intervals = len(optimiser_input.get("interval_start", []))
        for key, var in default.items():
            n_values = len(var.get("values", []))
            if n_values != n_intervals and n_values > 0:
                failures.append(
                    f"Output '{key}' has {n_values} values, "
                    f"expected {n_intervals} intervals"
                )

    elif template_ref == "pe/v1/da_setpoints_from_positions":
        if "setpoints" not in default:
            failures.append("Missing 'setpoints' in da_setpoints response")

    # Check _info is a list of strings
    info = result.get("_info", [])
    if not isinstance(info, list):
        failures.append(f"_info is not a list: {type(info)}")
    elif not all(isinstance(s, str) for s in info):
        failures.append("_info contains non-string entries")

    return failures


def main() -> int:
    if not DUMP_DIR.exists():
        print(f"SKIP: dump directory not found: {DUMP_DIR}")
        return 0

    ticks = _discover_ticks()
    if not ticks:
        print("SKIP: no debug dump ticks found")
        return 0

    print(f"Found {len(ticks)} ticks in {DUMP_DIR}")
    print(f"Starting bess-service on port {SERVICE_PORT}...")

    # Start the service as a subprocess
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "service.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(SERVICE_PORT),
        ],
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        client = httpx.Client()

        if not _wait_for_service(client):
            print("FAIL: service did not start within timeout")
            # Print captured stderr for debugging
            proc.terminate()
            _, stderr = proc.communicate(timeout=5)
            print(f"Service stderr:\n{stderr.decode(errors='replace')[:2000]}")
            return 1

        print("Service is healthy\n")

        total = 0
        passed = 0
        failed = 0

        for tick in ticks:
            files = _load_tick_files(tick)

            if "optimiser_input" not in files:
                print(f"  {tick}: SKIP (no optimiser_input)")
                continue

            # Load build_payload for model info
            if "build_payload" in files:
                build_payload = json.loads(
                    files["build_payload"].read_text(encoding="utf-8")
                )
                cfg = build_payload.get("optimiser_config", {})
                template_ref = cfg.get("template_ref", "")
                model_name = cfg.get(
                    "model_name", MODEL_MAP.get(template_ref, "unknown")
                )
            else:
                template_ref = ""
                model_name = "unknown"

            optimiser_input = json.loads(
                files["optimiser_input"].read_text(encoding="utf-8")
            )

            pe_result = {}
            if "optimiser_result" in files:
                pe_result = json.loads(
                    files["optimiser_result"].read_text(encoding="utf-8")
                )

            total += 1

            n_intervals = len(optimiser_input.get("interval_start", []))
            print(
                f"  {tick}  model={model_name}  template={template_ref}  "
                f"intervals={n_intervals} ... ",
                end="",
            )

            # Send request to live service
            try:
                resp = client.post(
                    f"{SERVICE_URL}/v1/models/{model_name}/submit_sync",
                    json={"model_input_data": optimiser_input},
                    timeout=REQUEST_TIMEOUT_S,
                )
            except httpx.TimeoutException:
                print("FAIL (timeout)")
                failed += 1
                continue

            failures = _validate_response(
                tick, model_name, template_ref, resp, pe_result, optimiser_input
            )

            if failures:
                print("FAIL")
                for f in failures:
                    print(f"    - {f}")
                failed += 1
            else:
                info_count = len(resp.json().get("result", {}).get("_info", []))
                print(f"PASS  ({info_count} _info messages)")
                passed += 1

        print(f"\n{'=' * 60}")
        print(f"RESULTS: {passed}/{total} passed, {failed} failed")

        if failed > 0:
            return 1
        return 0

    finally:
        print("\nShutting down service...")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        print("Done.")


if __name__ == "__main__":
    sys.exit(main())
