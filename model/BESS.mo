model BESS
  // Parameters
  parameter Real capacity = 100.0 "Battery capacity in MWh";
  parameter Real efficiency = 0.9 "Round-trip efficiency";
  parameter Real max_power = 50.0 "Maximum charge/discharge power in MW";
  
  // Variables
  output Real soc(start=50.0, min=0.0, max=capacity) "State of charge in MWh";
  output Real charge_power(min=0.0, max=max_power) "Charging power in MW";
  output Real discharge_power(min=0.0, max=max_power) "Discharging power in MW";
  output Real net_power "Net power (positive = discharge, negative = charge) in MW";
  
  // Binary variables for complementarity
  Boolean is_charging "True if battery is charging";
  Boolean is_discharging "True if battery is discharging";
  
  // Input variables
  input Real price(fixed = true) "Electricity price in $/MWh";
  
equation
  // State of charge dynamics
  3600 * der(soc) = charge_power * sqrt(efficiency) - discharge_power / sqrt(efficiency);
  
  // Net power calculation
  net_power = discharge_power - charge_power;
  
end BESS;
