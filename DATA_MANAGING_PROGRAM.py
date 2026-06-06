import paho.mqtt.client as mqtt
import cv2
import numpy as np
import csv
import time
from datetime import datetime
from ultralytics import YOLO
import threading
from picamera2 import Picamera2
import os
import signal         
import socket
import sys

# ================= SINGLE INSTANCE LOCK =================
def prevent_duplicates():
    global _lock_socket
    # Create an invisible, local-only Linux socket
    _lock_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        # The '\0' makes it an abstract socket. It auto-deletes if the script crashes!
        _lock_socket.bind('\0thesis_hvac_datagather_lock')
    except socket.error:
        print("🛑 FATAL: Another instance of the Data Gatherer is already running in the background!")
        print("🛑 Yielding control. Aborting VS Code run...")
        sys.exit(1) # Instantly kills this duplicate script

# Run the lock immediately before doing anything else!
prevent_duplicates()
# ========================================================

# global flag
is_shutting_down = False
def sigterm_handler(signum, frame):
    global is_shutting_down
    # If already shutting down from CTRL+C, ignore the Master's extra signal
    if is_shutting_down:
        return 
    is_shutting_down = True
    print("\n[V13] Received shutdown command from Master Controller!")
    raise KeyboardInterrupt  
signal.signal(signal.SIGTERM, sigterm_handler)

# ================= CONFIGURATION =================
CSV_FILE = "/home/rkrichkid2001/thesis_aiproject/venv/testdata.csv"
ESP_LOG_FILE = "/home/rkrichkid2001/thesis_aiproject/venv/test_esp32_log.csv"
STABLE_CSV_FILE = "/home/rkrichkid2001/thesis_aiproject/venv/testdata_stabilized.csv"
RISBY_CSV_FILE = "/home/rkrichkid2001/thesis_aiproject/venv/Risby_data.csv"
DUAL_AC_CSV_FILE = "/home/rkrichkid2001/thesis_aiproject/venv/testdata_non_borf.csv" 
INTERVAL_SECONDS = 60     #interval for main data collection loop (changeable) should be 2mins atleast
ESP_TIMEOUT = 120         # seconds before ESP considered offline


MQTT_BROKER = "localhost"
MQTT_PORT = 1883

STABLE_WINDOW_SECONDS = INTERVAL_SECONDS * 6      # 15 minutes (changeable)
STABLE_STD_THRESHOLD = 0.10       # °C standard deviation threshold (changeable)
MIN_STABLE_SAMPLES = 5           # Minimum samples required for stability (changeable)

# ================= MQTT TOPICS =================
TOPICS = {
    # Inside sensors (ESP1 & ESP2)
    "inside_temp": ["/room/temp1", "/room/temp2"],
    "inside_humid": ["/room/humid1", "/room/humid2"],

    # Outside sensor (ESP3)
    "outside_temp": "/outside/temp3",
    "outside_humid": "/outside/humid3",

    # AC power monitoring (ESP4 & ESP5)
    "ac_power_4": "/ac/power4",
    "ac_power_5": "/ac/power5",

    # All ESP device diagnostics (1–5)
    "voltage": [f"/esp/voltage{i}" for i in range(1,6)],
    "power":   [f"/esp/power{i}" for i in range(1,6)],
    "status":  [f"/esp/status{i}" for i in range(1,6)],

    "ac_setpoint": "/ac/setpoint",
    "ac_mode": "/ac/mode",
    "ac_fan": "/ac/fan",
    # --- ADD THESE 3 LINES ---
    "ac2_setpoint": "/ac2/setpoint",
    "ac2_mode": "/ac2/mode",
    "ac2_fan": "/ac2/fan",
}

# ================= GLOBAL DATA =================
data_buffers = {
    "inside_temps": [],
    "inside_humids": [],
    "outside_temp": None,
    "outside_humid": None,
    "ac_setpoint": None,
    "ac_mode": None,
    "ac_fan": None,
    "ac_power_4": [],
    "ac_power_5": [],
    # --- ADD THESE 3 LINES ---
    "ac2_setpoint": None,
    "ac2_mode": None,
    "ac2_fan": None,
}

temp_history = []   # stores (timestamp_epoch, avg_temp)

# ESP device data storage
esp_data = {
    f"esp{i}": {
        "temp": None,
        "humid": None,
        "ac_power": None,
        "esp_voltage": None,
        "esp_power": None,
        "esp_status": None,
        "last_seen": None
    }
    for i in range(1, 6)
}

current_occupancy = 0
occupancy_buffer = []  
OCCUPANCY_WINDOW = 20

# ================= LOAD YOLO =================
print("Loading YOLOv8n model...")
model = YOLO("yolov8n.pt")
print("YOLOv8n loaded.")

# ================= SIMPLE PMV =================
def calculate_pmv(ta, rh, tr=None, va=0.1, clo=0.5, met=1.1, wme=0):
    """
    ISO 7730 PMV calculation (Fanger model)
    ta  : air temperature (°C)
    tr  : mean radiant temperature (°C) (if None, assume tr = ta)
    rh  : relative humidity (%)
    va  : air velocity (m/s)
    clo : clothing insulation (clo)
    met : metabolic rate (met)
    wme : external work (met), normally 0
    """

    if ta is None or rh is None:
        return 0.0

    if tr is None: # If mean radiant temperature isn't provided, assume it's the same as air temperature. 
        tr = ta    # This is standard practice for simple PMV calculations when radiant temp isn't measured.

    pa = rh * 10 * np.exp(16.6536 - 4030.183 / (ta + 235))  # calculates partial water vapor pressure in Pa using temperature and relative humidity

    icl = 0.155 * clo # converts clothing insulation from clo to m²K/W
    m = met * 58.15 # converts metabolic rate from met to W/m² (1 met = 58.15 W/m²)
    w = wme * 58.15 # converts external work from met to W/m² (1 met = 58.15 W/m²)
    mw = m - w

    if icl <= 0.078: 
        fcl = 1 + (1.29 * icl) # calculates clothing area factor for low insulation
    else:
        fcl = 1.05 + (0.645 * icl) # calculates clothing area factor for higher insulation

    hcf = 12.1 * np.sqrt(va) # calculates convective heat transfer coefficient based on air velocity
    taa = ta + 273 # converts air temperature from °C to K
    tra = tr + 273 # converts mean radiant temperature from °C to K

    tcla = taa + (35.5 - ta) / (3.5 * icl + 0.1) # initial guess for clothing surface temperature in K. 
    #This is based on the assumption that the clothing surface temperature is influenced by the air temperature and the insulation level of the clothing.
    p1 = icl * fcl # calculates a factor that combines clothing insulation and clothing area factor, which will be used in the iterative calculation of the clothing surface temperature.
    p2 = p1 * 3.96 # calculates a factor that combines the previous factor with the Stefan-Boltzmann constant (5.67e-8 W/m²K⁴) multiplied by 1e8 to adjust for units, which will be used in the iterative calculation of the clothing surface temperature.
    p3 = p1 * 100 # calculates a factor that combines the previous factor with 100, which will be used in the iterative calculation of the clothing surface temperature.
    p4 = p1 * taa # calculates a factor that combines the previous factor with the air temperature in K.
    p5 = 308.7 - 0.028 * mw + p2 * (tra / 100) ** 4 # calculates a factor that combines a constant (308.7), a term based on metabolic rate, and a term based on mean radiant temperature, which will be used in the iterative calculation of the clothing surface temperature.

    xn = tcla / 100 # initial guess for the clothing surface temperature in the iterative calculation, normalized by dividing by 100 to improve numerical stability.
    xf = xn         # variable to store the previous iteration's clothing surface temperature, used to check for convergence in the iterative calculation.
    eps = 0.00015   # convergence criterion for the iterative calculation of the clothing surface temperature. The iteration will stop when the change in clothing surface temperature between iterations is less than or equal to this value.

    for _ in range(150): # maximum of 150 iterations to find the clothing surface temperature that satisfies the energy balance equations in the PMV model.
        xf = xn       
        hcn = 2.38 * abs(100 * xf - taa) ** 0.25 # calculates the convective heat transfer coefficient based on the difference between the clothing surface temperature and the air temperature, using a formula that accounts for natural convection.
        hc = max(hcf, hcn) # selects the maximum of the convective heat transfer coefficient based on air velocity and the one based on natural convection, to ensure that the most significant mode of heat transfer is considered in the calculation.
        xn = (p5 + p4 * hc - p2 * xf ** 4) / (100 + p3 * hc) # updates the estimate of the clothing surface temperature based on the energy balance equations of the PMV model, which consider metabolic heat production, convective heat transfer, and radiative heat transfer.
        if abs(xn - xf) <= eps: # checks for convergence of the iterative calculation by comparing the change in clothing surface temperature between iterations to the specified convergence criterion. If the change is small enough, the iteration stops.
            break

    tcl = 100 * xn - 273 # final clothing surface temperature in °C, converted from K by multiplying by 100 and subtracting 273.
    # calculate the 6 heat loss terms based on the PMV model equations, which account for different modes of heat transfer from the body to the environment:
    hl1 = 3.05 * 0.001 * (5733 - 6.99 * mw - pa) # calculates the heat loss due to skin diffusion (insensible perspiration) or diffusion through the skin, which is a function of metabolic rate and partial water vapor pressure. 
    hl2 = 0.42 * (mw - 58.15) if mw > 58.15 else 0 # calculates the heat loss due to active sweating (comfort sweating) or evaporation of sweat.
    hl3 = 1.7 * 0.00001 * m * (5867 - pa) # calculates the heat loss due to latent heat loss by respiration, which is a function of metabolic rate. If the metabolic rate is above 1 met (58.15 W/m²), there is additional heat loss due to increased breathing; otherwise, there is no additional heat loss.
    hl4 = 0.0014 * m * (34 - ta) # calculates the heat loss due to convection from the skin, which is a function of metabolic rate and the difference between skin temperature (assumed to be 34°C) and air temperature.
    hl5 = 3.96 * fcl * (xn ** 4 - (tra / 100) ** 4) # calculates the heat loss due to radiation from the skin, which is a function of the clothing area factor and the difference between the clothing surface temperature and the mean radiant temperature, using the Stefan-Boltzmann law.
    hl6 = fcl * hc * (tcl - ta) # calculates the heat loss due to convection from the clothing surface, which is a function of the clothing area factor, the convective heat transfer coefficient, and the difference between the clothing surface temperature and the air temperature.

    ts = 0.303 * np.exp(-0.036 * m) + 0.028 # calculates the PMV scaling factor, which is a function of metabolic rate and is used to convert the combined heat loss terms into the PMV index, which ranges from -3 (cold) to +3 (hot) with 0 being neutral.
    pmv = ts * (mw - hl1 - hl2 - hl3 - hl4 - hl5 - hl6) # calculates the PMV index by combining the metabolic heat production with the various heat loss terms, scaled by the PMV scaling factor. The resulting PMV value indicates the thermal comfort level of the environment, with negative values indicating cold discomfort, positive values indicating heat discomfort, and values close to zero indicating thermal neutrality.

    return round(pmv, 2)


# ================= MQTT =================
def on_connect(client, userdata, flags, rc):
    print("MQTT connected")
    for topic_list in TOPICS.values():
        if isinstance(topic_list, list):
            for t in topic_list:
                client.subscribe(t)
        else:
            client.subscribe(topic_list)

def on_message(client, userdata, msg):
    topic = msg.topic
    payload_raw = msg.payload.decode().strip()

    try:
        value = float(payload_raw)
    except:
        value = payload_raw

    # Store incoming data in buffers and esp_data
    for i in range(1, 6):

        if topic == f"/room/temp{i}" and i in [1, 2]:
            data_buffers["inside_temps"].append(value)
            esp_data[f"esp{i}"]["temp"] = value
            esp_data[f"esp{i}"]["last_seen"] = time.time()

        elif topic == f"/room/humid{i}" and i in [1, 2]:
            data_buffers["inside_humids"].append(value)
            esp_data[f"esp{i}"]["humid"] = value
            esp_data[f"esp{i}"]["last_seen"] = time.time()

        elif topic == "/outside/temp3":
            data_buffers["outside_temp"] = value
            esp_data["esp3"]["temp"] = value
            esp_data["esp3"]["last_seen"] = time.time()

        elif topic == "/outside/humid3":
            data_buffers["outside_humid"] = value
            esp_data["esp3"]["humid"] = value
            esp_data["esp3"]["last_seen"] = time.time()
            
        elif topic == f"/ac/power{i}" and i in [4, 5]:
            esp_data[f"esp{i}"]["ac_power"] = value
            esp_data[f"esp{i}"]["last_seen"] = time.time()

        elif topic == f"/esp/voltage{i}":
            esp_data[f"esp{i}"]["esp_voltage"] = value
            esp_data[f"esp{i}"]["last_seen"] = time.time()

        elif topic == f"/esp/power{i}":
            esp_data[f"esp{i}"]["esp_power"] = value
            esp_data[f"esp{i}"]["last_seen"] = time.time()

        elif topic == f"/esp/status{i}":
            esp_data[f"esp{i}"]["esp_status"] = value
            esp_data[f"esp{i}"]["last_seen"] = time.time()

    # AC Settings Topics   
    if topic == TOPICS["ac_power_4"]:
        data_buffers["ac_power_4"].append(value)
        
    elif topic == TOPICS["ac_power_5"]:
        data_buffers["ac_power_5"].append(value)

    elif topic == TOPICS["ac_setpoint"]:
        data_buffers["ac_setpoint"] = value

    elif topic == TOPICS["ac_mode"]:
        data_buffers["ac_mode"] = str(value).lower()

    elif topic == TOPICS["ac_fan"]:
        data_buffers["ac_fan"] = str(value).lower()
        
    # --- ADD THIS BLOCK FOR COOLIX ---
    elif topic == TOPICS["ac2_setpoint"]:
        data_buffers["ac2_setpoint"] = value

    elif topic == TOPICS["ac2_mode"]:
        data_buffers["ac2_mode"] = str(value).lower()

    elif topic == TOPICS["ac2_fan"]:
        data_buffers["ac2_fan"] = str(value).lower()


# ================= OCCLUSION CALIBRATION =================
def calibrate_crowd_count(raw_count):
    """
    Compensates for physical camera occlusion in a crowded classroom.
    The denser the crowd, the more people are hidden behind others.
    """
    if raw_count <= 4:
        return raw_count  # room count scale
    elif 5 <= raw_count <= 8:
        return int(raw_count * 1)  # Medium crowd scaler
    else:
        return int(raw_count * 1)  # Heavy crowd scaler

# ================= CAMERA THREAD =================
def people_counting_thread():
    global current_occupancy
    
    picam2 = None  # Initialize outside try so finally can access it
    
    try:
        print("Initializing Pi Camera 2...")
        picam2 = Picamera2()
        config = picam2.create_preview_configuration(main={"size": (1280, 720)})
        picam2.configure(config)
        picam2.start()
        print("Camera started successfully (resolution 1280x720, imgsz=832, conf=0.12, iou=0.45).")
    
    except Exception as e:
        print(f"!!! CAMERA FAILED TO START: {e}")
        print("Occupancy detection disabled - will remain 0")
        while True:
            time.sleep(30)  # Sleep longer to reduce CPU usage when failed
    
    else:
        # Only enter loop if camera started successfully
        print("Starting YOLO occupancy detection loop...")
        try:
            while True:
                frame = picam2.capture_array()              # RGB
                frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                
                # --- OPTIMIZED YOLO CALL ---
                # classes=[0]: Only looks for humans (Massive CPU saver)
                # conf=0.10: Accepts slightly blurry/distant faces
                # iou=0.35: Allows people sitting close together to overlap without being deleted
                results = model(frame, verbose=False, imgsz=832, classes=[0], conf=0.12, iou=0.45)
                
                # Because we filtered inside model(), we don't need any 'for' loops!
                raw_count = len(results[0].boxes)
                
                # --- APPLY OCCLUSION CALIBRATION ---
                calibrated_count = calibrate_crowd_count(raw_count)
                
                # --- NEW SMOOTHING LOGIC ---
                occupancy_buffer.append(calibrated_count)
                
                # Keep only the most recent X readings (Requires OCCUPANCY_WINDOW = 30 at the top of your script)
                if len(occupancy_buffer) > OCCUPANCY_WINDOW:
                    occupancy_buffer.pop(0)
                
                # The true occupancy is the MOST FREQUENT number of people seen in the last 3 minutes
                if len(occupancy_buffer) > 0:
                    # max(set(buffer), key=buffer.count) finds the most common number (the mode)
                    current_occupancy = max(set(occupancy_buffer), key=lambda x: (occupancy_buffer.count(x), x)) if occupancy_buffer else 0
                else:
                    current_occupancy = 0
                # ---------------------------
                
                time.sleep(6)  # ~10 fps effective, low CPU
                
        except Exception as e:
            print(f"Error during YOLO loop: {e}")
        
        finally:
            if picam2 is not None:
                picam2.stop()
                print("Camera resources released")
                
# ================= MAIN =================
def main():
    
    # # ================= MANUAL TIME SETUP (30s TIMEOUT) =================
    # print("\n=== OFFLINE TIME SYNCHRONIZATION ===")
    # print("The Raspberry Pi may not know the correct time without internet.")
    # print("Enter current time (YYYY-MM-DD HH:MM) [You have 30 seconds...]: ")
    
    # # Non-blocking input using select (Works perfectly on Raspberry Pi/Linux)
    # i, o, e = select.select([sys.stdin], [], [], 30)
    
    # if (i):
    #     user_input = sys.stdin.readline().strip()
    # else:
    #     print("\n⏳ Timeout reached! No input detected.")
    #     user_input = ""
        
    # manual_start_time = None
    # monotonic_start = None
    
    # if user_input:
    #     try:
    #         manual_start_time = datetime.strptime(user_input, "%Y-%m-%d %H:%M")
    #         monotonic_start = time.monotonic()
    #         print(f"✅ Success! Internal clock set to: {manual_start_time}")
    #     except ValueError:
    #         print("⚠️ Invalid format. Falling back to Raspberry Pi system time.")
    # else:
    #     print("⏭️ Skipped. Using Raspberry Pi system time.")
    # print("========================================\n")

    threading.Thread(target=people_counting_thread, daemon=True).start()

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()

    # Prepare main CSV
    header = [
        "inside air temp",
        "inside air humidity",
        "occupancy",
        "outside air temp",
        "outside air humidity",
        "time of the day",
        "ac temperature setpoint",
        "ac mode",
        "ac fan speed",
        "ac energy consumption (kw)",
        "thermal comfort PMV"
    ]

    #Prepare main CSV file
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'w', newline='') as f:
            csv.writer(f).writerow(header)

    # Prepare ESP CSV
    esp_header = ["timestamp", "esp_id", "voltage", "power", "status"]
    if not os.path.exists(ESP_LOG_FILE):
        with open(ESP_LOG_FILE, 'w', newline='') as f:
            csv.writer(f).writerow(esp_header)
            
    # Prepare Stabilized CSV
    if not os.path.exists(STABLE_CSV_FILE):
        with open(STABLE_CSV_FILE, 'w', newline='') as f:
            csv.writer(f).writerow(header + ["timestamp"])
            
    # --- ADD THIS: Prepare Dual AC CSV ---
    dual_ac_header = [
        "inside air temp", "inside air humidity", "occupancy",
        "outside air temp", "outside air humidity", "time of the day",
        "samsung setpoint", "samsung mode", "samsung fan speed", "samsung power (W)",
        "coolix setpoint", "coolix mode", "coolix fan speed", "coolix power (W)",
        "total combined power (W)", "thermal comfort PMV", "timestamp"
    ]
    if not os.path.exists(DUAL_AC_CSV_FILE):
        with open(DUAL_AC_CSV_FILE, 'w', newline='') as f:
            csv.writer(f).writerow(dual_ac_header)
            
    # --- ADD THIS: Prepare Risby CSV ---
    if not os.path.exists(RISBY_CSV_FILE):
        with open(RISBY_CSV_FILE, 'w', newline='') as f:
            csv.writer(f).writerow(header + ["timestamp", "energy_kWh"])

    print("=== Data collection started ===")
    
    # Set up a counter to track the 5-minute intervals
    five_min_counter = 0
    
    # --- ADD THESE TWO LINES ---
    last_ac_state_str = None
    stability_lockout_mins = 0

    try:
        while True:
            time.sleep(INTERVAL_SECONDS)
            
            # # ================= TIME CALCULATION =================
            # if manual_start_time and monotonic_start:
            #     # Calculate exactly how many seconds have passed since you typed the time
            #     elapsed_seconds = time.monotonic() - monotonic_start
            #     # Add those seconds to your manual start time
            #     now = manual_start_time + timedelta(seconds=elapsed_seconds)
            # else:
            #     # Fallback just in case you skipped the prompt
            #     now = datetime.now()
            now = datetime.now()

            time_of_day = now.strftime("%H:%M")

            print("\n================= FREQUENCY REPORT =================")

            # ===== PRINT ESP SUMMARY =====
            for i in range(1, 6):
                esp = esp_data[f"esp{i}"]

                if any(v is not None for v in esp.values()):
                    print(
                        f"[MQTT] ESP_{i}: "
                        f"Sensor Temp = {esp['temp']}, "
                        f"Sensor Humidity = {esp['humid']}, "
                        f"AC Power = {esp['ac_power']}, "
                        f"ESP Voltage = {esp['esp_voltage']}, "
                        f"ESP Power = {esp['esp_power']}, "
                        f"Status = {esp['esp_status']}"
                    )

            # ===== MAIN CSV SAVE =====
            inside_temp_avg = np.mean(data_buffers["inside_temps"]) if data_buffers["inside_temps"] else None
            inside_humid_avg = np.mean(data_buffers["inside_humids"]) if data_buffers["inside_humids"] else None
                
            # Calculate Averages safely
            p4_avg = np.mean(data_buffers["ac_power_4"]) if data_buffers["ac_power_4"] else 0.0
            p5_avg = np.mean(data_buffers["ac_power_5"]) if data_buffers["ac_power_5"] else 0.0
            
            # FIXED MATH
            ac_sum_power = float(p4_avg + p5_avg) 

            if inside_temp_avg is not None and inside_humid_avg is not None:

                inside_temp_avg = float(inside_temp_avg)
                inside_humid_avg = float(inside_humid_avg)

                pmv = calculate_pmv(inside_temp_avg, inside_humid_avg)

                row = [
                    round(inside_temp_avg, 1),
                    round(inside_humid_avg, 1),
                    current_occupancy,
                    data_buffers["outside_temp"] or "",
                    data_buffers["outside_humid"] or "",
                    time_of_day,
                    data_buffers["ac_setpoint"] or "",
                    data_buffers["ac_mode"] or "",
                    data_buffers["ac_fan"] or "",
                    round(ac_sum_power, 1),
                    pmv
                ]

                with open(CSV_FILE, 'a', newline='') as f:
                    csv.writer(f).writerow(row)

                print(
                    f"Calculated Value [General Average]:\n"
                    f"  Inside Temp Avg = {row[0]}\n"
                    f"  Inside Humidity Avg = {row[1]}\n"
                    f"  Occupancy = {row[2]}\n"
                    f"  Outside Temp = {row[3]}\n"
                    f"  Outside Humidity = {row[4]}\n"
                    f"  Time of Day = {row[5]}\n"
                    f"  AC Setpoint = {row[6]}\n"
                    f"  AC Mode = {row[7]}\n"
                    f"  AC Fan Speed = {row[8]}\n"
                    f"  AC Power (W) = {row[9]}\n"
                    f"  PMV = {row[10]}"
                )
                print("[testdata.csv updated and saved]")

                # ================= DUAL AC CSV SAVE =================
                samsung_sp = data_buffers["ac_setpoint"] or ""
                samsung_mode = data_buffers["ac_mode"] or ""
                samsung_fan = data_buffers["ac_fan"] or ""
                samsung_pwr = round(p4_avg, 1) if p4_avg else 0.0
                
                coolix_sp = data_buffers["ac2_setpoint"] or ""
                coolix_mode = data_buffers["ac2_mode"] or ""
                coolix_fan = data_buffers["ac2_fan"] or ""
                coolix_pwr = round(p5_avg, 1) if p5_avg else 0.0
                
                # Grab the exact timestamp for the final column
                timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
                
                dual_ac_row = [
                    round(inside_temp_avg, 1), round(inside_humid_avg, 1), current_occupancy,
                    data_buffers["outside_temp"] or "", data_buffers["outside_humid"] or "", time_of_day,
                    samsung_sp, samsung_mode, samsung_fan, samsung_pwr,
                    coolix_sp, coolix_mode, coolix_fan, coolix_pwr,
                    round(ac_sum_power, 1), pmv, timestamp_str
                ]
                
                with open(DUAL_AC_CSV_FILE, 'a', newline='') as f:
                    csv.writer(f).writerow(dual_ac_row)
                print("[testdata_non_borf.csv updated and saved]")
                # ====================================================
                
                # ================= 5-MINUTE RISBY DATA SAVE =================
                five_min_counter += 1
                
                if five_min_counter >= 5:
                    # Calculate kWh: (Watts / 1000) * (5 mins / 60 mins)
                    safe_power = ac_sum_power if ac_sum_power is not None else 0.0
                    interval_kwh = round((safe_power / 1000.0) * (5.0 / 60.0), 5)
                    
                    # Create the exact same row, but tack on the timestamp and kWh at the end
                    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
                    risby_row = row + [timestamp_str, interval_kwh]
                    
                    with open(RISBY_CSV_FILE, 'a', newline='') as f:
                        csv.writer(f).writerow(risby_row)
                        
                    print(f"💾 [Risby_data.csv updated] -> Interval Energy: {interval_kwh} kWh")
                    
                    # Reset the counter
                    five_min_counter = 0
                # ============================================================

                # ================= AC SETTING CHANGE DETECTOR =================
                # Combine the current AC settings into a single string to check for changes
                current_ac_state_str = f"{row[6]}_{row[7]}_{row[8]}"
                
                # If this isn't the first boot-up, and the settings just changed:
                if last_ac_state_str is not None and current_ac_state_str != last_ac_state_str:
                    print(f"⚠️ AC SETTINGS CHANGED: [{last_ac_state_str}] -> [{current_ac_state_str}]")
                    print("Initiating 5-minute stability lockout and clearing old temperature memory.")
                    stability_lockout_mins = 5
                    temp_history.clear() # Completely wipe the old state's temperatures!
                    
                last_ac_state_str = current_ac_state_str


                # ================= STABILITY TRACKING (STD METHOD) =================
                current_time_epoch = time.time()
                is_stable = False
                
                # If we are in the 5-minute cooldown, ignore the room temperatures completely
                if stability_lockout_mins > 0:
                    stability_lockout_mins -= 1
                    print(f"⏳ Stability Tracking Paused (Letting AC mix the air). {stability_lockout_mins} mins left.")
                
                # Otherwise, proceed with normal stability tracking
                else:
                    # Add current temperature to history (Only happens AFTER lockout is over)
                    if inside_temp_avg is not None:
                        temp_history.append((current_time_epoch, inside_temp_avg))

                    # Keep only last STABLE_WINDOW_SECONDS data
                    temp_history[:] = [
                        (t, temp) for (t, temp) in temp_history
                        if current_time_epoch - t <= STABLE_WINDOW_SECONDS
                    ]

                    # Require minimum samples to compute STD properly
                    if len(temp_history) >= MIN_STABLE_SAMPLES:
                        temps = [temp for (_, temp) in temp_history]
                        std_dev = np.std(temps)

                        print(f"Stability Check → STD over last {STABLE_WINDOW_SECONDS}s = {round(std_dev,3)} °C")

                        if std_dev <= STABLE_STD_THRESHOLD:
                            is_stable = True
                            print("✅ Room temperature is STABLE.")
                        else:
                            print(" ")
                    else:
                        print(f"Gathering fresh stability data... ({len(temp_history)}/{MIN_STABLE_SAMPLES} samples)")
                        
                # ================= SAVE TO STABILIZED CSV =================
                if is_stable:
                    stabilized_row = row + [now.strftime("%Y-%m-%d %H:%M:%S")]
                    with open(STABLE_CSV_FILE, 'a', newline='') as f:
                        csv.writer(f).writerow(stabilized_row)
                    print("[testdata_stabilized.csv updated and saved]")
                else:
                    print(" ")
                
                
            data_buffers["inside_temps"].clear()
            data_buffers["inside_humids"].clear()
            data_buffers["ac_power_4"].clear()
            data_buffers["ac_power_5"].clear()
            data_buffers["outside_temp"] = None
            data_buffers["outside_humid"] = None
            #data_buffers["ac_setpoint"] = None
            #data_buffers["ac_mode"] = None
            #data_buffers["ac_fan"] = None     

            # ===== ESP CSV SAVE =====

            esp_rows_written = False

            with open(ESP_LOG_FILE, 'a', newline='') as f:
                writer = csv.writer(f)

                for i in range(1, 6):
                    esp = esp_data[f"esp{i}"]
                    last_seen = esp["last_seen"]

                    if last_seen and (time.time() - last_seen) < ESP_TIMEOUT:

                        writer.writerow([
                            now.strftime("%Y-%m-%d %H:%M:%S"),
                            f"ESP_{i}",
                            esp["esp_voltage"] or "",
                            esp["esp_power"] or "",
                            esp["esp_status"] or ""
                        ])
                        esp_rows_written = True

                    else:
                        # Mark offline
                        esp_data[f"esp{i}"] = {
                            "temp": None,
                            "humid": None,
                            "ac_power": None,
                            "esp_voltage": None,
                            "esp_power": None,
                            "esp_status": "OFFLINE",
                            "last_seen": None
                        }

            if esp_rows_written:
                print("[ESP32 Log File updated and saved]")
            else:
                print("No active ESP devices. No Data Received")


            print("====================================================\n")
        
    except KeyboardInterrupt: #CTRL+C to stop program
        global is_shutting_down
        is_shutting_down = True  # Put the "Do Not Disturb" sign up!
        print("\nShutting down safely...")
        client.loop_stop()
        client.disconnect()
        print("MQTT disconnected.")
        print("Program terminated.")


if __name__ == "__main__":
    main()
