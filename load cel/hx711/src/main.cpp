#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include "HX711.h"

#include "secrets.h"  // define WIFI_SSID, WIFI_PASS, NTFY_URLs

const int HX1_DOUT = 21;
const int HX1_SCK  = 22;

const int HX2_DOUT = 18;
const int HX2_SCK  = 19;

// Adjust for each load cell
const long CELL1_LOW_THRESHOLD = 425000;
const long CELL2_LOW_THRESHOLD = 425000;
const char* NTFY_URL = P1_NTFY_URL;

const unsigned long READ_INTERVAL_MS = 2000;

int cell1_counter = 0;  // wait several low results before sending notification, to avoid false alarms
int cell2_counter = 0;
bool cell1_notified = false;  // avoid sending multiple notifications
bool cell2_notified = false;

// Latest readings, served at GET /data
long last1 = 0;
long last2 = 0;
unsigned long seq = 0;  // increments on every new reading, so clients can skip duplicates
bool sensorOk = false;
unsigned long lastRead = 0;
bool ipPrinted = false;

HX711 scale1;
HX711 scale2;
WebServer server(80);

void notify(const char* title, const String& message,
            const char* priority = "default", const char* tags = "") {
  Serial.printf("[ALARM] %s: %s\n", title, message.c_str());
  if (WiFi.status() != WL_CONNECTED) { Serial.println("[notify] no WiFi"); return; }

  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  if (!http.begin(client, NTFY_URL)) { Serial.println("[notify] begin failed"); return; }
  http.setTimeout(5000); // wait up to 5 seconds for the request to complete
  http.addHeader("Title", title);
  http.addHeader("Priority", priority);
  if (tags[0]) http.addHeader("Tags", tags);
  int code = http.POST(message);
  if (code <= 0) Serial.printf("[notify error] %s\n", http.errorToString(code).c_str()); // handle POST response
  http.end();
}

void handleData() {
  String json = "{\"seq\":" + String(seq) +
                ",\"ok\":" + (sensorOk ? "true" : "false") +
                ",\"cell1\":" + String(last1) +
                ",\"cell2\":" + String(last2) +
                ",\"threshold1\":" + String(CELL1_LOW_THRESHOLD) +
                ",\"threshold2\":" + String(CELL2_LOW_THRESHOLD) + "}";
  server.send(200, "application/json", json);
}

void readCells() {
  if (scale1.wait_ready_timeout(1000) && scale2.wait_ready_timeout(1000)) {
    long reading1 = scale1.read();
    long reading2 = scale2.read();
    Serial.printf("HX711 #1 reading: %ld\n", reading1);
    Serial.printf("HX711 #2 reading: %ld\n", reading2);
    last1 = reading1;
    last2 = reading2;
    sensorOk = true;
    seq++;

    if (reading1 > CELL1_LOW_THRESHOLD) {
        cell1_notified = false;  // reset notification flag
    }
    if (reading2 > CELL2_LOW_THRESHOLD) {
        cell2_notified = false;
    }

    if (reading1 < CELL1_LOW_THRESHOLD) cell1_counter++;
    if (reading2 < CELL2_LOW_THRESHOLD) cell2_counter++;

    if (cell1_counter >= 3 && !cell1_notified) {
            notify("Cell_1 Low Filament", "Reading: " + String(reading1), "high", "filament,alert");
            cell1_notified = true;
            cell1_counter = 0;  // reset counter after notification
    }
    if (cell2_counter >= 3 && !cell2_notified) {
            notify("Cell_2 Low Filament", "Reading: " + String(reading2), "high", "filament,alert");
            cell2_notified = true;
            cell2_counter = 0;
    }

  } else {
    sensorOk = false;
    Serial.println("HX711 not found.");
  }
}

void setup() {
  Serial.begin(115200);
  scale1.begin(HX1_DOUT, HX1_SCK);
  scale2.begin(HX2_DOUT, HX2_SCK);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  server.on("/data", handleData);
  server.begin();
}

void loop() {
  server.handleClient();

  // Print the IP once connected (and again after a reconnect), so it can be used from the PC
  if (WiFi.status() == WL_CONNECTED) {
    if (!ipPrinted) {
      Serial.print("[wifi] connected, data at http://");
      Serial.print(WiFi.localIP());
      Serial.println("/data");
      ipPrinted = true;
    }
  } else {
    ipPrinted = false;
  }

  // Non-blocking timer instead of delay(), so the web server keeps responding between reads
  if (millis() - lastRead >= READ_INTERVAL_MS) {
    lastRead = millis();
    readCells();
  }
}
