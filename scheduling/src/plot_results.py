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
                output_file='output/bess_optimisation_results.png'):
    """Create visualization plots from the exported CSV results."""
    
    # Set up plotting style
    colors = setup_plot_style()
    
    # Read the optimization results CSV file
    if not os.path.exists(csv_file):
        print(f"Error: Results file {csv_file} not found. Please run the optimization first.")
        return
    
    df_results = pd.read_csv(csv_file)
    
    # Read the electricity price from the input file
    if not os.path.exists(price_file):
        print(f"Error: Price file {price_file} not found.")
        return
    
    df_prices = pd.read_csv(price_file)
    
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
    
    # Extract electricity price from input file
    price = df_prices['price']
    
    # Calculate revenue (now done in Python, not Modelica)
    revenue = net_power * price
    
    # Create figure with subplots
    fig, axes = plt.subplots(4, 1, figsize=(12, 10))
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
    
    # Plot 3: Electricity Price (step function - prices valid until next timestamp)
    axes[2].step(times_hours, price, where='post', color=colors['foreground1'], 
                linewidth=2)
    axes[2].set_ylabel('Price ($/MWh)', color=colors['foreground4'])
    axes[2].set_title('Electricity Price', color=colors['foreground4'])
    axes[2].grid(True, alpha=0.3, color=colors['foreground2'])
    
    # Plot 4: Cumulative Revenue (continuous accumulation, use regular plot)
    # Calculate time step for integration (assuming uniform time steps)
    if len(times_hours) > 1:
        dt = times_hours[1] - times_hours[0]
    else:
        dt = 5/60  # 5 minutes default
    
    cumulative_revenue = np.cumsum(revenue * dt) / 1000.0
    axes[3].plot(times_hours, cumulative_revenue, color=colors['foreground1'], 
                linewidth=2)
    axes[3].set_ylabel('Cumulative Revenue (k$)', color=colors['foreground4'])
    axes[3].set_xlabel('Time (hours)', color=colors['foreground4'])
    axes[3].set_title('Cumulative Revenue', color=colors['foreground4'])
    axes[3].grid(True, alpha=0.3, color=colors['foreground2'])
    
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
    df_prices = pd.read_csv(price_file)
    
    # Extract variables
    soc = df_results['soc']
    charge_power = df_results['charge_power']
    discharge_power = df_results['discharge_power']
    net_power = df_results['net_power']
    price = df_prices['price']
    
    # Calculate revenue (now done in Python)
    revenue = net_power * price
    
    # Calculate time step (assuming 5-minute intervals)
    dt = 5/60  # hours
    
    total_energy_charged = np.sum(charge_power) * dt
    total_energy_discharged = np.sum(discharge_power) * dt
    total_revenue = np.sum(revenue) * dt
    
    # Calculate cycling penalty
    cycling_penalty_factor = 0.1  # Same as in bess.py
    total_cycling_penalty = np.sum((charge_power + discharge_power) * cycling_penalty_factor) * dt
    
    print("\n" + "="*50)
    print("BESS OPTIMIZATION RESULTS SUMMARY")
    print("="*50)
    print(f"Total Energy Charged: {total_energy_charged:.2f} MWh")
    print(f"Total Energy Discharged: {total_energy_discharged:.2f} MWh")
    print(f"Total Revenue: ${total_revenue:.2f}")
    print(f"Total Cycling Penalty: ${total_cycling_penalty:.2f}")
    print(f"Net Profit: ${total_revenue - total_cycling_penalty:.2f}")
    print(f"Initial SoC: {soc.iloc[0]:.2f} MWh")
    print(f"Final SoC: {soc.iloc[-1]:.2f} MWh")
    print("="*50)


if __name__ == "__main__":
    # Create plots and print summary
    create_plots()
    print_summary()
