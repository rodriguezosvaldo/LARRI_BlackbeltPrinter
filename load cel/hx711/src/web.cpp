#include "web.h"

#include <ArduinoJson.h>
#include <WebServer.h>
#include <WiFi.h>

#include "cells.h"
#include "config.h"
#include "pages.h"
#include "printer.h"
#include "spool.h"
#include "util.h"

namespace {

WebServer server(80);

void sendJson(int code, const String& body) {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.sendHeader("Cache-Control", "no-store");
  server.send(code, "application/json", body);
}

void sendJson(int code, JsonDocument& doc) {
  String body;
  serializeJson(doc, body);
  sendJson(code, body);
}

void setOpt(JsonObject o, const char* key, bool has, double v) {
  if (has) o[key] = v; else o[key] = nullptr;
}

// Same shape as spool_monitor.py /api/spools, with only this ESP32's printer, plus "printer"
// (Duet state), "peer" (the other ESP32) and "self" (this printer's key)
void handleSpools() {
  JsonDocument doc;
  uint32_t now = time(nullptr);
  if (timeValid()) doc["time"] = fmtTime(now, "%Y-%m-%d %H:%M:%S"); else doc["time"] = nullptr;
  doc["rate_window"] = RATE_WINDOW;
  doc["min_rate_span"] = MIN_RATE_SPAN;
  doc["self"] = PRINTER_KEY;
  doc["peer"] = spoolPeerUrl();
  doc["ready"] = spoolReady();

  JsonObject p = doc["printers"][PRINTER_KEY].to<JsonObject>();
  p["label"] = PRINTER_LABEL;
  p["online"] = cellsOk();
  uint32_t seen = cellsLastSeenUnix();
  if (seen) p["last_seen"] = seen; else p["last_seen"] = nullptr;
  if (cellsOk()) p["error"] = nullptr; else p["error"] = "HX711 not ready";
  p["ip"] = WiFi.localIP().toString();

  JsonObject cells = p["cells"].to<JsonObject>();
  for (int c = 0; c < NUM_CELLS; c++) {
    CellView v = spoolView(c, now);
    JsonObject x = cells[c == 0 ? "cell1" : "cell2"].to<JsonObject>();
    setOpt(x, "current", v.hasCurrent, v.current);
    if (v.source) x["current_source"] = v.source; else x["current_source"] = nullptr;
    setOpt(x, "full", v.cfg.hasFull, v.cfg.full);
    setOpt(x, "empty", v.cfg.hasEmpty, v.cfg.empty);
    setOpt(x, "ref_rate", v.cfg.hasRef, v.cfg.refRate);
    setOpt(x, "live_rate", v.hasLive, v.liveRate);
    x["live_span"] = v.span;
    x["consuming"] = v.consuming;
    x["tension_offset"] = v.tensionOffset;
    setOpt(x, "percent", v.hasPercent, v.percent);
    setOpt(x, "hours_left_live", v.hasLeftLive, v.leftLive);
    setOpt(x, "hours_left_ref", v.hasLeftRef, v.leftRef);
    x["note"] = v.cfg.note;
  }
  printerStatusJson(p["printer"].to<JsonObject>());
  sendJson(200, doc);
}

void handleConfigGet() {
  JsonDocument doc;
  spoolConfigJson(doc.to<JsonObject>());
  sendJson(200, doc);
}

void handleConfigPost() {
  JsonDocument in;
  if (deserializeJson(in, server.arg("plain"))) {
    sendJson(400, "{\"error\":\"invalid JSON\"}");
    return;
  }
  String error;
  if (!spoolSetConfig(in.as<JsonVariantConst>(), error)) {
    JsonDocument out;
    out["error"] = error;
    sendJson(400, out);
    return;
  }
  handleConfigGet();
}

}  // namespace

void webBegin() {
  server.on("/", HTTP_GET, [] { server.send_P(200, "text/html", DASHBOARD_HTML); });
  server.on("/calib", HTTP_GET, [] { server.send_P(200, "text/html", CALIB_HTML); });
  server.on("/config", HTTP_GET, [] { server.send_P(200, "text/html", CONFIG_HTML); });
  server.on("/api/spools", HTTP_GET, handleSpools);
  server.on("/api/config", HTTP_GET, handleConfigGet);
  server.on("/api/config", HTTP_POST, handleConfigPost);
  server.on("/data", HTTP_GET, [] { sendJson(200, cellsDataJson()); });
  server.on("/calibration", HTTP_GET, [] { sendJson(200, calibJson()); });
  server.on("/calibration/start", HTTP_POST, [] {  // restarts if running
    calibStart();
    sendJson(200, calibJson());
  });
  server.on("/calibration/cancel", HTTP_POST, [] {
    calibCancel();
    sendJson(200, calibJson());
  });
  server.onNotFound([] { server.send(404, "text/plain", "not found"); });
  server.begin();
}

void webLoop() { server.handleClient(); }
