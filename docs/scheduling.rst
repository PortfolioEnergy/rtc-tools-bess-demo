Scheduling Demo: Energy Time Arbitrage
=======================================

Introduction
------------

This example demonstrates how to use RTC-Tools to optimise a Battery Energy Storage System (BESS) for day-ahead time arbitrage. The optimisation maximizes revenue by strategically charging during low electricity price periods and discharging during high price periods, while accounting for round-trip efficiency losses and cycling penalties.

Market Applicability
~~~~~~~~~~~~~~~~~~~~

This model is applicable to multiple electricity markets:

**European Day-Ahead Markets**
   * Optimises for EPEX SPOT and other European day-ahead auctions
   * Typically 24-hour horizon with hourly resolution

**Australian National Electricity Market (NEM)**
   * Optimises for NEM dispatch intervals (5-minute settlement)
   * Accommodates NEM's 5-minute dispatch cycles and price volatility

The model is market-agnostic and can be adapted to any day-ahead or dispatch market by adjusting the time series resolution and price data inputs.

Problem Formulation
-------------------

Mathematical Model
~~~~~~~~~~~~~~~~~~

The BESS optimisation problem can be formulated as follows:

**Objective Function:**

.. math::

   \max \sum_{t} \left( P_{net}(t) \cdot \pi(t) - \lambda \cdot (P_{charge}(t) + P_{discharge}(t)) \right)

Where:
   * :math:`P_{net}(t) = P_{discharge}(t) - P_{charge}(t)` = Net power (positive for discharge, negative for charge)
   * :math:`\pi(t)` = Electricity price at time t
   * :math:`\lambda` = Cycling penalty factor (requires tuning based on market conditions; annual revision is a reasonable starting point)
   * :math:`P_{charge}(t)` = Charging power
   * :math:`P_{discharge}(t)` = Discharging power

**State of Charge Dynamics:**

.. math::

   3600 \cdot \frac{dSoC}{dt} = P_{charge}(t) \cdot \sqrt{\eta} - \frac{P_{discharge}(t)}{\sqrt{\eta}}

Where:
   * :math:`SoC` = State of charge in MWh
   * :math:`\eta` = Round-trip efficiency
   * The factor 3600 converts from MJ to MWh (seconds to hours)

**Variable Bounds:**

.. math::

   0 \leq SoC(t) \leq 100 \text{ MWh}

.. math::

   0 \leq P_{charge}(t) \leq 50 \text{ MW}

.. math::

   0 \leq P_{discharge}(t) \leq 50 \text{ MW}

**Complementarity Constraints:**

The complementarity between charging and discharging is enforced using binary variables and inequality constraints:

.. math::

   b_{charge}(t) + b_{discharge}(t) \leq 1

.. math::

   P_{charge}(t) \leq b_{charge}(t) \cdot P_{max}

.. math::

   P_{discharge}(t) \leq b_{discharge}(t) \cdot P_{max}

Where:
   * :math:`b_{charge}(t)` and :math:`b_{discharge}(t)` are binary variables
   * :math:`P_{max} = 50` MW is the maximum power rating

Model Implementation
--------------------

Architecture Overview
~~~~~~~~~~~~~~~~~~~~~

The BESS optimisation follows a clear separation of concerns:

**Physical Asset Model (Modelica)**
   The Modelica model (``BESS.mo``) focuses purely on the physical behavior of the battery system:
      * State of charge dynamics with efficiency losses
      * Power flow calculations
      * Physical constraints and bounds
      * No economic calculations

**Value Stream Model (Python)**
   The Python implementation (``bess.py``) handles all economic aspects:
      * Revenue calculations from energy arbitrage
      * Cycling penalty costs
      * Objective function formulation
      * Economic parameters and constraints

Physical Asset Model (Modelica)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The BESS physical model is implemented in pure Modelica without external library dependencies:

.. literalinclude:: ../scheduling/model/BESS.mo
   :language: modelica
   :caption: BESS.mo - Physical asset model (battery dynamics only)

Value Stream Model (Python)
~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Objective Function (Economic Model):**

.. literalinclude:: ../scheduling/src/bess.py
   :language: python
   :pyobject: BESS.path_objective

The objective function demonstrates the value stream modeling:

* **Revenue Stream**: ``net_power * price`` - income from energy arbitrage
* **Cost Stream**: ``cycling_penalty_factor * (charge_power + discharge_power)`` - operational costs
* **Optimization Goal**: Maximize net profit (revenue minus costs)

**Solver Configuration:**

.. literalinclude:: ../scheduling/src/bess.py
   :language: python
   :pyobject: BESS.solver_options

The example uses the HiGHS mixed-integer linear programming solver.

**Path Constraints:**

.. literalinclude:: ../scheduling/src/bess.py
   :language: python
   :pyobject: BESS.path_constraints

The path constraints implement complementarity between charging and discharging.

Input Data
----------

The example uses two CSV input files:

**timeseries_import.csv**
   Contains electricity price forecasts.

**initial_state.csv**
   Specifies the initial state of charge.

Running the Example
-------------------

1. **Install Dependencies:**

   .. code-block:: bash

      uv sync

2. **Navigate to the Example Directory:**

   .. code-block:: bash

      cd scheduling

3. **Run the Optimization:**

   .. code-block:: bash

      uv run python src/bess.py

   This will:
   * Solve the optimisation problem
   * Export results to ``output/timeseries_export.csv``

4. **Generate Plots and Summary:**

   .. code-block:: bash

      uv run python src/plot_results.py

   This will:
      * Read the exported CSV results
      * Calculate economic metrics (revenue, costs)
      * Generate visualization plots
      * Display summary statistics
      * Save plots to ``output/bess_optimisation_results.png``

5. **Alternative: Run Both Steps Together:**

   .. code-block:: bash

      uv run python src/bess.py && uv run python src/plot_results.py

Results and Analysis
--------------------

Key outputs from the optimization are visualized below:

.. image:: ../scheduling/output/bess_optimisation_results.png
   :alt: BESS Optimization Results
   :width: 800px
   :align: center

*Figure: BESS optimisation results showing (top to bottom): State of Charge profile, Charge/Discharge power decisions, Electricity price signal, and Cumulative revenue over the 24-hour optimisation horizon.*
