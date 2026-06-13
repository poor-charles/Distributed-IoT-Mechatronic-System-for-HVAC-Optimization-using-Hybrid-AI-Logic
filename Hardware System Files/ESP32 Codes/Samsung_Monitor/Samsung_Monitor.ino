#include <WiFi.h>
#include <PubSubClient.h>
#include <PZEM004Tv30.h>
#include <INA226_WE.h>
#include <ESPmDNS.h>
#include <Wire.h> 

// ================= WIFI =================
const char* ssid = "TP-Link_95CC";
const char* password = "11018605";

// ================= MQTT =================
const char* mqtt_hostname = "cvmpi";
const int mqtt_port = 1883;
const char* mqtt_client_id = "ESP32_InsideSensor4";

const char* ac_power   = "/ac/power4";
const char* voltage_topic = "/esp/voltage4";
const char* power_topic   = "/esp/power4";
const char* status_topic  = "/esp/status4";

// ✅ ADDED: Heartbeat Topic for LED State Machine
const char* master_heartbeat_topic = "/master/heartbeat";

const unsigned long send_interval = 30000;

// ================= WIFI RETRY =================
const unsigned long WIFI_TIMEOUT_MS = 10000;
const int WIFI_MAX_RETRIES = 2;

// ================= RGB LED =================
#define LED_RED    27
#define LED_GREEN  26
#define LED_BLUE   25

// ✅ ADDED: LED PWM settings for color mixing
#define LED_FREQ 5000
#define LED_RES    8

// ================= INA226 =================
#define INA226_ADDR 0x40
INA226_WE ina226 = INA226_WE(INA226_ADDR);

// ================= PZEM =================
HardwareSerial pzemSerial(2);
PZEM004Tv30 pzem(pzemSerial, 16, 17);

// ================= GLOBALS =================
IPAddress mqtt_server_ip;
WiFiClient espClient;
PubSubClient mqttClient(espClient);

bool pzem_ok = false;
bool ina_ok = false;
bool mqtt_available = false;

unsigned long previousMillis = 0; 

// ✅ ADDED: LED State Machine Variables
float voltage_avg = 0;
unsigned long last_master_heartbeat = 0;
bool master_alive = false;

// ================= FUNCTION DECLARATIONS =================
void setLED(uint32_t color);
void updateLED(float voltage);
void connectWiFi();
void discoverMQTTBroker();
void connectMQTT();

// ================= MQTT LISTENER =================
// ✅ ADDED: Triggers instantly to update master_alive for the LED
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
      master_alive = false; 
    }
  }
}

// ================= SETUP =================
void setup() {

  Serial.begin(115200);
  delay(1000);

  Serial.println("\n=== ESP32 SENSOR SYSTEM STARTING ===");

  // ✅ UPDATED: PWM setup for precise colors instead of basic pinMode
  ledcAttach(LED_RED, LED_FREQ, LED_RES);
  ledcAttach(LED_GREEN, LED_FREQ, LED_RES);
  ledcAttach(LED_BLUE, LED_FREQ, LED_RES);
  setLED(0x000000); // Start OFF

  Wire.begin(); 

  connectWiFi();

  if (MDNS.begin("ESP32Sensor")) {
    Serial.println("[MDNS] Started as ESP32Sensor.local");
  }

  discoverMQTTBroker();
  mqttClient.setServer(mqtt_server_ip, mqtt_port);
  mqttClient.setCallback(mqttCallback); // ✅ ADDED: Link callback

  Serial.println("[SYSTEM] Boot complete\n");
}

// ================= LOOP =================
void loop() {

  // ===== WIFI & MQTT MAINTENANCE (Runs instantly, every loop) =====
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WIFI] Disconnected. Reconnecting...");
    connectWiFi();
    discoverMQTTBroker();
    mqttClient.setServer(mqtt_server_ip, mqtt_port);
  } 
  
  if (!mqttClient.connected() && WiFi.status() == WL_CONNECTED) {
    Serial.println("[MQTT] Not connected. Reconnecting...");
    discoverMQTTBroker();
    mqttClient.setServer(mqtt_server_ip, mqtt_port);
    connectMQTT();
  } 

  mqttClient.loop(); 

  // ✅ ADDED: Check if Master Controller heartbeat flatlined (> 15 seconds)
  if (millis() - last_master_heartbeat > 15000) {
    master_alive = false; 
  }

  // ===== NON-BLOCKING SENSOR TIMER (Runs every 30 seconds) =====
  unsigned long currentMillis = millis();
  
  if (currentMillis - previousMillis >= send_interval) {
    previousMillis = currentMillis;

    Serial.println("\n====== SENSOR READ START ======");

    // ===== SENSOR CHECK =====
    float volt = pzem.voltage();
    float amp  = pzem.current();

    if (!isnan(volt) && !isnan(amp)) {
      if (!pzem_ok) Serial.println("[PZEM] FUNCTIONAL - GOOD");
      pzem_ok = true;
    } else {
      if (pzem_ok) Serial.println("[PZEM] FAILED");
      pzem_ok = false;
    }

    if (!ina_ok) {
      Serial.println("[INA226] Trying to initialize...");
      if (ina226.init()) {
        ina226.setAverage(INA226_AVERAGE_16);
        ina226.setConversionTime(INA226_CONV_TIME_1100);
        ina226.setMeasureMode(INA226_CONTINUOUS);
        ina_ok = true;
        Serial.println("[INA226] FUNCTIONAL - GOOD");
      } else {
        Serial.println("[INA226] INIT FAILED");
      }
    }

    float voltage = NAN;
    float power   = NAN;

    if (ina_ok) {
      voltage = ina226.getBusVoltage_V();
      power   = ina226.getBusPower();

      if (isnan(voltage) || isnan(power)) {
        Serial.println("[INA226] READ FAILED ERROR");
        ina_ok = false;
      } else {
        // ✅ ADDED: Weighted average filter for stable LED colors
        if (voltage_avg == 0) {
          voltage_avg = voltage;
        } else {
          voltage_avg = (voltage_avg * 9 + voltage) / 10;
        }
      }
    }

    // ===== STATUS PRINT =====
    Serial.print("[STATUS] PZEM: ");
    Serial.print(pzem_ok ? "OK" : "FAIL");
    Serial.print(" | INA226: ");
    Serial.print(ina_ok ? "OK" : "FAIL");
    Serial.print(" | MQTT: ");
    Serial.println(mqtt_available ? "OK" : "FAIL");

    // ===== LOGIC =====
    // ❌ REMOVED: Blocking LED calls (blinkGreen, blinkRed, updateLED) from here
    if (mqtt_available) {

      // ===== ALL OK =====
      if (pzem_ok && ina_ok) {
        Serial.println("[MODE] ALL OK");

        float ac_calc_pow = volt * amp;

        Serial.printf("[DATA] AC_V: %.2f | AC_I: %.2f | AC_P: %.2f\n", volt, amp, ac_calc_pow);
        Serial.printf("[DATA] INA_V: %.2f | INA_P: %.2f\n", voltage, power);

        mqttClient.publish(ac_power, String(ac_calc_pow, 2).c_str());
        mqttClient.publish(voltage_topic, String(voltage, 2).c_str());
        mqttClient.publish(power_topic, String(power, 2).c_str());
        mqttClient.publish(status_topic, "OK");

        Serial.println("[MQTT] Published ALL data");
      }

      // ===== PZEM OK, INA FAIL =====
      else if (pzem_ok && !ina_ok) {
        Serial.println("[MODE] INA FAIL, PZEM OK");

        float ac_calc_pow = volt * amp;

        Serial.printf("[DATA] AC_V: %.2f | AC_I: %.2f | AC_P: %.2f\n", volt, amp, ac_calc_pow);

        mqttClient.publish(ac_power, String(ac_calc_pow, 2).c_str());
        mqttClient.publish(status_topic, "ERROR: INA226_FAIL");

        Serial.println("[MQTT] Published PZEM only");
      }

      // ===== INA OK, PZEM FAIL =====
      else if (!pzem_ok && ina_ok) {
        Serial.println("[MODE] PZEM FAIL (But INA OK)");

        mqttClient.publish(voltage_topic, String(voltage, 2).c_str());
        mqttClient.publish(power_topic, String(power, 2).c_str());
        mqttClient.publish(status_topic, "ERROR: PZEM_FAIL");

        Serial.println("[MQTT] Published INA data + PZEM Error");
      }

      // ===== BOTH FAIL =====
      else {
        Serial.println("[MODE] BOTH SENSORS FAIL");
        mqttClient.publish(status_topic, "ERROR: PZEM_FAIL INA226_FAIL");
        Serial.println("[MQTT] Status only (ALL FAIL)");
      }

    } else {
      Serial.println("[MQTT] Not available — skipping publish");
    }

    Serial.println("====== SENSOR READ END ======\n");
  } 

  // ===== VISUAL STATE MACHINE (LED INDICATORS) =====
  // ✅ ADDED: Runs continuously without blocking the ESP32
  
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
  else if (!pzem_ok || !ina_ok) { // ✅ UPDATED: tailored to the PZEM/INA226 sensors
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
    static unsigned long lastMasterDeadBlink = 0;
    static bool showBlue = true;
    if (millis() - lastMasterDeadBlink >= 1000) {
      lastMasterDeadBlink = millis();
      showBlue = !showBlue;
      
      if (showBlue) {
        setLED(0x0000FF); 
      } else {
        updateLED(voltage_avg); 
      }
    }
  }
  else {
    // 🔋 SOLID COLOR: System Healthy
    updateLED(voltage_avg);
  }

} // End of loop

// ================= LED NORMAL MODE =================
// ✅ UPDATED: Adapted to the new reference format
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

// ================= LED FUNCTION =================
// ✅ ADDED: New setLED helper using PWM
void setLED(uint32_t color) {
  uint8_t r = (color >> 16) & 0xFF;
  uint8_t g = (color >> 8) & 0xFF;
  uint8_t b = color & 0xFF;

  ledcWrite(LED_RED, r);
  ledcWrite(LED_GREEN, g);
  ledcWrite(LED_BLUE, b);
}

// ================= WIFI =================
void connectWiFi() {

  int retry_count = 0;

  while (retry_count < WIFI_MAX_RETRIES) {

    Serial.printf("[WIFI] Connecting (%d/%d)...\n", retry_count+1, WIFI_MAX_RETRIES);

    WiFi.begin(ssid, password);

    unsigned long start = millis();

    while (WiFi.status() != WL_CONNECTED &&
           millis() - start < WIFI_TIMEOUT_MS) {
      delay(500);
      Serial.print(".");
    }

    if (WiFi.status() == WL_CONNECTED) {
      Serial.println("\n[WIFI] Connected!");
      Serial.println(WiFi.localIP());
      return;
    }

    Serial.println("\n[WIFI] Failed");
    WiFi.disconnect(true);
    retry_count++;
  }

  Serial.println("[WIFI] Restarting ESP...");
  ESP.restart();
}

// ================= MDNS =================
void discoverMQTTBroker() {

  Serial.printf("[MDNS] Searching '%s.local' ... ", mqtt_hostname);

  IPAddress ip = MDNS.queryHost(mqtt_hostname);

  if (ip) {
    mqtt_server_ip = ip;
    Serial.print("FOUND: ");
    Serial.println(mqtt_server_ip);
  } else {
    Serial.println("NOT FOUND");
    mqtt_server_ip = IPAddress(0,0,0,0);
  }
}

// ================= MQTT =================
void connectMQTT() {

  Serial.print("[MQTT] Connecting... ");

  if (mqtt_server_ip == IPAddress(0,0,0,0)) {
    Serial.println("No broker IP");
    mqtt_available = false;
    return;
  }

  if (mqttClient.connect(mqtt_client_id)) {
    Serial.println("SUCCESS");
    mqtt_available = true;
    
    // ✅ ADDED: Subscribe to master heartbeat
    mqttClient.subscribe(master_heartbeat_topic);
  } else {
    Serial.println("FAILED");
    mqtt_available = false;
  }
}