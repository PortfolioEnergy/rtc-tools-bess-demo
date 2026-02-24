"""Configurable BESS solver for day-ahead scheduling.

Inherits the demo's ``BESS`` class but accepts runtime-configurable
``cycling_penalty_factor`` and ``stored_energy_value`` via class
attributes set per request.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add the scheduling source directory to the path so we can import BESS
_scheduling_src = str(
    Path(__file__).resolve().parent.parent.parent / "scheduling" / "src"
)
if _scheduling_src not in sys.path:
    sys.path.insert(0, _scheduling_src)

from bess import BESS  # noqa: E402


class ConfigurableBESS(BESS):
    """BESS solver with runtime-configurable cycling penalty and terminal SoC value.

    Battery parameters (``capacity``, ``max_power``, ``efficiency``) are
    overridden via ``parameters.csv``.  ``cycling_penalty_factor`` and
    ``stored_energy_value`` need Python-level overrides because they are
    not Modelica parameters.
    """

    _cycling_penalty: float = 2.0
    _stored_energy_value: float = 0.0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cycling_penalty_factor = self.__class__._cycling_penalty
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
        super(BESS, self).post()
