from pathlib import Path
import pandas as pd
import numpy as np

PROJECT_DIR = Path(__file__).resolve().parents[1]
SCHED_DIR = PROJECT_DIR / "calibration" / "schedules"

# --> Change this path if you saved your occupancy CSV somewhere else! <--
ACTUAL_DATA_DIR = PROJECT_DIR / "calibration" / "schedules"

MASTER_FILE = SCHED_DIR / "measured_schedule_master.csv"
OCC_FILE = ACTUAL_DATA_DIR / "occupancy_schedule_timestamped.csv"

OUT_OCC = SCHED_DIR / "occupancy_schedule_values.csv"
OUT_SET = SCHED_DIR / "cooling_setpoint_schedule_values.csv"
OUT_WRK = SCHED_DIR / "workshop_temp_schedule_values.csv"

YEAR = 2026
DESIGN_PEOPLE = 24.0

# --------------------------------------------------
# 1. Load measured master schedule (Thermostat & Workshop Temp)
# --------------------------------------------------
df_master = pd.read_csv(MASTER_FILE, parse_dates=["timestamp"])
df_master["timestamp"] = df_master["timestamp"].dt.floor("5min")

# Remove the old occupancy column from the master file if it exists
if "occupancy" in df_master.columns:
    df_master = df_master.drop(columns=["occupancy"])

df_master["setpoint_C"] = pd.to_numeric(df_master["setpoint_C"], errors="coerce")
df_master["workshop_temp"] = pd.to_numeric(df_master["workshop_temp"], errors="coerce")

# Average out any duplicate timestamps
df_master = df_master.groupby("timestamp").mean().reset_index()

# --------------------------------------------------
# 2. Load NEW Occupancy Data (from Excel/CSV)
# --------------------------------------------------
df_occ = pd.read_csv(OCC_FILE)

# Parse the Filipino Date Format (Day/Month/Year)
df_occ["timestamp"] = pd.to_datetime(df_occ["timestamp"], format="%d/%m/%Y %H:%M", errors="coerce")
df_occ["timestamp"] = df_occ["timestamp"].dt.floor("5min")

# Make sure blank rows become 0 people
df_occ["occupancy"] = pd.to_numeric(df_occ["occupancy"], errors="coerce").fillna(0)

# If there are multiple headcount notes in a 5-min window, take the highest number!
df_occ = df_occ.groupby("timestamp")["occupancy"].max().reset_index()

# --------------------------------------------------
# 3. Combine Them
# --------------------------------------------------
df = pd.merge(df_occ, df_master, on="timestamp", how="outer")

# Convert final headcount to the EnergyPlus fraction (Max capacity 20)
df["occupancy"] = df["occupancy"].fillna(0) # Double-check empty rows are 0
df["occupancy_fraction"] = (df["occupancy"] / DESIGN_PEOPLE).clip(0, 1)

# --------------------------------------------------
# 4. Build full-year 5-minute index
# --------------------------------------------------
full_index = pd.date_range(
    start=f"{YEAR}-01-01 00:00:00",
    end=f"{YEAR}-12-31 23:55:00",
    freq="5min"
)

full = pd.DataFrame({"timestamp": full_index})

# --------------------------------------------------
# 5. Set default values for when sensors are off
# --------------------------------------------------
full["occupancy_fraction"] = 0.0      # Empty room at night
full["setpoint_C"] = 40.0             # AC is OFF (The brilliant hack)
full["workshop_temp"] = 32.0          # Hot workshop daytime default

# --------------------------------------------------
# 6. Overwrite with measured data where available
# --------------------------------------------------
merge_cols = ["timestamp", "occupancy_fraction", "setpoint_C", "workshop_temp"]
full = full.merge(
    df[merge_cols],
    on="timestamp",
    how="left",
    suffixes=("_default", "")
)

full["occupancy_fraction"] = full["occupancy_fraction"].fillna(full["occupancy_fraction_default"])
full["setpoint_C"] = full["setpoint_C"].fillna(full["setpoint_C_default"])
full["workshop_temp"] = full["workshop_temp"].fillna(full["workshop_temp_default"])

full = full[["timestamp", "occupancy_fraction", "setpoint_C", "workshop_temp"]]

# --------------------------------------------------
# 7. Save single-column files for EnergyPlus
# --------------------------------------------------
full[["occupancy_fraction"]].to_csv(OUT_OCC, index=False, header=False)
full[["setpoint_C"]].to_csv(OUT_SET, index=False, header=False)
full[["workshop_temp"]].to_csv(OUT_WRK, index=False, header=False)

print("✅ 5-minute full-year schedules successfully created!")
print(f"Total Rows: {len(full)} (Expected: 105120)")
print(f"Occupancy Fraction min/max: {full['occupancy_fraction'].min():.2f} / {full['occupancy_fraction'].max():.2f}")
print("Setpoint min/max:", full["setpoint_C"].min(), full["setpoint_C"].max())
print("Workshop temp min/max:", full["workshop_temp"].min(), full["workshop_temp"].max())