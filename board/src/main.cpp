#include <Arduino.h>
#include <Arduino.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <HTTPClient.h>
#include <PubSubClient.h>
#include <WiFi.h>

#if __has_include("secrets.h")
#include "secrets.h"
#endif

#ifndef WIFI_SSID
#define WIFI_SSID "Wokwi-GUEST"
#endif
#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD ""
#endif
#ifndef WIFI_CHANNEL
#define WIFI_CHANNEL 6
#endif
#ifndef DEVICE_KEY
#define DEVICE_KEY ""
#endif
#ifndef API_URL
#define API_URL "http://135.232.203.144"
#endif
#ifndef MQTT_NAME
#define MQTT_NAME "yourname"
#endif

// Standard Wokwi ESP32 pins (verify against your diagram.json if different)
const int DHT_PIN = 15;
const int FAN_LED_PIN = 2;

const char* MQTT_BROKER = "135.232.203.144";
const int MQTT_PORT = 1883;
const unsigned long WIFI_TIMEOUT_MS = 15000;
const unsigned long READING_INTERVAL_MS = 10000;

DHT dht(DHT_PIN, DHT22);
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

String settingsTopic = String("ecse3038/") + MQTT_NAME + "/fan/settings";
String stateTopic    = String("ecse3038/") + MQTT_NAME + "/fan/state";
String statusTopic   = String("ecse3038/") + MQTT_NAME + "/status";

// B4: Until the first settings arrive, the fan is off
bool hasSettings = false;
String fanMode = "off";
float maxHumidity = 100.0;
float currentHumidity = NAN;
bool fanIsOn = false;
bool statePublishedOnce = false;
bool stateNeedsPublish = false;

unsigned long lastReadingMs = 0;
unsigned long lastMqttAttemptMs = 0;

// B4 & B5: Evaluate fan state and publish on/off retained only when it changes
void updateFan() {
  bool desiredOn = false;

  if (!hasSettings) {
    desiredOn = false;
  } else if (fanMode == "on") {
    desiredOn = true;
  } else if (fanMode == "off") {
    desiredOn = false;
  } else if (fanMode == "auto") {
    if (!isnan(currentHumidity) && currentHumidity > maxHumidity) {
      desiredOn = true;
    } else {
      desiredOn = false;
    }
  }

  digitalWrite(FAN_LED_PIN, desiredOn ? HIGH : LOW);

  if (!statePublishedOnce || desiredOn != fanIsOn) {
    fanIsOn = desiredOn;
    statePublishedOnce = true;
    stateNeedsPublish = true;
  }

  if (mqtt.connected() && stateNeedsPublish &&
      mqtt.publish(stateTopic.c_str(), fanIsOn ? "on" : "off", true)) {
    stateNeedsPublish = false;
  }
}

// B3: Receive settings JSON, validate fields, and apply immediately
void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  if (String(topic) != settingsTopic) return;

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) {
    Serial.println("Invalid JSON on fan/settings; ignoring");
    return;
  }

  // Must contain both "mode" (string) and "max_humidity" (number)
  if (!doc["mode"].is<const char*>() || !doc["max_humidity"].is<float>()) {
    Serial.println("Missing or invalid fields in fan/settings; ignoring");
    return;
  }

  String newMode = doc["mode"].as<String>();
  float newMax = doc["max_humidity"].as<float>();

  if (newMode != "auto" && newMode != "on" && newMode != "off") {
    Serial.println("Invalid mode in fan/settings; ignoring");
    return;
  }

  if (newMax < 0.0 || newMax > 100.0) {
    Serial.println("Invalid max_humidity in fan/settings; ignoring");
    return;
  }

  fanMode = newMode;
  maxHumidity = newMax;
  hasSettings = true;

  Serial.print("Updated settings -> mode: ");
  Serial.print(fanMode);
  Serial.print(", max_humidity: ");
  Serial.println(maxHumidity);

  // React immediately when new settings arrive
  updateFan();
}

bool connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return true;

  Serial.print("Connecting to ");
  Serial.print(WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD, WIFI_CHANNEL);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > WIFI_TIMEOUT_MS) {
      Serial.println(" failed");
      return false;
    }
    delay(250);
    Serial.print(".");
  }

  Serial.println(" connected");
  Serial.print("IP address: ");
  Serial.println(WiFi.localIP());
  Serial.print("MAC address: ");
  Serial.println(WiFi.macAddress());
  return true;
}

// B2: Connect to broker with Last Will (offline, retained) & publish online (retained)
void connectMqtt() {
  if (WiFi.status() != WL_CONNECTED || mqtt.connected()) return;

  String clientId = String("esp32-fan-") + WiFi.macAddress();
  mqtt.setKeepAlive(15);

  if (mqtt.connect(clientId.c_str(), statusTopic.c_str(), 1, true, "offline")) {
    Serial.println("Connected to MQTT broker");
    mqtt.publish(statusTopic.c_str(), "online", true);
    mqtt.subscribe(settingsTopic.c_str(), 1);
    updateFan();
  } else {
    Serial.print("MQTT connect failed, rc=");
    Serial.println(mqtt.state());
  }
}

// B1: Read DHT22 humidity and POST to /readings with X-API-Key
void readAndPostHumidity() {
  float h = dht.readHumidity();
  if (isnan(h)) {
    Serial.println("Failed to read from DHT22 sensor");
    return;
  }

  currentHumidity = h;
  Serial.print("Humidity: ");
  Serial.print(currentHumidity);
  Serial.println("%");

  // Re-evaluate fan in case mode is "auto"
  updateFan();

  if (WiFi.status() != WL_CONNECTED) return;

  JsonDocument doc;
  doc["mac"] = WiFi.macAddress();
  doc["humidity"] = currentHumidity;

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.begin(String(API_URL) + "/readings");
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", DEVICE_KEY);

  int status = http.POST(body);
  Serial.print("POST /readings -> ");
  Serial.println(status);
  if (status > 0) {
    Serial.println(http.getString());
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  pinMode(FAN_LED_PIN, OUTPUT);
  digitalWrite(FAN_LED_PIN, LOW);

  dht.begin();
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);

  if (connectWiFi()) {
    connectMqtt();
    readAndPostHumidity();
    lastReadingMs = millis();
  }
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  if (!mqtt.connected()) {
    if (lastMqttAttemptMs == 0 || millis() - lastMqttAttemptMs >= 5000) {
      lastMqttAttemptMs = millis();
      connectMqtt();
    }
  } else {
    mqtt.loop();
  }

  if (millis() - lastReadingMs >= READING_INTERVAL_MS) {
    lastReadingMs = millis();
    readAndPostHumidity();
  }
}
