#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include "HX711.h"

#include "secrets.h"  // define WIFI_SSID, WIFI_PASS, NTFY_URLs

// --- Previous board: classic ESP32 (esp32dev) ---
// Pin definitions for the two HX711 modules of P2-188
// const int HX1_DOUT = 21;
// const int HX1_SCK  = 22;
//
// const int HX2_DOUT = 18;
// const int HX2_SCK  = 19;
//
// Pin definitions for the two HX711 modules of P1-243
// (GPIO 20 does not exist on the classic ESP32, so cell 2 never worked here)
// const int HX1_DOUT = 17;
// const int HX1_SCK  = 18;
//
// const int HX2_DOUT = 19;
// const int HX2_SCK  = 20;


// --- Current board: ESP32-S3-WROOM-1 ---
// Avoid GPIO 19/20 (native USB), 26-37 (flash/PSRAM) and 0/3/45/46 (strapping pins).
const int HX1_DOUT = 17;
const int HX1_SCK  = 18;

const int HX2_DOUT = 15;
const int HX2_SCK  = 16;


// --- Fixed IP (must match "esp32" in spools.json) ---
// Uncomment the line of the printer you are flashing.
const IPAddress STATIC_IP(192, 168, 1, 132);   // P1-243
// const IPAddress STATIC_IP(192, 168, 1, 226);   // P2-188
const IPAddress GATEWAY(192, 168, 1, 1);
const IPAddress SUBNET(255, 255, 255, 0);
const IPAddress DNS(192, 168, 1, 1);


const unsigned long READ_INTERVAL_MS = 2000;
const int LOW_READINGS_TO_ALERT = 3;  // consecutive low readings before alerting, to avoid false alarms


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

// void notify(const char* title, const String& message,
//             const char* priority = "default", const char* tags = "") {
//   Serial.printf("[ALARM] %s: %s\n", title, message.c_str());
//   if (WiFi.status() != WL_CONNECTED) { Serial.println("[notify] no WiFi"); return; }

//   WiFiClientSecure client;
//   client.setInsecure();
//   HTTPClient http;
//   if (!http.begin(client, NTFY_URL)) { Serial.println("[notify] begin failed"); return; }
//   http.setTimeout(5000); // wait up to 5 seconds for the request to complete
//   http.addHeader("Title", title);
//   http.addHeader("Priority", priority);
//   if (tags[0]) http.addHeader("Tags", tags);
//   int code = http.POST(message);
//   if (code <= 0) Serial.printf("[notify error] %s\n", http.errorToString(code).c_str()); // handle POST response
//   http.end();
// }

void handleData() {
  String json = "{\"seq\":" + String(seq) +
                ",\"ok\":" + (sensorOk ? "true" : "false") +
                ",\"cell1\":" + String(last1) +
                ",\"cell2\":" + String(last2) +
                "}";
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
  if (!WiFi.config(STATIC_IP, GATEWAY, SUBNET, DNS)) {
    Serial.println("[wifi] static IP config failed, using DHCP");
  }
  WiFi.setAutoReconnect(true);
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
