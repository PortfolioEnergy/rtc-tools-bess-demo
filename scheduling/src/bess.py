import numpy as np
from rtctools.optimization.collocated_integrated_optimization_problem import (
    CollocatedIntegratedOptimizationProblem,
)
from rtctools.optimization.csv_mixin import CSVMixin
from rtctools.optimization.modelica_mixin import ModelicaMixin
from rtctools.util import run_optimization_problem


# Default reserve config — every product closed, zero LER duration.
# Concrete configs are stamped onto the dynamically-derived solver subclass
# by service/solvers/scheduling.py.  Each entry has the shape::
#
#     {"open": bool, "t_min_hours": float, "blocks": list[list[int]]}
#
# where ``blocks`` lists the PTU-index groupings that must hold a constant
# bid (block-equality constraints).  Blocks come from runs of identical
# standby-price values in the input timeseries.
_DEFAULT_RESERVE_CONFIG: dict[str, dict] = {
    "fcr":       {"open": False, "t_min_hours": 0.0, "blocks": []},
    "afrr_up":   {"open": False, "t_min_hours": 0.0, "blocks": []},
    "afrr_down": {"open": False, "t_min_hours": 0.0, "blocks": []},
}


class BESS(
    CSVMixin,
    ModelicaMixin,
    CollocatedIntegratedOptimizationProblem,
):
    """
    BESS optimization problem for time arbitrage.

    This class implements a Battery Energy Storage System (BESS) optimization
    problem that maximizes revenue from time arbitrage while considering
    cycling penalties and round-trip efficiency.

    The physical asset (battery dynamics) is modeled in Modelica, while the
    revenue and costs are calculated in Python.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Economic parameters (not in Modelica model)
        self.cycling_penalty_factor = 0.1  # $/MWh cycling penalty
        self.stored_energy_value = (
            0.0  # EUR/MWh value assigned to SoC remaining at horizon end
        )
        # Reserve configuration; service wrappers override via class attribute.
        self.reserve_config: dict[str, dict] = {
            k: dict(v) for k, v in _DEFAULT_RESERVE_CONFIG.items()
        }

    def solver_options(self):
        """Configure solver options for mixed-integer optimization."""
        options = super().solver_options()
        options["casadi_solver"] = "qpsol"
        options["solver"] = "highs"
        return options

    def path_objective(self, ensemble_member):
        """
        Define optimization objective: maximize revenue minus cycling penalty.

        This separates the economic value streams (calculated in Python) from
        the physical asset model (defined in Modelica).
        """
        # Revenue from energy arbitrage
        revenue = self.state("net_power") * self.state("price")

        # Grid fees on power exchanged with the grid
        grid_fee_cost = self.state("grid_fee_in") * self.state(
            "charge_power"
        ) + self.state("grid_fee_out") * self.state("discharge_power")

        # Reserve standby revenue on this-run bids only.  Cleared revenue from
        # prior auctions is sunk and excluded from the current objective.
        # When a market is closed, ``bid_<p>_total`` is pinned to 0 below.
        standby_revenue = (
            self.state("bid_fcr_total")       * self.state("fcr_standby_price")
            + self.state("bid_afrr_up_total")   * self.state("afrr_up_standby_price")
            + self.state("bid_afrr_down_total") * self.state("afrr_down_standby_price")
        )

        # Activation revenue applies to total reserve (cleared + bid):
        # the cleared portion will still be called for energy during delivery.
        # FCR is symmetric so revenue is captured solely via standby; aFRR
        # activation gets its own per-MWh energy price.
        activation_revenue = (
            self.state("total_afrr_up")
            * self.state("afrr_activation_fraction")
            * self.state("afrr_up_price")
            + self.state("total_afrr_down")
            * self.state("afrr_activation_fraction")
            * self.state("afrr_down_price")
        )

        # Cycling penalty extended with expected activation throughput.
        # FCR is symmetric → 2 * total_fcr * fraction (both directions cycle).
        # aFRR is one-sided per product → 1 * total_* * fraction.
        cycling_penalty = self.cycling_penalty_factor * (
            self.state("charge_power")
            + self.state("discharge_power")
            + 2.0 * self.state("total_fcr") * self.state("fcr_activation_fraction")
            + self.state("total_afrr_up")   * self.state("afrr_activation_fraction")
            + self.state("total_afrr_down") * self.state("afrr_activation_fraction")
        )

        # Total objective (negative because we want to maximize)
        return -(
            revenue
            + standby_revenue
            + activation_revenue
            - grid_fee_cost
            - cycling_penalty
        )

    def objective(self, ensemble_member):
        """Add terminal SoC valuation to the path objective total.

        When ``stored_energy_value`` is non-zero (EUR/MWh), the solver is
        rewarded for energy remaining in the battery at the end of the
        optimisation horizon.  This prevents greedy end-of-horizon draining
        when future trading opportunities exist beyond the current window.

        RTC-Tools plain-sums ``path_objective`` over collocation points
        without multiplying by dt, so rates in EUR/h are effectively
        inflated by ``1/dt_hours``.  The terminal value must be scaled
        by the same factor to remain comparable in magnitude.
        """
        obj = super().objective(ensemble_member)
        if self.stored_energy_value != 0.0:
            times = self.times()
            dt_hours = (times[1] - times[0]) / 3600.0
            soc_final = self.state_at("soc", times[-1], ensemble_member)
            obj -= (self.stored_energy_value / dt_hours) * soc_final
        return obj

    def path_constraints(self, ensemble_member):
        """Define path constraints (inequality constraints over time)."""
        constraints = super().path_constraints(ensemble_member)

        parameters = self.parameters(ensemble_member)
        max_power = parameters["max_power"]
        capacity = parameters["capacity"]

        # Ensure only one mode can be active at a time (complementarity)
        constraints.append(
            (
                self.state("is_charging") + self.state("is_discharging"),
                -np.inf,
                1.0,
            )
        )
        constraints.append(
            (
                self.state("charge_power")
                - self.state("is_charging") * max_power,
                -np.inf,
                0,
            )
        )
        constraints.append(
            (
                self.state("discharge_power")
                - self.state("is_discharging") * max_power,
                -np.inf,
                0,
            )
        )

        # Reserve power-headroom constraints.  Up-direction reserves (FCR
        # which is symmetric, plus aFRR up) compete with discharge for the
        # inverter's discharging capacity; down-direction reserves (FCR plus
        # aFRR down) compete with charging.  Both inequalities are <= max_power.
        total_fcr = self.state("total_fcr")
        total_afrr_up = self.state("total_afrr_up")
        total_afrr_down = self.state("total_afrr_down")

        constraints.append(
            (
                self.state("discharge_power") + total_fcr + total_afrr_up - max_power,
                -np.inf,
                0.0,
            )
        )
        constraints.append(
            (
                self.state("charge_power") + total_fcr + total_afrr_down - max_power,
                -np.inf,
                0.0,
            )
        )

        # SoC LER (limited-energy reservoir) constraints.  The battery must
        # keep enough headroom to honour the worst-case activation for the
        # product's T_min duration.  Down-side reserves squeeze the *top* of
        # the SoC band; up-side reserves squeeze the *bottom*.
        fcr_t = float(self.reserve_config.get("fcr", {}).get("t_min_hours", 0.0))
        afrr_up_t = float(self.reserve_config.get("afrr_up", {}).get("t_min_hours", 0.0))
        afrr_down_t = float(
            self.reserve_config.get("afrr_down", {}).get("t_min_hours", 0.0)
        )

        if fcr_t > 0.0 or afrr_down_t > 0.0:
            constraints.append(
                (
                    self.state("soc")
                    + total_fcr * fcr_t
                    + total_afrr_down * afrr_down_t
                    - capacity,
                    -np.inf,
                    0.0,
                )
            )
        if fcr_t > 0.0 or afrr_up_t > 0.0:
            constraints.append(
                (
                    -self.state("soc")
                    + total_fcr * fcr_t
                    + total_afrr_up * afrr_up_t,
                    -np.inf,
                    0.0,
                )
            )

        # Closed-market pin: when the caller did not include a market in this
        # run, force the corresponding bid total to 0.  Combined with the
        # model's ``min=0`` bound this collapses the decision variable.
        for product in ("fcr", "afrr_up", "afrr_down"):
            pcfg = self.reserve_config.get(product) or {}
            if not pcfg.get("open"):
                constraints.append(
                    (self.state(f"bid_{product}_total"), -np.inf, 0.0)
                )

        return constraints

    def constraints(self, ensemble_member):
        """Cross-time constraints — block-equality on open reserve bids.

        For each open product, the bid quantity must be constant across all
        PTUs belonging to the same standby-price block (a 4h tranche by
        default).  Each block is a list of PTU indices supplied by the
        service-layer translation in ``reserve_config[product]["blocks"]``.
        """
        out = super().constraints(ensemble_member)
        times = self.times()
        if len(times) < 2:
            return out
        for product in ("fcr", "afrr_up", "afrr_down"):
            pcfg = self.reserve_config.get(product) or {}
            if not pcfg.get("open"):
                continue
            var = f"bid_{product}_total"
            for block in pcfg.get("blocks", []):
                if not block or len(block) < 2:
                    continue
                ref_idx = block[0]
                if ref_idx >= len(times):
                    continue
                ref_val = self.state_at(var, times[ref_idx], ensemble_member)
                for idx in block[1:]:
                    if idx >= len(times):
                        continue
                    other = self.state_at(var, times[idx], ensemble_member)
                    out.append((other - ref_val, 0.0, 0.0))
        return out

    def post(self):
        """Post-processing step to save results and call plotting script."""
        super().post()

        print("Optimization completed successfully!")
        print("Results saved to output/timeseries_export.csv")
        print(
            "Run 'uv run python src/plot_results.py' to generate plots and summary statistics."
        )


if __name__ == "__main__":
    # Run the optimization
    run_optimization_problem(BESS)
