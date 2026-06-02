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
    overridden via ``parameters.csv``.  ``cycling_penalty_factor``,
    ``stored_energy_value``, and the reserve-market config need Python-level
    overrides because they are not Modelica parameters.

    The ``objective()`` override with terminal SoC valuation lives in the
    base ``BESS`` class.  This subclass only needs to inject the per-request
    values via class attributes before instantiation.
    """

    _cycling_penalty: float = 2.0
    _stored_energy_value: float = 0.0
    _reserve_config: dict | None = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cycling_penalty_factor = self.__class__._cycling_penalty
        self.stored_energy_value = self.__class__._stored_energy_value
        if self.__class__._reserve_config is not None:
            # Deep-copy so concurrent requests can't mutate each other's state
            self.reserve_config = {
                k: dict(v) for k, v in self.__class__._reserve_config.items()
            }

    def post(self):
        # Skip the demo's print statements — we read CSV output directly
        # Call the grandparent's post() to ensure CSV export happens
        super(BESS, self).post()
