model BESSIntraday
  // Parameters
  parameter Real capacity = 100.0 "Battery capacity in MWh";
  parameter Real efficiency = 0.9 "Round-trip efficiency";
  parameter Real max_power = 50.0 "Maximum charge/discharge power in MW";
  parameter Integer n_orderbook_entries = 10 "Number of orderbook entries per direction";

  // Variables
  output Real soc(start=50.0, min=0.0, max=capacity) "State of charge in MWh";
  output Real charge_power(min=0.0, max=max_power) "Gross charging power in MW (committed + incremental)";
  output Real discharge_power(min=0.0, max=max_power) "Gross discharging power in MW (committed + incremental)";
  output Real net_power "Net power (positive = discharge, negative = charge) in MW";

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
  // Gross physical battery flows: committed position plus incremental trades.
  // Both charge_power and discharge_power can be non-zero simultaneously when
  // the committed position and incremental trades are in opposite directions.
  // Complementarity is enforced on incremental trades only (in bess_intraday.py).
  charge_power   = committed_charge   + sum(charge_power_asks);
  discharge_power = committed_discharge + sum(discharge_power_bids);

  // State of charge dynamics on gross flows — efficiency applied correctly per leg.
  // Using gross values means efficiency losses on the committed discharge and
  // incremental charge are counted independently, not on their net difference.
  3600 * der(soc) = charge_power * sqrt(efficiency) - discharge_power / sqrt(efficiency);

  // Net grid power: total position for output and settlement purposes
  net_power = discharge_power - charge_power;

end BESSIntraday;
