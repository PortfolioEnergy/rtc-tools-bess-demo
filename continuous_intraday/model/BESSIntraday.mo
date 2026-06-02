model BESSIntraday
  // Parameters
  parameter Real capacity = 100.0 "Battery capacity in MWh";
  parameter Real efficiency = 0.9 "Round-trip efficiency";
  parameter Real max_power = 50.0 "Maximum charge/discharge power in MW";
  parameter Integer n_orderbook_entries = 10 "Number of orderbook entries per direction";

  // Reserve bid-curve dimensions — intraday never decides reserves so the
  // Python solver pins all deltas to zero, but the variables are kept for
  // wire-shape symmetry with the scheduling model.
  parameter Integer n_fcr_bands = 1 "Number of FCR offer-price bands (pinned)";
  parameter Integer n_afrr_up_bands = 1 "Number of aFRR up offer-price bands (pinned)";
  parameter Integer n_afrr_down_bands = 1 "Number of aFRR down offer-price bands (pinned)";

  // Variables
  output Real soc(start=50.0, min=0.0, max=capacity) "State of charge in MWh";
  output Real charge_power(min=0.0, max=max_power) "Gross charging power in MW (committed + incremental)";
  output Real discharge_power(min=0.0, max=max_power) "Gross discharging power in MW (committed + incremental)";
  output Real net_power "Net power (positive = discharge, negative = charge) in MW";

  // Aggregated reserve quantities — same outputs as the scheduling model so
  // diagnostics and the rtc_to_pe layer can read them uniformly.
  output Real bid_fcr_total(min=0.0) "Sum of FCR per-band bids in MW (pinned to 0)";
  output Real bid_afrr_up_total(min=0.0) "Sum of aFRR up per-band bids in MW (pinned to 0)";
  output Real bid_afrr_down_total(min=0.0) "Sum of aFRR down per-band bids in MW (pinned to 0)";
  output Real total_fcr(min=0.0) "Effective FCR commitment (position + bid) in MW";
  output Real total_afrr_up(min=0.0) "Effective aFRR up commitment in MW";
  output Real total_afrr_down(min=0.0) "Effective aFRR down commitment in MW";

  // Binary variables for complementarity on incremental trades only
  Boolean is_charging "True if battery is placing incremental charge trades";
  Boolean is_discharging "True if battery is placing incremental discharge trades";

  // Input variables - grid fees
  input Real grid_fee_in(fixed = true) "Grid fee for importing power in $/MWh";
  input Real grid_fee_out(fixed = true) "Grid fee for exporting power in $/MWh";

  // Committed position decomposed into non-negative components by the translation
  // layer (pe_to_rtc.py).  Splitting avoids applying efficiency to a net flow when
  // committed and incremental trades partially offset, which would underestimate
  // SoC drain and produce grid imbalance.
  input Real committed_charge(fixed = true) "Committed charging power from prior trades (MW, >= 0)";
  input Real committed_discharge(fixed = true) "Committed discharging power from prior trades (MW, >= 0)";

  // Committed reserve positions inherited from prior auctions.  These do NOT
  // dispatch physical power; they consume headroom and (for aFRR) drive
  // expected SoC drift via the activation fractions below.
  input Real fcr_position(fixed = true) "Pre-cleared FCR capacity in MW";
  input Real afrr_up_position(fixed = true) "Pre-cleared aFRR up capacity in MW";
  input Real afrr_down_position(fixed = true) "Pre-cleared aFRR down capacity in MW";

  // Reserve prices — used for cleared-position activation revenue accounting.
  input Real fcr_standby_price(fixed = true) "FCR standby price in EUR/MW/h";
  input Real fcr_price(fixed = true) "FCR settlement / activation price in EUR/MWh";
  input Real afrr_up_standby_price(fixed = true) "aFRR up standby price in EUR/MW/h";
  input Real afrr_up_price(fixed = true) "aFRR up activation price in EUR/MWh";
  input Real afrr_down_standby_price(fixed = true) "aFRR down standby price in EUR/MW/h";
  input Real afrr_down_price(fixed = true) "aFRR down activation price in EUR/MWh";

  // Expected call-frequency fractions
  input Real fcr_activation_fraction(fixed = true) "FCR expected call fraction in [0, 1]";
  input Real afrr_activation_fraction(fixed = true) "aFRR expected call fraction in [0, 1]";

  // Bid arrays — present for shape symmetry with BESS.mo.  All entries
  // pinned to zero by bess_intraday.py since intraday never bids reserves.
  input Real fcr_capacity_deltas[n_fcr_bands](each fixed = false, each min = 0.0);
  input Real afrr_up_capacity_deltas[n_afrr_up_bands](each fixed = false, each min = 0.0);
  input Real afrr_down_capacity_deltas[n_afrr_down_bands](each fixed = false, each min = 0.0);

  // Input variables - orderbook bid and ask prices (time-varying)
  input Real bid_prices[n_orderbook_entries](each fixed = true) "Bid prices ($/MWh), sorted descending";
  input Real ask_prices[n_orderbook_entries](each fixed = true) "Ask prices ($/MWh), sorted ascending";

  // Orderbook volumes
  input Real bid_volumes[n_orderbook_entries](each fixed = true) "Bid volumes (MW)";
  input Real ask_volumes[n_orderbook_entries](each fixed = true) "Ask volumes (MW)";

  // Power allocation across orderbook levels (decision variables)
  input Real discharge_power_bids[n_orderbook_entries](each fixed = false, each min=0.0) "Power sold at each bid level (MW)";
  input Real charge_power_asks[n_orderbook_entries](each fixed = false, each min=0.0) "Power bought at each ask level (MW)";

equation
  // Aggregated reserve totals
  bid_fcr_total       = sum(fcr_capacity_deltas);
  bid_afrr_up_total   = sum(afrr_up_capacity_deltas);
  bid_afrr_down_total = sum(afrr_down_capacity_deltas);

  total_fcr        = fcr_position       + bid_fcr_total;
  total_afrr_up    = afrr_up_position   + bid_afrr_up_total;
  total_afrr_down  = afrr_down_position + bid_afrr_down_total;

  // Gross physical battery flows: committed position plus incremental trades.
  // Both charge_power and discharge_power can be non-zero simultaneously when
  // the committed position and incremental trades are in opposite directions.
  // Complementarity is enforced on incremental trades only (in bess_intraday.py).
  charge_power   = committed_charge   + sum(charge_power_asks);
  discharge_power = committed_discharge + sum(discharge_power_bids);

  // State of charge dynamics on gross flows plus expected aFRR activation drift.
  // sqrt(eff) applied per leg matches the existing physical round-trip model.
  3600 * der(soc) =
      charge_power * sqrt(efficiency)
    - discharge_power / sqrt(efficiency)
    + total_afrr_down * afrr_activation_fraction * sqrt(efficiency)
    - total_afrr_up   * afrr_activation_fraction / sqrt(efficiency);

  // Net grid power: total position for output and settlement purposes
  net_power = discharge_power - charge_power;

end BESSIntraday;
