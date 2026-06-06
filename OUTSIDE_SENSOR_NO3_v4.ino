#include <WiFi.h>
#include <PubSubClient.h>
#include <Adafruit_SHT31.h>
#include <INA226_WE.h>
#include <ESPmDNS.h>
#include <Wire.h> // ✅ ADDED: Required for I2C sensors

// ================= WIFI =================
const char* ssid = "TP-Link_95CC";
const char* password = "11018605";

// ================= MQTT =================
const char* mqtt_hostname = "cvmpi";
const int mqtt_port = 1883;
const char* mqtt_client_id = "ESP32_OutsideSensor3";

const char* temp_topic    = "/outside/temp3";
const char* humid_topic   = "/outside/humid3";
const char* voltage_topic = "/esp/voltage3";
const char* power_topic   = "/esp/power3";
const char* status_topic  = "/esp/status3";

// ✅ NEW: Heartbeat Topic
const char* master_heartbeat_topic = "/master/heartbeat";

const unsigned long send_interval = 30000;

// ================= WIFI RETRY =================
const unsigned long WIFI_TIMEOUT_MS = 10000;
const int WIFI_MAX_RETRIES = 2;

// ================= RGB LED PINS =================
#define LED_RED    27
#define LED_GREEN  26
#define LED_BLUE   25

#define CH_RED      0
#define CH_GREEN    1
#define CH_BLUE     2

#define LED_FREQ 5000
#define LED_RES     8

// ================= INA226 =================
#define INA226_ADDR 0x40
INA226_WE ina226 = INA226_WE(INA226_ADDR);

// ================= GLOBALS =================
IPAddress mqtt_server_ip;
Adafruit_SHT31 sht31 = Adafruit_SHT31();
WiFiClient espClient;
PubSubClient mqttClient(espClient);

bool sht_ok = false;
bool ina_ok = false;
bool system_ready = false;
bool mqtt_available = false;

float voltage_avg = 0;

// ✅ ADDED: Timer for non-blocking loop
unsigned long previousMillis = 0; 
unsigned long lastReconnectAttempt = 0;

// ✅ NEW: Master tracking variables
unsigned long last_master_heartbeat = 0;
bool master_alive = false;

// ================= FUNCTION DECLARATIONS =================
void setLED(uint32_t color);
void updateLED(float voltage);
void connectWiFi();
void discoverMQTTBroker();
void connectMQTT();

// ================= MQTT LISTENER =================
// ✅ NEW: This triggers instantly whenever the Python script pulses
void mqttCallback(char* topic, byte* payload, unsigned int length) {
  String msg = "";
  for (int i = 0; i < length; i++) {
    msg += (char)payload[i];
  }

  if (String(topic) == String(master_heartbeat_topic)) {
    if (msg == "ONLINE") {
      last_master_heartbeat = millis();
      master_alive = true;
    } 
    else if (msg == "OFFLINE") {
      master_alive = false; // Python script was manually shut down!
    }
  }
}

// ================= SETUP =================
void setup() {

  Serial.begin(115200);
  delay(1000);

  Serial.println("\n=== ESP32 SENSOR SYSTEM STARTING ===");

  // PWM setup
  ledcAttach(LED_RED, LED_FREQ, LED_RES);
  ledcAttach(LED_GREEN, LED_FREQ, LED_RES);
  ledcAttach(LED_BLUE, LED_FREQ, LED_RES);

  // LED initially off
  setLED(0x000000);

  Wire.begin(); // ✅ ADDED: Starts the I2C bus for SHT31 and INA226

  connectWiFi();

  if (MDNS.begin("ESP32Sensor")) {
    Serial.println("mDNS started");
  }

  discoverMQTTBroker();
  mqttClient.setServer(mqtt_server_ip, mqtt_port);
  mqttClient.setCallback(mqttCallback); // ✅ Tell MQTT to route incoming messages to our callback

  // ===== SENSOR CHECK =====
  if (sht31.begin(0x44)) {
    Serial.println("SHT31 initialized.");
    sht_ok = true;
  } else {
    Serial.println("SHT31 FAILED!");
    sht_ok = false;
  }

  if (ina226.init()) {
    Serial.println("INA226 initialized.");
    ina_ok = true;

    ina226.setResistorRange(0.1, 1.0);  // Calibration
    ina226.setAverage(INA226_AVERAGE_16);
    ina226.setConversionTime(INA226_CONV_TIME_1100);
    ina226.setMeasureMode(INA226_CONTINUOUS);

  } else {
    Serial.println("INA226 FAILED!");
    ina_ok = false;
  }

  if (sht_ok && ina_ok) {
    system_ready = true;
    Serial.println("System READY.");
  } else {
    system_ready = false;
    Serial.println("System NOT READY.");
  }
}

// ================= LOOP =================
void loop() {

  // ===== NETWORK MAINTENANCE =====
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
    discoverMQTTBroker();
    mqttClient.setServer(mqtt_server_ip, mqtt_port);
  }

  // Non-blocking MQTT reconnect (Checks every 5 seconds if disconnected)
  if (!mqttClient.connected() && WiFi.status() == WL_CONNECTED) {
    unsigned long now = millis();
    if (now - lastReconnectAttempt > 5000) {
      lastReconnectAttempt = now;
      discoverMQTTBroker();
      mqttClient.setServer(mqtt_server_ip, mqtt_port);
      connectMQTT();
    }
  }

  mqttClient.loop(); 

  // ✅ NEW: Check if the Master Controller's heartbeat flatlined (> 15 seconds)
  if (millis() - last_master_heartbeat > 15000) {
    master_alive = false; 
  }
  // ===== NON-BLOCKING SENSOR TIMER =====
  unsigned long currentMillis = millis();
  
  if (currentMillis - previousMillis >= send_interval) {
    previousMillis = currentMillis;

    // === NEW: SENSOR AUTO-RECOVERY ===
    if (!sht_ok) {
      sht_ok = sht31.begin(0x44);
    }
    if (!ina_ok) {
      if (ina226.init()) {
        ina226.setResistorRange(0.1, 1.0);
        ina226.setAverage(INA226_AVERAGE_16);
        ina226.setConversionTime(INA226_CONV_TIME_1100);
        ina226.setMeasureMode(INA226_CONTINUOUS);
        ina_ok = true;
      }
    }
    // Re-evaluate overall system health
    system_ready = (sht_ok && ina_ok);
    // ==================================
    if (!system_ready) {
      if (mqtt_available) {
        String errorMsg = "ERROR:";
        if (!sht_ok) errorMsg += " SHT31_FAIL";
        if (!ina_ok) errorMsg += " INA226_FAIL";
        mqttClient.publish(status_topic, errorMsg.c_str());
      }
    } 
    else {
      // NORMAL OPERATION
      float temp = sht31.readTemperature();
      float humid = sht31.readHumidity();
      float voltage = ina226.getBusVoltage_V();
      float power   = ina226.getBusPower();

      // Weighted average filter
      if (voltage_avg == 0) {
        voltage_avg = voltage;
      } else {
        voltage_avg = (voltage_avg * 9 + voltage) / 10;
      }

      // Publish only if MQTT AND both sensors are OK
      if (mqtt_available && sht_ok && ina_ok) {
        Serial.printf("Temp: %.2f °C | Hum: %.2f %% | V: %.2f V | P: %.2f W\n",
                    temp, humid, voltage, power);

        mqttClient.publish(temp_topic, String(temp, 2).c_str());
        mqttClient.publish(humid_topic, String(humid, 2).c_str());
        mqttClient.publish(voltage_topic, String(voltage, 2).c_str());
        mqttClient.publish(power_topic, String(power, 2).c_str());
        mqttClient.publish(status_topic, "OK");
      }
      else if (mqtt_available) {
        String errorMsg = "ERROR:";
        if (!sht_ok) errorMsg += " SHT31_FAIL";
        if (!ina_ok) errorMsg += " INA226_FAIL";
        mqttClient.publish(status_topic, errorMsg.c_str());
      }
    }
  } // End of 30-second timer

  // ===== VISUAL STATE MACHINE (LED INDICATORS) =====
  // This runs continuously without blocking the ESP32
  
  if (WiFi.status() != WL_CONNECTED || !mqttClient.connected()) {
    // 🔵 BLINK BLUE: Network / MQTT Drop
    static unsigned long lastNetBlink = 0;
    static bool netLed = false;
    if (millis() - lastNetBlink >= 300) {
      lastNetBlink = millis();
      netLed = !netLed;
      setLED(netLed ? 0x0000FF : 0x000000); 
    }
  } 
  else if (!system_ready || !sht_ok || !ina_ok) {
    // 🔴 BLINK RED: Physical Hardware / Sensor Failure
    static unsigned long lastHwBlink = 0;
    static bool hwLed = false;
    if (millis() - lastHwBlink >= 300) {
      lastHwBlink = millis();
      hwLed = !hwLed;
      setLED(hwLed ? 0xFF0000 : 0x000000);
    }
  } 
  else if (!master_alive) {
    // 🔵🔌 SLOW ALTERNATING: Master Python Script is DEAD
    // Alternates between BLUE and the BATTERY VOLTAGE color every 1 second
    static unsigned long lastMasterDeadBlink = 0;
    static bool showBlue = true;
    if (millis() - lastMasterDeadBlink >= 1000) {
      lastMasterDeadBlink = millis();
      showBlue = !showBlue;
      
      if (showBlue) {
        setLED(0x0000FF); // Force Blue
      } else {
        updateLED(voltage_avg); // Show Battery Status Color
      }
    }
  }
  else {
    // 🔋 SOLID COLOR: System Healthy, Master is Alive, showing battery status
    updateLED(voltage_avg);
  }

} // End of loop

// ================= LED NORMAL MODE =================
void updateLED(float voltage) {

  if (voltage > 3.9) {
    setLED(0x00FF00);   // Green
  }
  else if (voltage > 3.6) {
    setLED(0xFFFF00);   // Yellow
  }
  else if (voltage > 3.4) {
    setLED(0xFF8000);   // Orange
  }
  else {
    setLED(0xFF0000);   // Red
  }
}

// ================= WIFI =================
void connectWiFi() {

  int retry_count = 0;

  while (retry_count < WIFI_MAX_RETRIES) {

    Serial.printf("Connecting WiFi (%d/%d)...",
                  retry_count + 1, WIFI_MAX_RETRIES);

    WiFi.begin(ssid, password);

    unsigned long start = millis();

    while (WiFi.status() != WL_CONNECTED &&
           millis() - start < WIFI_TIMEOUT_MS) {
      delay(500);
      Serial.print(".");
    }

    if (WiFi.status() == WL_CONNECTED) {
      Serial.println(" Connected!");
      Serial.println(WiFi.localIP());
      return;
    }

    Serial.println(" Failed!");
    WiFi.disconnect(true);
    retry_count++;
  }

  Serial.println("WiFi failed → Restarting...");
  delay(3000);
  ESP.restart();
}

// ================= MDNS =================
void discoverMQTTBroker() {

  Serial.printf("Discovering '%s.local' ... ", mqtt_hostname);

  IPAddress ip = MDNS.queryHost(mqtt_hostname);

  if (ip) {
    mqtt_server_ip = ip;
    Serial.print("Found: ");
    Serial.println(mqtt_server_ip);
  } else {
    Serial.println("NOT FOUND!");
    mqtt_server_ip = IPAddress(0,0,0,0);
  }
}

// ================= MQTT =================
void connectMQTT() {

  Serial.print("Connecting MQTT...");

  if (mqtt_server_ip == IPAddress(0,0,0,0)) {
    Serial.println(" Host not found.");
    mqtt_available = false;
    return;
  }

  if (mqttClient.connect(mqtt_client_id)) {
    Serial.println(" Connected!");
    mqtt_available = true;
    
    // ✅ NEW: Tell the MQTT Broker we want to listen to the Heartbeat channel
    mqttClient.subscribe(master_heartbeat_topic); 
  } else {
    Serial.println(" Failed.");
    mqtt_available = false;
  }
}

// ================= LED FUNCTION =================
void setLED(uint32_t color) {

  uint8_t r = (color >> 16) & 0xFF;
  uint8_t g = (color >> 8) & 0xFF;
  uint8_t b = color & 0xFF;

  ledcWrite(LED_RED, r);
  ledcWrite(LED_GREEN, g);
  ledcWrite(LED_BLUE, b);
}