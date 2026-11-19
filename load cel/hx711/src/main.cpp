// Printer monitor running entirely on the ESP32 (one per printer, PRINTER_NUM picks which):
//   - load cells (HX711) + on-board calibration                      cells.cpp
//   - spool monitor: live consumption rate and time left             spool.cpp
//   - printer monitor: Duet polling, ntfy alerts, job logs           printer.cpp, ntfy.cpp
//   - dashboard and JSON API on port 80                              web.cpp, pages.h
//   - logs / CSV sent to the PC running log_server.py (optional)     pclog.cpp
//
// Tasks: loop() (core 1) = web server, button, HX711 reads.
//        core 0 = "reply" (seqs.reply every 0.5 s), "state" (every 10 s), "net" (ntfy + PC).
#include <Arduino.h>
#include <ESPmDNS.h>
#include <WiFi.h>

#include "cells.h"
#include "config.h"
#include "ntfy.h"
#include "pclog.h"
#include "printer.h"
#include "spool.h"
#include "util.h"
#include "web.h"

namespace {

bool ipPrinted = false;
bool mdnsStarted = false;

// Sends queued notifications and PC logs. First, once the clock is set, reloads the last
// RATE_WINDOW of load-cell readings from the PC so a reboot doesn't reset the live rate.
void netTask(void*) {
  bool historyDone = false;
  for (;;) {
    if (WiFi.isConnected()) {
      if (!historyDone && timeValid()) {
        int n = pclogLoadHistory();
        if (n >= 0) Serial.printf("[spool] reloaded %d readings from the PC\n", n);
        else if (pclogEnabled()) Serial.println("[spool] PC not reachable, live rate starts empty");
        spoolSetReady();
        historyDone = true;
      }
      ntfyProcess();
      pclogProcess();
    }
    vTaskDelay(pdMS_TO_TICKS(200));
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  Serial.printf("\n%s monitor (%s) starting\n", PRINTER_LABEL, PRINTER_URL);

  cellsBegin();
  spoolBegin();
  ntfyBegin();
  pclogBegin();

  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOSTNAME);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  configTzTime(TIMEZONE, "pool.ntp.org", "time.nist.gov");

  webBegin();
  printerBegin();
  xTaskCreatePinnedToCore(netTask, "net", 12288, nullptr, 1, nullptr, 0);
  if (!pclogEnabled()) Serial.println("[pclog] LOG_SERVER_URL not set: logs are not saved");
}

void loop() {
  webLoop();
  cellsLoop();

  // Print the IP once connected (and again after a reconnect)
  if (WiFi.status() == WL_CONNECTED) {
    if (!ipPrinted) {
      Serial.print("[wifi] connected, dashboard at http://");
      Serial.print(WiFi.localIP());
      Serial.printf("/  (or http://%s.local/), raw data at /data\n", HOSTNAME);
      ipPrinted = true;
      if (!mdnsStarted && MDNS.begin(HOSTNAME)) {
        MDNS.addService("http", "tcp", 80);
        mdnsStarted = true;
      }
    }
  } else {
    ipPrinted = false;
  }
}
