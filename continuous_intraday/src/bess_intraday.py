import numpy as np
from rtctools.optimization.collocated_integrated_optimization_problem import (
    CollocatedIntegratedOptimizationProblem,
)
from rtctools.optimization.csv_mixin import CSVMixin
from rtctools.optimization.modelica_mixin import ModelicaMixin
from rtctools.util import run_optimization_problem


# Default reserve config — intraday never *bids* reserves but it does inherit
# committed positions from prior auctions and must enforce their LER +
# power-headroom impact.  ``t_min_hours`` is the LER duration per product
# (e.g. 15 minutes / 0.25 h for FCR/aFRR).  ``open`` is always False here:
# every product is pinned to a zero bid because the intraday solver does
# not decide reserve auctions.
_DEFAULT_RESERVE_CONFIG: dict[str, dict] = {
    "fcr":       {"open": False, "t_min_hours": 0.0},
    "afrr_up":   {"open": False, "t_min_hours": 0.0},
    "afrr_down": {"open": False, "t_min_hours": 0.0},
}


class BESSIntraday(
    CSVMixin,
    ModelicaMixin,
    CollocatedIntegratedOptimizationProblem,
):
    """
    BESS continuous intraday trading optimization with rolling intrinsic policy.

    This class implements a Battery Energy Storage System (BESS) optimization
    for continuous intraday trading using a rolling intrinsic optimization approach.
    The model interacts with an orderbook containing bids and asks at multiple
    price levels.

    The rolling intrinsic policy optimizes over a receding horizon, making
    trading decisions based on the current orderbook state and future price
    expectations.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Trading parameters
        self.transaction_cost = 0.05  # $/MWh transaction cost
        self.cycling_penalty_factor = 0.1  # $/MWh cycling penalty
        self.stored_energy_value = (
            0.0  # EUR/MWh value assigned to SoC remaining at horizon end
        )
        # Reserve configuration; service wrappers override via class attribute.
        # Intraday never bids reserves; the config is used purely for the LER
        # duration parameters that scale committed-position headroom.
        self.reserve_config: dict[str, dict] = {
            k: dict(v) for k, v in _DEFAULT_RESERVE_CONFIG.items()
        }

    def solver_options(self):
        """Configure solver options for mixed-integer optimization."""
        options = super().solver_options()
        options["casadi_solver"] = "qpsol"
        options["solver"] = "highs"
        return options

    def pre(self):
        """Pre-processing to set up additional optimization variables."""
        super().pre()

        # Get number of orderbook entries from parameters
        params = self.parameters(0)
        self.n_entries = int(params["n_orderbook_entries"])

    def path_objective(self, ensemble_member):
        """
        Define optimization objective: maximize trading profit.

        For the rolling intrinsic policy, we optimize the expected value
        based on current orderbook state, with power allocated across
        different price levels.
        """
        # Revenue from selling to bids (discharging)
        total_discharge = 0.0
        discharge_revenue = 0.0
        for i in range(self.n_entries):
            bid_price = self.state(f"bid_prices[{i + 1}]")
            discharge_power_i = self.state(f"discharge_power_bids[{i + 1}]")
            total_discharge += discharge_power_i
            discharge_revenue += bid_price * discharge_power_i

        # Cost of buying from asks (charging)
        total_charge = 0.0
        charge_cost = 0.0
        for i in range(self.n_entries):
            ask_price = self.state(f"ask_prices[{i + 1}]")
            charge_power_i = self.state(f"charge_power_asks[{i + 1}]")
            total_charge += charge_power_i
            charge_cost += ask_price * charge_power_i

        # Grid fees on power exchanged with the grid
        grid_fee_cost = (
            self.state("grid_fee_in") * total_charge
            + self.state("grid_fee_out") * total_discharge
        )

        # Transaction costs on total traded volume
        transaction_cost = self.transaction_cost * (total_charge + total_discharge)

        # Activation revenue on committed aFRR positions.  Cleared FCR has no
        # separate energy revenue (symmetric, paid via standby).  Standby
        # revenue is excluded — that was earned in the prior auction.  Since
        # the intraday solver never bids, ``total_<p>`` here equals the
        # committed position for each product.
        activation_revenue = (
            self.state("total_afrr_up")
            * self.state("afrr_activation_fraction")
            * self.state("afrr_up_price")
            + self.state("total_afrr_down")
            * self.state("afrr_activation_fraction")
            * self.state("afrr_down_price")
        )

        # Cycling penalty includes expected throughput from cleared reserve
        # activations even though the intraday solver did not place them.
        reserve_cycling = self.cycling_penalty_factor * (
            2.0 * self.state("total_fcr") * self.state("fcr_activation_fraction")
            + self.state("total_afrr_up")   * self.state("afrr_activation_fraction")
            + self.state("total_afrr_down") * self.state("afrr_activation_fraction")
        )

        # Cycling penalty based on total power throughput
        cycling_penalty = self.cycling_penalty_factor * (total_charge + total_discharge)

        # Total objective (negative because we want to maximize profit)
        profit = (
            discharge_revenue
            + activation_revenue
            - charge_cost
            - grid_fee_cost
            - transaction_cost
            - cycling_penalty
            - reserve_cycling
        )
        return -profit

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

        # Complementarity on incremental trades only.
        #
        # charge_power and discharge_power are now GROSS flows
        # (committed + incremental) so they can both be non-zero when a
        # committed position is partially offset by a new trade.  Applying
        # complementarity to the gross variables would block those physically
        # valid states.
        #
        # Instead, we gate the incremental decision variables: the optimizer
        # cannot simultaneously place new charge orders (charge_power_asks)
        # AND new discharge orders (discharge_power_bids) in the same
        # interval.  This prevents gaming the cycling penalty via offsetting
        # trades while allowing legitimate committed-vs-incremental offsets.
        total_incr_charge = sum(
            self.state(f"charge_power_asks[{i + 1}]") for i in range(self.n_entries)
        )
        total_incr_discharge = sum(
            self.state(f"discharge_power_bids[{i + 1}]") for i in range(self.n_entries)
        )

        constraints.append(
            (
                self.state("is_charging") + self.state("is_discharging"),
                -np.inf,
                1.0,
            )
        )
        constraints.append(
            (
                total_incr_charge - self.state("is_charging") * max_power,
                -np.inf,
                0,
            )
        )
        constraints.append(
            (
                total_incr_discharge
                - self.state("is_discharging") * max_power,
                -np.inf,
                0,
            )
        )

        # Power allocated to each level cannot exceed available volume
        for i in range(self.n_entries):
            # Discharge limited by bid volume: discharge_power_bids[i] <= bid_volumes[i]
            # Reformulated as: discharge_power_bids[i] - bid_volumes[i] <= 0
            constraints.append(
                (
                    self.state(f"discharge_power_bids[{i + 1}]")
                    - self.state(f"bid_volumes[{i + 1}]"),
                    -np.inf,
                    0.0,
                )
            )

            # Charge limited by ask volume: charge_power_asks[i] <= ask_volumes[i]
            # Reformulated as: charge_power_asks[i] - ask_volumes[i] <= 0
            constraints.append(
                (
                    self.state(f"charge_power_asks[{i + 1}]")
                    - self.state(f"ask_volumes[{i + 1}]"),
                    -np.inf,
                    0.0,
                )
            )

        # Intraday never bids reserves — pin every bid total to 0 so the
        # Modelica decision variables collapse to their committed values.
        for product in ("fcr", "afrr_up", "afrr_down"):
            constraints.append(
                (self.state(f"bid_{product}_total"), -np.inf, 0.0)
            )

        # Reserve power-headroom constraints (committed positions still
        # squeeze the inverter's available MW for orderbook trades).
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

        # SoC LER constraints driven by the committed reserve durations.
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
    run_optimization_problem(BESSIntraday)
