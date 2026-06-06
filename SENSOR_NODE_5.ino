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
const char* mqtt_client_id = "ESP32_InsideSensor5";

const char* ac_power   = "/ac/power5";
const char* voltage_topic = "/esp/voltage5";
const char* power_topic   = "/esp/power5";
const char* status_topic  = "/esp/status5";

const unsigned long send_interval = 30000;

// ================= WIFI RETRY =================
const unsigned long WIFI_TIMEOUT_MS = 10000;
const int WIFI_MAX_RETRIES = 2;

// ================= RGB LED =================
#define LED_RED    25
#define LED_GREEN  26
#define LED_BLUE   27

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

// <--- FIX 2: Added global timer variable for non-blocking loop
unsigned long previousMillis = 0; 

// ================= SETUP =================
void setup() {

  Serial.begin(115200);
  delay(1000);

  Serial.println("\n=== ESP32 SENSOR SYSTEM STARTING ===");

  pinMode(LED_RED, OUTPUT);
  pinMode(LED_GREEN, OUTPUT);
  pinMode(LED_BLUE, OUTPUT);

  Wire.begin(); // <--- FIX 1: Started the I2C bus for the INA226

  connectWiFi();

  if (MDNS.begin("ESP32Sensor")) {
    Serial.println("[MDNS] Started as ESP32Sensor.local");
  }

  discoverMQTTBroker();
  mqttClient.setServer(mqtt_server_ip, mqtt_port);

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

  // <--- FIX 2: CRITICAL! Keeps MQTT connection alive
  mqttClient.loop(); 

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
    if (mqtt_available) {

      // ===== ALL OK =====
      if (pzem_ok && ina_ok) {
        Serial.println("[MODE] ALL OK");

        updateLED(voltage);

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

        blinkGreen();

        float ac_calc_pow = volt * amp;

        Serial.printf("[DATA] AC_V: %.2f | AC_I: %.2f | AC_P: %.2f\n", volt, amp, ac_calc_pow);

        mqttClient.publish(ac_power, String(ac_calc_pow, 2).c_str());
        mqttClient.publish(status_topic, "ERROR: INA226_FAIL");

        Serial.println("[MQTT] Published PZEM only");
      }

      // ===== INA OK, PZEM FAIL =====
      else if (!pzem_ok && ina_ok) {
        Serial.println("[MODE] PZEM FAIL (But INA OK)");

        blinkRed();

        // <--- FIX 3: Added INA226 data publishing so it isn't lost
        mqttClient.publish(voltage_topic, String(voltage, 2).c_str());
        mqttClient.publish(power_topic, String(power, 2).c_str());

        mqttClient.publish(status_topic, "ERROR: PZEM_FAIL");

        Serial.println("[MQTT] Published INA data + PZEM Error");
      }

      // ===== BOTH FAIL =====
      else {
        Serial.println("[MODE] BOTH SENSORS FAIL");

        blinkRed();

        mqttClient.publish(status_topic, "ERROR: PZEM_FAIL INA226_FAIL");

        Serial.println("[MQTT] Status only (ALL FAIL)");
      }

    } else {
      Serial.println("[MQTT] Not available — skipping publish");
    }

    Serial.println("====== SENSOR READ END ======\n");
    
  } 
}

// ================= LED =================
void updateLED(float voltage) {

  digitalWrite(LED_RED, LOW);
  digitalWrite(LED_GREEN, LOW);
  digitalWrite(LED_BLUE, LOW);

  if (voltage > 3.9) {
    digitalWrite(LED_GREEN, HIGH);
  }
  else if (voltage > 3.6) {
    digitalWrite(LED_RED, HIGH);
    digitalWrite(LED_GREEN, HIGH);
  }
  else if (voltage > 3.4) {
    digitalWrite(LED_BLUE, HIGH);
  }
  else {
    digitalWrite(LED_RED, HIGH);
  }
}

void blinkRed() {
  digitalWrite(LED_GREEN, LOW);
  digitalWrite(LED_BLUE, LOW);

  digitalWrite(LED_RED, HIGH);
  delay(300);
  digitalWrite(LED_RED, LOW);
  delay(300);
}

void blinkGreen() {
  digitalWrite(LED_RED, LOW);
  digitalWrite(LED_BLUE, LOW);

  digitalWrite(LED_GREEN, HIGH);
  delay(300);
  digitalWrite(LED_GREEN, LOW);
  delay(300);
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
  } else {
    Serial.println("FAILED");
    mqtt_available = false;
  }
}
