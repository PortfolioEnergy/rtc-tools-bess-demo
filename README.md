# RTC-Tools BESS Optimization Demos

This repository contains two Battery Energy Storage System (BESS) optimization examples using RTC-Tools:

1. **Scheduling Demo** (`scheduling/`): Day-ahead optimization for time arbitrage
2. **Continuous Intraday Demo** (`continuous_intraday/`): Rolling intrinsic policy[^1][^2] with orderbook trading

[^1]: Schaurecker, D., Wozabal, D., Löhndorf, N., & Staake, T. (2025). "Maximizing Battery Storage Profits via High-Frequency Intraday Trading." arXiv:2504.06932.
[^2]: Oeltz, D., & Pfingsten, T. (2025). "Rolling intrinsic for battery valuation in day-ahead and intraday markets." arXiv:2510.01956.

## Documentation

Full documentation is available at: https://portfolioenergy-bess-demo.readthedocs.io/en/latest/

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

Both examples follow the same structure:

```bash
cd <example_directory>
uv run python src/<script_name>.py
```

Each optimization will:
1. Read input data from `input/` folder
2. Solve the BESS optimization problem
3. Generate plots in `output/` folder
4. Print summary statistics

### Scheduling Demo (`scheduling/`)

Day-ahead optimization for time arbitrage using forecasted electricity prices.

```bash
cd scheduling
uv run python src/bess.py
```

**Key features:**
- Single optimization over full day-ahead horizon
- Uses price forecasts (`input/timeseries_import.csv`)
- Includes cycling penalty to reduce battery degradation

### Continuous Intraday Demo (`continuous_intraday/`)

Rolling intrinsic optimization with orderbook trading.

```bash
cd continuous_intraday
uv run python src/bess_intraday.py
```

**Key features:**
- Sequential rolling horizon optimizations
- Uses orderbook bid/ask prices and volumes (`input/orderbook_YYYYMMDD.csv`)
- Maximizes intrinsic value at each decision point

## Model Parameters

Both examples use the same battery model:
- **Capacity**: 100 MWh
- **Maximum Power**: 50 MW (charge/discharge)
- **Round-trip Efficiency**: 90%
- **Initial SoC**: 50 MWh
- **Solver**: HiGHS mixed-integer linear programming

## License

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.