model BESSIntraday
  // Parameters
  parameter Real capacity = 100.0 "Battery capacity in MWh";
  parameter Real efficiency = 0.9 "Round-trip efficiency";
  parameter Real max_power = 50.0 "Maximum charge/discharge power in MW";
  parameter Integer n_orderbook_entries = 10 "Number of orderbook entries per direction";

  // Variables
  output Real soc(start=50.0, min=0.0, max=capacity) "State of charge in MWh";
  output Real charge_power(min=0.0, max=max_power) "Charging power in MW";
  output Real discharge_power(min=0.0, max=max_power) "Discharging power in MW";
  output Real net_power "Net power (positive = discharge, negative = charge) in MW";

  // Binary variables for complementarity
  Boolean is_charging "True if battery is charging";
  Boolean is_discharging "True if battery is discharging";

  // Input variables - committed net position from day ahead and prior intraday trades
  input Real committed_net_power(fixed = true) "Committed net power from prior trades (MW, positive=discharge, negative=charge)";

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
  // State of charge dynamics
  3600 * der(soc) = charge_power * sqrt(efficiency) - discharge_power / sqrt(efficiency);

  // Net power calculation
  net_power = committed_net_power + sum(discharge_power_bids) - sum(charge_power_asks);

  // Decompose net power
  net_power = discharge_power - charge_power;

end BESSIntraday;
