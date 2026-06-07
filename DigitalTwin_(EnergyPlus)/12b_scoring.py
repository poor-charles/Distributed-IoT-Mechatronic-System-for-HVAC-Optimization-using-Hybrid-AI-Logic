from pathlib import Path
import sqlite3
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import shutil

PROJECT_DIR = Path(__file__).resolve().parents[1]

RUN_DIR = PROJECT_DIR / "results" / "current_run"
SQL_PATH = RUN_DIR / "eplusout.sql"

PLOT_DIR = RUN_DIR / "plots"
PLOT_DIR.mkdir(exist_ok=True)

MEAS_TS_PATH = PROJECT_DIR / "actual_data" / "measured_timestep.csv"
MEAS_HR_PATH = PROJECT_DIR / "actual_data" / "measured_hourly_ac.csv"

OUT_PATH = RUN_DIR / "current_run_score.csv"

ITER_OUT_DIR = PROJECT_DIR / "results" / "iteration_outputs"
ITER_OUT_DIR.mkdir(parents=True, exist_ok=True)

ZONE_NAME = "DME-CONFE THERMAL ZONE"
MEASURED_YEAR = 2026

# ---------------------------------------------------
# ASHRAE-style metrics
# ---------------------------------------------------

P_ASHRAE = 1

def rmse(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    return np.sqrt(np.mean((a - b) ** 2))

def cvrmse(a, b, p=P_ASHRAE):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)

    n = len(a)
    mean_a = np.mean(a) if n > 0 else np.nan

    if n <= p or mean_a == 0:
        return np.nan

    sse = np.sum((a - b) ** 2)
    return 100.0 * np.sqrt(sse / (n - p)) / mean_a

def nmbe(a, b, p=P_ASHRAE):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)

    n = len(a)
    mean_a = np.mean(a) if n > 0 else np.nan

    if n <= p or mean_a == 0:
        return np.nan

    return 100.0 * np.sum(a - b) / ((n - p) * mean_a)

# ---------------------------------------------------
# Checks
# ---------------------------------------------------

for p in [SQL_PATH, MEAS_TS_PATH, MEAS_HR_PATH]:
    if not p.exists():
        raise FileNotFoundError(f"Missing required file: {p}")

# ---------------------------------------------------
# Load measured data
# ---------------------------------------------------

meas_ts = pd.read_csv(MEAS_TS_PATH, parse_dates=["timestamp"])
meas_hr = pd.read_csv(MEAS_HR_PATH, parse_dates=["timestamp"])

meas_ts = meas_ts.sort_values("timestamp").reset_index(drop=True)
meas_hr = meas_hr.sort_values("timestamp").reset_index(drop=True)

print("Measured TS rows:", len(meas_ts))
print("Measured AC rows:", len(meas_hr))

# ---------------------------------------------------
# Read SQL
# ---------------------------------------------------

con = sqlite3.connect(SQL_PATH)

time_df = pd.read_sql_query("""
    SELECT TimeIndex, Month, Day, Hour, Minute
    FROM Time
    ORDER BY TimeIndex
""", con)

# EnergyPlus hour convention: 1-24
time_df["Hour_adj"] = time_df["Hour"] - 1

time_df["timestamp"] = pd.to_datetime(
    dict(
        year=MEASURED_YEAR,
        month=time_df["Month"],
        day=time_df["Day"],
        hour=time_df["Hour_adj"],
        minute=time_df["Minute"]
    ),
    errors="coerce"
)

# detect iteration number from bayesian history
hist_file = PROJECT_DIR / "results" / "bayesian_history.csv"

if hist_file.exists():
    iter_num = len(pd.read_csv(hist_file)) + 1
else:
    iter_num = 1

# ---------------------------------------------------
# Extract timestep temp
# ---------------------------------------------------

temp = pd.read_sql_query("""
    SELECT rd.TimeIndex, rd.Value
    FROM ReportData rd
    JOIN ReportDataDictionary rdd
      ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
    WHERE rdd.Name = 'Zone Air Temperature'
      AND rdd.KeyValue = ?
""", con, params=[ZONE_NAME])

# ---------------------------------------------------
# Extract timestep RH
# ---------------------------------------------------

rh = pd.read_sql_query("""
    SELECT rd.TimeIndex, rd.Value
    FROM ReportData rd
    JOIN ReportDataDictionary rdd
      ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
    WHERE rdd.Name = 'Zone Air Relative Humidity'
      AND rdd.KeyValue = ?
""", con, params=[ZONE_NAME])

# ---------------------------------------------------
# Extract AC electricity
# ---------------------------------------------------

ac = pd.read_sql_query("""
    SELECT rd.TimeIndex, rd.Value
    FROM ReportData rd
    JOIN ReportDataDictionary rdd
      ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
    WHERE rdd.Name = 'Zone Packaged Terminal Air Conditioner Electricity Energy'
""", con)

con.close()

if temp.empty:
    raise ValueError("No Zone Air Temperature found in current_run SQL.")
if rh.empty:
    raise ValueError("No Zone Air Relative Humidity found in current_run SQL.")
if ac.empty:
    raise ValueError("No PTAC electricity energy found in current_run SQL.")

# ---------------------------------------------------
# Build simulated timestep dataframe
# ---------------------------------------------------

temp = temp.merge(time_df[["TimeIndex", "timestamp"]], on="TimeIndex", how="left")
rh = rh.merge(time_df[["TimeIndex", "timestamp"]], on="TimeIndex", how="left")

temp = temp[["timestamp", "Value"]].rename(columns={"Value": "sim_temp"})
rh = rh[["timestamp", "Value"]].rename(columns={"Value": "sim_rh"})

sim_ts = temp.merge(rh, on="timestamp", how="inner")
sim_ts = sim_ts.sort_values("timestamp").reset_index(drop=True)

# ---------------------------------------------------
# Save simulated timestep outputs (5-min)
# ---------------------------------------------------

ts_out_file = ITER_OUT_DIR / f"iteration_{iter_num:03d}_temp_rh_5min.csv"
sim_ts.to_csv(ts_out_file, index=False)


# ---------------------------------------------------
# Build simulated hourly AC dataframe
# ---------------------------------------------------

ac = ac.merge(time_df[["TimeIndex", "timestamp"]], on="TimeIndex", how="left")
ac = ac.sort_values("timestamp").reset_index(drop=True)

# J -> kWh
ac["sim_ac_kWh_total"] = ac["Value"] / 3_600_000.0

# two real units cool room, but measured PZEM is only one unit
ac["sim_ac_kWh_one_unit"] = ac["sim_ac_kWh_total"] / 2.0

ac_hourly = ac[["timestamp", "sim_ac_kWh_one_unit"]].copy()
ac_hourly["hour"] = ac_hourly["timestamp"].dt.floor("h")

sim_ac_hour = (
    ac_hourly.groupby("hour", as_index=False)["sim_ac_kWh_one_unit"]
    .sum()
    .rename(columns={
        "hour": "timestamp",
        "sim_ac_kWh_one_unit": "sim_ac_kWh_hour"
    })
)

sim_ac_hour = sim_ac_hour.sort_values("timestamp").reset_index(drop=True)

# ---------------------------------------------------
# Save simulated AC hourly consumption
# ---------------------------------------------------

ac_out_file = ITER_OUT_DIR / f"iteration_{iter_num:03d}_ac_hourly.csv"
sim_ac_hour.to_csv(ac_out_file, index=False)

# ---------------------------------------------------
# Timestamp merge first
# ---------------------------------------------------

cmp_ts = meas_ts.merge(sim_ts, on="timestamp", how="inner")
cmp_hr = meas_hr.merge(sim_ac_hour, on="timestamp", how="inner")

print("Merged TS rows:", len(cmp_ts))
print("Merged AC rows:", len(cmp_hr))

# ---------------------------------------------------
# Fallback to sequential alignment if merge is weak
# ---------------------------------------------------

if len(cmp_ts) < max(10, int(0.5 * len(meas_ts))):
    print("TS timestamp mismatch detected -> using sequential alignment")
    n_ts = min(len(meas_ts), len(sim_ts))
    cmp_ts = pd.DataFrame({
        "temp_C": meas_ts["temp_C"].iloc[:n_ts].values,
        "sim_temp": sim_ts["sim_temp"].iloc[:n_ts].values,
        "RH": meas_ts["RH"].iloc[:n_ts].values,
        "sim_rh": sim_ts["sim_rh"].iloc[:n_ts].values
    })

if len(cmp_hr) < max(3, int(0.5 * len(meas_hr))):
    print("AC timestamp mismatch detected -> using sequential alignment")
    n_hr = min(len(meas_hr), len(sim_ac_hour))
    cmp_hr = pd.DataFrame({
        "ac_kWh_hour": meas_hr["ac_kWh_hour"].iloc[:n_hr].values,
        "sim_ac_kWh_hour": sim_ac_hour["sim_ac_kWh_hour"].iloc[:n_hr].values
    })

if len(cmp_ts) == 0:
    raise ValueError("No valid timestep comparison rows found.")
if len(cmp_hr) == 0:
    raise ValueError("No valid hourly AC comparison rows found.")

# ---------------------------------------------------
# Metrics
# ---------------------------------------------------

temp_rmse = rmse(cmp_ts["temp_C"], cmp_ts["sim_temp"])
temp_cvrmse = cvrmse(cmp_ts["temp_C"], cmp_ts["sim_temp"])
temp_nmbe = nmbe(cmp_ts["temp_C"], cmp_ts["sim_temp"])

rh_rmse = rmse(cmp_ts["RH"], cmp_ts["sim_rh"])
rh_cvrmse = cvrmse(cmp_ts["RH"], cmp_ts["sim_rh"])
rh_nmbe = nmbe(cmp_ts["RH"], cmp_ts["sim_rh"])

ac_rmse = rmse(cmp_hr["ac_kWh_hour"], cmp_hr["sim_ac_kWh_hour"])
ac_cvrmse = cvrmse(cmp_hr["ac_kWh_hour"], cmp_hr["sim_ac_kWh_hour"])
ac_nmbe = nmbe(cmp_hr["ac_kWh_hour"], cmp_hr["sim_ac_kWh_hour"])

score = (
    0.45 * temp_cvrmse +
    0.25 * rh_cvrmse +
    0.30 * ac_cvrmse
)

# ---------------------------------------------------
# LIVE DASHBOARD PATHS (These overwrite every run)
# ---------------------------------------------------
live_temp_path = PROJECT_DIR / "results" / "live_temp_comparison.png"
live_rh_path = PROJECT_DIR / "results" / "live_rh_comparison.png"
live_ac_path = PROJECT_DIR / "results" / "live_ac_comparison.png"
live_csv_path  = PROJECT_DIR / "results" / "live_ac_comparison.csv"

# ---------------------------------------------------
# Temperature plot (5-min)
# ---------------------------------------------------
plt.figure()
plt.plot(cmp_ts["timestamp"], cmp_ts["temp_C"], label="Measured Temp")
plt.plot(cmp_ts["timestamp"], cmp_ts["sim_temp"], label="Simulated Temp")

if "setpoint" in cmp_ts.columns:
    plt.plot(cmp_ts["timestamp"], cmp_ts["setpoint"], linestyle="--", label="Setpoint")

plt.title("Temperature Comparison")
plt.xlabel("Time")
plt.ylabel("Temperature (°C)")
plt.legend()
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig(live_temp_path)
plt.close()

# ---------------------------------------------------
# RH plot (5-min)
# ---------------------------------------------------
plt.figure()
plt.plot(cmp_ts["timestamp"], cmp_ts["RH"], label="Measured RH")
plt.plot(cmp_ts["timestamp"], cmp_ts["sim_rh"], label="Simulated RH")

plt.title("RH Comparison")
plt.xlabel("Time")
plt.ylabel("Relative Humidity (%)")
plt.legend()
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig(live_rh_path)
plt.close()

# ---------------------------------------------------
# AC Consumption plot (hourly)
# ---------------------------------------------------
plt.figure()
plt.plot(cmp_hr["timestamp"], cmp_hr["ac_kWh_hour"], label="Measured AC")
plt.plot(cmp_hr["timestamp"], cmp_hr["sim_ac_kWh_hour"], label="Simulated AC")

plt.title("AC Consumption Comparison (Hourly)")
plt.xlabel("Time")
plt.ylabel("kWh")
plt.legend()
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig(live_ac_path)
plt.close()

# --------------------------------------------------
# EXPORT LIVE CSV
# --------------------------------------------------
cmp_hr.to_csv(live_csv_path, index=False)

# ---------------------------------------------------
# TOP 10 LEADERBOARD & THE #1 CHAMPION SAVER
# ---------------------------------------------------
RESULTS_DIR = PROJECT_DIR / "results"
LEADERBOARD_PATH = RESULTS_DIR / "top10_models.csv"

# 1. Gather current run's data
current_data = {
    "ac_cvrmse": ac_cvrmse,
    "ac_nmbe": ac_nmbe,
    "score": score
}

# Grab the parameters from the bayesian history to save in the leaderboard
if hist_file.exists():
    try:
        history_df = pd.read_csv(hist_file)
        latest_params = history_df.iloc[-1].to_dict()
        current_data.update(latest_params)
    except Exception:
        pass

current_df = pd.DataFrame([current_data])

# 2. Update the Top 10 Leaderboard
if LEADERBOARD_PATH.exists():
    leaderboard_df = pd.read_csv(LEADERBOARD_PATH)
    leaderboard_df = pd.concat([leaderboard_df, current_df], ignore_index=True)
else:
    leaderboard_df = current_df

# Sort from lowest CVRMSE to highest, and strictly keep only the top 10
leaderboard_df = leaderboard_df.sort_values(by="ac_cvrmse", ascending=True).head(10)
leaderboard_df.to_csv(LEADERBOARD_PATH, index=False)

# 3. Check if the current run is the #1 Absolute Best
best_cvrmse = leaderboard_df["ac_cvrmse"].min()
is_number_one = (ac_cvrmse <= best_cvrmse)

# 4. Save/Overwrite the Champion IDF only if it is #1
if is_number_one:
    print(f"\n🏆 NEW #1 CHAMPION MODEL FOUND! 🏆")
    print(f"AC CV(RMSE): {ac_cvrmse:.2f}% (New All-Time Low!)")
    print(f"Overwriting previous Top 1 IDF...\n")
    
    shutil.copy(live_temp_path, RESULTS_DIR / "Best_Model_Top1_temp.png")
    shutil.copy(live_rh_path, RESULTS_DIR / "Best_Model_Top1_rh.png")
    shutil.copy(live_ac_path, RESULTS_DIR / "Best_Model_Top1_ac.png")
    shutil.copy(live_csv_path, RESULTS_DIR / "Best_Model_Top1_data.csv")
    
    idf_files = list(RUN_DIR.glob("*.idf"))
    if idf_files:
        shutil.copy(idf_files[0], RESULTS_DIR / "Best_Model_Top1.idf")
        print(f"✅ Saved IDF to: {RESULTS_DIR / 'Best_Model_Top1.idf'}")

# Check if it made the Top 10 but didn't win #1
elif ac_cvrmse <= leaderboard_df["ac_cvrmse"].max():
    print(f"\n⭐ Run added to Top 10 Leaderboard (CVRMSE: {ac_cvrmse:.2f}%)")
    print(f"But it did not beat the #1 Champion (Best is {best_cvrmse:.2f}%).")

else:
    print(f"\n✔️ Run finished (CVRMSE: {ac_cvrmse:.2f}%). Not in Top 10.")

# ---------------------------------------------------
# Save result for Bayesian optimizer (Crucial for the loop!)
# ---------------------------------------------------
result_df = pd.DataFrame([{
    "temp_rmse": temp_rmse,
    "temp_cvrmse": temp_cvrmse,
    "temp_nmbe": temp_nmbe,
    "rh_rmse": rh_rmse,
    "rh_cvrmse": rh_cvrmse,
    "rh_nmbe": rh_nmbe,
    "ac_rmse": ac_rmse,
    "ac_cvrmse": ac_cvrmse,
    "ac_nmbe": ac_nmbe,
    "score": score,
    "comp_rows_ts": len(cmp_ts),
    "comp_rows_ac": len(cmp_hr)
}])

result_df.to_csv(OUT_PATH, index=False)

