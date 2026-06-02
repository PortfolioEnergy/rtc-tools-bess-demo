"""Deterministic reasoning-markdown generation for the BESS service.

Produces a human-readable markdown document explaining *why* the optimiser
made each decision.  The document interleaves:

- KPI / energy / constraint / orderbook / solver **tables** — structured
  numbers are far cheaper to transfer and far easier to query than a
  rendered chart, so anything that is essentially tabular is emitted here;
- a **per-cycle merit order** table — one row per detected cycle, ranked by
  net margin, making the diminishing returns of successive cycles explicit;
- the genuinely visual **charts** embedded inline as ``data:`` URIs, each
  preceded by prose explaining what it shows.

The markdown string is surfaced as the top-level ``reasoning_markdown`` key
of the optimiser response, which the poc-backtesting manifest stores per run.

Output is deterministic: identical inputs always produce identical markdown.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


# ── formatting helpers ───────────────────────────────────────────────────────


def _md_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    """Render a markdown table; returns a list of lines."""
    out = ["| " + " | ".join(headers) + " |"]
    out.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return out


def _eur(value: Any) -> str:
    try:
        return f"{float(value):.2f} \u20ac"
    except (TypeError, ValueError):
        return "n/a"


def _num(value: Any, decimals: int = 2) -> str:
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return "n/a"


def _delivery_line(model_input: dict[str, Any], metrics: dict[str, Any]) -> str:
    """Build the ``Delivery / Intervals / Horizon`` subtitle line."""
    starts = (model_input or {}).get("interval_start", [])
    delivery = ""
    if starts:
        try:
            delivery = datetime.fromisoformat(
                str(starts[0]).replace("Z", "+00:00")
            ).strftime("%Y-%m-%d %H:%M UTC")
        except (ValueError, TypeError):
            # Unparseable timestamp — embed it defensively: strip markdown
            # control characters and cap the length.
            delivery = str(starts[0]).replace("|", " ").replace("\n", " ")[:40]
    n = metrics.get("horizon_intervals", 0)
    mins = metrics.get("interval_minutes", 0.0)
    try:
        horizon_h = n * float(mins) / 60.0
    except (TypeError, ValueError):
        horizon_h = 0.0
    parts = []
    if delivery:
        parts.append(f"**Start:** {delivery}")
    parts.append(f"**Intervals:** {n} \u00d7 {int(mins)} min")
    parts.append(f"**Horizon:** {horizon_h:.1f} h")
    return " \u00b7 ".join(parts)


def _notes_section(info: list[str]) -> list[str]:
    """Render the non-image solver ``_info`` lines as a notes section."""
    notes = [
        line
        for line in (info or [])
        if isinstance(line, str) and not line.startswith("image:")
    ]
    if not notes:
        return []
    out = ["## Solver Notes", ""]
    for line in notes:
        out.append(f"- {line}")
    out.append("")
    return out


def _cycle_merit_section(
    cycle_rows: list[dict[str, Any]], has_tx: bool, *, incremental: bool
) -> list[str]:
    """Render the per-cycle merit-order table.

    This is an analyst's post-hoc grouping — the optimiser does not reason in
    discrete cycles; its objective applies a flat per-MWh cycling penalty.
    """
    out = ["## Per-Cycle Merit Order", ""]
    if not cycle_rows:
        out.append("*No charge/discharge cycles were detected in this run.*")
        out.append("")
        return out
    scope = (
        "the optimiser's **incremental** trades (the committed position is "
        "excluded — see the Committed Position section)"
        if incremental
        else "the optimiser's charge → discharge activity"
    )
    out.append(
        f"A post-hoc grouping of {scope} into charge → discharge cycles, ranked "
        "by reconstructed net margin. The optimiser does not evaluate discrete "
        "cycles — its objective applies a flat per-MWh cycling penalty — so this "
        "is an analytical view, not the solver's internal logic. Efficiency loss "
        "is not a separate column: it is already reflected in a discharge "
        "revenue lower than the charged energy implies."
    )
    out.append("")
    headers = ["#", "Type", "Charge", "Discharge", "Charge \u20ac", "Discharge \u20ac"]
    if has_tx:
        headers.append("Transaction \u20ac")
    headers += ["Cycling \u20ac", "Grid fee \u20ac", "Net \u20ac", "Cumulative \u20ac", "Eq. cycles"]
    rows: list[list[Any]] = []
    for r in cycle_rows:
        row = [
            r["rank"],
            r["kind"],
            r["charge_label"],
            r["discharge_label"],
            _num(r["charge_eur"]),
            _num(r["discharge_eur"]),
        ]
        if has_tx:
            row.append(_num(r["transaction_eur"]))
        row += [
            _num(r["cycling_eur"]),
            _num(r["grid_fee_eur"]),
            f"**{_num(r['net_eur'])}**",
            _num(r["cumulative_eur"]),
            _num(r["equiv_cycles"]),
        ]
        rows.append(row)
    out += _md_table(headers, rows)
    out.append("")
    marginal = cycle_rows[-1]
    out.append(
        f"> The lowest-ranked entry netted **{_eur(marginal['net_eur'])}**."
    )
    out.append("")
    return out


def _constraint_section(constraint_rows: list[dict[str, Any]]) -> list[str]:
    out = ["## Constraint Binding", ""]
    out.append(
        "Share of intervals each physical limit was binding. A high value means "
        "the optimiser was *pinned* against that limit and would do more if it could."
    )
    out.append("")
    rows = [
        [r["constraint"], f"{r['count']} / {r['intervals']}", f"{r['pct']:.1f}%"]
        for r in constraint_rows
    ]
    out += _md_table(["Constraint", "Intervals binding", "Share"], rows)
    out.append("")
    return out


def _chart_section(
    title: str, name: str, description: str, images: dict[str, str]
) -> list[str]:
    out = [f"## {title}", "", description, ""]
    uri = (images or {}).get(name)
    if uri:
        out.append(f"![{title}]({uri})")
    else:
        out.append(f"*Chart `{name}` unavailable for this run.*")
    out.append("")
    return out


def _solver_section(metrics: dict[str, Any]) -> list[str]:
    out = ["## Solver Statistics", ""]
    wall = metrics.get("solver_wall_time")
    rows = [
        ["Status", metrics.get("solver_status", "unknown")],
        [
            "Objective value (solver-internal, minimised)",
            _num(metrics.get("objective_value"), 4),
        ],
        ["Wall-clock time", f"{wall:.3f} s" if wall is not None else "n/a"],
        ["NLP variables", metrics.get("n_nlp_variables", "n/a")],
    ]
    out += _md_table(["Metric", "Value"], rows)
    out.append("")
    out.append(
        "> *The objective value is the solver's internal minimised quantity, "
        "not a EUR figure — the net-profit reconstruction under Results is the "
        "money number.*"
    )
    out.append("")
    return out


def _energy_balance_rows(metrics: dict[str, Any]) -> list[list[Any]]:
    delta = metrics.get("final_soc_mwh", 0.0) - metrics.get("initial_soc_mwh", 0.0)
    return [
        ["Initial SoC", f"{_num(metrics.get('initial_soc_mwh'))} MWh"],
        ["Final SoC", f"{_num(metrics.get('final_soc_mwh'))} MWh"],
        ["\u0394 SoC", f"{delta:+.2f} MWh"],
        ["Energy charged", f"{_num(metrics.get('total_charged_mwh'))} MWh"],
        ["Energy discharged", f"{_num(metrics.get('total_discharged_mwh'))} MWh"],
        ["Throughput", f"{_num(metrics.get('throughput_mwh'))} MWh"],
        ["Equivalent full cycles", _num(metrics.get("equivalent_full_cycles"))],
    ]


def _intraday_energy_balance_rows(metrics: dict[str, Any]) -> list[list[Any]]:
    """Energy balance for intraday — split into committed obligation vs trades."""
    delta = metrics.get("final_soc_mwh", 0.0) - metrics.get("initial_soc_mwh", 0.0)
    return [
        ["Initial SoC", f"{_num(metrics.get('initial_soc_mwh'))} MWh"],
        ["Final SoC", f"{_num(metrics.get('final_soc_mwh'))} MWh"],
        ["\u0394 SoC", f"{delta:+.2f} MWh"],
        ["Gross energy charged", f"{_num(metrics.get('total_charged_mwh'))} MWh"],
        ["Gross energy discharged", f"{_num(metrics.get('total_discharged_mwh'))} MWh"],
        ["— committed charge", f"{_num(metrics.get('committed_charged_mwh'))} MWh"],
        ["— committed discharge", f"{_num(metrics.get('committed_discharged_mwh'))} MWh"],
        ["— incremental charge", f"{_num(metrics.get('incremental_charged_mwh'))} MWh"],
        [
            "— incremental discharge",
            f"{_num(metrics.get('incremental_discharged_mwh'))} MWh",
        ],
        ["Gross throughput", f"{_num(metrics.get('throughput_mwh'))} MWh"],
        [
            "Equivalent full cycles (gross, physical)",
            _num(metrics.get("equivalent_full_cycles")),
        ],
    ]


def _why_loss_note(metrics: dict[str, Any]) -> list[str]:
    """Auto-generated note when the horizon's loss is structural.

    Emitted only when the data shows the loss is forced by the committed
    obligation plus the SoC ≥ 0 constraint — not a discretionary choice.
    Every clause restates a fact from the optimiser's own inputs/constraints.
    """
    net = metrics.get("net_profit_eur", 0.0) or 0.0
    committed_dis = metrics.get("committed_discharged_mwh", 0.0) or 0.0
    forced = metrics.get("forced_charge_mwh", 0.0) or 0.0
    incr_dis = metrics.get("incremental_discharged_mwh", 0.0) or 0.0
    gross_dis = metrics.get("total_discharged_mwh", 0.0) or 0.0
    if not (net < 0 and committed_dis > 0.01 and forced > 0.01):
        return []
    if incr_dis > 0.01 * max(gross_dis, 1e-9):
        return []
    return [
        "> **Why this horizon shows a loss:** the committed discharge obligation "
        f"({_num(committed_dis)} MWh), combined with the SoC \u2265 0 constraint, "
        f"requires at least {_num(forced)} MWh of charging that the orderbook "
        "supplies only at a net cost. With effectively no incremental discharge "
        "available to offset it, the optimiser's best feasible incremental "
        "result is a loss — a consequence of the fixed inputs and constraints, "
        "not a discretionary choice.",
        "",
    ]


def _reserve_bids_section(reserve_data: dict[str, Any] | None) -> list[str]:
    """Per-block reserve-bid table with rank and binding-constraint column.

    Emitted only when the run produced at least one non-zero reserve bid.
    """
    bid_rows = (reserve_data or {}).get("bid_rows") or []
    bid_rows = [r for r in bid_rows if (r.get("bid_mw") or 0.0) > 1e-9]
    if not bid_rows:
        return []
    out = ["## Reserve Bids", ""]
    out.append(
        "Per (product, 4h-block) bid that the solver placed into the "
        "currently-open auction.  Rank orders products by EUR per MW within "
        "the product so the diminishing returns of additional blocks are "
        "visible.  *Binding* names the constraint that capped the bid below "
        "``max_power`` (empty when the bid was the discretionary optimum)."
    )
    out.append("")
    out += _md_table(
        [
            "Product", "Block PTUs", "Bid MW", "Standby \u20ac",
            "Activation \u20ac", "Total \u20ac", "EUR/MW", "Rank",
        ],
        [
            [
                r["product"],
                f"#{r['block_start_idx']}–#{r['block_end_idx']}",
                _num(r["bid_mw"]),
                _num(r["standby_eur"]),
                _num(r["activation_eur"]),
                f"**{_num(r['total_eur'])}**",
                _num(r["eur_per_mw"]),
                r.get("rank", "—"),
            ]
            for r in bid_rows
        ],
    )
    out.append("")
    return out


def _reserve_shadow_prices_section(reserve_data: dict[str, Any] | None) -> list[str]:
    """Shadow-price table for open markets (one row per bid block).

    The Lagrange-multiplier proxy column captures the *average* magnitude of
    the constraint multipliers over the run — a coarse but free
    approximation that tells the reader whether reserves are squeezing the
    feasible set tightly or not.
    """
    rows = (reserve_data or {}).get("shadow_price_rows") or []
    if not rows:
        return []
    out = ["## Reserve Shadow Prices", ""]
    out.append(
        "Per (product, block) marginal-cost diagnostic.  *Binding* names the "
        "physical constraint that throttled the bid; a non-empty value means "
        "loosening that constraint by 1 unit would let the solver bid more.  "
        "The EUR/MW column is an average-multiplier proxy: a higher number "
        "means the constraint set is tight overall (small relaxations move "
        "the objective a lot)."
    )
    out.append("")
    out += _md_table(
        ["Product", "Block PTUs", "Bid MW", "Binding constraint", "~ EUR/MW relaxed"],
        [
            [
                r["product"],
                f"#{r['block_start_idx']}–#{r['block_end_idx']}",
                _num(r["bid_mw"]),
                r["binding_constraint"],
                _num(r["shadow_proxy_eur_per_mw"], 4),
            ]
            for r in rows
        ],
    )
    out.append("")
    return out


def _committed_reserves_section(reserve_data: dict[str, Any] | None) -> list[str]:
    """Per-product summary of inherited reserve commitments."""
    rows = (reserve_data or {}).get("committed_rows") or []
    if not rows:
        return []
    out = ["## Committed Reserves", ""]
    out.append(
        "Reserve positions inherited from prior auctions.  These do not earn "
        "*new* standby revenue in this horizon (that revenue was booked when "
        "they cleared), but they consume power-headroom and SoC-LER capacity "
        "and contribute to activation revenue and cycling penalty here."
    )
    out.append("")
    out += _md_table(
        [
            "Product", "Peak MW", "Avg MW",
            "Active intervals", "MWh reserved (\u00b7 t_min)",
        ],
        [
            [
                r["product"],
                _num(r["peak_mw"]),
                _num(r["avg_mw"]),
                f"{r['active_intervals']} / {r['horizon_intervals']}",
                _num(r["mwh_reserved"]),
            ]
            for r in rows
        ],
    )
    out.append("")
    return out


def _counterfactual_section(
    metrics: dict[str, Any],
    reserve_data: dict[str, Any] | None,
) -> list[str]:
    """Side-by-side comparison: this run versus a "no reserves" re-solve.

    When the counterfactual fired, this is the section that quantifies what
    reserves contributed (or cost) the portfolio over the horizon.  When
    the caller suppressed the counterfactual via the
    ``skip_counterfactual_reserves`` parameter, a brief note is emitted
    instead of the table.
    """
    skip = bool((reserve_data or {}).get("skip_counterfactual_reserves"))
    cf = (reserve_data or {}).get("counterfactual_metrics")
    if not (reserve_data or {}).get("config") and not skip and cf is None:
        return []
    out = ["## Counterfactual — Without Reserves", ""]
    if skip:
        out.append(
            "*Counterfactual analysis skipped via the "
            "``skip_counterfactual_reserves`` parameter.  Re-run with "
            "``parameters[skip_counterfactual_reserves] = 0`` (or unset) "
            "to see the EUR delta reserves contributed to this horizon.*"
        )
        out.append("")
        return out
    if cf is None:
        out.append(
            "*No counterfactual data available (the re-solve was not triggered "
            "or failed silently — see solver notes).*"
        )
        out.append("")
        return out
    out.append(
        "A second optimisation was executed with all reserve markets stripped "
        "from the input (no committed positions, no standby or activation "
        "prices, no aFRR activation drift).  Comparing the two runs isolates "
        "the EUR impact of reserves on this horizon.  This roughly doubles "
        "solver wall time; pass "
        "``parameters[skip_counterfactual_reserves] = 1`` in the request to "
        "suppress it on subsequent calls."
    )
    out.append("")
    actual_net = metrics.get("net_profit_eur", 0.0) or 0.0
    cf_net = cf.get("net_profit_eur", 0.0) or 0.0
    delta_net = actual_net - cf_net
    rows = [
        ["Net profit", _eur(actual_net), _eur(cf_net), _eur(delta_net)],
        [
            "Arbitrage revenue",
            _eur(metrics.get("total_revenue_eur")),
            _eur(cf.get("arbitrage_revenue_eur")),
            _eur(
                (metrics.get("total_revenue_eur") or 0.0)
                - (cf.get("arbitrage_revenue_eur") or 0.0)
            ),
        ],
        [
            "Throughput",
            f"{_num(metrics.get('throughput_mwh'))} MWh",
            f"{_num(cf.get('throughput_mwh'))} MWh",
            f"{_num((metrics.get('throughput_mwh') or 0.0) - (cf.get('throughput_mwh') or 0.0))} MWh",
        ],
        [
            "Final SoC",
            f"{_num(metrics.get('final_soc_mwh'))} MWh",
            f"{_num(cf.get('final_soc_mwh'))} MWh",
            f"{_num((metrics.get('final_soc_mwh') or 0.0) - (cf.get('final_soc_mwh') or 0.0))} MWh",
        ],
    ]
    out += _md_table(
        ["Metric", "With reserves (actual)", "Without reserves (counterfactual)", "\u0394"],
        rows,
    )
    out.append("")
    sign = (
        "**added value**" if delta_net > 0
        else "**cost value**" if delta_net < 0
        else "**were neutral**"
    )
    out.append(
        f"> Reserves {sign} of "
        f"**{_eur(abs(delta_net))}** over this horizon."
    )
    out.append("")
    return out


def _committed_position_section(
    metrics: dict[str, Any], images: dict[str, str]
) -> list[str]:
    """Dedicated section on the inherited committed obligation — usually the
    dominant driver of intraday battery activity."""
    out = ["## Committed Position", ""]
    out.append(
        "Before any trading decision the optimiser is handed a fixed committed "
        "power schedule. It enters the model only as a fixed input to the "
        "battery's state-of-charge dynamics and carries no price — the optimiser "
        "neither earns nor is charged for it. For an intraday run this inherited "
        "obligation is typically the dominant driver of battery activity; the "
        "trading tables below describe only the discretionary slice layered on "
        "top of it."
    )
    out.append("")
    share = (metrics.get("committed_share_of_throughput", 0.0) or 0.0) * 100.0
    out += _md_table(
        ["Metric", "Value"],
        [
            [
                "Committed discharge (export obligation)",
                f"{_num(metrics.get('committed_discharged_mwh'))} MWh",
            ],
            [
                "Committed charge (import obligation)",
                f"{_num(metrics.get('committed_charged_mwh'))} MWh",
            ],
            [
                "Committed net (export − import)",
                f"{_num(metrics.get('committed_net_mwh'))} MWh",
            ],
            [
                "Incremental discharge (this run's trades)",
                f"{_num(metrics.get('incremental_discharged_mwh'))} MWh",
            ],
            [
                "Incremental charge (this run's trades)",
                f"{_num(metrics.get('incremental_charged_mwh'))} MWh",
            ],
            [
                "Intervals with a committed obligation",
                f"{metrics.get('n_committed_intervals', 0)} / "
                f"{metrics.get('horizon_intervals', 0)}",
            ],
            [
                "Peak committed discharge / charge",
                f"{_num(metrics.get('peak_committed_discharge_mw'))} / "
                f"{_num(metrics.get('peak_committed_charge_mw'))} MW",
            ],
            ["Committed share of total throughput", f"{share:.1f}%"],
        ],
    )
    out.append("")
    forced = metrics.get("forced_charge_mwh", 0.0) or 0.0
    if forced > 0.01:
        trough = metrics.get("committed_soc_trough_mwh")
        out.append(
            "> **Feasibility:** running the committed schedule alone through the "
            f"battery's SoC dynamics drives state-of-charge to {_num(trough)} MWh "
            f"(interval {metrics.get('committed_soc_trough_interval', 0)}) — below "
            "the 0 MWh floor. The SoC \u2265 0 constraint therefore makes at least "
            f"{_num(forced)} MWh of charging necessary for *any* feasible "
            "solution, before economics enter at all."
        )
        out.append("")
    out.append(
        "The chart contrasts the committed obligation with the optimiser's "
        "incremental trades, and shows the SoC the committed schedule alone "
        "would produce."
    )
    out.append("")
    uri = (images or {}).get("committed_position")
    if uri:
        out.append(f"![Committed Position]({uri})")
    else:
        out.append("*Chart `committed_position` unavailable for this run.*")
    out.append("")
    return out


# ── public builders ──────────────────────────────────────────────────────────


def generate_intraday_markdown(
    *,
    metrics: dict[str, Any],
    cycle_rows: list[dict[str, Any]],
    constraint_rows: list[dict[str, Any]],
    orderbook_rows: list[dict[str, Any]],
    images: dict[str, str],
    info: list[str],
    model_input: dict[str, Any],
    reserve_data: dict[str, Any] | None = None,
) -> str:
    """Build the intraday reasoning-markdown document."""
    L: list[str] = []
    L.append("# Intraday Trading \u2014 Optimiser Reasoning")
    L.append("")
    L.append(_delivery_line(model_input, metrics))
    L.append("")

    # ── Results ──
    L.append("## Results")
    L.append("")
    L += _md_table(
        ["Metric", "Value"],
        [
            ["**Net profit**", f"**{_eur(metrics.get('net_profit_eur'))}**"],
            ["Trading revenue (net of buy cost)", _eur(metrics.get("total_revenue_eur"))],
            ["Cycling penalty", _eur(metrics.get("total_cycling_penalty_eur"))],
            ["Transaction cost", _eur(metrics.get("total_transaction_cost_eur"))],
            ["Grid fees", _eur(metrics.get("total_grid_fee_eur"))],
            ["Equivalent full cycles", _num(metrics.get("equivalent_full_cycles"))],
            ["Cycling penalty factor", f"{_num(metrics.get('cycling_penalty_factor'), 4)} EUR/MWh"],
        ],
    )
    L.append("")
    L.append(
        "> *Net profit covers the optimiser's **incremental trades only**. The "
        "committed position carries no price in the objective, so it "
        "contributes neither revenue nor cost here — see the Committed Position "
        "section. The figure is reconstructed from the exported timeseries and "
        "is not the solver's internal objective value.*"
    )
    L.append("")
    L += _why_loss_note(metrics)

    # ── Committed position ──
    L += _committed_position_section(metrics, images)

    # ── Reserve obligations + counterfactual (intraday has no Bid/Shadow) ──
    L += _committed_reserves_section(reserve_data)
    L += _counterfactual_section(metrics, reserve_data)

    # ── Per-cycle merit order ──
    L += _cycle_merit_section(cycle_rows, has_tx=True, incremental=True)

    # ── Energy balance ──
    L.append("## Energy Balance")
    L.append("")
    L += _md_table(["Metric", "Value"], _intraday_energy_balance_rows(metrics))
    L.append("")

    # ── Constraint binding ──
    L += _constraint_section(constraint_rows)

    # ── Orderbook depth ──
    L.append("## Orderbook Depth Utilisation")
    L.append("")
    L.append(
        "Average share of each orderbook level's available volume that was "
        "traded. Heavy use of deep (worse-priced) levels signals the optimiser "
        "is volume-constrained at the best prices."
    )
    L.append("")
    L += _md_table(
        ["Level", "Charge fill", "Discharge fill", "Charge MW\u00b7\u03a3", "Discharge MW\u00b7\u03a3"],
        [
            [
                r["level"],
                f"{r['charge_fill_pct']:.1f}%",
                f"{r['discharge_fill_pct']:.1f}%",
                _num(r["charge_mw_sum"]),
                _num(r["discharge_mw_sum"]),
            ]
            for r in orderbook_rows
        ]
        or [["—", "—", "—", "—", "—"]],
    )
    L.append("")

    # ── Charts ──
    L += _chart_section(
        "Revenue Decomposition",
        "revenue_decomposition",
        "Per-interval value breakdown (trading revenue, trading cost, cycling + "
        "transaction costs) and the cumulative net profit curve below it.",
        images,
    )
    L += _chart_section(
        "SoC Headroom",
        "soc_headroom",
        "State-of-charge trajectory against the capacity band, and the capacity "
        "utilisation percentage. Reveals how much headroom the optimiser kept.",
        images,
    )
    L += _chart_section(
        "Spread Duration",
        "spread_duration",
        "Sorted best bid/ask price-duration curves and the round-trip gross "
        "spread against the break-even threshold. The shaded area counts the "
        "intervals where the market offered a spread worth cycling for.",
        images,
    )

    # ── Solver + notes ──
    L += _solver_section(metrics)
    L += _notes_section(info)

    return "\n".join(L)


def generate_scheduling_markdown(
    *,
    metrics: dict[str, Any],
    cycle_rows: list[dict[str, Any]],
    constraint_rows: list[dict[str, Any]],
    schedule_rows: list[dict[str, Any]],
    images: dict[str, str],
    info: list[str],
    model_input: dict[str, Any],
    reserve_data: dict[str, Any] | None = None,
) -> str:
    """Build the day-ahead scheduling reasoning-markdown document."""
    L: list[str] = []
    L.append("# Day-Ahead Scheduling \u2014 Optimiser Reasoning")
    L.append("")
    L.append(_delivery_line(model_input, metrics))
    L.append("")

    # ── Results ──
    L.append("## Results")
    L.append("")
    L += _md_table(
        ["Metric", "Value"],
        [
            ["**Net profit**", f"**{_eur(metrics.get('net_profit_eur'))}**"],
            ["Arbitrage revenue (net of buy cost)", _eur(metrics.get("total_revenue_eur"))],
            ["Cycling penalty", _eur(metrics.get("total_cycling_penalty_eur"))],
            ["Grid fees", _eur(metrics.get("total_grid_fee_eur"))],
            ["Equivalent full cycles", _num(metrics.get("equivalent_full_cycles"))],
            ["Cycling penalty factor", f"{_num(metrics.get('cycling_penalty_factor'), 4)} EUR/MWh"],
        ],
    )
    L.append("")
    L.append(
        "> *Net profit is reconstructed from the exported timeseries; it may "
        "differ slightly from the solver's internal objective value (see "
        "Solver Statistics) when the objective carries extra terms such as "
        "terminal SoC valuation.*"
    )
    L.append("")

    # ── Per-cycle merit order ──
    L += _cycle_merit_section(cycle_rows, has_tx=False, incremental=False)

    # ── Reserve bids, shadow prices, committed positions, counterfactual ──
    L += _reserve_bids_section(reserve_data)
    L += _reserve_shadow_prices_section(reserve_data)
    L += _committed_reserves_section(reserve_data)
    L += _counterfactual_section(metrics, reserve_data)

    # ── Energy balance ──
    L.append("## Energy Balance")
    L.append("")
    L += _md_table(["Metric", "Value"], _energy_balance_rows(metrics))
    L.append("")

    # ── Constraint binding ──
    L += _constraint_section(constraint_rows)

    # ── Full-day schedule ──
    L.append("## Full-Day Schedule")
    L.append("")
    rows = [
        [
            f"{r['interval']:02d}",
            _num(r["price"]),
            r["action"],
            _num(r["mw"]) if r["action"] != "Idle" else "\u2014",
            _num(r["soc"]),
        ]
        for r in schedule_rows
    ]
    L += _md_table(
        ["Interval", "Price", "Action", "MW", "SoC after (MWh)"],
        rows or [["—", "—", "—", "—", "—"]],
    )
    L.append("")

    # ── Charts ──
    L += _chart_section(
        "Revenue Decomposition",
        "revenue_decomposition",
        "Per-interval value breakdown (gross revenue, gross cost, grid fees, "
        "cycling penalty) and the cumulative net profit curve below it.",
        images,
    )
    L += _chart_section(
        "SoC Headroom",
        "soc_headroom",
        "State-of-charge trajectory against the capacity band, and the capacity "
        "utilisation percentage.",
        images,
    )
    L += _chart_section(
        "Decision Rationale",
        "decision_rationale",
        "Market price overlaid with the effective charge/discharge thresholds "
        "and a charge/discharge/idle background. Directly answers why the "
        "optimiser did — or did not — trade in each interval.",
        images,
    )

    # ── Solver + notes ──
    L += _solver_section(metrics)
    L += _notes_section(info)

    return "\n".join(L)
