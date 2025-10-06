import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
import os


def setup_plot_style():
    """Set up the plotting style with custom colors and font."""
    # Color scheme as specified in requirements
    colors = {
        'background1': "#ffffff",
        'background2': '#000020',
        'foreground1': '#ced73e',
        'foreground2': '#ffffff',
        'foreground3': 'red',
        'foreground4': '#000000',
    }

    # Set matplotlib style
    plt.rcParams['figure.facecolor'] = colors['background2']
    plt.rcParams['axes.facecolor'] = colors['background2']
    plt.rcParams['axes.edgecolor'] = colors['foreground2']
    plt.rcParams['axes.labelcolor'] = colors['foreground4']
    plt.rcParams['xtick.color'] = colors['foreground4']
    plt.rcParams['ytick.color'] = colors['foreground4']
    plt.rcParams['text.color'] = colors['foreground2']

    return colors


def create_plots(csv_file='output/timeseries_export.csv',
                price_file='input/timeseries_import.csv',
                output_file='output/bess_intraday_results.png'):
    """Create visualization plots from the exported CSV results."""

    # Set up plotting style
    colors = setup_plot_style()

    # Read the optimization results CSV file
    if not os.path.exists(csv_file):
        print(f"Error: Results file {csv_file} not found. Please run the optimization first.")
        return

    df_results = pd.read_csv(csv_file)

    # Read the orderbook data from the input file
    if not os.path.exists(price_file):
        print(f"Error: Price file {price_file} not found.")
        return

    df_orderbook = pd.read_csv(price_file)

    # Convert time column to datetime if it's not already
    if 'time' in df_results.columns:
        df_results['time'] = pd.to_datetime(df_results['time'])
        # Convert to hours from start for plotting
        start_time = df_results['time'].iloc[0]
        times_hours = (df_results['time'] - start_time).dt.total_seconds() / 3600.0
    else:
        # Fallback: assume 5-minute intervals
        times_hours = np.arange(len(df_results)) * (5/60)

    # Extract variables from optimization results
    soc = df_results['soc']
    charge_power = df_results['charge_power']
    discharge_power = df_results['discharge_power']
    net_power = df_results['net_power']

    # Extract best bid and ask prices for plotting
    best_bid = df_orderbook['bid_prices[1]']
    best_ask = df_orderbook['ask_prices[1]']
    mid_price = (best_bid + best_ask) / 2

    # Calculate revenue from orderbook trading
    revenue = 0.0
    for i in range(1, 11):  # 10 orderbook levels
        if f'discharge_power_bids[{i}]' in df_results.columns:
            revenue += df_results[f'discharge_power_bids[{i}]'] * df_orderbook[f'bid_prices[{i}]']
        if f'charge_power_asks[{i}]' in df_results.columns:
            revenue -= df_results[f'charge_power_asks[{i}]'] * df_orderbook[f'ask_prices[{i}]']

    # Create figure with subplots
    fig, axes = plt.subplots(7, 1, figsize=(12, 16))
    fig.patch.set_facecolor(colors['background1'])

    # Plot 1: State of Charge (continuous variable, use regular plot)
    axes[0].plot(times_hours, soc, color=colors['foreground1'], linewidth=2)
    axes[0].set_ylabel('SoC (MWh)', color=colors['foreground4'])
    axes[0].set_title('Battery State of Charge', color=colors['foreground4'])
    axes[0].grid(True, alpha=0.3, color=colors['foreground4'])

    # Plot 2: Power (step functions - values valid until next timestamp)
    widths = np.diff(times_hours.array)
    axes[1].bar(times_hours[1:], discharge_power[1:], color=colors['foreground1'],
                label='Discharge Power', align='edge', width=widths)
    axes[1].bar(times_hours[1:], -charge_power[1:], color=colors['foreground3'],
                label='Charge Power', align='edge', width=widths)
    axes[1].set_ylabel('Power (MW)', color=colors['foreground4'])
    axes[1].set_title('Charge/Discharge Power', color=colors['foreground4'])
    axes[1].legend()
    axes[1].grid(True, alpha=0.3, color=colors['foreground2'])
    axes[1].axhline(0, linewidth=2, color=colors['foreground2'])

    # Plot 3: Orderbook Prices (bid-ask spread) - using twin axes for better visibility
    ax3_main = axes[2]
    ax3_twin = ax3_main.twinx()

    # Plot mid price on main axis
    ax3_main.step(times_hours, mid_price, where='post', color=colors['foreground1'],
                linewidth=2, label='Mid Price')
    ax3_main.set_ylabel('Mid Price ($/MWh)', color=colors['foreground4'])
    ax3_main.tick_params(axis='y', labelcolor=colors['foreground4'])
    ax3_main.grid(True, alpha=0.3, color=colors['foreground2'])

    # Plot spread on twin axis
    ax3_twin.step(times_hours, best_ask - best_bid, where='post',
                 color=colors['foreground3'], linewidth=2, label='Bid-Ask Spread')
    ax3_twin.set_ylabel('Spread ($/MWh)', color=colors['foreground3'])
    ax3_twin.tick_params(axis='y', labelcolor=colors['foreground3'])

    ax3_main.set_title('Orderbook Mid Price and Spread', color=colors['foreground4'])

    # Combine legends
    lines1, labels1 = ax3_main.get_legend_handles_labels()
    lines2, labels2 = ax3_twin.get_legend_handles_labels()
    ax3_main.legend(lines1 + lines2, labels1 + labels2, loc='upper left')

    # Plot 4: Power allocation across orderbook levels (stacked bars)
    # Show discharge allocation
    discharge_allocations = []
    charge_allocations = []
    for i in range(1, 11):
        if f'discharge_power_bids[{i}]' in df_results.columns:
            discharge_allocations.append(df_results[f'discharge_power_bids[{i}]'])
        if f'charge_power_asks[{i}]' in df_results.columns:
            charge_allocations.append(df_results[f'charge_power_asks[{i}]'])

    if discharge_allocations and charge_allocations:
        bottom_discharge = np.zeros(len(times_hours))
        bottom_charge = np.zeros(len(times_hours))

        for i, alloc in enumerate(discharge_allocations):
            axes[3].bar(times_hours[1:], alloc[1:], bottom=bottom_discharge[1:],
                       color=plt.cm.Greens(0.3 + 0.07*i), align='edge', width=widths,
                       label=f'Bid Level {i+1}' if i < 3 else '')
            bottom_discharge += alloc

        for i, alloc in enumerate(charge_allocations):
            axes[3].bar(times_hours[1:], -alloc[1:], bottom=-bottom_charge[1:],
                       color=plt.cm.Reds(0.3 + 0.07*i), align='edge', width=widths,
                       label=f'Ask Level {i+1}' if i < 3 else '')
            bottom_charge += alloc

        axes[3].set_ylabel('Power (MW)', color=colors['foreground4'])
        axes[3].set_title('Orderbook Level Allocation', color=colors['foreground4'])
        axes[3].legend(loc='upper right')
        axes[3].grid(True, alpha=0.3, color=colors['foreground2'])
        axes[3].axhline(0, linewidth=2, color=colors['foreground2'])

    # Plot 5: Orderbook Price Levels
    for i in range(1, 11):
        alpha = 1.0 - (i-1) * 0.08  # Fade out deeper levels
        axes[4].step(times_hours, df_orderbook[f'bid_prices[{i}]'], where='post',
                    color=plt.cm.Greens(0.4 + 0.06*i), linewidth=1, alpha=alpha,
                    label=f'Bid {i}' if i <= 3 else '')
        axes[4].step(times_hours, df_orderbook[f'ask_prices[{i}]'], where='post',
                    color=plt.cm.Reds(0.4 + 0.06*i), linewidth=1, alpha=alpha,
                    label=f'Ask {i}' if i <= 3 else '')
    axes[4].set_ylabel('Price ($/MWh)', color=colors['foreground4'])
    axes[4].set_title('Orderbook Price Levels (All 10 Levels)', color=colors['foreground4'])
    axes[4].legend(loc='upper right', fontsize=8)
    axes[4].grid(True, alpha=0.3, color=colors['foreground2'])

    # Plot 6: Orderbook Volume Levels
    for i in range(1, 11):
        alpha = 1.0 - (i-1) * 0.08  # Fade out deeper levels
        axes[5].step(times_hours, df_orderbook[f'bid_volumes[{i}]'], where='post',
                    color=plt.cm.Greens(0.4 + 0.06*i), linewidth=1, alpha=alpha,
                    label=f'Bid {i}' if i <= 3 else '')
        axes[5].step(times_hours, df_orderbook[f'ask_volumes[{i}]'], where='post',
                    color=plt.cm.Reds(0.4 + 0.06*i), linewidth=1, alpha=alpha,
                    label=f'Ask {i}' if i <= 3 else '')
    axes[5].set_ylabel('Volume (MW)', color=colors['foreground4'])
    axes[5].set_title('Orderbook Volume Levels (All 10 Levels)', color=colors['foreground4'])
    axes[5].legend(loc='upper right', fontsize=8)
    axes[5].grid(True, alpha=0.3, color=colors['foreground2'])

    # Plot 7: Cumulative Revenue (continuous accumulation, use regular plot)
    # Calculate time step for integration (assuming uniform time steps)
    if len(times_hours) > 1:
        dt = times_hours[1] - times_hours[0]
    else:
        dt = 5/60  # 5 minutes default

    cumulative_revenue = np.cumsum(revenue * dt) / 1000.0
    axes[6].plot(times_hours, cumulative_revenue, color=colors['foreground1'],
                linewidth=2)
    axes[6].set_ylabel('Cumulative Revenue (k$)', color=colors['foreground4'])
    axes[6].set_xlabel('Time (hours)', color=colors['foreground4'])
    axes[6].set_title('Cumulative Trading Revenue', color=colors['foreground4'])
    axes[6].grid(True, alpha=0.3, color=colors['foreground2'])

    plt.tight_layout()

    # Create output directory if it doesn't exist
    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    plt.savefig(output_file,
               edgecolor='none', dpi=300)
    plt.close()

    print(f"Plots saved to {output_file}")


def print_summary(csv_file='output/timeseries_export.csv',
                 price_file='input/timeseries_import.csv'):
    """Print optimization summary statistics from CSV results."""

    if not os.path.exists(csv_file):
        print(f"Error: Results file {csv_file} not found. Please run the optimization first.")
        return

    if not os.path.exists(price_file):
        print(f"Error: Price file {price_file} not found.")
        return

    df_results = pd.read_csv(csv_file)
    df_orderbook = pd.read_csv(price_file)

    # Extract variables
    soc = df_results['soc']
    charge_power = df_results['charge_power']
    discharge_power = df_results['discharge_power']

    # Calculate revenue from orderbook trading
    revenue = 0.0
    for i in range(1, 11):  # 10 orderbook levels
        if f'discharge_power_bids[{i}]' in df_results.columns:
            revenue += df_results[f'discharge_power_bids[{i}]'] * df_orderbook[f'bid_prices[{i}]']
        if f'charge_power_asks[{i}]' in df_results.columns:
            revenue -= df_results[f'charge_power_asks[{i}]'] * df_orderbook[f'ask_prices[{i}]']

    # Calculate time step (assuming 5-minute intervals)
    dt = 5/60  # hours

    total_energy_charged = np.sum(charge_power) * dt
    total_energy_discharged = np.sum(discharge_power) * dt
    total_revenue = np.sum(revenue) * dt

    # Calculate transaction costs and cycling penalty
    transaction_cost_rate = 0.05  # Same as in bess_intraday.py
    total_transaction_cost = np.sum((charge_power + discharge_power) * transaction_cost_rate) * dt

    cycling_penalty_factor = 0.1  # Same as in bess_intraday.py
    total_cycling_penalty = np.sum((charge_power + discharge_power) * cycling_penalty_factor) * dt

    # Count number of trades
    n_charge_trades = np.sum(charge_power > 0.01)
    n_discharge_trades = np.sum(discharge_power > 0.01)

    print("\n" + "="*50)
    print("BESS INTRADAY TRADING RESULTS SUMMARY")
    print("="*50)
    print(f"Total Energy Charged: {total_energy_charged:.2f} MWh")
    print(f"Total Energy Discharged: {total_energy_discharged:.2f} MWh")
    print(f"Number of Charge Trades: {n_charge_trades}")
    print(f"Number of Discharge Trades: {n_discharge_trades}")
    print(f"Total Revenue: ${total_revenue:.2f}")
    print(f"Total Transaction Costs: ${total_transaction_cost:.2f}")
    print(f"Total Cycling Penalty: ${total_cycling_penalty:.2f}")
    print(f"Net Profit: ${total_revenue - total_transaction_cost - total_cycling_penalty:.2f}")
    print(f"Initial SoC: {soc.iloc[0]:.2f} MWh")
    print(f"Final SoC: {soc.iloc[-1]:.2f} MWh")
    print("="*50)


if __name__ == "__main__":
    # Create plots and print summary
    create_plots()
    print_summary()
