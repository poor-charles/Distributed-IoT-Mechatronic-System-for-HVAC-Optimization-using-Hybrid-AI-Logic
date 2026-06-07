from pathlib import Path
import subprocess
import sqlite3
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import shutil
import sys
from eppy.modeleditor import IDF

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
VAL_MEAS_TRH = VAL_DIR / "val_measured_temp_rh.csv"  # NEW: Target Temp/RH file

MEASURED_YEAR = 2026
ZONE_NAME = "DME-CONFE THERMAL ZONE"

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

# NEW: Extract Zone Air Temperature
temp = pd.read_sql_query("""
    SELECT rd.TimeIndex, rd.Value
    FROM ReportData rd
    JOIN ReportDataDictionary rdd ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
    WHERE rdd.Name = 'Zone Air Temperature' AND rdd.KeyValue = ?
""", con, params=[ZONE_NAME])

# NEW: Extract Zone Air Relative Humidity
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

# NEW: Temp/RH Processing (5-Minute Timestep)
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
# 8. Validation Output Report
# --------------------------------------------------
print("\n" + "="*55)
print("🏆 DIGITAL TWIN VALIDATION REPORT (UNSEEN DATA) 🏆")
print("="*55)
print(f"Validation Target Data Range: {len(cmp_hr)} Hours")

print("\n--- 1. AC Energy Validation (ASHRAE Guideline 14) ---")
print(f"NMBE:   {ac_nmbe:7.2f}%  (Target: < ±10%)")
print(f"CVRMSE: {ac_cvrmse:7.2f}%  (Target: < 30%)")

print("\n--- 2. Thermodynamic Validation (Timestep) ---")
print(f"Temperature CVRMSE: {temp_cvrmse:7.2f}%  (RMSE: {temp_rmse:.2f}°C)")
print(f"Humidity CVRMSE:    {rh_cvrmse:7.2f}%  (RMSE: {rh_rmse:.2f}%)")
print("-" * 55)

if abs(ac_nmbe) <= 10 and ac_cvrmse <= 30:
    print("✅ STATUS: VALIDATED!")
    print("The model successfully predicts unseen physics and meets ASHRAE standards.")
else:
    print("❌ STATUS: NOT VALIDATED.")
    print("The model exceeds ASHRAE thresholds on unseen data.")
print("="*55)

# --------------------------------------------------
# 9. Save CSVs and Auto-Generate Plots
# --------------------------------------------------
print("Saving output files and generating plots...")
cmp_hr.to_csv(RUN_DIR / "final_val_comparison_ac.csv", index=False)
cmp_ts.to_csv(RUN_DIR / "final_val_comparison_temp_rh.csv", index=False)

# Plot 1: Temperature
plt.figure(figsize=(10, 5))
plt.plot(cmp_ts["timestamp"], cmp_ts["temp_C"], label="Actual Measured Temp", color="blue")
plt.plot(cmp_ts["timestamp"], cmp_ts["sim_temp"], label="Simulated Digital Twin Temp", color="orange", linestyle="--")
plt.title("Validation Phase: Classroom Inside Temperature")
plt.ylabel("Temperature (°C)")
plt.legend()
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig(RUN_DIR / "Validation_Plot_Temp.png")
plt.close()

# Plot 2: Relative Humidity
plt.figure(figsize=(10, 5))
plt.plot(cmp_ts["timestamp"], cmp_ts["RH"], label="Actual Measured RH", color="teal")
plt.plot(cmp_ts["timestamp"], cmp_ts["sim_rh"], label="Simulated Digital Twin RH", color="red", linestyle="--")
plt.title("Validation Phase: Classroom Relative Humidity")
plt.ylabel("Relative Humidity (%)")
plt.legend()
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig(RUN_DIR / "Validation_Plot_RH.png")
plt.close()

# Plot 3: Hourly AC Energy
plt.figure(figsize=(10, 5))
plt.plot(cmp_hr["timestamp"], cmp_hr["ac_kWh_hour"], label="Actual AC kWh", color="green", marker="o")
plt.plot(cmp_hr["timestamp"], cmp_hr["sim_ac_kWh_hour"], label="Simulated AC kWh", color="purple", linestyle="--", marker="x")
plt.title("Validation Phase: Hourly AC Electricity Consumption")
plt.ylabel("Electricity (kWh)")
plt.legend()
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig(RUN_DIR / "Validation_Plot_AC.png")
plt.close()

print(f"✅ All plots and CSVs saved in: {RUN_DIR}")
