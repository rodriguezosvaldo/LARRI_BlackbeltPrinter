#include "web.h"

#include <ArduinoJson.h>
#include <Update.h>
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
  doc["fw"] = FW_BUILD;
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

// ======================= Firmware update over WiFi =======================
// POST /update[?force=1], multipart file, Basic auth ota:OTA_PASS. Written to the other OTA
// partition while the current firmware keeps running; the bootloader only switches to it if
// Update.end() verified it (size, checksum, chip), so a bad upload changes nothing.
struct {
  int code = 0;  // 0 = OK so far
  String error;
} ota;
uint32_t rebootAt = 0;  // millis() of the pending restart after a good update, 0 = none

void otaFail(int code, const String& error) {
  if (ota.code) return;  // keep the first error
  ota.code = code;
  ota.error = error;
  if (Update.isRunning()) Update.abort();
  Serial.printf("[ota] rejected: %s\n", error.c_str());
}

void handleUpdateUpload() {
  HTTPUpload& up = server.upload();
  if (up.status == UPLOAD_FILE_START) {
    ota.code = 0;
    ota.error = "";
    if (!strlen(OTA_PASS)) otaFail(403, "OTA disabled: OTA_PASS not set in secrets.h");
    else if (!server.authenticate("ota", OTA_PASS)) otaFail(401, "wrong password");
    else if (rebootAt) otaFail(409, "already updated, rebooting");
    else if (printerBusy() && server.arg("force") != "1")
      otaFail(409, "printer has a job (printing/paused): wait, or force=1");
    else if (!Update.begin(UPDATE_SIZE_UNKNOWN)) otaFail(500, Update.errorString());
    else Serial.printf("[ota] receiving %s\n", up.filename.c_str());
  } else if (up.status == UPLOAD_FILE_WRITE) {
    if (!ota.code && Update.write(up.buf, up.currentSize) != up.currentSize)
      otaFail(500, Update.errorString());
  } else if (up.status == UPLOAD_FILE_END) {
    if (!ota.code && !Update.end(true)) otaFail(400, Update.errorString());
    if (!ota.code) Serial.printf("[ota] %u bytes OK, rebooting\n", up.totalSize);
  } else if (up.status == UPLOAD_FILE_ABORTED) {
    otaFail(400, "upload aborted");
  }
}

void handleUpdateDone() {
  JsonDocument doc;
  if (!ota.code && !Update.isFinished()) otaFail(400, "no firmware file in the request");
  if (ota.code) {
    doc["error"] = ota.error;
    sendJson(ota.code, doc);
    return;
  }
  doc["ok"] = true;
  doc["previous_build"] = FW_BUILD;
  sendJson(200, doc);
  rebootAt = millis() + 1500;  // let the reply (and queued alerts) go out first
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
  server.on("/update", HTTP_GET, [] { server.send_P(200, "text/html", UPDATE_HTML); });
  server.on("/update", HTTP_POST, handleUpdateDone, handleUpdateUpload);
  server.onNotFound([] { server.send(404, "text/plain", "not found"); });
  server.begin();
}

void webLoop() {
  server.handleClient();
  if (rebootAt && (int32_t)(millis() - rebootAt) >= 0) ESP.restart();
}
