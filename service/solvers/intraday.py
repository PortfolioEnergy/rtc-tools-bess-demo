"""Configurable BESSIntraday solver for continuous intraday trading.

Inherits the demo's ``BESSIntraday`` class but accepts runtime-configurable
``cycling_penalty_factor``, ``transaction_cost``, and ``stored_energy_value``
via class attributes set per request.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add the continuous_intraday source directory to the path
_intraday_src = str(
    Path(__file__).resolve().parent.parent.parent / "continuous_intraday" / "src"
)
if _intraday_src not in sys.path:
    sys.path.insert(0, _intraday_src)

from bess_intraday import BESSIntraday  # noqa: E402


class ConfigurableBESSIntraday(BESSIntraday):
    """BESSIntraday solver with runtime-configurable economic parameters.

    Battery parameters (``capacity``, ``max_power``, ``efficiency``,
    ``n_orderbook_entries``) are overridden via ``parameters.csv``.
    ``cycling_penalty_factor``, ``transaction_cost``, and
    ``stored_energy_value`` need Python-level overrides.
    """

    _cycling_penalty: float = 2.0
    _transaction_cost: float = 0.05
    _stored_energy_value: float = 0.0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cycling_penalty_factor = self.__class__._cycling_penalty
        self.transaction_cost = self.__class__._transaction_cost
        self.stored_energy_value = self.__class__._stored_energy_value

    def objective(self, ensemble_member):
        """Add terminal SoC valuation to the objective.

        The ``stored_energy_value`` (EUR/MWh) rewards energy remaining in
        the battery at the end of the optimisation horizon, preventing
        the solver from greedily draining the battery when future trading
        opportunities exist beyond the current window.

        RTC-Tools plain-sums ``path_objective`` over collocation points
        without multiplying by dt, so rates in EUR/h are effectively
        inflated by ``1/dt_hours``.  The terminal value must be scaled
        by the same factor to be comparable in magnitude.
        """
        obj = super().objective(ensemble_member)
        if self.stored_energy_value != 0.0:
            times = self.times()
            dt_hours = (times[1] - times[0]) / 3600.0
            soc_final = self.state_at("soc", times[-1], ensemble_member)
            obj -= (self.stored_energy_value / dt_hours) * soc_final
        return obj

    def post(self):
        # Skip the demo's print statements — we read CSV output directly
        # Call the grandparent's post() to ensure CSV export happens
        super(BESSIntraday, self).post()
