"""Configurable BESSIntraday solver for continuous intraday trading.

Inherits the demo's ``BESSIntraday`` class but accepts runtime-configurable
``cycling_penalty_factor`` and ``transaction_cost`` via class attributes
set per request.
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
    Only ``cycling_penalty_factor`` and ``transaction_cost`` need
    Python-level overrides.
    """

    _cycling_penalty: float = 2.0
    _transaction_cost: float = 0.05

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cycling_penalty_factor = self.__class__._cycling_penalty
        self.transaction_cost = self.__class__._transaction_cost

    def post(self):
        # Skip the demo's print statements — we read CSV output directly
        # Call the grandparent's post() to ensure CSV export happens
        super(BESSIntraday, self).post()
