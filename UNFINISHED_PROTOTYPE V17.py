# =================================================================
# THESIS MASTER CONTROLLER: HYBRID AI + STABILITY GUARDRAILS
# =================================================================

import time
import pandas as pd
import threading
import subprocess
import os
import sys
import paho.mqtt.client as mqtt
import numpy as np
import socket

# --- SINGLE INSTANCE LOCK ---
def prevent_duplicates():
    global _lock_socket
    # Create an invisible, local-only Linux socket
    _lock_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        # The '\0' makes it an abstract socket. It auto-deletes if the script crashes!
        _lock_socket.bind('\0thesis_hvac_master_lock') 
    except socket.error:
        print("🛑 FATAL: Another instance of the Master Controller is already running in the background!")
        print("🛑 Yielding control to the background service. Aborting VS Code run...")
        sys.exit(1) # Instantly kills this duplicate script

# Run the lock immediately before doing anything else
prevent_duplicates()
# -----------------------------

# Import your V7 AI Deployment script
from THESIS_DEPLOYMENT_MODEL_V8 import find_best_action_for_environment

# --- CONFIGURATION ---
DATA_GATHER_SCRIPT = "THESIS_GATHER_DATA_V17.py"
LIVE_DATA_CSV = "/home/rkrichkid2001/thesis_aiproject/venv/testdata.csv"
LOG_FILE = "/home/rkrichkid2001/thesis_aiproject/venv/master_debug_logfile.txt"
LOOP_INTERVAL = 60  # The Master Controller checks the room EVERY MINUTE
COMPRESSOR_DELAY_MINS = 5  # Anti-short cycle delay for AC on/off

# --- MQTT CONFIGURATION ---
MQTT_BROKER = "localhost"  # Since this script runs on the Pi itself
MQTT_PORT = 1883
COMMAND_TOPIC = "/ac/master_command"

# STABILITY AND TRIGGER CONFIGS
STABILITY_WINDOW = 5          # Look at the last 5 minutes to determine stability
PMV_STD_THRESHOLD = 0.015      # STRICT: Max Standard Deviation allowed over 5 mins
#EXTENDED_STABILITY_WINDOW = 10 # Fallback: Look at the last 10 minutes
EXTENDED_PMV_STD_THRESHOLD = 0.025 # FALLBACK: Max Standard Deviation allowed over 10 mins
DRASTIC_OCCUPANCY_CHANGE = 5  # If occupancy changes by this much, force AI to re-evaluate instantly
TIMEOUT_WINDOW = 35           # The absolute maximum 35 time to wait before forcing AI intervention

gather_process = None

        
class DualLogger(object):
    """Hijacks the terminal output to print to the screen AND save to a text file with timestamps."""
    def __init__(self, filename):
        self.terminal = sys.stdout
        self.log = open(filename, "a", encoding="utf-8")
        self.needs_timestamp = True # Tracks if we are at the start of a fresh line

    def write(self, message):
        if not message:
            return
        
        # Break the incoming print message into chunks to handle multi-line prints safely
        lines = message.splitlines(True)
        
        for line in lines:
            # If we are starting a fresh line, and the line isn't JUST a blank enter key
            if self.needs_timestamp and line.strip() != "":
                timestamp = time.strftime("[%Y-%m-%d %H:%M:%S] ")
                self.terminal.write(timestamp + line)
                self.log.write(timestamp + line)
            else:
                # Print normally without a timestamp (preserves your blank spacing and formatting)
                self.terminal.write(line)
                self.log.write(line)
            
            # If this piece of text ended with a newline, flag the NEXT piece to get a timestamp
            self.needs_timestamp = line.endswith('\n')
            
        self.log.flush()           # Push from Python memory
        os.fsync(self.log.fileno()) # Force Linux to write to SD card instantly

    def flush(self):
        self.terminal.flush()
        self.log.flush()

def relay_subprocess_output(process_pipe):
    """Listens to the background script and forces its prints through the DualLogger."""
    # Read the pipe line-by-line as it comes in
    for line in iter(process_pipe.readline, b''):
        # Decode the bytes to text, strip the extra newline, and print it
        decoded_line = line.decode('utf-8', errors='replace').rstrip()
        print(f"[V16 GATHERER] {decoded_line}")
    
def start_data_gatherer():
    """Starts V16 and pipes its live output into the Master's DualLogger."""
    global gather_process
    print(f"🔄 [MASTER] Starting Data Gatherer ({DATA_GATHER_SCRIPT})...")
    
    # Add stdout=subprocess.PIPE and stderr=subprocess.STDOUT
    # This captures both normal prints and crash errors from V16!
    gather_process = subprocess.Popen(
        [sys.executable, "-u", DATA_GATHER_SCRIPT],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )
    
    # Start the relay thread so it doesn't block the Master Controller
    threading.Thread(
        target=relay_subprocess_output, 
        args=(gather_process.stdout,), 
        daemon=True
    ).start()
    
    time.sleep(5)

def get_csv_row_count():
    """Helper function to cleanly and efficiently count rows without loading Pandas."""
    try:
        with open(LIVE_DATA_CSV, 'r') as f:
            # This is a highly optimized Python trick to count lines instantly
            return sum(1 for _ in f)
    except Exception:
        return 0

def get_latest_environment():
    """Reads the exact current room state from the CSV."""
    try:
        df = pd.read_csv(LIVE_DATA_CSV)
        if df.empty: return None
        latest_row = df.iloc[-1]
        
        if pd.isna(latest_row['outside air temp']): return None

        return {
            'inside_air_temperature': float(latest_row['inside air temp']),
            'inside_air_humidity': float(latest_row['inside air humidity']),
            'num_occupants': int(latest_row['occupancy']),
            'outside_air_temperature': float(latest_row['outside air temp']),
            'outside_air_humidity': float(latest_row['outside air humidity']),
            'actual_pmv': float(latest_row['thermal comfort PMV'])
        }
    except Exception:
        return None

def print_v7_prediction(env, ai_result):
    """Helper to beautifully print V7 predictions exactly as requested."""
    b = ai_result['best_action']
    display_setp = f"{b['ac_temperature_setpoint']} °C" if b['ac_temperature_setpoint'] != 0 else "N/A"
    print(f"📥 INPUTS  -> Occupancy: {env['num_occupants']} | "
          f"Outside Temp: {env['outside_air_temperature']}°C | "
          f"Outside Hum: {env['outside_air_humidity']}%")
    print(f"📤 OUTPUTS -> AC Setpoint: {display_setp} | "
          f"Mode: {b['ac_mode_label'].upper()} | "
          f"Fan: {b['ac_fan_speed_label'].upper()}")
    print(f"⚡ EXPECT  -> PMV Comfort: {ai_result['best_pmv']:.3f} | "
          f"Power Draw: {ai_result['best_energy_watts']:.1f} W | "
          f"Inside Temp: {ai_result['best_temp']:.1f} °C")
    
def calculate_dynamic_timeout(current_pmv):
    """
    Scales the timeout window between 15 and 35 minutes.
    If PMV is far (e.g., > 2.0), timeout = 35 mins.
    If PMV is close (e.g., < 0.5), timeout = 15 mins.
    """
    dist = max(0.0, abs(current_pmv) - 0.5)
    
    # Math: 15 (base) + (dist / 1.5) * 20 (the range difference between 15 and 35)
    calc_timeout = 15.0 + (dist / 1.5) * 20.0
    
    # Clip safely between the new minimum (15) and maximum (35)
    return int(np.clip(calc_timeout, 15, 35))

def main_ai_loop():
    print(f"\n🧠 [MASTER] AI Engine active. Checking room every {LOOP_INTERVAL} seconds...\n")
    
    # --- SETUP MQTT ---
    mqtt_client = mqtt.Client("Master_Controller_V5")
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_start()
        print("🌐 [MQTT] Connected to Broker. Ready to transmit AC commands.")
    except Exception as e:
        print(f"❌ [MQTT] Failed to connect to Broker: {e}")
    # ------------------
    
    current_state = "WAITING_FOR_DATA"
    pmv_buffer = []  # Stores recent PMV readings to check stability
    last_env = None
    last_sent_command = None
    
    # AC Hardware Tracking
    stored_v7_action = None
    active_action = {
        'ac_temperature_setpoint': 0,
        'ac_mode_label': 'off',
        'ac_fan_speed_label': 'off'
    }
    # --- ADD THESE TWO NEW MEMORY VARIABLES ---
    last_successful_action = None  
    wake_reason = None             
    # ------------------------------------------
    last_power_toggle_time = 0  # Tracks the exact time the AC was turned ON or OFF
    # Record the starting row count to prevent reading yesterday's data
    # --- DEBOUNCE TRACKERS ---
    zero_occ_counter = 0
    last_positive_occ = 1
    # -------------------------
    golden_zone_active = False
    baseline_occupancy = None
    # --- NEW: ANTI-OVERCOOL TRACKERS ---
    anti_overcool_start_time = 0
    anti_overcool_start_pmv = 0
    anti_overcool_escalated = False
    # 1. Rename this tracker
    last_processed_row_count = get_csv_row_count()
    current_timeout_window = TIMEOUT_WINDOW  # This will dynamically adjust based on how far we are from the comfort zone
    
    while True:
        
        # --- ADD THIS: THE MASTER HEARTBEAT ---
        try:
            mqtt_client.publish("/master/heartbeat", "ONLINE")
        except:
            pass
        # --------------------------------------
        
        # 2. Change the sleep to 5 seconds. It will act as a fast "polling" monitor.
        time.sleep(5) 
        
        current_row_count = get_csv_row_count()
        
        # 3. 🛡️ THE GATEKEEPER: Absolutely refuse to proceed unless a NEW row exists
        if current_row_count <= last_processed_row_count:
            # If we are just booting up, print a nice message, otherwise stay silent to avoid spam
            if current_state == "WAITING_FOR_DATA":
                print("⏳ [MASTER] Booting: Waiting for V16 to finish scan and log fresh data...")
            continue # Instantly skip the rest of the code and wait 5 more seconds
            
        # 4. WE HAVE NEW DATA! Update the tracker so we don't read this row twice.
        last_processed_row_count = current_row_count
        
        # Now we can safely proceed with the logic!
        print("\n" + "█"*60)
        print(f"🕒 [STATE: {current_state}] TICK: {time.strftime('%H:%M:%S')}")

        env = get_latest_environment()
        if env is None:
            print("⏳ [MASTER] Waiting for valid sensor data...")
            print("█"*60 + "\n")
            continue
            
        pmv = env['actual_pmv']
        #occ = env['num_occupants']
        
        # ==========================================
        # 🛡️ 5-MINUTE OCCUPANCY DEBOUNCE GUARDRAIL
        # ==========================================
        actual_occ = env['num_occupants']
        
        if actual_occ == 0:
            zero_occ_counter += 1
            if zero_occ_counter < 5:
                print(f"⏳ [STANDBY] Room empty for {zero_occ_counter}/5 mins. Masking data to prevent premature AC shutoff...")
                occ = last_positive_occ # Fake the data to V7 so it doesn't shut down
            else:
                occ = 0 # It has been 5 minutes. Pass the truth.
        else:
            zero_occ_counter = 0
            last_positive_occ = actual_occ
            occ = actual_occ
            
        env['num_occupants'] = occ # Overwrite the value for the AI inference
        # ==========================================
        
        # ==========================================
        # 📡 ISOLATED MQTT OCCUPANCY PUBLISHER
        # ==========================================
        try:
            # Reuses your existing active connection to silently beam the crowd size
            mqtt_client.publish("hvac/sensor/occupancy", str(occ))
            mqtt_client.publish("/room/pmv", f"{pmv:.2f}")
        except Exception:
            pass # If network drops, fail silently. NEVER crash the Master loop.
        # ==========================================
        
        # --- 1. TRACK STABILITY (Dynamic STD Trend Method) ---
        pmv_buffer.append(pmv)
        if len(pmv_buffer) > current_timeout_window:
            pmv_buffer.pop(0) # Keep buffer safely at the dynamic size
            
        is_stable = False
        
        # We need at least 5 minutes to do anything
        if len(pmv_buffer) >= STABILITY_WINDOW:
            
            # --- THE FULL TREND CHECK (When buffer hits dynamic limit) ---
            if len(pmv_buffer) == current_timeout_window:
                # FIX: Always grab the LAST 15 minutes for the trend check
                trend_data = pmv_buffer[-15:]
                
                # NEW OVERLAPPING LOGIC
                w1 = trend_data[0:5]   # Oldest 5 mins 
                w2 = trend_data[0:10]  # Minutes 0 to 10 (Cumulative 10 mins)
                w3 = trend_data[5:15]  # Minutes 5 to 15 (Rolling 10 mins)

                std1, std2, std3 = np.std(w1), np.std(w2), np.std(w3)

                print(f"-> ROOM -> PMV: {pmv:.2f} | Occ: {occ} | Target Limit: {current_timeout_window}m")
                print(f"   ↳ STD TREND -> Mins 1-5: {std1:.3f} | Mins 1-10: {std2:.3f} | Mins 6-15: {std3:.3f}")

                if std3 <= PMV_STD_THRESHOLD and std3 <= (std2 + 0.01):
                    is_stable = True
                    print("   ✓ SYSTEM SETTLED: Variance has successfully flattened out.")
                else:
                    print("   ⏳ SYSTEM TRANSIENT: Variance is still shifting. Waiting for flatline.")

            # --- THE EARLY-BIRD CHECK (Now requires 10 minutes instead of 5) ---
            elif len(pmv_buffer) >= 10:
                # Grab the last 10 minutes for the strict early-bird check
                last_10 = pmv_buffer[-10:]
                std_recent = np.std(last_10)

                print(f"-> ROOM -> PMV: {pmv:.2f} | Occ: {occ}")
                print(f"   ↳ RECENT STD (Last 10m): {std_recent:.3f} | Gathering trend... ({len(pmv_buffer)}/{current_timeout_window})")

                if std_recent <= PMV_STD_THRESHOLD:
                    is_stable = True
                    print("   ✓ SYSTEM SETTLED EARLY: Variance is exceptionally low over the last 10 minutes.")

            # --- STILL GATHERING INITIAL DATA (Minutes 1 to 9) ---
            else:
                print(f"-> ROOM -> PMV: {pmv:.2f} | Occ: {occ}")
                print(f"   ↳ Gathering initial data... ({len(pmv_buffer)}/{STABILITY_WINDOW})")

        # --- 2. CHECK FOR DRASTIC ENVIRONMENT CHANGES ---
        drastic_change = False
        if last_env is not None and current_state not in ["WAITING_FOR_DATA", "PRELIM_EVALUATION"]:
            
            if baseline_occupancy is None:
                baseline_occupancy = occ
            
            # THE FIX: Did the crowd grow/shrink by 5+ since the AI LAST made a decision?
            if abs(occ - baseline_occupancy) >= DRASTIC_OCCUPANCY_CHANGE:
                print(f"⚠️ CROWD SHIFT DETECTED! Occupancy drifted from {baseline_occupancy} (Baseline) to {occ}.")
                drastic_change = True
             
            #Safely Trigger on the 5th minute of zero occupancy   
            elif occ == 0 and baseline_occupancy != 0:
                print(f"🚷 5-MIN ZERO OCCUPANCY CONFIRMED! Room is officially empty. Forcing immediate shutoff.")
                drastic_change = True

        last_env = env

        if drastic_change:
            current_state = "PRELIM_EVALUATION"
            golden_zone_active = False
            baseline_occupancy = occ # Update the baseline so it doesn't trigger repeatedly!
            pmv_buffer.clear()
            last_successful_action = None
            
        # ==========================================
        # 🛡️ THE FIX: GLOBAL ANTI-OVERCOOL TRIPWIRE
        # ==========================================
        # If the room drops to -0.1 and there are people inside, instantly override EVERYTHING.
        if pmv < -0.1 and current_state != "ANTI_OVERCOOL" and occ > 0:
            print(f"🥶 CRITICAL OVERCOOLING DETECTED (PMV: {pmv:.2f} <= -0.1). ENGAGING BRUTE-FORCE OVERRIDE.")
            current_state = "ANTI_OVERCOOL"
            anti_overcool_start_time = time.time()
            anti_overcool_start_pmv = pmv
            anti_overcool_escalated = False
            golden_zone_active = False
            pmv_buffer.clear()

        # ==========================================
        # STATE LOGIC MACHINE
        # ==========================================
        
        if current_state == "WAITING_FOR_DATA":
            print("🔄 Initializing baseline AI evaluation...")
            current_state = "PRELIM_EVALUATION"

        if current_state == "PRELIM_EVALUATION":
            print("🔎 Room is outside comfort zone. V7 Analyzing...")
            baseline_occupancy = occ # <--- ADD THIS HERE
            try:
                ai_result = find_best_action_for_environment(env)
                print_v7_prediction(env, ai_result)
                
                stored_v7_action = ai_result['best_action']
                v7_setpoint = stored_v7_action['ac_temperature_setpoint']
                
                # =====================================================================
                # THE FIX: CONTEXTUAL MEMORY GUARDRAIL
                # If the room just drifted HOT, the AI is strictly forbidden from 
                # suggesting a setpoint warmer than the one that originally cooled the room.
                # =====================================================================
                if wake_reason == "drift_hot" and last_successful_action is not None:
                    old_setp = last_successful_action['ac_temperature_setpoint']
                    
                    if v7_setpoint > old_setp and v7_setpoint != 0 and old_setp != 0:
                        print(f"🛡️ GUARDRAIL ACTIVE: Room drifted HOT, but AI guessed WARMER ({v7_setpoint}°C > {old_setp}°C).")
                        print(f"🛡️ Rejecting AI hallucination. Reverting to last successful command ({old_setp}°C).")
                        stored_v7_action = last_successful_action.copy()
                        v7_setpoint = stored_v7_action['ac_temperature_setpoint']
                
                # Reset the wake reason so this guardrail only fires once per wake-up
                wake_reason = None
                # =====================================================================
                
                # Check for TURBO override: Prelim + Crowded (>10) + Hot room (>=1.2)
                if occ >= 8 and pmv >= 1.2:
                    print(f"🔥 CROWD & HEAT DETECTED ({occ} people, PMV {pmv:.2f}). OVERRIDING V7 FOR FAST COOLING!")
                    active_action = {
                        'ac_temperature_setpoint': max(17, v7_setpoint - 4), # Ensure it doesn't go below AC min
                        'ac_mode_label': 'cool',
                        'ac_fan_speed_label': '3'
                    }
                    current_state = "TURBO_COOL"
                else:
                    print("✅ Applying V7 predictions directly.")
                    active_action = stored_v7_action.copy()
                    current_state = "MONITOR_ACTION"
                
                # --- THE FIX: CALCULATE THE DYNAMIC TIMEOUT HERE ---
                current_timeout_window = calculate_dynamic_timeout(pmv)
                print(f"⏱️ DYNAMIC TIMEOUT SET: {current_timeout_window} mins (Starting PMV: {pmv:.2f})")
                
                pmv_buffer.clear() # Reset stability tracker for the new action
                
            except Exception as e:
                print(f"❌ [MASTER AI] Inference Error: {e}")
                
        # ==========================================
        # 🧊 NEW STATE: ANTI_OVERCOOL
        # ==========================================
        elif current_state == "ANTI_OVERCOOL":
            # 1. Exit Condition: Have we reached the waking threshold?
            if pmv >= 0.25:
                print(f"☀️ Room warmed up! PMV reached waking threshold ({pmv:.2f} >= 0.25). Exiting Anti-Overcool.")
                current_state = "PRELIM_EVALUATION"
                wake_reason = "drift_cold_recovered"
                baseline_occupancy = occ
                pmv_buffer.clear()
            else:
                mins_in_state = (time.time() - anti_overcool_start_time) / 60.0
                
                # 2. Check for 10-Minute Escalation Rule
                if mins_in_state >= 7.0 and not anti_overcool_escalated:
                    if pmv < anti_overcool_start_pmv:
                        print(f"❄️ WARNING: 10 mins passed and PMV dropped further ({pmv:.2f} < {anti_overcool_start_pmv:.2f}).")
                        print("📈 Escalating ECO setpoint by +5°C to force compressor shutoff!")
                        anti_overcool_escalated = True

                # 3. Apply the Brute-Force Output
                active_action['ac_mode_label'] = 'eco'
                active_action['ac_fan_speed_label'] = '1'
                
                current_room_temp = int(env['inside_air_temperature'] + 2)
                
                if anti_overcool_escalated:
                    active_action['ac_temperature_setpoint'] = min(30, current_room_temp + 3) # Cap max limit at 30
                    print(f"🛡️ ANTI-OVERCOOL [ESCALATED] -> Holding ECO Mode | Fan: 1 | Setpoint: {active_action['ac_temperature_setpoint']}°C (+5°C offset)")
                else:
                    active_action['ac_temperature_setpoint'] = current_room_temp
                    print(f"🛡️ ANTI-OVERCOOL [ACTIVE] -> Holding ECO Mode | Fan: 1 | Setpoint: {active_action['ac_temperature_setpoint']}°C (Current Room Temp)")

        elif current_state == "TURBO_COOL":
            if pmv <= 0.5:
                print("❄️ Target PMV 0.5 reached! Exiting Turbo and kicking back to V7 proposed settings.")
                active_action = stored_v7_action.copy()
                current_state = "MONITOR_ACTION"
                # --- ADD THE NEW LINE HERE ---
                current_timeout_window = calculate_dynamic_timeout(pmv)
                pmv_buffer.clear()
            else:
                print(f"🚀 TURBO ACTIVE. Holding setpoint at {active_action['ac_temperature_setpoint']}°C...")

        elif current_state == "MONITOR_ACTION":
            is_ac_off = active_action['ac_mode_label'] == 'off'
            
            # ---------------------------------------------------------
            # THE FIX: IMMEDIATE PASSTHROUGH TO MAINTENANCE
            # Do not wait for mathematical stability if we hit the target!
            # ---------------------------------------------------------
            if -0.1 <= pmv <= 0.5:
                print(f"🎯 PMV has successfully entered the Comfort Zone ({pmv:.2f})! Moving to Maintenance.")
                current_state = "COMFORT_MAINTENANCE"
                last_successful_action = active_action.copy()
                pmv_buffer.clear()

                if is_ac_off:
                    print("✅ AC was OFF. Accepting V7's strategy to turn it ON.")
                    active_action = stored_v7_action.copy()
                
            # If we are OUTSIDE the comfort zone, we evaluate stability
            if is_stable or is_ac_off:
                if is_ac_off and not is_stable:
                    print("🛑 AC is OFF. Bypassing impossible stability wait.")
                else:
                    print("🛑 PMV has stabilized.")
                
                # --- MICRO-NUDGE LOGIC (Close & Stable) ---
                is_close_to_target = (0.5 < pmv <= 0.65) 
                
                if is_stable and is_close_to_target and not is_ac_off:
                    print(f"🤏 PMV is close ({pmv:.2f}) and stable. Bypassing V7 for a gentle 1°C Micro-Nudge.")
                    active_action['ac_temperature_setpoint'] = max(17, active_action['ac_temperature_setpoint'] - 1)
                        
                    baseline_occupancy = occ
                    current_timeout_window = calculate_dynamic_timeout(pmv)
                    pmv_buffer.clear()
                    
                # --- FULL AI RESET LOGIC (Far away or AC is OFF) ---
                else:
                    # --- TOO HOT LOGIC ---
                    if pmv > 0.5:  
                        print("🥵 Stabilized too HOT. Asking V7 for advice...")
                        baseline_occupancy = occ
                        ai_result = find_best_action_for_environment(env)
                        print_v7_prediction(env, ai_result)
                        new_v7 = ai_result['best_action']
                        
                        if is_ac_off:
                            print("✅ AC was OFF. Accepting V7's strategy to turn it ON.")
                            active_action = new_v7.copy()
                        
                        # DIRECTIONAL GUARDRAIL: AI must provide a COLDER setpoint. 
                        elif new_v7['ac_temperature_setpoint'] >= active_action['ac_temperature_setpoint'] or new_v7['ac_temperature_setpoint'] == 0:
                            print(f"⚠️ V7 suggested {new_v7['ac_temperature_setpoint']}°C. That is NOT colder! Guardrail: Dropping 2°C.")
                            active_action['ac_temperature_setpoint'] = max(17, active_action['ac_temperature_setpoint'] - 2)
                        else:
                            print("✅ V7 provided a colder strategy. Applying.")
                            active_action = new_v7.copy()
                        current_timeout_window = calculate_dynamic_timeout(pmv)
                        pmv_buffer.clear()
                    
            else:
                # --- PATIENCE-MINUTE TIMEOUT (Tier 3) ---
                if len(pmv_buffer) >= current_timeout_window:
                    # Calculate recent stability and check if we are "close" to the boundary
                    std_recent = np.std(pmv_buffer[-5:]) # Check the last 5 minutes for stability
                    is_close_to_target = (0.5 < pmv <= 0.65) # Are we within a gentle nudge range but just can't stabilize?
                    
                    # MICRO-NUDGE LOGIC (Close & Stable)
                    if is_close_to_target and std_recent <= EXTENDED_PMV_STD_THRESHOLD and not is_ac_off:
                        print(f"⏱️ 16-MIN TIMEOUT: PMV is very close ({pmv:.2f}) and stable (STD {std_recent:.3f}).")
                        print("🤏 Bypassing V7 and applying a gentle 1°C Micro-Nudge to prevent overshooting.")
                        
                        active_action['ac_temperature_setpoint'] = max(17, active_action['ac_temperature_setpoint'] - 1)
                            
                        baseline_occupancy = occ
                        current_timeout_window = calculate_dynamic_timeout(pmv)
                        pmv_buffer.clear()
                        
                    # FULL AI RESET LOGIC (Far away or Chaotic)
                    else:
                        print(f"⏱IN TIMEOUT: PMV stuck at {pmv:.2f}. Forcing full AI intervention!")
                        baseline_occupancy = occ 
                        ai_result = find_best_action_for_environment(env)
                        print_v7_prediction(env, ai_result)
                        new_v7 = ai_result['best_action']
                        
                        # Apply the exact same Directional Guardrails for the timeout
                        if pmv > 0.5:
                            if is_ac_off:
                                active_action = new_v7.copy()
                            elif new_v7['ac_temperature_setpoint'] >= active_action['ac_temperature_setpoint'] or new_v7['ac_temperature_setpoint'] == 0:
                                print("⚠️ Guardrail: Dropping 2°C.")
                                active_action['ac_temperature_setpoint'] = max(17, active_action['ac_temperature_setpoint'] - 2)
                            else:
                                active_action = new_v7.copy()
                                
                        pmv_buffer.clear()
                        current_timeout_window = calculate_dynamic_timeout(pmv)
                else:
                    print("⏳ Waiting for PMV to stabilize before taking next action...")

        elif current_state == "COMFORT_MAINTENANCE":
            # --- DYNAMIC WAKE-UP THRESHOLD ---
            # Wake up at 0.25 if coasting in Eco/Dry. Wake up at 0.50 if running normally.
            wake_threshold = 0.25 if golden_zone_active else 0.50
            
            if pmv > wake_threshold:
                print(f"🚨 PMV Drifted HOT out of bounds to {pmv:.2f}! Waking up V7.")
                current_state = "PRELIM_EVALUATION"
                wake_reason = "drift_hot"  # <--- RECORD THAT IT GOT TOO HOT
                golden_zone_active = False 
                pmv_buffer.clear()
            else:
                # --- ENERGY SAVING LOGIC (The Golden Zone) ---
                # THE FIX: Dynamic Eco-Trigger based on Occupancy size
                eco_trigger = 0.15 if occ <= 7 else 0
                
                if pmv <= eco_trigger: 
                    if env['inside_air_humidity'] < 75 and active_action['ac_mode_label'] != "eco":
                        print(f"🍃 Golden Zone reached (PMV <= {eco_trigger} for {occ} people). Switching to ECO MODE.")
                        active_action['ac_mode_label'] = "eco"
                        active_action['ac_fan_speed_label'] = "auto"
                        current_temp = env['inside_air_temperature']
                        active_action['ac_temperature_setpoint'] = int(current_temp)
                        golden_zone_active = True # Turn on the early wake-up tripwire!
                        
                    elif env['inside_air_humidity'] >= 75 and active_action['ac_mode_label'] != "dry":
                        print(f"💧 Golden Zone reached (PMV <= {eco_trigger} for {occ} people) but High Hum. Switching to DRY mode.")
                        active_action['ac_mode_label'] = "dry"
                        active_action['ac_fan_speed_label'] = "auto"
                        golden_zone_active = True # Turn on the early wake-up tripwire!
                        
                    else:
                        print(f"✅ Golden Zone holding (PMV <= {eco_trigger}). Energy saving active.")
                else:
                    print(f"✅ Comfort holding steady (PMV: {pmv:.2f}). Waiting to reach {eco_trigger} to trigger Eco Mode.")

        # ==========================================
        # HARDWARE OUTPUT
        # ==========================================
        current_time = time.time()
        time_since_last_toggle = (current_time - last_power_toggle_time) / 60.0
        
        # Determine if the NEW action is trying to change the power state
        is_currently_off = active_action.get('_actual_hardware_mode', 'cool') == 'off'
        wants_to_be_off = active_action['ac_mode_label'] == 'off'
        
        power_state_changing = is_currently_off != wants_to_be_off
        
        # Check the Compressor Delay
        if power_state_changing and time_since_last_toggle < COMPRESSOR_DELAY_MINS:
            mins_left = COMPRESSOR_DELAY_MINS - time_since_last_toggle
            print(f"⏱️ COMPRESSOR LOCKOUT: Must wait {mins_left:.1f} more mins before turning {'ON' if wants_to_be_off else 'OFF'}.")
            
            # --- THE FIX: Revert to EXACTLY what the hardware was doing before the attempt ---
            if last_sent_command:
                display_mode, clean_setpoint, display_fan = last_sent_command.split(',')
            else:
                display_mode, clean_setpoint, display_fan = 'off', 0, 'off'
                
            display_setp = f"{clean_setpoint}°C" if display_mode not in ['fan', 'off'] else "N/A"
            
        else:
            # Safe to execute!
            if power_state_changing:
                last_power_toggle_time = current_time # Record the time of this valid toggle
                active_action['_actual_hardware_mode'] = active_action['ac_mode_label'] # Update our true state
            
            display_mode = active_action['ac_mode_label']
            display_fan = active_action['ac_fan_speed_label']
            clean_setpoint = active_action['ac_temperature_setpoint'] if display_mode not in ['fan', 'off'] else 0
            display_setp = f"{clean_setpoint}°C" if display_mode not in ['fan', 'off'] else "N/A"

        print(f"⚙️ CURRENT AC SETTING -> Mode: [{display_mode.upper()}] Fan: [{display_fan.upper()}] Setpoint: [{display_setp}]")
        
        # ==========================================
        # 📡 BEAM COMMAND TO ESP32 VIA MQTT
        # ==========================================
        
        # The ESP32 expects: "mode,temp,fan" (e.g., "cool,24,auto" or "off,0,off")
        command_string = f"{str(display_mode).lower()},{str(clean_setpoint)},{str(display_fan).lower()}"
        
        # 🛡️ ONLY SEND IF THE COMMAND ACTUALLY CHANGED!
        if command_string != last_sent_command:
            try:
                # 1. Send the Master Command to the ESP32 Blaster
                mqtt_client.publish(COMMAND_TOPIC, command_string)
                print(f"📡 [MQTT] COMMAND CHANGED! Beaming to ESP32: {command_string}")
                
                # 2. Send the individual states to V17 Data Gatherer so it can log them!
                # Samsung (AC1)
                mqtt_client.publish("/ac/mode", str(display_mode).lower())
                mqtt_client.publish("/ac/fan", str(display_fan).lower())
                mqtt_client.publish("/ac/setpoint", str(clean_setpoint))
                
                # --- ADD THIS: Coolix (AC2) ---
                mqtt_client.publish("/ac2/mode", str(display_mode).lower())
                mqtt_client.publish("/ac2/fan", str(display_fan).lower())
                mqtt_client.publish("/ac2/setpoint", str(clean_setpoint))
                # ------------------------------
                
                # 3. Update the tracker so we don't spam this next minute
                last_sent_command = command_string
                
            except Exception as e:
                print(f"❌ [MQTT] Failed to send command: {e}")
        else:
            print(f"💤 [MQTT] AC settings unchanged ({command_string}). No IR blast needed.")

        print("█"*60 + "\n")

        
if __name__ == "__main__":
    # --- ACTIVATE DUAL LOGGING ---
    sys.stdout = DualLogger(LOG_FILE)
    sys.stderr = sys.stdout 
    
    print("\n🚀 === THESIS MASTER CONTROLLER BOOT SEQUENCE === 🚀")
    try:
        start_data_gatherer()
        main_ai_loop()
        
    except KeyboardInterrupt:
        print("\n\n🛑 [MASTER] CTRL+C Detected! Initiating synchronized shutdown...")
        
    except Exception as e:
        # ✅ CAPTURES ANY UNEXPECTED CRASH
        print(f"\n\n💥 [MASTER] FATAL SCRIPT ERROR: {e}")
        
    finally:
        # ✅ THIS RUNS NO MATTER HOW THE SCRIPT DIES
        print("📡 Transmitting OFFLINE heartbeat to sensor network...")
        try:
            mqtt_client = mqtt.Client("DeathCry_Sender")
            mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            mqtt_client.publish("/master/heartbeat", "OFFLINE")
            # ========================================================
            # THE FIX: EMERGENCY HARDWARE SHUTDOWN BLAST
            # ========================================================
            print("🛑 EMERGENCY SHUTDOWN: Blasting final OFF command to all AC units...")
            mqtt_client.publish(COMMAND_TOPIC, "off,0,off")
            
            # Send the OFF state to the loggers so it registers in the CSV!
            mqtt_client.publish("/ac/mode", "off")
            mqtt_client.publish("/ac/fan", "off")
            mqtt_client.publish("/ac/setpoint", "0")
            mqtt_client.publish("/ac2/mode", "off")
            mqtt_client.publish("/ac2/fan", "off")
            mqtt_client.publish("/ac2/setpoint", "0")
            # ========================================================
            mqtt_client.disconnect()
        except:
            pass
            
        if gather_process is not None:
            print("🔪 Terminating V16 Data Gatherer...")
            gather_process.terminate()  
            try:
                gather_process.wait(timeout=5) 
            except subprocess.TimeoutExpired:
                gather_process.kill()  
                gather_process.wait()
                
        print("✅ [MASTER] Master Controller safely terminated. Goodbye!")