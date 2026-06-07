import sys
import time
import csv
import json
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from pathlib import Path
from datetime import datetime, timedelta
import paho.mqtt.client as mqtt
import threading
import os

sys.path.insert(0, r"C:\EnergyPlusV25-1-0")
from pyenergyplus.api import EnergyPlusAPI
from eppy.modeleditor import IDF

# ---------------------------------------------------------
# MASTER SWITCHES
# ---------------------------------------------------------
USE_AUTO_IR_BLASTER = True
MANUAL_AC_SETPOINT = 27.0
USE_AUTO_OCCUPANCY = True
MANUAL_OCCUPANCY = 2.0

DESIGN_PEOPLE = 24.0

# ---------------------------------------------------------
# PMV ASSUMPTIONS
# ---------------------------------------------------------
PMV_MET = 1.2
PMV_CLO = 0.5
PMV_VEL = 0.1

PROJECT_DIR = Path(__file__).resolve().parents[1]
LIVE_IDF = PROJECT_DIR / "results" / "calibrated_model" / "live_twin1.idf"
EPW_PATH = PROJECT_DIR / "Weather" / "site.epw"

SESSION_ID = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
SESSION_DIR = PROJECT_DIR / "live_data" / f"session_{SESSION_ID}"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

data_lock = threading.Lock()

# ---------------------------------------------------------
# NOTE ABOUT PMV
# ---------------------------------------------------------
# Simulated PMV is NOT calculated in Python.
# It is retrieved directly from EnergyPlus using:
#   Zone Thermal Comfort Fanger Model PMV
# The constants below are written into the IDF as Fanger comfort schedules
# before the simulation starts.

# ---------------------------------------------------------
# LIVE SENSOR MEMORY
# ---------------------------------------------------------
live_sensor_data = {
    "workshop_temp": None,
    "workshop_rh": None,
    "occupancy": MANUAL_OCCUPANCY,
    "setpoint": MANUAL_AC_SETPOINT,
    "ac_mode": "COOL",
    "room_temp1": None,
    "room_temp2": None,
    "room_rh1": None,
    "room_rh2": None,
    "ac_power4": None,
    "ac_power5": None,
    "room_pmv": None
}

# ---------------------------------------------------------
# MQTT TOPICS
# ---------------------------------------------------------
TOPIC_OUTSIDE_TEMP = "/outside/temp3"
TOPIC_OUTSIDE_RH = "/outside/humid3"
TOPIC_OCCUPANCY = "hvac/sensor/occupancy"
TOPIC_AC_SETPOINT = "/ac/setpoint"
TOPIC_AC_MODE = "/ac/mode"
TOPIC_INSIDE_TEMP1 = "/room/temp1"
TOPIC_INSIDE_TEMP2 = "/room/temp2"
TOPIC_INSIDE_RH1 = "/room/humid1"
TOPIC_INSIDE_RH2 = "/room/humid2"
TOPIC_AC_POWER4 = "/ac/power4"
TOPIC_AC_POWER5 = "/ac/power5"
TOPIC_ROOM_PMV = "/room/pmv"

def on_connect(client, userdata, flags, rc, *args):
    print(f"Connected to MQTT Broker with result code {rc}")
    client.subscribe([
        (TOPIC_OUTSIDE_TEMP, 0),
        (TOPIC_OUTSIDE_RH, 0),
        (TOPIC_OCCUPANCY, 0),
        (TOPIC_AC_SETPOINT, 0),
        (TOPIC_AC_MODE, 0),
        (TOPIC_INSIDE_TEMP1, 0),
        (TOPIC_INSIDE_TEMP2, 0),
        (TOPIC_INSIDE_RH1, 0),
        (TOPIC_INSIDE_RH2, 0),
        (TOPIC_AC_POWER4, 0),
        (TOPIC_AC_POWER5, 0),
        (TOPIC_ROOM_PMV, 0)
    ])

def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode("utf-8")

    try:
        updated = False

        with data_lock:
            if topic == TOPIC_OUTSIDE_TEMP:
                live_sensor_data["workshop_temp"] = float(payload)
                updated = True

            elif topic == TOPIC_OUTSIDE_RH:
                live_sensor_data["workshop_rh"] = float(payload)
                updated = True

            elif topic == TOPIC_OCCUPANCY:
                live_sensor_data["occupancy"] = float(payload)
                updated = True

            elif topic == TOPIC_INSIDE_TEMP1:
                live_sensor_data["room_temp1"] = float(payload)
                updated = True

            elif topic == TOPIC_INSIDE_TEMP2:
                live_sensor_data["room_temp2"] = float(payload)
                updated = True

            elif topic == TOPIC_INSIDE_RH1:
                live_sensor_data["room_rh1"] = float(payload)
                updated = True

            elif topic == TOPIC_INSIDE_RH2:
                live_sensor_data["room_rh2"] = float(payload)
                updated = True

            elif topic == TOPIC_AC_POWER4:
                live_sensor_data["ac_power4"] = float(payload)
                updated = True

            elif topic == TOPIC_AC_POWER5:
                live_sensor_data["ac_power5"] = float(payload)
                updated = True

            elif topic == TOPIC_ROOM_PMV:
                live_sensor_data["room_pmv"] = float(payload)
                updated = True

            elif topic == TOPIC_AC_SETPOINT:
                payload_clean = payload.strip().upper()

                if payload_clean == "OFF":
                    live_sensor_data["ac_mode"] = "OFF"
                else:
                    live_sensor_data["setpoint"] = float(payload)

                    if live_sensor_data.get("ac_mode") == "OFF":
                        live_sensor_data["ac_mode"] = "COOL"

                updated = True

            elif topic == TOPIC_AC_MODE:
                live_sensor_data["ac_mode"] = payload.strip().upper()
                updated = True

        if updated:
            with open(SESSION_DIR / "live_memory.json", "w") as f:
                json.dump(live_sensor_data, f)

    except ValueError:
        pass

try:
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
except AttributeError:
    mqtt_client = mqtt.Client()

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

print(f"Connecting to MQTT... Session folder created: {SESSION_DIR.name}")

try:
    mqtt_client.connect("cvmpi", 1883, 60)
    mqtt_client.loop_start()
except Exception:
    print("MQTT Connection failed. Proceeding with defaults.")

# ---------------------------------------------------------
# CSV LOGGING
# ---------------------------------------------------------
CSV_FILE_PATH = SESSION_DIR / "live_data.csv"

CSV_HEADERS = [
    "real_timestamp",
    "sim_time_str",
    "status",
    "input_occupancy",
    "input_setpoint",
    "input_workshop_temp",
    "input_workshop_rh",
    "actual_temp_C",
    "sim_temp_C",
    "actual_rh_percent",
    "sim_rh_percent",
    "actual_pmv",
    "sim_pmv",
    "pmv_met_assumption",
    "pmv_clo_assumption",
    "pmv_vel_assumption",
    "actual_ac_kWh_1min",
    "sim_ac_kWh_1min"
]

with open(CSV_FILE_PATH, mode="w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(CSV_HEADERS)

# ---------------------------------------------------------
# ENERGYPLUS API SETUP
# ---------------------------------------------------------
api = EnergyPlusAPI()
state = api.state_manager.new_state()

handles = {
    "occ_actuator": -1,
    "setpoint_actuator": -1,
    "workshop_actuator": -1,
    "zone_temp_sensor": -1,
    "zone_rh_sensor": -1,
    "ac_power_sensor_1": -1,
    "ac_power_sensor_2": -1,
    "weather_temp_actuator": -1,
    "weather_rh_actuator": -1,
    "pmv_sensor": -1
}

next_step_real_time = None
TIMESTEP_MINUTES = 1

# ---------------------------------------------------------
# ENERGYPLUS PMV HANDLE FINDER
# ---------------------------------------------------------
def get_energyplus_pmv_handle(state_arg):
    """
    EnergyPlus Fanger PMV output is usually keyed by the People object name,
    but some models/tools expose it by zone or space name. Try all likely keys.
    """
    candidate_keys = [
    "SPACE 106 PEOPLE 1",
    "People 1",
    "DME-CONFE THERMAL ZONE",
    "DME-Confe Thermal Zone",
    "DME-Conference Space"
    ]

    for key in candidate_keys:
        h = api.exchange.get_variable_handle(
            state_arg,
            "Zone Thermal Comfort Fanger Model PMV",
            key
        )
        if h != -1:
            print(f"✅ EnergyPlus PMV handle found using key: {key}")
            return h

    print("❌ EnergyPlus PMV handle not found. Simulated PMV will be logged as NaN.")
    print("   Check that the People object has Thermal Comfort Model 1 Type = Fanger")
    print("   and that Output:Variable requests Zone Thermal Comfort Fanger Model PMV.")
    return -1

def timestep_callback(state_arg):
    global next_step_real_time

    if not api.exchange.api_data_fully_ready(state_arg):
        return

    # -----------------------------------------------------
    # GET ACTUATOR AND VARIABLE HANDLES
    # -----------------------------------------------------
    if handles["occ_actuator"] == -1:
        handles["occ_actuator"] = api.exchange.get_actuator_handle(
            state_arg,
            "Schedule:Constant",
            "Schedule Value",
            "Measured_Occupancy"
        )

        handles["setpoint_actuator"] = api.exchange.get_actuator_handle(
            state_arg,
            "Schedule:Constant",
            "Schedule Value",
            "Measured_Setpoint"
        )

        handles["workshop_actuator"] = api.exchange.get_actuator_handle(
            state_arg,
            "Schedule:Constant",
            "Schedule Value",
            "Workshop_Temp"
        )

        handles["zone_temp_sensor"] = api.exchange.get_variable_handle(
            state_arg,
            "Zone Air Temperature",
            "DME-CONFE THERMAL ZONE"
        )

        handles["zone_rh_sensor"] = api.exchange.get_variable_handle(
            state_arg,
            "Zone Air Relative Humidity",
            "DME-CONFE THERMAL ZONE"
        )

        handles["ac_power_sensor_1"] = api.exchange.get_variable_handle(
            state_arg,
            "Zone Packaged Terminal Air Conditioner Electricity Energy",
            "Cycling PTAC DX Clg Elec Htg"
        )

        handles["ac_power_sensor_2"] = api.exchange.get_variable_handle(
            state_arg,
            "Zone Packaged Terminal Air Conditioner Electricity Energy",
            "Cycling PTAC DX Clg Elec Htg 1"
        )

        handles["weather_temp_actuator"] = api.exchange.get_actuator_handle(
            state_arg,
            "Weather Data",
            "Outdoor Dry Bulb",
            "Environment"
        )

        handles["weather_rh_actuator"] = api.exchange.get_actuator_handle(
            state_arg,
            "Weather Data",
            "Outdoor Relative Humidity",
            "Environment"
        )

        handles["pmv_sensor"] = get_energyplus_pmv_handle(state_arg)

    # -----------------------------------------------------
    # WARMUP SYNC
    # -----------------------------------------------------
    if api.exchange.warmup_flag(state_arg):
        with data_lock:
            start_room_temp = (
                live_sensor_data["room_temp1"] + live_sensor_data["room_temp2"]
            ) / 2.0

            start_room_rh = (
                live_sensor_data["room_rh1"] + live_sensor_data["room_rh2"]
            ) / 2.0

            start_workshop_temp = live_sensor_data["workshop_temp"]

        api.exchange.set_actuator_value(state_arg, handles["occ_actuator"], 0.0)
        api.exchange.set_actuator_value(state_arg, handles["setpoint_actuator"], 40.0)
        api.exchange.set_actuator_value(state_arg, handles["workshop_actuator"], start_workshop_temp)
        api.exchange.set_actuator_value(state_arg, handles["weather_temp_actuator"], start_room_temp)
        api.exchange.set_actuator_value(state_arg, handles["weather_rh_actuator"], start_room_rh)
        return

    if next_step_real_time is None:
        next_step_real_time = datetime.now()

    sim_month = api.exchange.month(state_arg)
    sim_day = api.exchange.day_of_month(state_arg)
    sim_hour = api.exchange.hour(state_arg)
    sim_minute = api.exchange.minutes(state_arg)

    sim_time_str = f"Sim Time: {sim_month:02d}-{sim_day:02d} {sim_hour:02d}:{sim_minute:02d}"

    # -----------------------------------------------------
    # FETCH LIVE DATA
    # -----------------------------------------------------
    with data_lock:
        live_workshop_temp = live_sensor_data["workshop_temp"]
        live_workshop_rh = live_sensor_data["workshop_rh"]

        raw_people = live_sensor_data["occupancy"] if USE_AUTO_OCCUPANCY else MANUAL_OCCUPANCY
        live_people_fraction = min(max(raw_people / DESIGN_PEOPLE, 0.0), 1.0)

        current_mode = live_sensor_data.get("ac_mode", "COOL")
        raw_setpoint = live_sensor_data["setpoint"]

        act_temp = (
            live_sensor_data["room_temp1"] + live_sensor_data["room_temp2"]
        ) / 2.0

        act_rh = (
            live_sensor_data["room_rh1"] + live_sensor_data["room_rh2"]
        ) / 2.0

        total_ac_power_W = live_sensor_data["ac_power4"] + live_sensor_data["ac_power5"]
        act_ac_kwh = (total_ac_power_W / 1000.0) / 60.0

        actual_pmv = live_sensor_data["room_pmv"]

        if actual_pmv is None:
            actual_pmv = np.nan

    # -----------------------------------------------------
    # DETERMINE LIVE THERMOSTAT
    # -----------------------------------------------------
    if current_mode == "OFF":
        live_thermostat = 40.0
    elif USE_AUTO_IR_BLASTER:
        live_thermostat = raw_setpoint - 2.0 if current_mode == "DRY" else raw_setpoint
    else:
        live_thermostat = MANUAL_AC_SETPOINT

    # -----------------------------------------------------
    # INJECT LIVE DATA INTO ENERGYPLUS
    # -----------------------------------------------------
    api.exchange.set_actuator_value(state_arg, handles["occ_actuator"], live_people_fraction)
    api.exchange.set_actuator_value(state_arg, handles["setpoint_actuator"], live_thermostat)
    api.exchange.set_actuator_value(state_arg, handles["workshop_actuator"], live_workshop_temp)
    api.exchange.set_actuator_value(state_arg, handles["weather_temp_actuator"], live_workshop_temp)
    api.exchange.set_actuator_value(state_arg, handles["weather_rh_actuator"], live_workshop_rh)

    # -----------------------------------------------------
    # EXTRACT SIMULATED OUTPUTS
    # -----------------------------------------------------
    sim_temp = api.exchange.get_variable_value(state_arg, handles["zone_temp_sensor"])
    sim_rh = api.exchange.get_variable_value(state_arg, handles["zone_rh_sensor"])

    if handles["pmv_sensor"] != -1:
        sim_pmv = api.exchange.get_variable_value(
            state_arg,
            handles["pmv_sensor"]
        )
    else:
        sim_pmv = np.nan

    sim_ac_joules_1 = api.exchange.get_variable_value(state_arg, handles["ac_power_sensor_1"])
    sim_ac_joules_2 = api.exchange.get_variable_value(state_arg, handles["ac_power_sensor_2"])
    sim_ac_kwh_1min = (sim_ac_joules_1 + sim_ac_joules_2) / 3_600_000.0

    # -----------------------------------------------------
    # LOG DATA
    # -----------------------------------------------------
    real_time_now = datetime.now()

    current_data_row = {
        "real_timestamp": real_time_now.strftime("%Y-%m-%d %H:%M:%S"),
        "sim_time_str": sim_time_str,
        "status": "LIVE",
        "input_occupancy": raw_people,
        "input_setpoint": live_thermostat,
        "input_workshop_temp": live_workshop_temp,
        "input_workshop_rh": live_workshop_rh,
        "actual_temp_C": act_temp,
        "sim_temp_C": sim_temp,
        "actual_rh_percent": act_rh,
        "sim_rh_percent": sim_rh,
        "actual_pmv": actual_pmv,
        "sim_pmv": sim_pmv,
        "pmv_met_assumption": PMV_MET,
        "pmv_clo_assumption": PMV_CLO,
        "pmv_vel_assumption": PMV_VEL,
        "actual_ac_kWh_1min": act_ac_kwh,
        "sim_ac_kWh_1min": sim_ac_kwh_1min
    }

    with open(CSV_FILE_PATH, mode="a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writerow(current_data_row)

    # -----------------------------------------------------
    # REAL-TIME PACING
    # -----------------------------------------------------
    next_step_real_time += timedelta(minutes=TIMESTEP_MINUTES)
    time_to_wait = (next_step_real_time - datetime.now()).total_seconds()

    if time_to_wait > 0:
        try:
            time.sleep(time_to_wait)
        except KeyboardInterrupt:
            force_shutdown()

# =====================================================================
# ACCURACY REPORT
# =====================================================================
def force_shutdown():
    print("\n🛑 Simulation stopped manually by user.")
    print("Stopping MQTT background threads...")

    try:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
    except Exception:
        pass

    generate_accuracy_report()

    print("Session completely closed. Forcing terminal exit.")
    os._exit(0)

def generate_accuracy_report():
    print("\n" + "=" * 50)
    print("📊 GENERATING SESSION ACCURACY REPORT...")
    print("=" * 50)

    try:
        df = pd.read_csv(CSV_FILE_PATH)

        if len(df) < 5:
            print("Not enough data points gathered to generate a report.")
            return

        df["real_timestamp"] = pd.to_datetime(df["real_timestamp"])
        df.set_index("real_timestamp", inplace=True)

        def calc_metrics(act, sim, p=1):
            act = np.asarray(act, dtype=float)
            sim = np.asarray(sim, dtype=float)

            mask = ~np.isnan(act) & ~np.isnan(sim)
            act = act[mask]
            sim = sim[mask]

            n = len(act)

            if n <= p:
                return np.nan, np.nan, np.nan, np.nan

            mean_act = np.mean(act)

            if mean_act == 0:
                return np.nan, np.nan, np.nan, np.nan

            rmse = np.sqrt(np.sum((act - sim) ** 2) / (n - p))
            cvrmse = (rmse / mean_act) * 100
            nmbe = (np.sum(act - sim) / ((n - p) * mean_act)) * 100

            ss_res = np.sum((act - sim) ** 2)
            ss_tot = np.sum((act - mean_act) ** 2)
            r2 = 1.0 - (ss_res / ss_tot) if ss_tot != 0 else 0.0

            return rmse, cvrmse, nmbe, r2

        t_rmse, t_cvrmse, t_nmbe, t_r2 = calc_metrics(df["actual_temp_C"], df["sim_temp_C"])
        r_rmse, r_cvrmse, r_nmbe, r_r2 = calc_metrics(df["actual_rh_percent"], df["sim_rh_percent"])
        p_rmse, p_cvrmse, p_nmbe, p_r2 = calc_metrics(df["actual_pmv"], df["sim_pmv"])

        df_reset = df.reset_index()
        df_reset["hour"] = df_reset["real_timestamp"].dt.floor("h")

        hourly_ac = (
            df_reset
            .groupby("hour")[["actual_ac_kWh_1min", "sim_ac_kWh_1min"]]
            .sum()
            .reset_index()
        )

        hourly_ac.set_index("hour", inplace=True)

        a_rmse, a_cvrmse, a_nmbe, a_r2 = calc_metrics(
            hourly_ac["actual_ac_kWh_1min"],
            hourly_ac["sim_ac_kWh_1min"]
        )

        metrics_data = {
            "Variable": [
                "Room Temperature (°C)",
                "Room Relative Humidity (%)",
                "Thermal Comfort PMV",
                "Hourly AC Energy (kWh)"
            ],
            "RMSE": [t_rmse, r_rmse, p_rmse, a_rmse],
            "CVRMSE (%)": [t_cvrmse, r_cvrmse, p_cvrmse, a_cvrmse],
            "NMBE (%)": [t_nmbe, r_nmbe, p_nmbe, a_nmbe],
            "R-Squared": [t_r2, r_r2, p_r2, a_r2]
        }

        pd.DataFrame(metrics_data).to_csv(
            SESSION_DIR / "accuracy_metrics_table.csv",
            index=False
        )

        print(f"✅ Accuracy Table saved to: {SESSION_DIR.name}/accuracy_metrics_table.csv")

        graph_df = df.copy()

        graph_df["sim_temp_C_smooth"] = graph_df["sim_temp_C"].rolling(window=15, min_periods=1).mean()
        graph_df["actual_temp_C_smooth"] = graph_df["actual_temp_C"].rolling(window=15, min_periods=1).mean()

        graph_df["sim_rh_percent_smooth"] = graph_df["sim_rh_percent"].rolling(window=15, min_periods=1).mean()
        graph_df["actual_rh_percent_smooth"] = graph_df["actual_rh_percent"].rolling(window=15, min_periods=1).mean()

        graph_df["sim_pmv_smooth"] = graph_df["sim_pmv"].rolling(window=15, min_periods=1).mean()
        graph_df["actual_pmv_smooth"] = graph_df["actual_pmv"].rolling(window=15, min_periods=1).mean()

        graph_df["sim_ac_smooth"] = graph_df["sim_ac_kWh_1min"].rolling(window=15, min_periods=1).mean()
        graph_df["actual_ac_smooth"] = graph_df["actual_ac_kWh_1min"].rolling(window=15, min_periods=1).mean()

        def make_plots(
            act_col,
            sim_col,
            title,
            unit,
            filename,
            act_name,
            sim_name,
            act_color,
            sim_color,
            source_df=graph_df
        ):
            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 5))

            ax1.plot(source_df.index, source_df[act_col], label=act_name, color=act_color)
            ax1.plot(source_df.index, source_df[sim_col], label=sim_name, color=sim_color, linestyle="--")

            ax1.set_title(f"{title} (Time Series)")
            ax1.set_ylabel(unit)
            ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            fig.autofmt_xdate(rotation=45)
            ax1.legend()

            valid_df = source_df[[act_col, sim_col]].dropna()

            if not valid_df.empty:
                min_val = min(valid_df[act_col].min(), valid_df[sim_col].min())
                max_val = max(valid_df[act_col].max(), valid_df[sim_col].max())

                ax2.scatter(valid_df[act_col], valid_df[sim_col], alpha=0.6, color="purple")
                ax2.plot([min_val, max_val], [min_val, max_val], "k--", lw=2)

            ax2.set_title(f"{title} (Scatter Plot)")
            ax2.set_xlabel(f"Actual {unit}")
            ax2.set_ylabel(f"Simulated {unit}")

            plt.tight_layout()
            plt.savefig(SESSION_DIR / filename)
            plt.close()

        make_plots(
            "actual_temp_C_smooth",
            "sim_temp_C_smooth",
            "Room Temperature",
            "°C",
            "plot_temperature.png",
            "Actual Measured Temp",
            "Simulated Digital Twin Temp",
            "blue",
            "orange"
        )

        make_plots(
            "actual_rh_percent_smooth",
            "sim_rh_percent_smooth",
            "Room Relative Humidity",
            "%",
            "plot_humidity.png",
            "Actual Measured RH",
            "Simulated Digital Twin RH",
            "teal",
            "red"
        )

        make_plots(
            "actual_pmv_smooth",
            "sim_pmv_smooth",
            "Thermal Comfort PMV",
            "PMV",
            "plot_pmv.png",
            "Actual Measured PMV",
            "Simulated Digital Twin PMV",
            "blue",
            "orange"
        )

        make_plots(
            "actual_ac_smooth",
            "sim_ac_smooth",
            "AC Energy (1-Minute Trend)",
            "kWh/min",
            "plot_ac_energy.png",
            "Actual AC Trend",
            "Simulated AC Trend",
            "green",
            "purple"
        )

        print("✅ Smoothed charts generated successfully.")

    except Exception as e:
        print(f"Could not generate report: {e}")


# =====================================================================
# IDF PMV CONFIGURATION
# =====================================================================
def ensure_schedule_constant(idf, name, schedule_type, value):
    obj = idf.getobject("SCHEDULE:CONSTANT", name)
    if obj is None:
        obj = idf.newidfobject("SCHEDULE:CONSTANT")
        obj.Name = name

    obj.Schedule_Type_Limits_Name = schedule_type
    obj.Hourly_Value = value
    return obj

def set_idf_field(obj, field_name, value):
    """Safely set an Eppy IDF object field if the field exists."""
    if hasattr(obj, "fieldnames") and field_name in obj.fieldnames:
        setattr(obj, field_name, value)
        return True
    return False

def configure_energyplus_pmv(idf):
    """
    Configure the IDF so EnergyPlus calculates Fanger PMV internally.
    This makes simulated PMV a direct Digital Twin output instead of a Python calculation.
    """
    # Schedules required by the People object for Fanger PMV
    ensure_schedule_constant(idf, "PMV_Work_Efficiency", "Fraction", 0)
    ensure_schedule_constant(idf, "PMV_Clothing_Insulation", "Any Number", PMV_CLO)
    ensure_schedule_constant(idf, "PMV_Air_Velocity", "Velocity", PMV_VEL)

    people_objects = idf.idfobjects.get("PEOPLE", [])

    if not people_objects:
        print("⚠️ No People object found. EnergyPlus PMV cannot be generated.")
        return

    # Prefer the known conference-room People object if available
    people = None
    for p in people_objects:
        if str(getattr(p, "Name", "")).strip().lower() == "people 1":
            people = p
            break

    if people is None:
        people = people_objects[0]

    # Keep your existing people schedule and activity schedule.
    # Only add the Fanger thermal comfort fields.
    set_idf_field(people, "Enable_ASHRAE_55_Comfort_Warnings", "No")
    set_idf_field(people, "Mean_Radiant_Temperature_Calculation_Type", "EnclosureAveraged")
    set_idf_field(people, "Surface_NameAngle_Factor_List_Name", "")
    set_idf_field(people, "Work_Efficiency_Schedule_Name", "PMV_Work_Efficiency")
    set_idf_field(people, "Clothing_Insulation_Calculation_Method", "ClothingInsulationSchedule")

    # For EnergyPlus Fanger PMV, when ClothingInsulationSchedule is used,
    # the constant clothing value must be assigned to Clothing_Insulation_Schedule_Name.
    # Do not put the clothing schedule in Clothing_Insulation_Calculation_Method_Schedule_Name;
    # that field is only for selecting a calculation method by schedule.
    set_idf_field(people, "Clothing_Insulation_Schedule_Name", "PMV_Clothing_Insulation")
    set_idf_field(people, "Air_Velocity_Schedule_Name", "PMV_Air_Velocity")
    set_idf_field(people, "Thermal_Comfort_Model_1_Type", "Fanger")

    # Request PMV output from EnergyPlus
    existing_pmv_output = False
    for ov in idf.idfobjects.get("OUTPUT:VARIABLE", []):
        if str(getattr(ov, "Variable_Name", "")).strip().lower() == "zone thermal comfort fanger model pmv":
            existing_pmv_output = True
            ov.Key_Value = "*"
            ov.Reporting_Frequency = "Timestep"
            break

    if not existing_pmv_output:
        ov = idf.newidfobject("OUTPUT:VARIABLE")
        ov.Key_Value = "*"
        ov.Variable_Name = "Zone Thermal Comfort Fanger Model PMV"
        ov.Reporting_Frequency = "Timestep"

    print("✅ EnergyPlus Fanger PMV configuration applied to IDF.")

# =====================================================================
# PRE-RUN STATE SYNC
# =====================================================================
print("⏳ Waiting for initial physical sensor data from MQTT...")

def check_sensors_ready():
    with data_lock:
        required = [
            "workshop_temp",
            "workshop_rh",
            "room_temp1",
            "room_temp2",
            "room_rh1",
            "room_rh2",
            "ac_power4",
            "ac_power5"
        ]

        for key in required:
            if live_sensor_data[key] is None:
                return False

        return True

while not check_sensors_ready():
    time.sleep(1)

print("✅ First wave of MQTT data received! Booting digital twin...")

print("🔄 Syncing virtual walls to match the actual room...")

IDF.setiddname(r"C:\EnergyPlusV25-1-0\Energy+.idd")
idf = IDF(str(LIVE_IDF))

# Keep a backup of the IDF before the script applies live PMV/schedule edits
try:
    backup_idf = LIVE_IDF.with_suffix(".before_live_pmv_patch.idf")
    if not backup_idf.exists():
        import shutil
        shutil.copy2(LIVE_IDF, backup_idf)
except Exception:
    pass

# Configure EnergyPlus to generate simulated PMV directly from the Digital Twin
configure_energyplus_pmv(idf)

with data_lock:
    starting_workshop = live_sensor_data["workshop_temp"]
    start_room = (
        live_sensor_data["room_temp1"] + live_sensor_data["room_temp2"]
    ) / 2.0

workshop_sch = idf.getobject("SCHEDULE:CONSTANT", "Workshop_Temp")
if workshop_sch:
    workshop_sch.Hourly_Value = starting_workshop

ac_setpoint_sch = idf.getobject("SCHEDULE:CONSTANT", "Measured_Setpoint")
if ac_setpoint_sch:
    ac_setpoint_sch.Hourly_Value = 40.0

occ_sch = idf.getobject("SCHEDULE:CONSTANT", "Measured_Occupancy")
if occ_sch:
    occ_sch.Hourly_Value = 0.0

idf.saveas(str(LIVE_IDF))

print(f"✅ State Sync Complete! Starting room pre-conditioned to {start_room:.1f}°C.")

# =====================================================================
# LAUNCH ENGINE
# =====================================================================
api.runtime.callback_begin_zone_timestep_after_init_heat_balance(
    state,
    timestep_callback
)

cmd_args = [
    "-w",
    str(EPW_PATH),
    "-d",
    str(SESSION_DIR),
    str(LIVE_IDF)
]

print("🚀 Starting Dynamic Operational Twin Engine (1-Minute Resolution)...")

try:
    api.runtime.run_energyplus(state, cmd_args)
except KeyboardInterrupt:
    force_shutdown()
