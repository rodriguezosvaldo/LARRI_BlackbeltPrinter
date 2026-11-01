// Printer monitor (port of alarm.py) for this ESP32's printer: polls the Duet object model,
// sends ntfy alerts on state transitions / new messages / low filament, and writes job logs
// (sent to the PC through pclog).
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

void printerBegin();  // starts the polling tasks

// Latest printer state for the dashboard: {url, online, status, layer, file, last_seen}
void printerStatusJson(JsonObject out);
