"""Unit tests for service/translation/reasoning.py.

Verify that the deterministic reasoning-markdown builders produce well-formed
markdown: KPI/cycle/constraint tables, embedded charts, and stable output for
identical inputs.
"""

from __future__ import annotations

from typing import Any

from service.translation.reasoning import (
    generate_intraday_markdown,
    generate_scheduling_markdown,
)


def _metrics() -> dict[str, Any]:
    return {
        "horizon_intervals": 8,
        "interval_minutes": 15.0,
        "cycling_penalty_factor": 0.1,
        "transaction_cost": 0.05,
        "capacity_mwh": 20.0,
        "total_charged_mwh": 5.0,
        "total_discharged_mwh": 4.5,
        "throughput_mwh": 9.5,
        "equivalent_full_cycles": 0.2375,
        "total_revenue_eur": 120.0,
        "total_cycling_penalty_eur": 9.5,
        "total_transaction_cost_eur": 4.75,
        "total_grid_fee_eur": 0.0,
        "net_profit_eur": 105.75,
        "n_charge_intervals": 4,
        "n_discharge_intervals": 4,
        "initial_soc_mwh": 10.0,
        "final_soc_mwh": 10.5,
        "solver_status": "Optimal",
        "objective_value": -105.75,
        "solver_wall_time": 0.42,
        "n_nlp_variables": 120,
        "committed_charged_mwh": 0.0,
        "committed_discharged_mwh": 2.0,
        "committed_net_mwh": 2.0,
        "incremental_charged_mwh": 5.0,
        "incremental_discharged_mwh": 2.5,
        "n_committed_intervals": 4,
        "peak_committed_charge_mw": 0.0,
        "peak_committed_discharge_mw": 2.0,
        "committed_share_of_throughput": 0.21,
        "committed_soc_trough_mwh": 8.0,
        "committed_soc_trough_interval": 4,
        "forced_charge_mwh": 0.0,
    }


def _forced_loss_metrics() -> dict[str, Any]:
    """Metrics for a horizon whose loss is forced by a committed obligation."""
    m = _metrics()
    m.update(
        {
            "net_profit_eur": -50.0,
            "committed_discharged_mwh": 27.0,
            "total_discharged_mwh": 28.0,
            "incremental_discharged_mwh": 0.0,
            "forced_charge_mwh": 5.0,
            "committed_soc_trough_mwh": -5.0,
            "committed_soc_trough_interval": 40,
        }
    )
    return m


def _cycle_rows() -> list[dict[str, Any]]:
    return [
        {
            "rank": 1,
            "kind": "cycle",
            "charge_label": "#0–#1",
            "discharge_label": "#4–#5",
            "charge_eur": 80.0,
            "discharge_eur": 140.0,
            "cycling_eur": 6.0,
            "transaction_eur": 3.0,
            "grid_fee_eur": 0.0,
            "net_eur": 51.0,
            "cumulative_eur": 51.0,
            "equiv_cycles": 0.15,
        },
        {
            "rank": 2,
            "kind": "cycle",
            "charge_label": "#2–#3",
            "discharge_label": "#6–#7",
            "charge_eur": 40.0,
            "discharge_eur": 60.0,
            "cycling_eur": 3.5,
            "transaction_eur": 1.75,
            "grid_fee_eur": 0.0,
            "net_eur": 14.75,
            "cumulative_eur": 65.75,
            "equiv_cycles": 0.0875,
        },
    ]


def _constraint_rows() -> list[dict[str, Any]]:
    return [
        {"constraint": "SoC at full capacity", "count": 0, "intervals": 8, "pct": 0.0},
        {"constraint": "SoC fully drained", "count": 1, "intervals": 8, "pct": 12.5},
    ]


def test_intraday_markdown_has_core_sections() -> None:
    md = generate_intraday_markdown(
        metrics=_metrics(),
        cycle_rows=_cycle_rows(),
        constraint_rows=_constraint_rows(),
        orderbook_rows=[
            {
                "level": 1,
                "charge_fill_pct": 50.0,
                "discharge_fill_pct": 45.0,
                "charge_mw_sum": 20.0,
                "discharge_mw_sum": 18.0,
            }
        ],
        images={"revenue_decomposition": "data:image/png;base64,AAAA"},
        info=["solver: HiGHS MILP", "image:revenue_decomposition: data:image/png;base64,AAAA"],
        model_input={"interval_start": ["2025-08-01T00:00:00Z"]},
    )
    assert md.startswith("# Intraday Trading")
    for section in (
        "## Results",
        "## Committed Position",
        "## Per-Cycle Merit Order",
        "## Energy Balance",
        "## Constraint Binding",
        "## Orderbook Depth Utilisation",
        "## Solver Statistics",
    ):
        assert section in md
    # embedded chart
    assert "![Revenue Decomposition](data:image/png;base64,AAAA)" in md
    # merit-order table marks the net column in bold
    assert "**51.00**" in md
    # solver note included, image _info line filtered out
    assert "- solver: HiGHS MILP" in md
    assert "image:revenue_decomposition" not in md.split("## Solver Notes")[-1]


def test_scheduling_markdown_has_full_day_schedule() -> None:
    md = generate_scheduling_markdown(
        metrics=_metrics(),
        cycle_rows=_cycle_rows(),
        constraint_rows=_constraint_rows(),
        schedule_rows=[
            {"interval": 0, "price": 30.0, "action": "Charge", "mw": 8.0, "soc": 14.0},
            {"interval": 1, "price": 90.0, "action": "Discharge", "mw": 8.0, "soc": 6.0},
            {"interval": 2, "price": 50.0, "action": "Idle", "mw": 0.0, "soc": 6.0},
        ],
        images={},
        info=[],
        model_input={"interval_start": ["2025-08-01T00:00:00Z"]},
    )
    assert md.startswith("# Day-Ahead Scheduling")
    assert "## Full-Day Schedule" in md
    assert "Discharge" in md
    # missing chart degrades to a placeholder, not a crash
    assert "*Chart `revenue_decomposition` unavailable for this run.*" in md


def test_markdown_is_deterministic() -> None:
    kwargs: dict[str, Any] = dict(
        metrics=_metrics(),
        cycle_rows=_cycle_rows(),
        constraint_rows=_constraint_rows(),
        orderbook_rows=[],
        images={},
        info=[],
        model_input={},
    )
    assert generate_intraday_markdown(**kwargs) == generate_intraday_markdown(**kwargs)


def test_no_cycles_detected_message() -> None:
    md = generate_intraday_markdown(
        metrics=_metrics(),
        cycle_rows=[],
        constraint_rows=_constraint_rows(),
        orderbook_rows=[],
        images={},
        info=[],
        model_input={},
    )
    assert "No charge/discharge cycles were detected" in md


def test_committed_position_section_shows_obligation() -> None:
    md = generate_intraday_markdown(
        metrics=_forced_loss_metrics(),
        cycle_rows=_cycle_rows(),
        constraint_rows=_constraint_rows(),
        orderbook_rows=[],
        images={"committed_position": "data:image/png;base64,AAAA"},
        info=[],
        model_input={},
    )
    assert "## Committed Position" in md
    assert "Committed discharge (export obligation)" in md
    # the committed-position chart is embedded
    assert "![Committed Position](data:image/png;base64,AAAA)" in md
    # feasibility line fires because forced_charge_mwh > 0
    assert "**Feasibility:**" in md


def test_why_loss_note_fires_for_forced_loss() -> None:
    md = generate_intraday_markdown(
        metrics=_forced_loss_metrics(),
        cycle_rows=_cycle_rows(),
        constraint_rows=_constraint_rows(),
        orderbook_rows=[],
        images={},
        info=[],
        model_input={},
    )
    assert "Why this horizon shows a loss" in md


def test_why_loss_note_absent_when_profitable() -> None:
    # base _metrics() is a profitable run with no forced charging
    md = generate_intraday_markdown(
        metrics=_metrics(),
        cycle_rows=_cycle_rows(),
        constraint_rows=_constraint_rows(),
        orderbook_rows=[],
        images={},
        info=[],
        model_input={},
    )
    assert "Why this horizon shows a loss" not in md


def test_energy_balance_splits_committed_and_incremental() -> None:
    md = generate_intraday_markdown(
        metrics=_metrics(),
        cycle_rows=_cycle_rows(),
        constraint_rows=_constraint_rows(),
        orderbook_rows=[],
        images={},
        info=[],
        model_input={},
    )
    assert "committed discharge" in md
    assert "incremental discharge" in md
