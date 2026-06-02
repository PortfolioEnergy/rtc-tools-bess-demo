model BESS
  // Parameters
  parameter Real capacity = 100.0 "Battery capacity in MWh";
  parameter Real efficiency = 0.9 "Round-trip efficiency";
  parameter Real max_power = 50.0 "Maximum charge/discharge power in MW";

  // Bid-curve dimensions (patched per request by the service layer; default 1 = single-band)
  parameter Integer n_fcr_bands = 1 "Number of FCR offer-price bands";
  parameter Integer n_afrr_up_bands = 1 "Number of aFRR up offer-price bands";
  parameter Integer n_afrr_down_bands = 1 "Number of aFRR down offer-price bands";

  // Physical battery state and dispatch
  output Real soc(start=50.0, min=0.0, max=capacity) "State of charge in MWh";
  output Real charge_power(min=0.0, max=max_power) "Charging power in MW";
  output Real discharge_power(min=0.0, max=max_power) "Discharging power in MW";
  output Real net_power "Net power (positive = discharge, negative = charge) in MW";

  // Aggregated reserve quantities — exposed as outputs so the result CSV
  // carries them directly for diagnostics and downstream consumers.
  output Real bid_fcr_total(min=0.0) "Sum of FCR per-band bids in MW";
  output Real bid_afrr_up_total(min=0.0) "Sum of aFRR up per-band bids in MW";
  output Real bid_afrr_down_total(min=0.0) "Sum of aFRR down per-band bids in MW";
  output Real total_fcr(min=0.0) "Effective FCR commitment (position + bid) in MW";
  output Real total_afrr_up(min=0.0) "Effective aFRR up commitment in MW";
  output Real total_afrr_down(min=0.0) "Effective aFRR down commitment in MW";

  // Binary variables for complementarity on physical dispatch
  Boolean is_charging "True if battery is charging";
  Boolean is_discharging "True if battery is discharging";

  // Energy-market inputs
  input Real price(fixed = true) "Electricity price in EUR/MWh";
  input Real grid_fee_in(fixed = true) "Grid fee for importing power in EUR/MWh";
  input Real grid_fee_out(fixed = true) "Grid fee for exporting power in EUR/MWh";

  // Committed reserve positions inherited from prior auctions (always fixed)
  input Real fcr_position(fixed = true) "Pre-cleared FCR capacity in MW";
  input Real afrr_up_position(fixed = true) "Pre-cleared aFRR up capacity in MW";
  input Real afrr_down_position(fixed = true) "Pre-cleared aFRR down capacity in MW";

  // Reserve prices (per PTU, constant within product block)
  input Real fcr_standby_price(fixed = true) "FCR standby price in EUR/MW/h";
  input Real fcr_price(fixed = true) "FCR settlement / activation price in EUR/MWh";
  input Real afrr_up_standby_price(fixed = true) "aFRR up standby price in EUR/MW/h";
  input Real afrr_up_price(fixed = true) "aFRR up activation price in EUR/MWh";
  input Real afrr_down_standby_price(fixed = true) "aFRR down standby price in EUR/MW/h";
  input Real afrr_down_price(fixed = true) "aFRR down activation price in EUR/MWh";

  // Expected call-frequency fractions used for SoC drift and degradation modelling
  input Real fcr_activation_fraction(fixed = true) "FCR expected call fraction in [0, 1]";
  input Real afrr_activation_fraction(fixed = true) "aFRR expected call fraction in [0, 1]";

  // Per-band bid decision variables (incremental MW added at each offer price band).
  // When a market is closed for this run the Python solver pins these to zero.
  input Real fcr_capacity_deltas[n_fcr_bands](each fixed = false, each min = 0.0)
    "FCR bid stack — MW added at each ascending offer price";
  input Real afrr_up_capacity_deltas[n_afrr_up_bands](each fixed = false, each min = 0.0)
    "aFRR up bid stack — MW added at each ascending offer price";
  input Real afrr_down_capacity_deltas[n_afrr_down_bands](each fixed = false, each min = 0.0)
    "aFRR down bid stack — MW added at each ascending offer price";

equation
  // Total bid per product = sum across price-bands
  bid_fcr_total       = sum(fcr_capacity_deltas);
  bid_afrr_up_total   = sum(afrr_up_capacity_deltas);
  bid_afrr_down_total = sum(afrr_down_capacity_deltas);

  // Total reserve commitment (cleared + this-run bid).  Drives both headroom
  // constraints (in the Python solver) and the expected SoC drift below.
  total_fcr        = fcr_position       + bid_fcr_total;
  total_afrr_up    = afrr_up_position   + bid_afrr_up_total;
  total_afrr_down  = afrr_down_position + bid_afrr_down_total;

  // State of charge dynamics with expected aFRR activation drift.
  // FCR is symmetric, so its expected SoC contribution is zero; the
  // call-frequency contribution to cell wear is handled in the Python
  // cycling penalty term, not here.
  // Sign convention: down-activation charges (positive drift), up-activation
  // discharges (negative drift). sqrt(eff) per leg matches the existing
  // physical dispatch round-trip efficiency model.
  3600 * der(soc) =
      charge_power * sqrt(efficiency)
    - discharge_power / sqrt(efficiency)
    + total_afrr_down * afrr_activation_fraction * sqrt(efficiency)
    - total_afrr_up   * afrr_activation_fraction / sqrt(efficiency);

  // Net physical power as before (reserves don't dispatch physical power here;
  // they only reserve headroom and account for expected drift / revenue).
  net_power = discharge_power - charge_power;

end BESS;
