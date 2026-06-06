import paho.mqtt.client as mqtt
import subprocess
import signal
import time
import os

##########################== TO MAKE IT RUN IN BACKGROUND AUTO ==############################### 
###################################==== START HERE
#sudo nano /etc/systemd/system/thesis_hvac.service
###################################==== THEN PASTE THIS IN THE FILE
# [Unit]
# Description=Thesis HVAC Auto Manager
# After=network.target mosquitto.service
# Requires=mosquitto.service

# [Service]
# Type=simple
# User=rkrichkid2001
# WorkingDirectory=/home/rkrichkid2001/thesis_aiproject/venv/
# Environment="PATH=/home/rkrichkid2001/thesis_aiproject/venv/bin:/usr/bin:/bin"
# ExecStartPre=/bin/sleep 10
# ExecStart=/home/rkrichkid2001/thesis_aiproject/venv/bin/python /home/rkrichkid2001/thesis_aiproject/venv/SUBPROCESS_PROGRAM.py
# Restart=always
# RestartSec=5
# StandardOutput=journal
# StandardError=journal

# [Install]
# WantedBy=multi-user.target
#####################################===== THEN RUN THESE COMMANDS IN TERMINAL
# # Tell systemd to look for the new file we just made
# sudo systemctl daemon-reload

# # Tell it to run automatically every time you plug the Pi into the wall
# sudo systemctl enable thesis_hvac.service

# # Start the service right now without rebooting
# sudo systemctl start thesis_hvac.service
######################################################### == DONE HERE #########################


########################################= CONTROL OVER THIS PROGRAM =#################################
################## AND TO CHECK STATUS, USE THIS COMMAND IN TERMINAL
# sudo systemctl status thesis_hvac.service
################## AND TO VIEW LOGS, USE THIS COMMAND IN TERMINAL
# sudo journalctl -u thesis_hvac.service -f
# sudo journalctl -u thesis_hvac.service
######################################################################################################
#TO DISABLE THE SERVICE, USE THIS COMMAND IN TERMINAL
# sudo systemctl disable thesis_hvac.service
# sudo systemctl stop thesis_hvac.service
#TO ENABLE THE SERVICE AGAIN, USE THIS COMMAND IN TERMINAL
# sudo systemctl enable thesis_hvac.service
# sudo systemctl start thesis_hvac.service
######################################################################################################


# --- CONFIGURATION ---
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
ESP2_TOPIC = "/esp/status2"  # The heartbeat trigger we are listening for
TIMEOUT_SECONDS = 180       # 3 minutes (60 seconds * 3 readings)

# --- PATHS (Must be absolute paths) ---
# IMPORTANT: Point this to your virtual environment's python!
PYTHON_BIN = "/home/rkrichkid2001/thesis_aiproject/venv/bin/python" 
MASTER_SCRIPT = "/home/rkrichkid2001/thesis_aiproject/venv/UNFINISHED_PROTOTYPE V13.py"
WORKING_DIR = "/home/rkrichkid2001/thesis_aiproject/venv/"

# --- STATE VARIABLES ---
last_seen_esp2 = 0.0
master_process = None

def start_master():
    """Starts the Master Controller if it isn't already running."""
    global master_process
    if master_process is None or master_process.poll() is not None:
        print(f"🟢 [AUTO] ESP2 Data Detected! Starting Master Controller...")
        master_process = subprocess.Popen(
            [PYTHON_BIN, "-u", MASTER_SCRIPT],
            cwd=WORKING_DIR,
            preexec_fn=os.setsid # Puts it in its own process group for clean shutdown
        )

def stop_master():
    """Sends Ctrl+C to the Master Controller to safely shut it down."""
    global master_process
    if master_process is not None and master_process.poll() is None:
        print(f"🔴 [AUTO] ESP2 Offline for 3 mins. Sending Stop Command...")
        try:
            # Send the exact equivalent of CTRL+C to the Master Controller
            os.killpg(os.getpgid(master_process.pid), signal.SIGINT)
            master_process.wait(timeout=10) # Give it 10 seconds to shut down cameras/CSV
        except Exception as e:
            print(f"Force killing due to error: {e}")
            master_process.kill()
            
        master_process = None
        print("✅ Shutdown Complete. System dormant. Waiting for ESP2 to return...")

def on_connect(client, userdata, flags, rc):
    print(f"📡 [AUTO] Connected to MQTT Broker. Listening to {ESP2_TOPIC}...")
    client.subscribe(ESP2_TOPIC)

def on_message(client, userdata, msg):
    """Every time ESP2 sends a message, update the timestamp."""
    global last_seen_esp2
    last_seen_esp2 = time.time()
    # Decode the message so we can print it to the journal
    payload = msg.payload.decode('utf-8')
    print(f"💓 [AUTO] ESP2 Heartbeat received: {payload}")

def main():
    global last_seen_esp2
    
    client = mqtt.Client("AutoManager_Watcher")
    client.on_connect = on_connect
    client.on_message = on_message
    
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
    except Exception as e:
        print(f"❌ [AUTO] MQTT Connection failed: {e}")
        return

    print("⚙️  Auto Manager Active. System is dormant. Waiting for ESP2 data...")
    
    try:
        # The infinite watcher loop
        while True:
            time.sleep(5) # Check the timestamps every 5 seconds
            
            current_time = time.time()
            
            # Condition 1: ESP2 is active (has been seen recently)
            if last_seen_esp2 > 0 and (current_time - last_seen_esp2) <= TIMEOUT_SECONDS:
                start_master()
                
            # Condition 2: ESP2 has been silent for over 3 minutes
            elif last_seen_esp2 > 0 and (current_time - last_seen_esp2) > TIMEOUT_SECONDS:
                stop_master()
                last_seen_esp2 = 0.0 # Reset the timer to 0 so we don't spam the kill command
                
    except KeyboardInterrupt:
        print("\n🛑 Auto Manager closing...")
        stop_master()
        client.loop_stop()

if __name__ == "__main__":
    main()