import pandas as pd
import numpy as np
from pathlib import Path

# --- Setup Paths ---
PROJECT_DIR = Path(__file__).resolve().parents[1]
VAL_DIR = PROJECT_DIR / "validation_data"
VAL_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR = PROJECT_DIR / "actual_data_new" 

YEAR = 2026
DESIGN_PEOPLE = 24.0

print("1. Loading raw validation data...")
try:
    df_ac = pd.read_csv(RAW_DIR / "measured_acEnergy.csv")
    df_ac["timestamp"] = pd.to_datetime(df_ac["timestamp"], format="%d/%m/%Y %H:%M")
    
    df_occ = pd.read_csv(RAW_DIR / "measured_occupancy.csv")
    df_occ["timestamp"] = pd.to_datetime(df_occ["timestamp"], format="%d/%m/%Y %H:%M")
    
    df_set = pd.read_csv(RAW_DIR / "measured_setpoint.csv")
    df_set["timestamp"] = pd.to_datetime(df_set["timestamp"], format="%d/%m/%Y %H:%M")
    
    df_wrk = pd.read_csv(RAW_DIR / "meausred_workshopTemp.csv")
    df_wrk["timestamp"] = pd.to_datetime(df_wrk["timestamp"], format="%d/%m/%Y %H:%M")
    
    # NEW: Load the Inside Temp and RH
    df_trh = pd.read_csv(RAW_DIR / "measured_TempRH.csv")
    df_trh["timestamp"] = pd.to_datetime(df_trh["timestamp"], format="%d/%m/%Y %H:%M")
    
except FileNotFoundError as e:
    print(f"Error: {e}. Please ensure the files are in the {RAW_DIR} folder.")
    exit()

# Snap all data to 5-minute intervals
for df in [df_ac, df_occ, df_set, df_wrk, df_trh]:
    df["timestamp"] = df["timestamp"].dt.floor("5min")

print("2. Formatting EnergyPlus Injection Schedules...")

# Process Injection Schedules
df_occ_agg = df_occ.groupby("timestamp")["occupancy"].max().reset_index()
df_set_agg = df_set.groupby("timestamp")["ac temperature setpoint"].mean().reset_index()
df_wrk_agg = df_wrk.groupby("timestamp")["outside air temp"].mean().reset_index()

df_sched = pd.merge(df_occ_agg, df_set_agg, on="timestamp", how="outer")
df_sched = pd.merge(df_sched, df_wrk_agg, on="timestamp", how="outer")
df_sched["occupancy_fraction"] = (df_sched["occupancy"].fillna(0) / DESIGN_PEOPLE).clip(0, 1)

# Build 8760-hour index and apply physical defaults for gaps
full_index = pd.date_range(start=f"{YEAR}-01-01 00:00:00", end=f"{YEAR}-12-31 23:55:00", freq="5min")
full = pd.DataFrame({"timestamp": full_index})

full["occupancy_fraction"] = 0.0
full["ac temperature setpoint"] = 40.0   
full["outside air temp"] = 32.0          

full = full.merge(df_sched, on="timestamp", how="left", suffixes=("_default", ""))

full["occupancy_fraction"] = full["occupancy_fraction"].fillna(full["occupancy_fraction_default"])
full["ac temperature setpoint"] = full["ac temperature setpoint"].fillna(full["ac temperature setpoint_default"])
full["outside air temp"] = full["outside air temp"].fillna(full["outside air temp_default"])

# Save individual 8760-hour schedule files
full[["occupancy_fraction"]].to_csv(VAL_DIR / "val_occupancy.csv", index=False, header=False)
full[["ac temperature setpoint"]].to_csv(VAL_DIR / "val_setpoint.csv", index=False, header=False)
full[["outside air temp"]].to_csv(VAL_DIR / "val_workshop_temp.csv", index=False, header=False)


print("3. Formatting Target Data for Scoring Comparisons...")

# Process Target AC Data (Hourly)
df_ac["hour"] = df_ac["timestamp"].dt.floor("h")
hourly_ac = df_ac.groupby("hour")["energy_kWh"].sum().reset_index()
hourly_ac = hourly_ac.rename(columns={"hour": "timestamp", "energy_kWh": "ac_kWh_hour"})
hourly_ac.to_csv(VAL_DIR / "val_measured_hourly_ac.csv", index=False)

# NEW: Process Target Temp/RH Data (5-minute Timestep)
# We take the mean if there are duplicate timestamps, rename columns for clarity, and drop blanks
df_trh_agg = df_trh.groupby("timestamp").mean().reset_index()
df_trh_agg = df_trh_agg.rename(columns={
    "inside air temp": "temp_C",
    "inside air humidity": "RH"
}).dropna()

df_trh_agg.to_csv(VAL_DIR / "val_measured_temp_rh.csv", index=False)

print(f"✅ Success! Validation data created in {VAL_DIR}")