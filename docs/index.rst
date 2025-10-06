RTC-Tools BESS Optimisation Demos
==================================

.. image:: _static/bess_image.jpg
   :alt: Battery Energy Storage System
   :width: 600px
   :align: center

*Image: Industrial Battery Energy Storage System. Source: Wikimedia Commons, CC BY-SA 4.0*

Introduction
------------

This repository contains two Battery Energy Storage System (BESS) optimisation examples using RTC-Tools:

1. **Scheduling Demo**: Day-ahead optimisation for time arbitrage
2. **Continuous Intraday Demo**: Rolling intrinsic policy [1]_ [2]_ with orderbook trading

.. [1] Schaurecker, D., Wozabal, D., Löhndorf, N., & Staake, T. (2025). "Maximizing Battery Storage Profits via High-Frequency Intraday Trading." arXiv:2504.06932.
.. [2] Oeltz, D., & Pfingsten, T. (2025). "Rolling intrinsic for battery valuation in day-ahead and intraday markets." arXiv:2510.01956.

.. toctree::
   :maxdepth: 2
   :caption: Examples

   scheduling
   continuous_intraday

Towards Full Value Capture
--------------------------

We at `PortfolioEnergy <https://www.portfolioenergy.com>`_ offer a commercial platform utilising RTC-Tools that also includes:
   
* **Advanced Asset Modeling**:
   * Non-linear efficiency curves
   * Temperature-dependent performance
   * Parasitic power modelling
   * Degradation models
* **Sophisticated Market Modeling**:
   * Value stacking across multiple energy and multiple ancillary markets
   * Bid/offer volume & price co-optimisation
   * Market impact modeling
   * Modelling of transmission constraints
   * Stochastic optimisation for robustness against forecast uncertainty

The `PortfolioEnergy <https://www.portfolioenergy.com>`_ open-core optimisation platform—built on RTC-Tools and already maximising value on three continents—gives traders, developers and aggregators an interrogatable “glass box” solution. In addition to the capabilities above, it supports full value stacking across wholesale and ancillary markets as well as probabilistic optimisation for true risk-adjusted dispatch.

Ready to see the numbers? Reach us at `info@portfolioenergy.com <mailto:info@portfolioenergy.com>`_.