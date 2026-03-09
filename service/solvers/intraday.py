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

    The ``objective()`` override with terminal SoC valuation lives in the
    base ``BESSIntraday`` class.  This subclass only needs to inject the
    per-request values via class attributes before instantiation.
    """

    _cycling_penalty: float = 2.0
    _transaction_cost: float = 0.05
    _stored_energy_value: float = 0.0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cycling_penalty_factor = self.__class__._cycling_penalty
        self.transaction_cost = self.__class__._transaction_cost
        self.stored_energy_value = self.__class__._stored_energy_value

    def post(self):
        # Skip the demo's print statements — we read CSV output directly
        # Call the grandparent's post() to ensure CSV export happens
        super(BESSIntraday, self).post()
