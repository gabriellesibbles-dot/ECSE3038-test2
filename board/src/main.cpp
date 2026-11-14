#include <Arduino.h>
#include <ArduinoJson.h>
#include <DHTesp.h>
#include <HTTPClient.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include <math.h>

#if __has_include("secrets.h")
#include "secrets.h"
#else
#define WIFI_SSID "Wokwi-GUEST"
#define WIFI_PASSWORD ""
#define WIFI_CHANNEL 6
#define API_URL ""
#define FAN_API_KEY ""
#endif

const char *MQTT_NAME = "gabrielle4";
const char *BROKER_HOST = "135.232.203.144";
const uint16_t BROKER_PORT = 1883;

const unsigned long WIFI_TIMEOUT_MS = 15000;
const unsigned long RETRY_EVERY_MS = 5000;
const unsigned long SEND_EVERY_MS = 10000;
const int DHT_PIN = 15;
const int LED_PIN = 2;

DHTesp dht;
WiFiClient net;
PubSubClient mqtt(net);

String statusTopic;
String settingsTopic;
String ledStateTopic;

unsigned long lastSend = 0;
unsigned long lastAttempt = 0;
float maxHumidity = 65.0f;
float lastHumidity = NAN;
String fanMode = "auto";
String reportedLed;
bool settingsReceived = false;

bool connectWiFi() {
  Serial.print("Connecting to ");
  Serial.print(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD, WIFI_CHANNEL);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > WIFI_TIMEOUT_MS) {
      Serial.println();
      Serial.println("No Wi-Fi after 15 s. Trying again in 5 s.");
      return false;
    }
    delay(250);
    Serial.print(".");
  }

  Serial.println(" connected");
  Serial.print("MAC address: ");
  Serial.println(WiFi.macAddress());
  return true;
}

void updateLed() {
  bool ledOn = false;
  if (fanMode == "on") {
    ledOn = true;
  } else if (fanMode == "auto" && settingsReceived &&
             !isnan(lastHumidity) && lastHumidity > maxHumidity) {
    ledOn = true;
  }

  digitalWrite(LED_PIN, ledOn ? HIGH : LOW);
  String ledState = ledOn ? "on" : "off";

  Serial.print("Mode: ");
  Serial.print(fanMode);
  Serial.print("  Max humidity: ");
  Serial.print(maxHumidity);
  Serial.print("  Humidity: ");
  Serial.print(lastHumidity);
  Serial.print("  -> LED ");
  Serial.println(ledState);

  if (mqtt.connected() && ledState != reportedLed) {
    if (mqtt.publish(ledStateTopic.c_str(), ledState.c_str(), true)) {
      reportedLed = ledState;
    } else {
      Serial.println("Could not publish LED state.");
    }
  }
}

void onMessage(char *topic, byte *payload, unsigned int length) {
  if (String(topic) != settingsTopic) {
    return;
  }

  JsonDocument document;
  DeserializationError error = deserializeJson(document, payload, length);
  if (error) {
    Serial.print("Invalid settings JSON: ");
    Serial.println(error.c_str());
    return;
  }

  const char *newMode = document["mode"] | "";
  float newMaxHumidity = document["max_humidity"] | NAN;
  if ((strcmp(newMode, "auto") != 0 && strcmp(newMode, "on") != 0 &&
       strcmp(newMode, "off") != 0) ||
      !isfinite(newMaxHumidity) || newMaxHumidity < 0.0f ||
      newMaxHumidity > 100.0f) {
    Serial.println("Settings need mode auto/on/off and max_humidity 0-100.");
    return;
  }

  fanMode = newMode;
  maxHumidity = newMaxHumidity;
  settingsReceived = true;
  updateLed();
}

void sendReading(float humidity) {
  if (strlen(API_URL) == 0 || strlen(FAN_API_KEY) == 0) {
    Serial.println("Set API_URL and FAN_API_KEY in board/include/secrets.h.");
    return;
  }

  char body[100];
  snprintf(body, sizeof(body), "{\"mac\":\"%s\",\"humidity\":%.1f}",
           WiFi.macAddress().c_str(), humidity);

  String apiUrl = String(API_URL);
  while (apiUrl.endsWith("/")) {
    apiUrl.remove(apiUrl.length() - 1);
  }

  HTTPClient http;
  if (!http.begin(apiUrl + "/readings")) {
    Serial.println("Could not start the HTTP request.");
    return;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", FAN_API_KEY);
  int status = http.POST(body);

  Serial.print("POST ");
  Serial.print(body);
  Serial.print(" -> ");
  Serial.println(status);
  if (status == 401 || status == 403) {
    Serial.println("Check FAN_API_KEY in secrets.h.");
  } else if (status == 404) {
    Serial.println("Register this MAC with POST /fans first.");
  } else if (status == 422) {
    Serial.println("The API rejected the reading; humidity must be 0-100.");
  } else if (status < 0) {
    Serial.println(http.errorToString(status));
  }
  http.end();
}

bool connectMqtt() {
  String clientId =
      "esp32-" + String(MQTT_NAME) + "-" +
      String((uint32_t)ESP.getEfuseMac(), HEX);
  Serial.print("Connecting to broker ");
  Serial.print(BROKER_HOST);
  Serial.print(" as ");
  Serial.println(clientId);

  bool connected = mqtt.connect(
      clientId.c_str(), statusTopic.c_str(), 1, true, "offline");
  if (!connected) {
    Serial.print("Broker said no, state ");
    Serial.println(mqtt.state());
    return false;
  }

  Serial.println("Connected to broker");
  if (!mqtt.publish(statusTopic.c_str(), "online", true)) {
    Serial.println("Could not publish online status.");
  }
  if (!mqtt.subscribe(settingsTopic.c_str())) {
    Serial.println("Could not subscribe to fan settings.");
    mqtt.disconnect();
    return false;
  }

  reportedLed = "";
  updateLed();
  return true;
}

void setup() {
  Serial.begin(115200);
  Serial.println("Hello from the ESP32");
  dht.setup(DHT_PIN, DHTesp::DHT22);
  pinMode(LED_PIN, OUTPUT);

  String base = "ecse3038/" + String(MQTT_NAME);
  statusTopic = base + "/status";
  settingsTopic = base + "/fan/settings";
  ledStateTopic = base + "/led/state";

  mqtt.setServer(BROKER_HOST, BROKER_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setKeepAlive(15);

  connectWiFi();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    if (millis() - lastAttempt >= RETRY_EVERY_MS) {
      lastAttempt = millis();
      connectWiFi();
    }
    return;
  }

  if (mqtt.connected()) {
    mqtt.loop();
  } else if (millis() - lastAttempt >= RETRY_EVERY_MS) {
    lastAttempt = millis();
    connectMqtt();
  }

  if (millis() - lastSend < SEND_EVERY_MS) {
    return;
  }
  lastSend = millis();

  float humidity = dht.getHumidity();
  if (isnan(humidity)) {
    Serial.println("No humidity reading. Check the DHT22 wiring.");
    return;
  }

  lastHumidity = humidity;
  sendReading(humidity);
  updateLed();
}
