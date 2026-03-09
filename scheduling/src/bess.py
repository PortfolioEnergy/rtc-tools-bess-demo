import numpy as np
from rtctools.optimization.collocated_integrated_optimization_problem import (
    CollocatedIntegratedOptimizationProblem,
)
from rtctools.optimization.csv_mixin import CSVMixin
from rtctools.optimization.modelica_mixin import ModelicaMixin
from rtctools.util import run_optimization_problem


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

        # Cycling penalty based on total power throughput
        cycling_penalty = self.cycling_penalty_factor * (
            self.state("charge_power") + self.state("discharge_power")
        )

        # Total objective (negative because we want to maximize)
        return -(revenue - grid_fee_cost - cycling_penalty)

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
                - self.state("is_charging") * parameters["max_power"],
                -np.inf,
                0,
            )
        )
        constraints.append(
            (
                self.state("discharge_power")
                - self.state("is_discharging") * parameters["max_power"],
                -np.inf,
                0,
            )
        )

        return constraints

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
