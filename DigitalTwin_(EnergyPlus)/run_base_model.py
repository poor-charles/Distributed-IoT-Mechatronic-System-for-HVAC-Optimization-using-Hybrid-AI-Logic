"""
Run and assess the uncalibrated/base EnergyPlus model.

Exports:
  - results/base_model_assessment/base_model_metrics.csv
  - results/base_model_assessment/base_model_temp_rh_5min_sim_vs_measured.csv
  - results/base_model_assessment/base_model_ac_hourly_sim_vs_measured.csv

This script assumes your existing run script reads:
  calibration/current_params.csv
and writes:
  results/current_run/eplusout.sql
"""

from pathlib import Path
import subprocess
import sqlite3
import sys

import numpy as np
import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parents[1]

MEAS_TS_PATH = PROJECT_DIR / "actual_data" / "measured_timestep.csv"
MEAS_HR_PATH = PROJECT_DIR / "actual_data" / "measured_hourly_ac.csv"

RUN_DIR = PROJECT_DIR / "results" / "current_run"
RUN_SCRIPT = PROJECT_DIR / "Scripts" / "11c_run_sim.py"
PARAM_FILE = PROJECT_DIR / "calibration" / "current_params.csv"

OUT_DIR = PROJECT_DIR / "results" / "base_model_assessment"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# Update these values if your methodology uses different baseline assumptions.
BASE_PARAMS = {
    "lighting_multiplier": 1.0,
    "equipment_multiplier": 1.0,
    "cop": 3.2,
    "occupancy_multiplier": 1.0,
    "cooling_capacity_multiplier": 1.0,
    "fan_flow_multiplier": 1.0,
}


def calculate_metrics(measured, simulated, p=1):
    """Return RMSE, CVRMSE, NMBE, and R².

    NMBE convention:
        NMBE = sum(measured - simulated) / ((n - p) * mean(measured)) * 100

    Positive NMBE means model underprediction.
    Negative NMBE means model overprediction.
    """
    measured = np.asarray(measured, dtype=float)
    simulated = np.asarray(simulated, dtype=float)

    valid = np.isfinite(measured) & np.isfinite(simulated)
    measured = measured[valid]
    simulated = simulated[valid]

    n = len(measured)
    if n <= p:
        return np.nan, np.nan, np.nan, np.nan

    mean_measured = np.mean(measured)
    if mean_measured == 0:
        return np.nan, np.nan, np.nan, np.nan

    rmse = np.sqrt(np.sum((measured - simulated) ** 2) / (n - p))
    cvrmse = (rmse / mean_measured) * 100
    nmbe = (np.sum(measured - simulated) / ((n - p) * mean_measured)) * 100

    ss_res = np.sum((measured - simulated) ** 2)
    ss_tot = np.sum((measured - mean_measured) ** 2)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot != 0 else np.nan

    return rmse, cvrmse, nmbe, r2


def read_report_variable(con, variable_name, alias):
    query = """
        SELECT
            rd.TimeIndex,
            rd.Value AS value
        FROM ReportData rd
        JOIN ReportDataDictionary rdd
            ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
        WHERE rdd.Name = ?
    """
    df = pd.read_sql_query(query, con, params=[variable_name])
    return df.rename(columns={"value": alias})


def extract_base_sql_data():
    sql_path = RUN_DIR / "eplusout.sql"
    if not sql_path.exists():
        raise FileNotFoundError(f"EnergyPlus SQL output not found: {sql_path}")

    con = sqlite3.connect(sql_path)

    temp = read_report_variable(con, "Zone Air Temperature", "base_temp_C")
    rh = read_report_variable(con, "Zone Air Relative Humidity", "base_RH")
    ac = read_report_variable(con, "Zone Packaged Terminal Air Conditioner Electricity Energy", "base_ac_J")

    time_df = pd.read_sql_query(
        "SELECT TimeIndex, Month, Day, Hour, Minute FROM Time ORDER BY TimeIndex",
        con
    )
    con.close()

    # EnergyPlus Hour is commonly reported as 1-24. Convert to 0-23.
    time_df["Hour_adj"] = time_df["Hour"] - 1
    time_df["timestamp"] = pd.to_datetime(
        dict(
            year=2026,
            month=time_df["Month"],
            day=time_df["Day"],
            hour=time_df["Hour_adj"],
            minute=time_df["Minute"],
        ),
        errors="coerce"
    )

    ts = (
        time_df[["TimeIndex", "timestamp"]]
        .merge(temp, on="TimeIndex", how="inner")
        .merge(rh, on="TimeIndex", how="inner")
        .dropna(subset=["timestamp"])
    )
    ts = ts[["timestamp", "base_temp_C", "base_RH"]]

    ac = time_df[["TimeIndex", "timestamp"]].merge(ac, on="TimeIndex", how="inner")

    # Keep the same convention as your extraction script: divide by 2 because
    # two PTAC objects are returned in the SQL output.
    ac["base_ac_kWh_timestep"] = (ac["base_ac_J"] / 3_600_000.0) / 2.0
    ac["hour"] = ac["timestamp"].dt.floor("h")

    ac_hr = (
        ac.groupby("hour", as_index=False)["base_ac_kWh_timestep"]
        .sum()
        .rename(columns={
            "hour": "timestamp",
            "base_ac_kWh_timestep": "base_ac_kWh_hour",
        })
    )

    return ts, ac_hr


def run_base_model():
    print("Writing base model parameters...")
    pd.DataFrame([BASE_PARAMS]).to_csv(PARAM_FILE, index=False)

    print("Running base model EnergyPlus simulation...")
    subprocess.run([sys.executable, str(RUN_SCRIPT)], check=True)


def main():
    print("=" * 70)
    print("BASE MODEL ASSESSMENT")
    print("=" * 70)

    meas_ts = pd.read_csv(MEAS_TS_PATH, parse_dates=["timestamp"])
    meas_hr = pd.read_csv(MEAS_HR_PATH, parse_dates=["timestamp"])

    run_base_model()

    base_ts, base_ac_hr = extract_base_sql_data()

    temp_rh_compare = (
        meas_ts[["timestamp", "temp_C", "RH"]]
        .merge(base_ts, on="timestamp", how="inner")
        .sort_values("timestamp")
    )

    ac_compare = (
        meas_hr[["timestamp", "ac_kWh_hour"]]
        .merge(base_ac_hr, on="timestamp", how="inner")
        .sort_values("timestamp")
    )

    temp_rh_csv = OUT_DIR / "base_model_temp_rh_5min_sim_vs_measured.csv"
    ac_csv = OUT_DIR / "base_model_ac_hourly_sim_vs_measured.csv"
    temp_rh_compare.to_csv(temp_rh_csv, index=False)
    ac_compare.to_csv(ac_csv, index=False)

    t_rmse, t_cvrmse, t_nmbe, t_r2 = calculate_metrics(
        temp_rh_compare["temp_C"],
        temp_rh_compare["base_temp_C"],
    )
    rh_rmse, rh_cvrmse, rh_nmbe, rh_r2 = calculate_metrics(
        temp_rh_compare["RH"],
        temp_rh_compare["base_RH"],
    )
    ac_rmse, ac_cvrmse, ac_nmbe, ac_r2 = calculate_metrics(
        ac_compare["ac_kWh_hour"],
        ac_compare["base_ac_kWh_hour"],
    )

    metrics = pd.DataFrame({
        "Variable": [
            "Indoor Temperature (degC)",
            "Indoor Relative Humidity (%)",
            "Hourly AC Energy (kWh)",
        ],
        "RMSE": [t_rmse, rh_rmse, ac_rmse],
        "CVRMSE (%)": [t_cvrmse, rh_cvrmse, ac_cvrmse],
        "NMBE (%)": [t_nmbe, rh_nmbe, ac_nmbe],
        "R-Squared": [t_r2, rh_r2, ac_r2],
    })

    metrics_csv = OUT_DIR / "base_model_metrics.csv"
    metrics.to_csv(metrics_csv, index=False)

    print("\nBase Model Performance Metrics:")
    print(metrics.to_string(index=False))
    print(f"\nSaved metrics CSV: {metrics_csv}")
    print(f"Saved timestep comparison CSV: {temp_rh_csv}")
    print(f"Saved hourly AC comparison CSV: {ac_csv}")
    print("\nDone.")


if __name__ == "__main__":
    main()



def plot_time_series(
    plt.plot(
    df[x_col],
    df[measured_col],
    label="Measured",
    color="black",
    linewidth=1.5
    
):

    df,
    x_col,
    measured_col,
    simulated_col,
    title,
    ylabel,
    filename,
    sim_color
)





    plot_time_series(
    ac_compare,
    "timestamp",
    "ac_kWh_hour",
    "base_ac_kWh",
    "Base Model AC Energy Consumption: Measured vs Simulated",
    "AC Energy Consumption (kWh)",
    "base_model_ac_energy_timeseries.png",
    "purple"
    )

    plot_time_series(
    temp_rh_compare,
    "timestamp",
    "RH",
    "base_rh",
    "Base Model Indoor Relative Humidity: Measured vs Simulated",
    "Relative Humidity (%)",
    "base_model_rh_timeseries.png",
    "red"
    )

    plot_time_series(
    temp_rh_compare,
    "timestamp",
    "temp_C",
    "base_temp",
    "Base Model Indoor Temperature: Measured vs Simulated",
    "Indoor Temperature (°C)",
    "base_model_temperature_timeseries.png",
    "blue"
    )