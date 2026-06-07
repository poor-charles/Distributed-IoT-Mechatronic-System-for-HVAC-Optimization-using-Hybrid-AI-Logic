import pandas as pd

# ---------------------------------------------------
# Read conference measured data
# ---------------------------------------------------

conf = pd.read_excel("actual_data/actual_data_1.xlsx")

conf = conf.rename(columns={
    "inside air temp": "temp_C",
    "inside air humidity": "RH",
    "ac_consumption_kWh": "ac_kWh"
})

conf = conf[["timestamp", "temp_C", "RH", "ac_kWh"]]

# ---------------------------------------------------
# Read workshop temperature
# ---------------------------------------------------

work = pd.read_excel("actual_data/workshoptemp.xlsx")

work = work.rename(columns={
    "outside air temp": "workshop_temp",
    "outside air humidity": "workshop_RH"
})

work = work[["timestamp", "workshop_temp"]]

# ---------------------------------------------------
# Merge datasets
# ---------------------------------------------------

df = pd.merge(conf, work, on="timestamp")

df["timestamp"] = pd.to_datetime(df["timestamp"])

df = df.sort_values("timestamp")

# ---------------------------------------------------
# Save timestep dataset
# ---------------------------------------------------

df.to_csv("actual_data/measured_timestep.csv", index=False)

# ---------------------------------------------------
# Create hourly AC energy for ASHRAE metrics
# ---------------------------------------------------

df["hour"] = df["timestamp"].dt.floor("h")

hourly = df.groupby("hour")["ac_kWh"].sum().reset_index()

hourly = hourly.rename(columns={
    "hour": "timestamp",
    "ac_kWh": "ac_kWh_hour"
})

hourly.to_csv("actual_data/measured_hourly_ac.csv", index=False)

print("Measured data prepared successfully.")
print("Timestep rows:", len(df))
print("Hourly rows:", len(hourly))