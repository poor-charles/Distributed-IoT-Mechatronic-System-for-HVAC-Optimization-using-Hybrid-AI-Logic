from pathlib import Path
import subprocess
import sqlite3
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import shutil
import sys
from eppy.modeleditor import IDF
from sklearn.metrics import r2_score

PROJECT_DIR = Path(__file__).resolve().parents[1]

# --- Core Paths ---
ENERGYPLUS_EXE = r"C:\EnergyPlusV25-1-0\energyplus.exe"
IDD_PATH = r"C:\EnergyPlusV25-1-0\Energy+.idd"
IDF.setiddname(IDD_PATH)

EPW_PATH = PROJECT_DIR / "Weather" / "site.epw"
CALIBRATED_IDF = PROJECT_DIR / "results" / "calibrated_model" / "calibrated_model.idf"
RUN_DIR = PROJECT_DIR / "validation_data" / "validation_run"

# --- Validation Data Paths (Unseen Data) ---
VAL_DIR = PROJECT_DIR / "validation_data"
VAL_OCC = VAL_DIR / "val_occupancy.csv"
VAL_SET = VAL_DIR / "val_setpoint.csv"
VAL_WRK = VAL_DIR / "val_workshop_temp.csv"
VAL_MEAS_AC = VAL_DIR / "val_measured_hourly_ac.csv"
VAL_MEAS_TRH = VAL_DIR / "val_measured_temp_rh.csv"  # Target Temp/RH file

MEASURED_YEAR = 2026
ZONE_NAME = "DME-CONFE THERMAL ZONE"

# --------------------------------------------------
# Helper Functions: Generate Scientific Graphs
# --------------------------------------------------
def plot_scatter_r2(measured, simulated, title, xlabel, ylabel, filename, color):
    r2 = r2_score(measured, simulated)
    plt.figure(figsize=(8, 8))
    plt.scatter(measured, simulated, alpha=0.6, color=color, edgecolors='black', linewidth=0.5)
    
    z = np.polyfit(measured, simulated, 1)
    p = np.poly1d(z)
    plt.plot(measured, p(measured), "r--", label="Trendline")
    
    min_val = min(measured.min(), simulated.min())
    max_val = max(measured.max(), simulated.max())
    plt.plot([min_val, max_val], [min_val, max_val], "k-", alpha=0.5, label="Perfect Match (1:1)")
    
    plt.title(f"{title}\n$R^2 = {r2:.4f}$", fontsize=14)
    plt.xlabel(xlabel, fontsize=12)
    plt.ylabel(ylabel, fontsize=12)
    plt.legend()
    plt.grid(True, linestyle="--", alpha=0.7)
    plt.tight_layout()
    plt.savefig(RUN_DIR / filename, dpi=300)
    plt.close()

def plot_naked_scatter(measured, simulated, title, xlabel, ylabel, filename, color):
    plt.figure(figsize=(8, 8))
    plt.scatter(measured, simulated, alpha=0.6, color=color, edgecolors='black', linewidth=0.5)
    
    min_val = min(measured.min(), simulated.min())
    max_val = max(measured.max(), simulated.max())
    plt.plot([min_val, max_val], [min_val, max_val], "k-", alpha=0.8, linewidth=2, label="Perfect Match (1:1)")
    
    plt.title(f"{title}\n(1:1 Distribution)", fontsize=14)
    plt.xlabel(xlabel, fontsize=12)
    plt.ylabel(ylabel, fontsize=12)
    plt.legend()
    plt.grid(True, linestyle="--", alpha=0.7)
    plt.tight_layout()
    plt.savefig(RUN_DIR / filename, dpi=300)
    plt.close()

def plot_residual_histogram(measured, simulated, title, xlabel, filename, color):
    residuals = simulated - measured
    mean_res = residuals.mean()

    plt.figure(figsize=(10, 6))
    plt.hist(residuals, bins=30, color=color, alpha=0.7, edgecolor='black')
    
    plt.axvline(0, color='black', linestyle='-', linewidth=2, label='Perfect Zero Error')
    plt.axvline(mean_res, color='red', linestyle='--', linewidth=2, label=f'Mean Error: {mean_res:.2f}')
    
    plt.title(f"{title}\nError Distribution (Simulated minus Measured)", fontsize=14)
    plt.xlabel(xlabel, fontsize=12)
    plt.ylabel("Frequency (Number of Timesteps)", fontsize=12)
    plt.legend()
    plt.grid(axis='y', linestyle="--", alpha=0.7)
    plt.tight_layout()
    plt.savefig(RUN_DIR / filename, dpi=300)
    plt.close()


# --------------------------------------------------
# 1. Preparation
# --------------------------------------------------
if not CALIBRATED_IDF.exists():
    print(f"❌ Cannot find calibrated model at {CALIBRATED_IDF}")
    sys.exit(1)

if RUN_DIR.exists():
    shutil.rmtree(RUN_DIR)
RUN_DIR.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------
# 2. Inject Validation Schedules via Eppy
# --------------------------------------------------
print("Loading Calibrated Digital Twin...")
idf = IDF(str(CALIBRATED_IDF), str(EPW_PATH))

# Force the simulation to run a full year so it catches the March 12+ data
for rp in idf.idfobjects["RUNPERIOD"]:
    rp.Begin_Month = 3
    rp.Begin_Day_of_Month = 1
    rp.End_Month = 3
    rp.End_Day_of_Month = 31

for sched in idf.idfobjects["SCHEDULE:FILE"]:
    if sched.Name.upper() == "MEASURED_OCCUPANCY":
        sched.File_Name = str(VAL_OCC.resolve())
    elif sched.Name.upper() == "MEASURED_SETPOINT":
        sched.File_Name = str(VAL_SET.resolve())
    elif sched.Name.upper() == "WORKSHOP_TEMP":
        sched.File_Name = str(VAL_WRK.resolve())

val_idf_path = RUN_DIR / "validation_model.idf"
idf.saveas(str(val_idf_path))
print("Validation schedules injected successfully.")

# --------------------------------------------------
# 3. Run EnergyPlus
# --------------------------------------------------
print("Running validation simulation on unseen data...")
cmd = [ENERGYPLUS_EXE, "-w", str(EPW_PATH), "-d", str(RUN_DIR), str(val_idf_path)]
try:
    subprocess.run(cmd, check=True, capture_output=True, text=True)
except subprocess.CalledProcessError as e:
    print("EnergyPlus failed!")
    print(e.stderr)
    sys.exit(1)

# --------------------------------------------------
# 4. Extract SQL Results
# --------------------------------------------------
print("Extracting SQL results (Energy, Temp, and RH)...")
sql_path = RUN_DIR / "eplusout.sql"
con = sqlite3.connect(sql_path)

# Time definitions
time_df = pd.read_sql_query("SELECT TimeIndex, Month, Day, Hour, Minute FROM Time ORDER BY TimeIndex", con)
time_df["Hour_adj"] = time_df["Hour"] - 1
time_df["timestamp"] = pd.to_datetime(
    dict(year=MEASURED_YEAR, month=time_df["Month"], day=time_df["Day"], hour=time_df["Hour_adj"], minute=time_df["Minute"]),
    errors="coerce"
)

# Extract AC Energy
ac = pd.read_sql_query("""
    SELECT rd.TimeIndex, rd.Value
    FROM ReportData rd
    JOIN ReportDataDictionary rdd ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
    WHERE rdd.Name = 'Zone Packaged Terminal Air Conditioner Electricity Energy'
""", con)

# Extract Zone Air Temperature
temp = pd.read_sql_query("""
    SELECT rd.TimeIndex, rd.Value
    FROM ReportData rd
    JOIN ReportDataDictionary rdd ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
    WHERE rdd.Name = 'Zone Air Temperature' AND rdd.KeyValue = ?
""", con, params=[ZONE_NAME])

# Extract Zone Air Relative Humidity
rh = pd.read_sql_query("""
    SELECT rd.TimeIndex, rd.Value
    FROM ReportData rd
    JOIN ReportDataDictionary rdd ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
    WHERE rdd.Name = 'Zone Air Relative Humidity' AND rdd.KeyValue = ?
""", con, params=[ZONE_NAME])

con.close()

# --------------------------------------------------
# 5. Process Data for Comparison
# --------------------------------------------------
# AC Processing (Hourly)
ac = ac.merge(time_df[["TimeIndex", "timestamp"]], on="TimeIndex", how="left")
ac["sim_ac_kWh_total"] = ac["Value"] / 3_600_000.0
ac["sim_ac_kWh_one_unit"] = ac["sim_ac_kWh_total"] / 2.0  
ac_hourly = ac[["timestamp", "sim_ac_kWh_one_unit"]].copy()
ac_hourly["hour"] = ac_hourly["timestamp"].dt.floor("h")
sim_ac_hour = ac_hourly.groupby("hour", as_index=False)["sim_ac_kWh_one_unit"].sum()
sim_ac_hour = sim_ac_hour.rename(columns={"hour": "timestamp", "sim_ac_kWh_one_unit": "sim_ac_kWh_hour"})

# Temp/RH Processing (5-Minute Timestep)
temp = temp.merge(time_df[["TimeIndex", "timestamp"]], on="TimeIndex", how="left")
rh = rh.merge(time_df[["TimeIndex", "timestamp"]], on="TimeIndex", how="left")
sim_ts = pd.DataFrame({
    "timestamp": temp["timestamp"],
    "sim_temp": temp["Value"],
    "sim_rh": rh["Value"]
})

# --------------------------------------------------
# 6. Merge with Actual Measured Data
# --------------------------------------------------
meas_hr = pd.read_csv(VAL_MEAS_AC, parse_dates=["timestamp"])
cmp_hr = meas_hr.merge(sim_ac_hour, on="timestamp", how="inner")

meas_trh = pd.read_csv(VAL_MEAS_TRH, parse_dates=["timestamp"])
cmp_ts = meas_trh.merge(sim_ts, on="timestamp", how="inner")

if len(cmp_hr) == 0 or len(cmp_ts) == 0:
    print("❌ Error: Timestamp mismatch between validation CSV and simulation output.")
    sys.exit(1)

# --------------------------------------------------
# 7. Calculate Validation Metrics
# --------------------------------------------------
def calc_metrics(actual, sim, p=1):
    n = len(actual)
    mean_act = np.mean(actual)
    rmse = np.sqrt(np.sum((actual - sim)**2) / (n - p))
    cvrmse = (rmse / mean_act) * 100
    nmbe = (np.sum(actual - sim) / ((n - p) * mean_act)) * 100
    return rmse, cvrmse, nmbe

ac_rmse, ac_cvrmse, ac_nmbe = calc_metrics(cmp_hr["ac_kWh_hour"], cmp_hr["sim_ac_kWh_hour"])
temp_rmse, temp_cvrmse, temp_nmbe = calc_metrics(cmp_ts["temp_C"], cmp_ts["sim_temp"])
rh_rmse, rh_cvrmse, rh_nmbe = calc_metrics(cmp_ts["RH"], cmp_ts["sim_rh"])

# --------------------------------------------------
# 8. Validation Output Report & CSV Export
# --------------------------------------------------
print("\n" + "="*55)
print("🏆 DIGITAL TWIN VALIDATION REPORT (UNSEEN DATA) 🏆")
print("="*55)
print(f"Validation Target Data Range: {len(cmp_hr)} Hours")

print("\n--- 1. AC Energy Validation (ASHRAE Guideline 14) ---")
print(f"NMBE:   {ac_nmbe:7.2f}%  (Target: < ±15%)")
print(f"CVRMSE: {ac_cvrmse:7.2f}%  (Target: < 30%)")

print("\n--- 2. Thermodynamic Validation (Timestep) ---")
print(f"Temperature CVRMSE: {temp_cvrmse:7.2f}%  (RMSE: {temp_rmse:.2f}°C)")
print(f"Humidity CVRMSE:    {rh_cvrmse:7.2f}%  (RMSE: {rh_rmse:.2f}%)")
print("-" * 55)

if abs(ac_nmbe) <= 15 and ac_cvrmse <= 30:
    print("✅ STATUS: VALIDATED!")
    print("The model successfully predicts unseen physics and meets ASHRAE standards.")
else:
    print("❌ STATUS: NOT VALIDATED.")
    print("The model exceeds ASHRAE thresholds on unseen data.")
print("="*55)

# Export Metrics to CSV
metrics_df = pd.DataFrame({
    "Metric": ["AC Consumption (Hourly)", "Temperature", "Relative Humidity"],
    "RMSE": [ac_rmse, temp_rmse, rh_rmse],
    "CVRMSE (%)": [ac_cvrmse, temp_cvrmse, rh_cvrmse],
    "NMBE (%)": [ac_nmbe, temp_nmbe, rh_nmbe]
})
metrics_df.to_csv(RUN_DIR / "validation_metrics_summary.csv", index=False)
print("✅ Validation metrics saved to CSV.")

# --------------------------------------------------
# 9. Save CSVs and Auto-Generate Plots
# --------------------------------------------------
print("Saving output files and generating all plots...")
cmp_hr.to_csv(RUN_DIR / "final_val_comparison_ac.csv", index=False)
cmp_ts.to_csv(RUN_DIR / "final_val_comparison_temp_rh.csv", index=False)

# Plot 1: Temperature Time Series
plt.figure(figsize=(10, 5))
plt.plot(cmp_ts["timestamp"], cmp_ts["temp_C"], label="Actual Measured Temp", color="blue")
plt.plot(cmp_ts["timestamp"], cmp_ts["sim_temp"], label="Simulated Digital Twin Temp", color="orange", linestyle="--")
plt.title("Validation Phase: Classroom Inside Temperature")
plt.ylabel("Temperature (°C)")
plt.legend()
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig(RUN_DIR / "Validation_Plot_Temp_TimeSeries.png")
plt.close()

# Plot 2: Relative Humidity Time Series
plt.figure(figsize=(10, 5))
plt.plot(cmp_ts["timestamp"], cmp_ts["RH"], label="Actual Measured RH", color="teal")
plt.plot(cmp_ts["timestamp"], cmp_ts["sim_rh"], label="Simulated Digital Twin RH", color="red", linestyle="--")
plt.title("Validation Phase: Classroom Relative Humidity")
plt.ylabel("Relative Humidity (%)")
plt.legend()
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig(RUN_DIR / "Validation_Plot_RH_TimeSeries.png")
plt.close()

# Plot 3: Hourly AC Energy Time Series
plt.figure(figsize=(10, 5))
plt.plot(cmp_hr["timestamp"], cmp_hr["ac_kWh_hour"], label="Actual AC kWh", color="green", marker="o")
plt.plot(cmp_hr["timestamp"], cmp_hr["sim_ac_kWh_hour"], label="Simulated AC kWh", color="purple", linestyle="--", marker="x")
plt.title("Validation Phase: Hourly AC Electricity Consumption")
plt.ylabel("Electricity (kWh)")
plt.legend()
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig(RUN_DIR / "Validation_Plot_AC_TimeSeries.png")
plt.close()

# --- SCATTER PLOTS & HISTOGRAMS ---

# Temp (Naked + Histogram)
plot_naked_scatter(cmp_ts["temp_C"], cmp_ts["sim_temp"], "Validation: Temp", "Measured Temp (°C)", "Simulated Temp (°C)", "val_scatter_temp.png", "orange")
plot_residual_histogram(cmp_ts["temp_C"], cmp_ts["sim_temp"], "Validation: Temp Residuals", "Residual Error (°C)", "val_hist_temp.png", "orange")

# RH (Naked + Histogram)
plot_naked_scatter(cmp_ts["RH"], cmp_ts["sim_rh"], "Validation: RH", "Measured RH (%)", "Simulated RH (%)", "val_scatter_rh.png", "red")
plot_residual_histogram(cmp_ts["RH"], cmp_ts["sim_rh"], "Validation: RH Residuals", "Residual Error (%)", "val_hist_rh.png", "red")

# AC Consumption (R2 + Histogram)
plot_scatter_r2(cmp_hr["ac_kWh_hour"], cmp_hr["sim_ac_kWh_hour"], "Validation: AC Consumption", "Measured AC (kWh)", "Simulated AC (kWh)", "val_scatter_ac.png", "purple")
plot_residual_histogram(cmp_hr["ac_kWh_hour"], cmp_hr["sim_ac_kWh_hour"], "Validation: AC Residuals", "Residual Error (kWh)", "val_hist_ac.png", "purple")

print(f"✅ All plots, histograms, and CSVs saved in: {RUN_DIR}")
