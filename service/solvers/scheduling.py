"""Configurable BESS solver for day-ahead scheduling.

Inherits the demo's ``BESS`` class but accepts runtime-configurable
``cycling_penalty_factor`` via a class attribute set per request.
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
    """BESS solver with runtime-configurable cycling penalty.

    Battery parameters (``capacity``, ``max_power``, ``efficiency``) are
    overridden via ``parameters.csv``.  Only ``cycling_penalty_factor``
    needs a Python-level override because it is not a Modelica parameter.
    """

    _cycling_penalty: float = 2.0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cycling_penalty_factor = self.__class__._cycling_penalty

    def post(self):
        # Skip the demo's print statements — we read CSV output directly
        # Call the grandparent's post() to ensure CSV export happens
        super(BESS, self).post()
