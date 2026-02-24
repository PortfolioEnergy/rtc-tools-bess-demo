"""E2E test client: sends debug dump requests to a running bess-service on port 8010.

Usage:
    uv run python scripts/e2e_client.py

Expects the service to already be running on http://localhost:8010.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

SERVICE_URL = "http://localhost:8010"
REQUEST_TIMEOUT_S = 120

DUMP_DIR = Path(
    r"C:\Code\poc-backtesting\debug_dumps"
    r"\2c64556c-c63d-48f2-8951-405aa543da45\2025-08-01"
)

MODEL_MAP = {
    "pe/v1/ic_trading": "bess_rolling",
    "pe/v1/day_ahead": "bess_day_ahead",
    "pe/v1/da_setpoints_from_positions": "bess_day_ahead",
}


def main() -> int:
    if not DUMP_DIR.exists():
        print(f"SKIP: dump directory not found: {DUMP_DIR}")
        return 0

    # Discover ticks
    ticks: set[str] = set()
    for f in DUMP_DIR.glob("*__optimiser_input__*"):
        ticks.add(f.name[:9])
    ticks_sorted = sorted(ticks)

    if not ticks_sorted:
        print("SKIP: no debug dump ticks found")
        return 0

    # Verify service is reachable
    client = httpx.Client()
    try:
        resp = client.get(f"{SERVICE_URL}/health", timeout=5)
        if resp.status_code != 200:
            print(f"FAIL: service health check returned {resp.status_code}")
            return 1
    except httpx.ConnectError:
        print("FAIL: cannot connect to service on port 8010")
        return 1

    print(f"Service healthy. Found {len(ticks_sorted)} ticks.\n")

    total = 0
    passed = 0
    failed = 0

    for tick in ticks_sorted:
        files: dict[str, Path] = {}
        for f in DUMP_DIR.glob(f"{tick}__*"):
            parts = f.stem.split("__")
            if len(parts) >= 2:
                files[parts[1]] = f

        if "optimiser_input" not in files:
            continue

        # Get model info from build_payload
        if "build_payload" in files:
            bp = json.loads(files["build_payload"].read_text(encoding="utf-8"))
            cfg = bp.get("optimiser_config", {})
            template_ref = cfg.get("template_ref", "")
            model_name = cfg.get("model_name", MODEL_MAP.get(template_ref, "unknown"))
        else:
            template_ref = ""
            model_name = "unknown"

        optimiser_input = json.loads(
            files["optimiser_input"].read_text(encoding="utf-8")
        )
        n_intervals = len(optimiser_input.get("interval_start", []))

        total += 1
        print(
            f"  {tick}  model={model_name}  template={template_ref}  "
            f"intervals={n_intervals} ... ",
            end="",
            flush=True,
        )

        # Send request
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
        except Exception as exc:
            print(f"FAIL ({exc})")
            failed += 1
            continue

        # Validate
        errors: list[str] = []

        if resp.status_code != 200:
            errors.append(f"HTTP {resp.status_code}")
            try:
                detail = resp.json().get("detail", "")[:200]
                errors.append(f"  detail: {detail}")
            except Exception:
                pass
        else:
            body = resp.json()
            result = body.get("result", {})

            if "members" not in result:
                errors.append("Missing 'members'")
            if "_info" not in result:
                errors.append("Missing '_info'")

            default = result.get("members", {}).get("default", {})

            if template_ref in ("pe/v1/day_ahead", "pe/v1/ic_trading"):
                # Check key outputs exist (DA uses day_ahead_power_*, IC uses battery_power_*)
                if template_ref == "pe/v1/day_ahead":
                    expected_keys = ("state_of_charge", "day_ahead_power_out")
                else:
                    expected_keys = ("state_of_charge", "battery_power_out")
                for key in expected_keys:
                    if key not in default:
                        errors.append(f"Missing output key '{key}'")

                # SoC bounds check
                soc_vals = default.get("state_of_charge", {}).get("values", [])
                if soc_vals:
                    if min(soc_vals) < -0.01:
                        errors.append(f"SoC below 0: {min(soc_vals):.4f}")

                # Value count check
                for key, var in default.items():
                    n_vals = len(var.get("values", []))
                    if n_vals != n_intervals and n_vals > 0:
                        errors.append(
                            f"'{key}': {n_vals} values != {n_intervals} intervals"
                        )

        if errors:
            print("FAIL")
            for e in errors:
                print(f"    - {e}")
            failed += 1
        else:
            info_count = len(resp.json().get("result", {}).get("_info", []))
            print(f"PASS  ({info_count} _info msgs)")
            passed += 1

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed}/{total} passed, {failed} failed")
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
