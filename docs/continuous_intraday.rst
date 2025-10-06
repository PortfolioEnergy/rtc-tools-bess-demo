Continuous Intraday Demo: Rolling Intrinsic Policy
===================================================

Introduction
------------

This example demonstrates how to use RTC-Tools to optimise a BESS for continuous intraday trading using the rolling intrinsic policy. The optimisation makes trading decisions based on the current orderbook state with a receding horizon, allocating power across multiple price levels in the bid and ask orderbooks.

The rolling intrinsic policy is a widely-used approach for real-time battery trading that optimizes over a receding horizon window, making decisions based on current market conditions.

Problem Formulation
-------------------

Mathematical Model
~~~~~~~~~~~~~~~~~~

The continuous intraday trading problem extends the basic BESS optimization with orderbook-aware trading:

**Objective Function:**

.. math::

   \max \sum_{t} \left( \sum_{i=1}^{N} P_{discharge,i}^{new}(t) \cdot \pi_{bid,i}(t) - \sum_{i=1}^{N} P_{charge,i}^{new}(t) \cdot \pi_{ask,i}(t) - \lambda_{tx} \cdot (P_{charge}^{new}(t) + P_{discharge}^{new}(t)) - \lambda_{cyc} \cdot (P_{charge}^{new}(t) + P_{discharge}^{new}(t)) \right)

Where:
   * :math:`P_{discharge,i}^{new}(t)` = New power sold at bid level :math:`i` (intraday trades only)
   * :math:`P_{charge,i}^{new}(t)` = New power bought at ask level :math:`i` (intraday trades only)
   * :math:`\pi_{bid,i}(t)` = Bid price at level :math:`i` (descending order)
   * :math:`\pi_{ask,i}(t)` = Ask price at level :math:`i` (ascending order)
   * :math:`N` = Number of orderbook levels (default: 10)
   * :math:`\lambda_{tx}` = Transaction cost per MWh traded
   * :math:`\lambda_{cyc}` = Cycling penalty factor (requires tuning based on market conditions)

**Power Allocation Constraints:**

.. math::

   P_{discharge}^{new}(t) = \sum_{i=1}^{N} P_{discharge,i}^{new}(t)

.. math::

   P_{charge}^{new}(t) = \sum_{i=1}^{N} P_{charge,i}^{new}(t)

.. math::

   0 \leq P_{discharge,i}^{new}(t) \leq V_{bid,i}(t) \quad \forall i

.. math::

   0 \leq P_{charge,i}^{new}(t) \leq V_{ask,i}(t) \quad \forall i

Where:
   * :math:`V_{bid,i}(t)` = Available volume at bid level :math:`i`
   * :math:`V_{ask,i}(t)` = Available volume at ask level :math:`i`

**Committed Position Integration:**

The model accounts for committed positions from day ahead and prior intraday trades:

.. math::

   P_{net}(t) = P_{committed}(t) + P_{discharge}^{new}(t) - P_{charge}^{new}(t)

Where:
   * :math:`P_{committed}(t)` = Net committed power from prior trades (positive=discharge, negative=charge)
   * :math:`P_{net}(t)` = Total net power position after netting committed and new intraday trades

**Splitting Constraint:**

The net power is decomposed into non-negative charge and discharge components:

.. math::

   P_{net}(t) = P_{discharge}^{total}(t) - P_{charge}^{total}(t)

.. math::

   P_{discharge}^{total}(t) \geq 0, \quad P_{charge}^{total}(t) \geq 0

.. math::

   P_{discharge}^{total}(t) \cdot P_{charge}^{total}(t) = 0 \quad \text{(complementarity)}

This ensures at most one component is non-zero, allowing proper netting of positions.

**State of Charge Dynamics:**

.. math::

   3600 \cdot \frac{dSoC}{dt} = P_{charge}^{total}(t) \cdot \sqrt{\eta} - \frac{P_{discharge}^{total}(t)}{\sqrt{\eta}}

The SOC dynamics are governed by the total netted position components, not just the new trades.

Model Implementation
--------------------

Architecture Overview
~~~~~~~~~~~~~~~~~~~~~

The continuous intraday model extends the scheduling demo architecture with orderbook-aware trading:

**Physical Asset Model (Modelica)**
   The Modelica model (``BESSIntraday.mo``) includes:
      * State of charge dynamics with efficiency losses
      * Power allocation arrays across orderbook levels
      * Orderbook price and volume arrays (configurable :math:`N` levels)
      * Power flow calculations

**Value Stream Model (Python)**
   The Python implementation (``bess_intraday.py``) handles:
      * Revenue calculations from orderbook trading
      * Transaction cost modeling
      * Objective function formulation
      * Volume constraints for each orderbook level

Physical Asset Model (Modelica)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The BESS intraday model with orderbook arrays:

.. literalinclude:: ../continuous_intraday/model/BESSIntraday.mo
   :language: modelica
   :caption: BESSIntraday.mo - Physical asset model with orderbook trading

Key features:
   * Parametrized number of orderbook entries (``n_orderbook_entries``)
   * Bid/ask price and volume arrays
   * Power allocation decision variables (``discharge_power_bids``, ``charge_power_asks``)

Value Stream Model (Python)
~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Objective Function (Economic Model):**

.. literalinclude:: ../continuous_intraday/src/bess_intraday.py
   :language: python
   :pyobject: BESSIntraday.path_objective

The objective function maximizes trading profit by:
   * Calculating revenue from selling to each bid level
   * Calculating costs from buying from each ask level
   * Subtracting transaction costs on total traded volume
   * Subtracting cycling penalties to account for battery degradation

**Path Constraints:**

.. literalinclude:: ../continuous_intraday/src/bess_intraday.py
   :language: python
   :pyobject: BESSIntraday.path_constraints

The path constraints enforce:
   * Complementarity between charging and discharging
   * Volume limits for each orderbook level

Input Data
----------

The example uses two CSV input files:

**timeseries_import.csv**
   Contains orderbook data with bid/ask prices and volumes for each level:
      * ``committed_net_power`` - Net committed power from day ahead and prior intraday trades (MW, positive=discharge, negative=charge)
      * ``bid_prices[1]`` to ``bid_prices[10]`` - Bid prices (descending)
      * ``ask_prices[1]`` to ``ask_prices[10]`` - Ask prices (ascending)
      * ``bid_volumes[1]`` to ``bid_volumes[10]`` - Available bid volumes
      * ``ask_volumes[1]`` to ``ask_volumes[10]`` - Available ask volumes

**initial_state.csv**
   Specifies the initial state of charge (e.g., resulting from previous day ahead/intraday trading).

Running the Example
-------------------

1. **Install Dependencies:**

   .. code-block:: bash

      uv sync

2. **Navigate to the Example Directory:**

   .. code-block:: bash

      cd continuous_intraday

3. **Run the Optimization:**

   .. code-block:: bash

      uv run python src/bess_intraday.py

   This will:
   * Solve the rolling intrinsic optimisation problem
   * Export results to ``output/timeseries_export.csv``

4. **Generate Plots and Summary:**

   .. code-block:: bash

      uv run python src/plot_results.py

   This will:
      * Read the exported CSV results
      * Calculate trading metrics (revenue, costs, trades)
      * Generate visualization plots including orderbook allocation
      * Display summary statistics
      * Save plots to ``output/bess_intraday_results.png``

5. **Alternative: Run Both Steps Together:**

   .. code-block:: bash

      uv run python src/bess_intraday.py && uv run python src/plot_results.py

Results and Analysis
--------------------

The optimization produces detailed visualizations showing:
   * Battery state of charge profile
   * Charge/discharge power decisions
   * Orderbook bid-ask spread dynamics
   * Power allocation across different orderbook levels (stacked bars)
   * Cumulative trading revenue

The rolling intrinsic policy enables the battery to:
   * React to real-time orderbook conditions
   * Optimize across multiple price levels
   * Account for liquidity constraints at each level
   * Maximize trading profit while respecting volume limits
