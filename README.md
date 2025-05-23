# RTC-Tools BESS Optimization Demo

This example demonstrates a Battery Energy Storage System (BESS) optimization using RTC-Tools for time arbitrage. The optimization maximizes revenue by charging during low-price periods and discharging during high-price periods, while considering cycling penalties and round-trip efficiency.

## Requirements

- Python 3.8+
- RTC-Tools
- uv (for dependency management)

## Installation

Install dependencies using uv:
```bash
uv sync
```

## Usage

Run the optimization:
```bash
uv run python src/bess_optimization.py
```

The optimization will:
1. Read input data from `input/` folder
2. Solve the BESS optimization problem
3. Generate plots in `output/` folder
4. Print summary statistics

## Input Files

- `input/timeseries_import.csv`: Electricity price forecast
- `input/initial_state.csv`: Initial state of charge

## Output

- `output/bess_optimization_results.png`: Visualization plots
- Console output with optimization summary

## Model Parameters

- **Capacity**: 100 MWh
- **Maximum Power**: 50 MW (charge/discharge)
- **Round-trip Efficiency**: 90%
- **Cycling Penalty Factor**: 0.1
- **Initial SoC**: 50 MWh

## Optimization Objective

Maximize: Revenue - Cycling Penalty

Where:
- Revenue = Net Power × Electricity Price
- Cycling Penalty = Cycling Factor × (Charge Power + Discharge Power)

## Solver

Uses HiGHS mixed-integer linear programming solver.

## License

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.