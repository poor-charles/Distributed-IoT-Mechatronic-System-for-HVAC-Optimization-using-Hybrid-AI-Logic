#include <WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include <Adafruit_SHT31.h>
#include <INA226_WE.h> 
#include <ESPmDNS.h>
#include <IRremoteESP8266.h>
#include <IRsend.h>
#include <ir_Coolix.h>
#include <ir_Samsung.h>

// ================= WIFI =================
const char* ssid = "TP-Link_95CC";
const char* password = "11018605";

// ================= WIFI RETRY =================
const unsigned long WIFI_TIMEOUT_MS = 10000;
const int WIFI_MAX_RETRIES = 2;

// ================= MQTT =================
const char* mqtt_hostname = "cvmpi"; 
const int mqtt_port = 1883;
const char* mqtt_client_id = "ESP32_MasterNode";

// Outgoing Sensor Topics
const char* temp_topic    = "/room/temp2";
const char* humid_topic   = "/room/humid2";
const char* status_topic  = "/esp/status2";
const char* voltage_topic = "/esp/voltage2"; 
const char* power_topic   = "/esp/power2";   

// Incoming Command Topic
const char* command_topic = "/ac/master_command"; 

// ================= HARDWARE PINS =================
#define LED_RED    27
#define LED_GREEN  26
#define LED_BLUE   25

#define LED_FREQ 5000
#define LED_RES     8

const uint16_t SAMSUNG_PIN = 4; // IR LED for Samsung
const uint16_t COOLIX_PIN = 5;  // IR LED for General Royal

// ================= INA226 =================
#define INA226_ADDR 0x40
INA226_WE ina226 = INA226_WE(INA226_ADDR);

// ================= GLOBALS & OBJECTS =================
IPAddress mqtt_server_ip;
WiFiClient espClient;
PubSubClient mqttClient(espClient);
Adafruit_SHT31 sht31 = Adafruit_SHT31();

// --- PRIMARY BLASTERS (Standard Setup) ---
IRSamsungAc samsungPrimary(SAMSUNG_PIN);
IRCoolixAC coolixPrimary(COOLIX_PIN);

// --- SECONDARY BLASTERS (Cross-Fire Swapped Setup) ---
IRSamsungAc samsungSecondary(COOLIX_PIN); // Pointing Samsung to Coolix Pin!
IRCoolixAC coolixSecondary(SAMSUNG_PIN);  // Pointing Coolix to Samsung Pin!

// Non-blocking timer for sensors
unsigned long previousMillis = 0;
const unsigned long send_interval = 30000; // 30 seconds

bool ina_ok = false;
float voltage_avg = 0;

bool sht_ok = false;
bool system_ready = false;

// --- DOUBLE BLAST TRACKERS ---
bool pendingSecondaryBlast = false;
unsigned long primaryBlastTime = 0;
String pendingMode = "";
String pendingTempStr = "";
String pendingFan = "";
// -----------------------------

// ================= FUNCTION DECLARATIONS =================
void connectWiFi();
void discoverMQTTBroker();
void connectMQTT();
void setLED(uint32_t color);
void updateLED(float voltage);
void blinkRedError();

// ==========================================
// AC EXECUTION LOGIC (Cross-Fire Enabled)
// ==========================================
void executeACCommand(String mode, String tempStr, String fan, bool isSecondary) {
  
  // Create pointers to dynamically select our blasters
  IRSamsungAc *samsung;
  IRCoolixAC *coolix;

  if (isSecondary) {
    Serial.println("\n🔁 EXECUTING SECONDARY BLAST (LEDs SWAPPED!)...");
    samsung = &samsungSecondary;
    coolix  = &coolixSecondary;
  } else {
    Serial.println("\n⚡ EXECUTING PRIMARY BLAST (Standard LEDs)...");
    samsung = &samsungPrimary;
    coolix  = &coolixPrimary;
  }

  // --- HANDLE SYSTEM OFF ---
  if (mode == "off") {
    Serial.println("🛑 SHUTTING DOWN BOTH ACs");
    
    // Samsung Off
    samsung->off();
    samsung->send();
    
    // Wait 1 FULL SECOND to ensure no IR collision (Samsung codes are very long!)
    delay(1000); 
    
    // General Royal (Coolix) Off
    coolix->off();
    coolix->send();
    
    return; 
  }

  // --- HANDLE SYSTEM ON & SETTINGS ---
  Serial.println("✅ UPDATING AC SETTINGS");
  int targetTemp = tempStr.toInt();

  // 1. Setup General Royal (Coolix)
  coolix->on();
  coolix->setTemp(targetTemp);
  
  if (mode == "cool") coolix->setMode(kCoolixCool);
  else if (mode == "dry") coolix->setMode(kCoolixDry);
  else if (mode == "fan") coolix->setMode(kCoolixFan);
  else if (mode == "eco") {
    coolix->setMode(kCoolixCool); 
    coolix->setSleep();       
  }
  
  if (fan == "auto") coolix->setFan(kCoolixFanAuto);
  else if (fan == "1") coolix->setFan(kCoolixFanMin);
  else if (fan == "2") coolix->setFan(kCoolixFanMed);
  else if (fan == "3") coolix->setFan(kCoolixFanMax);
  
  coolix->send(); // Blast Coolix

  delay(1000); // Increased breather to 1 full second for safety

  // 2. Setup Samsung
  samsung->on();
  samsung->setTemp(targetTemp);
  
  if (mode == "cool") samsung->setMode(kSamsungAcCool);
  else if (mode == "dry") samsung->setMode(kSamsungAcDry);
  else if (mode == "fan") samsung->setMode(kSamsungAcFan);
  else if (mode == "eco") {
    samsung->setMode(kSamsungAcCool); 
    samsung->setEcono(true);          
    samsung->setQuiet(true);          
  }

  if (mode != "eco") {
    samsung->setEcono(false);
    samsung->setQuiet(false);
  }

  if (fan == "auto") samsung->setFan(kSamsungAcFanAuto);
  else if (fan == "1") samsung->setFan(kSamsungAcFanLow);
  else if (fan == "2") samsung->setFan(kSamsungAcFanMed);
  else if (fan == "3") samsung->setFan(kSamsungAcFanHigh);
  
  samsung->send(); // Blast Samsung
  delay(150);
  samsung->send();
}

// ==========================================
// MQTT CALLBACK
// ==========================================
void mqttCallback(char* topic, byte* message, unsigned int length) {
  String msg;
  for (int i = 0; i < length; i++) {
    msg += (char)message[i];
  }
  
  Serial.print("\n📥 Command Received: ");
  Serial.println(msg);

  int firstComma = msg.indexOf(',');
  int secondComma = msg.indexOf(',', firstComma + 1);
  
  String mode = msg.substring(0, firstComma);
  String tempStr = msg.substring(firstComma + 1, secondComma);
  String fan = msg.substring(secondComma + 1);
  
  mode.toLowerCase();
  fan.toLowerCase();

  // Save parameters to memory for the secondary blast
  pendingMode = mode;
  pendingTempStr = tempStr;
  pendingFan = fan;
  pendingSecondaryBlast = true;
  primaryBlastTime = millis(); // Start the 5-second stopwatch

  // Execute the primary blast immediately
  executeACCommand(mode, tempStr, fan, false);
}

// ================= SETUP =================
void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n=== ESP32 MASTER NODE STARTING ===");

  ledcAttach(LED_RED, LED_FREQ, LED_RES);
  ledcAttach(LED_GREEN, LED_FREQ, LED_RES);
  ledcAttach(LED_BLUE, LED_FREQ, LED_RES);
  setLED(0x000000); 

  samsungPrimary.begin();
  coolixPrimary.begin();
  samsungSecondary.begin();
  coolixSecondary.begin();

  connectWiFi();

  if (MDNS.begin("ESP32Sensor")) {
    Serial.println("mDNS started");
  }

  discoverMQTTBroker();
  mqttClient.setServer(mqtt_server_ip, mqtt_port);
  mqttClient.setCallback(mqttCallback);

  if (sht31.begin(0x44)) {
    Serial.println("SHT31 initialized.");
    sht_ok = true;
  } else {
    Serial.println("⚠️ SHT31 FAILED!");
    sht_ok = false;
  }

  if (ina226.init()) {
    Serial.println("INA226 initialized.");
    ina_ok = true;
    ina226.setResistorRange(0.1, 1.0); 
    ina226.setAverage(INA226_AVERAGE_16);
    ina226.setConversionTime(INA226_CONV_TIME_1100);
    ina226.setMeasureMode(INA226_CONTINUOUS);
  } else {
    Serial.println("⚠️ INA226 FAILED!");
    ina_ok = false;
  }

  if (sht_ok && ina_ok) {
     system_ready = true;
  } else {
     system_ready = false;
     setLED(0xFF0000); 
  }
}

// ================= LOOP =================
void loop() {

  // 1. Maintain WiFi Connection
  if (WiFi.status() != WL_CONNECTED) {
    blinkRedError(); 
    connectWiFi();
    discoverMQTTBroker();
    mqttClient.setServer(mqtt_server_ip, mqtt_port);
  }

  // 2. Maintain MQTT Connection
  static unsigned long lastMqttRetry = 0; 
  if (!mqttClient.connected()) {
    blinkRedError(); 
    if (millis() - lastMqttRetry >= 5000) {
      lastMqttRetry = millis();
      connectMQTT();
    }
  } else {
    mqttClient.loop(); 
  }

  // 3. BACKGROUND TIMER: Trigger Secondary Blast after 5 seconds
  if (pendingSecondaryBlast && (millis() - primaryBlastTime >= 5000)) {
    pendingSecondaryBlast = false; // Turn off the flag so it only fires once
    executeACCommand(pendingMode, pendingTempStr, pendingFan, true);
  }

  // 4. Normal Operation - Non-Blocking Sensor Read
  unsigned long currentMillis = millis();
  
  if (currentMillis - previousMillis >= send_interval) {
    previousMillis = currentMillis;

    if (!sht_ok) sht_ok = sht31.begin(0x44);
    if (!ina_ok) ina_ok = ina226.init();

    if (sht_ok && ina_ok) {
      float temp = sht31.readTemperature();
      float humid = sht31.readHumidity();
      float voltage = ina226.getBusVoltage_V();
      float power   = ina226.getBusPower();

      if (!isnan(temp) && !isnan(humid)) {
        if (voltage_avg == 0) {
          voltage_avg = voltage;
        } else {
          voltage_avg = (voltage_avg * 9 + voltage) / 10;
        }
        
        updateLED(voltage_avg); 

        Serial.printf("Temp: %.2f °C | Hum: %.2f %% | V: %.2f V | P: %.2f W\n", temp, humid, voltage, power);

        if (mqttClient.connected()) {
          mqttClient.publish(temp_topic, String(temp, 2).c_str());
          mqttClient.publish(humid_topic, String(humid, 2).c_str());
          mqttClient.publish(voltage_topic, String(voltage, 2).c_str());
          mqttClient.publish(power_topic, String(power, 2).c_str());
          mqttClient.publish(status_topic, "OK");
        }
      } 
    } else {
      Serial.println("⚠️ SENSOR OFFLINE. IR Blaster still active.");
      setLED(0xFF0000); 
      
      if (mqttClient.connected()) {
        String errorMsg = "ERROR:";
        if (!sht_ok) errorMsg += " SHT31_FAIL";
        if (!ina_ok) errorMsg += " INA226_FAIL";
        mqttClient.publish(status_topic, errorMsg.c_str());
      }
    }
  }
}

// ================= LED FUNCTIONS =================
void setLED(uint32_t color) {
  uint8_t r = (color >> 16) & 0xFF;
  uint8_t g = (color >> 8) & 0xFF;
  uint8_t b = color & 0xFF;

  ledcWrite(LED_RED, r);
  ledcWrite(LED_GREEN, g);
  ledcWrite(LED_BLUE, b);
}

void updateLED(float voltage) {
  if (voltage > 3.9) {
    setLED(0x00FF00);   // Green
  } else if (voltage > 3.6) {
    setLED(0xFFFF00);   // Yellow
  } else if (voltage > 3.4) {
    setLED(0xFF8000);   // Orange
  } else {
    setLED(0xFF0000);   // Red
  }
}

void blinkRedError() {
  static unsigned long lastBlink = 0;
  static bool ledState = false;
  
  if (millis() - lastBlink >= 300) {
    lastBlink = millis();
    ledState = !ledState;
    if (ledState) {
      setLED(0xFF0000); // Red ON
    } else {
      setLED(0x000000); // OFF
    }
  }
}

// ================= WIFI =================
void connectWiFi() {
  int retry_count = 0;
  while (retry_count < WIFI_MAX_RETRIES) {
    Serial.printf("Connecting WiFi (%d/%d)...", retry_count + 1, WIFI_MAX_RETRIES);
    WiFi.begin(ssid, password);
    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {
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
    return;
  }
  if (mqttClient.connect(mqtt_client_id)) {
    Serial.println(" Connected!");
    mqttClient.subscribe(command_topic); 
  } else {
    Serial.print(" Failed, rc=");
    Serial.print(mqttClient.state());
    Serial.println(" (Retrying next loop)");
  }
}