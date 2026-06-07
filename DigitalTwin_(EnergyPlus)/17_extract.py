import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
import subprocess
import sqlite3
import sys
import shutil
from sklearn.metrics import r2_score

# ---------------------------------------------------
# 1. Paths and Setup
# ---------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parents[1]

MEAS_TS_PATH = PROJECT_DIR / "actual_data" / "measured_timestep.csv"
MEAS_HR_PATH = PROJECT_DIR / "actual_data" / "measured_hourly_ac.csv"
HISTORY_PATH = PROJECT_DIR / "results" / "bayesian_scoring.csv"  
RUN_DIR = PROJECT_DIR / "results" / "current_run"

EXTRACTION_DIR = PROJECT_DIR / "results" / "final_extraction"
EXTRACTION_DIR.mkdir(parents=True, exist_ok=True)

RUN_SCRIPT = PROJECT_DIR / "Scripts" / "11c_run_sim.py"
SCORE_SCRIPT = PROJECT_DIR / "Scripts" / "12b_scoring.py"
PARAM_FILE = PROJECT_DIR / "calibration" / "current_params.csv"

# ---------------------------------------------------
# Helper Function: Extract Data Directly from SQL
# ---------------------------------------------------
def extract_sql_data(prefix):
    con = sqlite3.connect(RUN_DIR / "eplusout.sql")
    
    temp = pd.read_sql_query(f"SELECT rd.TimeIndex, rd.Value as {prefix}_temp FROM ReportData rd JOIN ReportDataDictionary rdd ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex WHERE rdd.Name = 'Zone Air Temperature'", con)
    rh = pd.read_sql_query(f"SELECT rd.TimeIndex, rd.Value as {prefix}_rh FROM ReportData rd JOIN ReportDataDictionary rdd ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex WHERE rdd.Name = 'Zone Air Relative Humidity'", con)
    ac = pd.read_sql_query("SELECT rd.TimeIndex, rd.Value FROM ReportData rd JOIN ReportDataDictionary rdd ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex WHERE rdd.Name = 'Zone Packaged Terminal Air Conditioner Electricity Energy'", con)
    
    time_df = pd.read_sql_query("SELECT TimeIndex, Month, Day, Hour, Minute FROM Time ORDER BY TimeIndex", con)
    time_df["Hour_adj"] = time_df["Hour"] - 1
    time_df["timestamp"] = pd.to_datetime(dict(year=2026, month=time_df["Month"], day=time_df["Day"], hour=time_df["Hour_adj"], minute=time_df["Minute"]), errors="coerce")
    con.close()

    # Format TS
    ts = temp.merge(time_df[["TimeIndex", "timestamp"]], on="TimeIndex").merge(rh, on="TimeIndex")
    ts = ts[["timestamp", f"{prefix}_temp", f"{prefix}_rh"]]

    # Format Hourly AC
    ac = ac.merge(time_df[["TimeIndex", "timestamp"]], on="TimeIndex")
    ac[f"{prefix}_ac_kWh"] = (ac["Value"] / 3_600_000.0) / 2.0
    ac["hour"] = ac["timestamp"].dt.floor("h")
    ac_hr = ac.groupby("hour", as_index=False)[f"{prefix}_ac_kWh"].sum().rename(columns={"hour": "timestamp"})
    
    return ts, ac_hr

# ---------------------------------------------------
# Helper Functions: Generate Scientific Graphs
# ---------------------------------------------------
def plot_scatter_r2(measured, simulated, title, xlabel, ylabel, filename, color):
    # Calculate R2 using standard scikit-learn metrics
    r2 = r2_score(measured, simulated)
    
    plt.figure(figsize=(8, 8))
    plt.scatter(measured, simulated, alpha=0.6, color=color, edgecolors='black', linewidth=0.5)
    
    # Calculate and plot line of best fit
    z = np.polyfit(measured, simulated, 1)
    p = np.poly1d(z)
    plt.plot(measured, p(measured), "r--", label="Trendline")
    
    # Plot perfect 1:1 match line for reference
    min_val = min(measured.min(), simulated.min())
    max_val = max(measured.max(), simulated.max())
    plt.plot([min_val, max_val], [min_val, max_val], "k-", alpha=0.5, label="Perfect Match (1:1)")
    
    plt.title(f"{title}\n$R^2 = {r2:.4f}$", fontsize=14)
    plt.xlabel(xlabel, fontsize=12)
    plt.ylabel(ylabel, fontsize=12)
    plt.legend()
    plt.grid(True, linestyle="--", alpha=0.7)
    plt.tight_layout()
    plt.savefig(EXTRACTION_DIR / filename, dpi=300)
    plt.close()

def plot_naked_scatter(measured, simulated, title, xlabel, ylabel, filename, color):
    plt.figure(figsize=(8, 8))
    plt.scatter(measured, simulated, alpha=0.6, color=color, edgecolors='black', linewidth=0.5)
    
    # Plot perfect 1:1 match line for reference (No R2, no trendline)
    min_val = min(measured.min(), simulated.min())
    max_val = max(measured.max(), simulated.max())
    plt.plot([min_val, max_val], [min_val, max_val], "k-", alpha=0.8, linewidth=2, label="Perfect Match (1:1)")
    
    plt.title(f"{title}\n(1:1 Distribution)", fontsize=14)
    plt.xlabel(xlabel, fontsize=12)
    plt.ylabel(ylabel, fontsize=12)
    plt.legend()
    plt.grid(True, linestyle="--", alpha=0.7)
    plt.tight_layout()
    plt.savefig(EXTRACTION_DIR / filename, dpi=300)
    plt.close()

def plot_residual_histogram(measured, simulated, title, xlabel, filename, color):
    residuals = simulated - measured
    mean_res = residuals.mean()

    plt.figure(figsize=(10, 6))
    plt.hist(residuals, bins=30, color=color, alpha=0.7, edgecolor='black')
    
    # Highlight the zero error line and the actual mean error
    plt.axvline(0, color='black', linestyle='-', linewidth=2, label='Perfect Zero Error')
    plt.axvline(mean_res, color='red', linestyle='--', linewidth=2, label=f'Mean Error: {mean_res:.2f}')
    
    plt.title(f"{title}\nError Distribution (Simulated minus Measured)", fontsize=14)
    plt.xlabel(xlabel, fontsize=12)
    plt.ylabel("Frequency (Number of Timesteps)", fontsize=12)
    plt.legend()
    plt.grid(axis='y', linestyle="--", alpha=0.7)
    plt.tight_layout()
    plt.savefig(EXTRACTION_DIR / filename, dpi=300)
    plt.close()

# ---------------------------------------------------
# 2. Load Real Data & Identify True Top Model
# ---------------------------------------------------
print("Loading measured data and full Bayesian history...")
meas_ts = pd.read_csv(MEAS_TS_PATH, parse_dates=["timestamp"])
meas_hr = pd.read_csv(MEAS_HR_PATH, parse_dates=["timestamp"])

# Load FULL history
history_df = pd.read_csv(HISTORY_PATH)

# STRICT ASHRAE FILTER: CVRMSE < 30 AND |NMBE| < 10
valid_df = history_df[(history_df["ac_cvrmse"] < 30) & (history_df["ac_nmbe"].abs() < 10)]

if valid_df.empty:
    print("\n⚠️ WARNING: Out of all iterations, NO model passed BOTH CVRMSE < 30 and |NMBE| < 10!")
    print("Falling back to the model with the lowest CVRMSE...")
    valid_df = history_df

# Sort only the valid models to find the real Top 10
top10_df = valid_df.sort_values(by="ac_cvrmse", ascending=True).head(10)

# Save the true top 10 for your records
top10_df.to_csv(EXTRACTION_DIR / "true_top10_models.csv", index=False)
print(f"✅ Saved 'true_top10_models.csv' to the extraction folder.")

top1_iteration = int(top10_df.iloc[0]["iteration"])
print(f"🏆 True Champion Found! Iteration {top1_iteration} with AC CVRMSE: {top10_df.iloc[0]['ac_cvrmse']:.2f}% and NMBE: {top10_df.iloc[0]['ac_nmbe']:.2f}%")

# ---------------------------------------------------
# 3. Run Base Model (Uncalibrated)
# ---------------------------------------------------
print("\nRunning Uncalibrated Base Model...")
base_params = {
    "lighting_multiplier": 1.0,
    "equipment_multiplier": 1.0,
    "cop": 3.0, 
    "occupancy_multiplier": 1.0,
    "cooling_capacity_multiplier": 1.0,
    "fan_flow_multiplier": 1.0
}
pd.DataFrame([base_params]).to_csv(PARAM_FILE, index=False)
subprocess.run([sys.executable, str(RUN_SCRIPT)], check=True)
subprocess.run([sys.executable, str(SCORE_SCRIPT)], check=True)

base_ts, base_ac_hr = extract_sql_data("base")

# ---------------------------------------------------
# 4. Run Top 1 Calibrated Model & Copy IDF
# ---------------------------------------------------
print(f"\nRunning True Champion Model (Iteration {top1_iteration})...")
cal_params = {
    "lighting_multiplier": top10_df.iloc[0]["lighting_multiplier"],
    "equipment_multiplier": top10_df.iloc[0]["equipment_multiplier"],
    "cop": top10_df.iloc[0]["cop"],
    "occupancy_multiplier": top10_df.iloc[0]["occupancy_multiplier"],
    "cooling_capacity_multiplier": top10_df.iloc[0]["cooling_capacity_multiplier"],
    "fan_flow_multiplier": top10_df.iloc[0]["fan_flow_multiplier"]
}
pd.DataFrame([cal_params]).to_csv(PARAM_FILE, index=False)
subprocess.run([sys.executable, str(RUN_SCRIPT)], check=True)
subprocess.run([sys.executable, str(SCORE_SCRIPT)], check=True)

cal_ts, cal_hr = extract_sql_data("cal")

# ---> NEW: Copy the Champion IDF to the extraction folder <---
champion_idf_source = RUN_DIR / "model.idf"
champion_idf_dest = EXTRACTION_DIR / f"champion_model_iter_{top1_iteration}.idf"

if champion_idf_source.exists():
    shutil.copy(champion_idf_source, champion_idf_dest)
    print(f"✅ Saved Champion IDF to: {champion_idf_dest}")
else:
    print(f"⚠️ Warning: Could not find the IDF file at {champion_idf_source}")

# ---------------------------------------------------
# 5. Merge and Export CSVs
# ---------------------------------------------------
print("\nMerging and saving consolidated CSVs...")
final_ts = meas_ts.merge(base_ts, on="timestamp", how="inner").merge(cal_ts, on="timestamp", how="inner")
final_ts.to_csv(EXTRACTION_DIR / "comparison_temp_rh_5min.csv", index=False)

final_hr = meas_hr.merge(base_ac_hr, on="timestamp", how="inner").merge(cal_hr, on="timestamp", how="inner")
final_hr.to_csv(EXTRACTION_DIR / "comparison_ac_hourly.csv", index=False)

# ---------------------------------------------------
# 6. Generate Comparison Graphs (Premium Style & Scatter)
# ---------------------------------------------------
print("Generating premium graphs, naked scatters, and histograms...")

# --- TIME SERIES PLOTS ---

# Plot Temperature
plt.figure(figsize=(12, 6))
plt.plot(final_ts["timestamp"], final_ts["temp_C"], label="Measured Temp", color="black", linewidth=1.5)
plt.plot(final_ts["timestamp"], final_ts["base_temp"], label="Base Model (Uncalibrated)", color="blue", alpha=0.7)
plt.plot(final_ts["timestamp"], final_ts["cal_temp"], label="Calibrated Model", color="orange", alpha=0.9)
plt.title("Room Temperature: Measured vs Base vs Calibrated")
plt.xlabel("Time")
plt.ylabel("Temperature (°C)")
plt.legend()
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig(EXTRACTION_DIR / "graph_temperature_comparison.png", dpi=300)
plt.close()

# Plot Relative Humidity
plt.figure(figsize=(12, 6))
plt.plot(final_ts["timestamp"], final_ts["RH"], label="Measured RH", color="black", linewidth=1.5)
plt.plot(final_ts["timestamp"], final_ts["base_rh"], label="Base Model (Uncalibrated)", color="red", alpha=0.7)
plt.plot(final_ts["timestamp"], final_ts["cal_rh"], label="Calibrated Model", color="teal", alpha=0.9)
plt.title("Relative Humidity: Measured vs Base vs Calibrated")
plt.xlabel("Time")
plt.ylabel("Relative Humidity (%)")
plt.legend()
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig(EXTRACTION_DIR / "graph_rh_comparison.png", dpi=300)
plt.close()

# Plot AC Consumption
plt.figure(figsize=(12, 6))
plt.plot(final_hr["timestamp"], final_hr["ac_kWh_hour"], label="Measured AC", color="black", linewidth=1.5)
plt.plot(final_hr["timestamp"], final_hr["base_ac_kWh"], label="Base Model (Uncalibrated)", color="purple", alpha=0.7)
plt.plot(final_hr["timestamp"], final_hr["cal_ac_kWh"], label="Calibrated Model", color="green", alpha=0.9)
plt.title("AC Consumption: Measured vs Base vs Calibrated")
plt.xlabel("Time")
plt.ylabel("Electricity (kWh)")
plt.legend()
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig(EXTRACTION_DIR / "graph_ac_comparison.png", dpi=300)
plt.close()


# --- NAKED SCATTER PLOTS (For Temp and RH only) ---
plot_naked_scatter(final_ts["temp_C"], final_ts["base_temp"], "Base Model: Temp Calibration", "Measured Temp (°C)", "Simulated Temp (°C)", "scatter_base_temp.png", "blue")
plot_naked_scatter(final_ts["RH"], final_ts["base_rh"], "Base Model: RH Calibration", "Measured RH (%)", "Simulated RH (%)", "scatter_base_rh.png", "red")
plot_naked_scatter(final_ts["temp_C"], final_ts["cal_temp"], "Calibrated Model: Temp Calibration", "Measured Temp (°C)", "Simulated Temp (°C)", "scatter_cal_temp.png", "orange")
plot_naked_scatter(final_ts["RH"], final_ts["cal_rh"], "Calibrated Model: RH Calibration", "Measured RH (%)", "Simulated RH (%)", "scatter_cal_rh.png", "teal")

# --- FULL R2 SCATTER PLOTS (For AC only) ---
plot_scatter_r2(final_hr["ac_kWh_hour"], final_hr["base_ac_kWh"], "Base Model: AC Calibration", "Measured AC (kWh)", "Simulated AC (kWh)", "scatter_base_ac.png", "purple")
plot_scatter_r2(final_hr["ac_kWh_hour"], final_hr["cal_ac_kWh"], "Calibrated Model: AC Calibration", "Measured AC (kWh)", "Simulated AC (kWh)", "scatter_cal_ac.png", "green")

# --- RESIDUAL HISTOGRAMS (Error Distributions) ---
plot_residual_histogram(final_ts["temp_C"], final_ts["cal_temp"], "Calibrated Model: Temp Residuals", "Residual Error (°C)", "hist_cal_temp.png", "orange")
plot_residual_histogram(final_ts["RH"], final_ts["cal_rh"], "Calibrated Model: RH Residuals", "Residual Error (%)", "hist_cal_rh.png", "teal")
plot_residual_histogram(final_hr["ac_kWh_hour"], final_hr["cal_ac_kWh"], "Calibrated Model: AC Residuals", "Residual Error (kWh)", "hist_cal_ac.png", "green")

print(f"\n✅ Success! All data and graphs have been saved to: {EXTRACTION_DIR}")