#include "pclog.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <deque>

#include "config.h"
#include "spool.h"

namespace {

// Pending load-cell rows when the PC is down (12 bytes each)
#if CONFIG_IDF_TARGET_ESP32S3
const size_t PCLOG_MAX_ROWS = 3600;  // 2 h at one reading every 2 s
#else
const size_t PCLOG_MAX_ROWS = 1800;  // 1 h (classic ESP32 has less RAM)
#endif
const size_t MAX_EVENTS = 300;           // pending job-log / reply lines
const size_t ROWS_PER_POST = 300;
const unsigned long CELLS_SEND_MS = 60000;   // send readings once a minute
const unsigned long RETRY_MS = 30000;        // after a failed request

struct Event {
  const char* path;  // "/job" or "/reply"
  String body;       // JSON
};
struct Row {
  uint32_t t;
  int32_t c1, c2;
};

std::deque<Event> events;
std::deque<Row> rows;
SemaphoreHandle_t lock;
unsigned long nextTry = 0;
unsigned long lastCellsSend = 0;
bool lastOk = true;

String baseUrl() { return String(LOG_SERVER_URL); }

void addEvent(const char* path, JsonDocument& doc) {
  if (!pclogEnabled()) return;
  Event e{path, ""};
  serializeJson(doc, e.body);
  xSemaphoreTake(lock, portMAX_DELAY);
  if (events.size() >= MAX_EVENTS) events.pop_front();
  events.push_back(e);
  xSemaphoreGive(lock);
}

bool post(const char* path, const String& body) {
  WiFiClient client;
  HTTPClient http;
  http.setConnectTimeout(2000);
  http.setTimeout(5000);
  if (!http.begin(client, baseUrl() + path)) return false;
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(body);
  http.end();
  bool ok = code >= 200 && code < 300;
  if (ok != lastOk) {
    if (ok) Serial.println("[pclog] PC reachable again, sending pending logs");
    else Serial.printf("[pclog] PC not reachable (%s), will retry\n", HTTPClient::errorToString(code).c_str());
    lastOk = ok;
  }
  if (code >= 400 && code < 500) {
    Serial.printf("[pclog] PC rejected %s (HTTP %d), dropped\n", path, code);
    return true;  // a bad request won't get better by retrying
  }
  return ok;
}

}  // namespace

bool pclogEnabled() { return strlen(LOG_SERVER_URL) > 0; }

void pclogBegin() { lock = xSemaphoreCreateMutex(); }

void pclogJobLine(const String& key, const String& base, const String& line) {
  JsonDocument doc;
  doc["key"] = key;
  doc["base"] = base;
  doc["line"] = line;
  addEvent("/job", doc);
}

void pclogReply(const String& line) {
  JsonDocument doc;
  doc["slug"] = PRINTER_SLUG;
  doc["line"] = line;
  addEvent("/reply", doc);
}

void pclogCells(uint32_t t, long c1, long c2) {
  if (!pclogEnabled()) return;
  xSemaphoreTake(lock, portMAX_DELAY);
  if (rows.size() >= PCLOG_MAX_ROWS) rows.pop_front();
  rows.push_back({t, (int32_t)c1, (int32_t)c2});
  xSemaphoreGive(lock);
}

void pclogProcess() {
  if (!pclogEnabled() || !WiFi.isConnected()) return;
  if ((long)(millis() - nextTry) < 0) return;

  // Job-log and reply lines first, in order
  for (;;) {
    xSemaphoreTake(lock, portMAX_DELAY);
    if (events.empty()) {
      xSemaphoreGive(lock);
      break;
    }
    Event e = events.front();
    xSemaphoreGive(lock);
    if (!post(e.path, e.body)) {
      nextTry = millis() + RETRY_MS;
      return;
    }
    xSemaphoreTake(lock, portMAX_DELAY);
    if (!events.empty()) events.pop_front();
    xSemaphoreGive(lock);
  }

  // Load-cell rows once a minute, in chunks
  if (millis() - lastCellsSend < CELLS_SEND_MS) return;
  for (;;) {
    JsonDocument doc;
    doc["printer"] = PRINTER_KEY;
    JsonArray arr = doc["rows"].to<JsonArray>();
    size_t n;
    uint32_t sentUntil = 0;
    xSemaphoreTake(lock, portMAX_DELAY);
    n = std::min(rows.size(), ROWS_PER_POST);
    if (n) sentUntil = rows[n - 1].t;
    for (size_t i = 0; i < n; i++) {
      JsonArray r = arr.add<JsonArray>();
      r.add(rows[i].t);
      r.add(rows[i].c1);
      r.add(rows[i].c2);
    }
    xSemaphoreGive(lock);
    if (n == 0) break;
    String body;
    serializeJson(doc, body);
    doc.clear();
    if (!post("/cells", body)) {
      nextTry = millis() + RETRY_MS;
      return;
    }
    // Drop the rows just sent. New rows only go to the back; if the queue overflowed meanwhile,
    // the front was already dropped, so only remove rows not newer than the last one sent.
    xSemaphoreTake(lock, portMAX_DELAY);
    for (size_t i = 0; i < n && !rows.empty() && rows.front().t <= sentUntil; i++) rows.pop_front();
    xSemaphoreGive(lock);
  }
  lastCellsSend = millis();
}

int pclogLoadHistory() {
  if (!pclogEnabled()) return -1;
  WiFiClient client;
  HTTPClient http;
  http.setConnectTimeout(3000);
  http.setTimeout(5000);
  String url = baseUrl() + "/cells/recent?printer=" PRINTER_KEY "&seconds=" + String(RATE_WINDOW);
  if (!http.begin(client, url)) return -1;
  int code = http.GET();
  if (code != 200) {
    http.end();
    return -1;
  }
  // Plain text "unix,cell1,cell2" lines, streamed (too big to hold as one String)
  WiFiClient* s = http.getStreamPtr();
  s->setTimeout(3000);
  int loaded = 0;
  while (s->connected() || s->available()) {
    String line = s->readStringUntil('\n');
    if (!line.length()) {
      if (!s->available() && !s->connected()) break;
      continue;
    }
    unsigned long t;
    long c1, c2;
    if (sscanf(line.c_str(), "%lu,%ld,%ld", &t, &c1, &c2) == 3) {
      spoolAdd(0, t, c1);
      spoolAdd(1, t, c2);
      loaded++;
    }
  }
  http.end();
  return loaded;
}
