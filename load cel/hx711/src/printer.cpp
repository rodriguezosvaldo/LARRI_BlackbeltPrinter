#include "printer.h"

#include <HTTPClient.h>
#include <WiFi.h>
#include <math.h>
#include <vector>

#include "cells.h"
#include "config.h"
#include "ntfy.h"
#include "pclog.h"
#include "spool.h"
#include "util.h"

// See: https://github.com/Duet3D/RepRapFirmware/wiki/Object-Model-Documentation
namespace {

// ============================ Shared state (dashboard) ============================
SemaphoreHandle_t stateLock;
struct {
  bool online = false;
  String status;  // "" = unknown
  bool hasLayer = false;
  long layer = 0;
  String file;  // "" = no job
  uint32_t lastSeen = 0;
} shared;

// ====================== Previous state (edge detection) ======================
// seqs.reply: reply task only; the rest: state task only
bool hasPrevSeqs = false;
long prevSeqs = 0;
String prevStatus;  // "" = None
bool prevHasLayer = false;
long prevLayer = 0;

// ============================ notify_once ============================
SemaphoreHandle_t notifiedLock;
std::vector<String> notified;
const size_t MAX_NOTIFIED = 32;

// Send a single alert per problem until clearProblem() is called
bool notifyOnce(const String& problem, const String& title, const String& message,
                const char* priority, const char* tags) {
  xSemaphoreTake(notifiedLock, portMAX_DELAY);
  for (const String& k : notified) {
    if (k == problem) {
      xSemaphoreGive(notifiedLock);
      return false;
    }
  }
  notified.push_back(problem);
  // m118:<text> keys are never cleared (as in alarm.py); keep the list bounded by
  // forgetting the oldest message keys first
  if (notified.size() > MAX_NOTIFIED) {
    for (auto it = notified.begin(); it != notified.end(); ++it) {
      if (it->startsWith("m118:")) {
        notified.erase(it);
        break;
      }
    }
  }
  xSemaphoreGive(notifiedLock);
  notify(title, message, priority, tags);
  return true;
}

void clearProblem(const String& problem) {
  xSemaphoreTake(notifiedLock, portMAX_DELAY);
  for (auto it = notified.begin(); it != notified.end(); ++it) {
    if (*it == problem) {
      notified.erase(it);
      break;
    }
  }
  xSemaphoreGive(notifiedLock);
}

// ============================ Duet HTTP ============================
bool httpGet(const String& path, String& body, bool logErrors) {
  WiFiClient client;
  HTTPClient http;
  http.setConnectTimeout(3000);
  http.setTimeout(10000);
  if (!http.begin(client, String(PRINTER_URL) + path)) return false;
  int code = http.GET();
  if (code != 200) {
    if (logErrors)
      Serial.printf("[get %s error] %s: %s\n", path.c_str(), PRINTER_SLUG,
                    code < 0 ? HTTPClient::errorToString(code).c_str() : String(code).c_str());
    http.end();
    return false;
  }
  body = http.getString();
  http.end();
  return true;
}

// GET /rr_model?key=...&flags=... -> doc["result"]
bool duetModel(const char* key, const char* flags, JsonDocument& doc, bool logErrors = true,
               const JsonDocument* filter = nullptr) {
  String body;
  if (!httpGet(String("/rr_model?key=") + key + "&flags=" + flags, body, logErrors)) return false;
  DeserializationError e = filter ? deserializeJson(doc, body, DeserializationOption::Filter(*filter))
                                  : deserializeJson(doc, body);
  if (e) {
    if (logErrors) Serial.printf("[get %s error] %s: %s\n", key, PRINTER_SLUG, e.c_str());
    return false;
  }
  return true;
}

// ============================ Checks ============================
bool isPrinting(const String& s) { return s == "processing" || s == "simulating"; }
bool isStopped(const String& s) {
  return s == "paused" || s == "pausing" || s == "halted" || s == "cancelling" || s == "off" ||
         s == "idle";
}
bool isPaused(const String& s) { return s == "pausing" || s == "paused"; }

String layerText() { return prevHasLayer ? String(prevLayer) : String("None"); }

void saveLastReply(const String& reply) {
  if (!timeValid()) return;
  pclogReply(fmtTime(time(nullptr), "%Y-%m-%d  %H:%M:%S") + "," + reply);
}

// New M118 / error / warning message: seqs.reply changes, then rr_reply has the text
void checkReply() {
  static bool lastFailed = false;
  JsonDocument doc;
  if (!duetModel("seqs.reply", "d99n", doc, !lastFailed)) {
    lastFailed = true;  // log once per outage, this runs every 0.5 s
    return;
  }
  lastFailed = false;
  if (!doc["result"].is<long>()) return;
  long seqs = doc["result"].as<long>();
  if (hasPrevSeqs && seqs != prevSeqs) {
    String reply;
    if (httpGet("/rr_reply", reply, false)) {
      reply.trim();
      if (reply.length()) {
        notifyOnce("m118:" + reply, "New M118 Message",
                   String(PRINTER_SLUG) + "\nMessage: " + reply, "default", "warning");
        saveLastReply(reply);
      }
    }
  }
  // Always remember the last value (alarm.py only did it after a change, so the first
  // change was never seen)
  prevSeqs = seqs;
  hasPrevSeqs = true;
}

void checkLayer() {
  JsonDocument doc;
  prevHasLayer = duetModel("job.layer", "d99nfo", doc) && doc["result"].is<long>();
  prevLayer = prevHasLayer ? doc["result"].as<long>() : 0;
  xSemaphoreTake(stateLock, portMAX_DELAY);
  shared.hasLayer = prevHasLayer;
  shared.layer = prevLayer;
  xSemaphoreGive(stateLock);
}

// state.status: main indicator of pause/stop/halt (edge detection). Returns "" if unknown.
String checkStateStatus() {
  JsonDocument doc;
  bool ok = duetModel("state.status", "d99nfo", doc) && doc["result"].is<const char*>();
  String status = ok ? doc["result"].as<String>() : String();
  const String last = prevStatus;
  const char* slug = PRINTER_SLUG;
  if (ok && status != last && last.length()) {
    if (status == "halted") {
      notify("Printer Halted", String(slug) + "\nstate " + last + " -> halted (emergency stop)",
             "urgent", "rotating_light");
    } else if (status == "paused" && !isPaused(last)) {
      notify("Print Paused",
             String(slug) + "\nstate " + last + " -> paused\nCurrent layer: " + layerText() + "\n",
             "high", "pause_button");
    } else if (status == "resuming") {
      notify("Print Resuming", String(slug) + "\nstate " + last + " -> resuming", "default",
             "arrow_forward");
    } else if (isPrinting(last) && isStopped(status)) {
      notify("Print Stopped", String(slug) + "\nstate " + last + " -> " + status, "urgent",
             "octagonal_sign,warning");
    }
  }
  if (ok) prevStatus = status;
  Serial.printf("%s_state_status: %s\n", slug, ok ? status.c_str() : "None");

  xSemaphoreTake(stateLock, portMAX_DELAY);
  shared.online = ok;
  if (ok) {
    shared.status = status;
    shared.lastSeen = timeValid() ? time(nullptr) : 0;
  }
  xSemaphoreGive(stateLock);
  return ok ? status : String();
}

// ============================ Print Job Log ============================
struct JobLog {
  bool active = false;
  String file, base, key;
  time_t start = 0;
  time_t pauseStart = 0;  // 0 = not paused
  double pausedTotal = 0;
  // Filament consumption: summed positive deltas of job.rawExtrusion
  bool hasSample = false;
  double firstSample = 0, lastRaw = 0, lastTime = 0, extruded = 0;
} jobLog;

void jobWrite(const String& line) {
  Serial.printf("[job] %s\n", line.c_str());
  pclogJobLine(jobLog.key, jobLog.base, line);
}

// "dir/My part.gcode" -> "My_part" (Path.stem + re.sub(r"[^\w.-]+", "_", ...))
String jobStem(String fileName) {
  fileName.replace('\\', '/');
  int slash = fileName.lastIndexOf('/');
  String name = fileName.substring(slash + 1);
  int dot = name.lastIndexOf('.');
  if (dot > 0) name = name.substring(0, dot);
  String out;
  bool inRun = false;
  for (size_t i = 0; i < name.length(); i++) {
    char c = name[i];
    if (isalnum((unsigned char)c) || c == '_' || c == '.' || c == '-') {
      out += c;
      inRun = false;
    } else if (!inRun) {
      out += '_';
      inRun = true;
    }
  }
  return out;
}

void jobLogStart(const String& fileName, double duration, time_t now) {
  // p1_243_2026-10-11_<job>.log (the PC adds _2, _3... if the same job prints again that day)
  jobLog = JobLog();
  jobLog.active = true;
  jobLog.file = fileName;
  jobLog.start = now - (time_t)duration;
  String slug = PRINTER_SLUG;
  String octet = slug.substring(slug.lastIndexOf('.') + 1);
  jobLog.base = String(PRINTER_KEY) + "_" + octet + "_" + fmtTime(jobLog.start, "%Y-%m-%d") + "_" +
                jobStem(fileName);
  jobLog.key = jobLog.base + "|" + String((long)jobLog.start);
  jobWrite("Job: " + fileName);
  jobWrite(String("Printer: ") + PRINTER_NAME + " (" + PRINTER_SLUG + ")");
  const char* note = duration > 2 * REQUEST_STATE_MS / 1000 ? "  (log started mid-print)" : "";
  jobWrite("START   " + fmtDt(jobLog.start) + note);
}

void jobLogResume(time_t now) {
  double paused = difftime(now, jobLog.pauseStart);
  jobLog.pausedTotal += paused;
  jobLog.pauseStart = 0;
  jobWrite("RESUME  " + fmtDt(now) + "  (paused " + fmtDuration(paused) + ")");
}

// Accumulate extruded filament (positive rawExtrusion deltas only; a drop = firmware reset)
void jobLogSample(double raw, time_t now) {
  double t = now;
  if (!jobLog.hasSample) {
    jobLog.hasSample = true;
    jobLog.firstSample = t;
  } else if (raw >= jobLog.lastRaw) {
    jobLog.extruded += raw - jobLog.lastRaw;
  }
  jobLog.lastRaw = raw;
  jobLog.lastTime = t;
}

// Average rate over printing time (paused time excluded) since the first sample
void jobLogConsumption() {
  if (!jobLog.hasSample) return;
  double active = jobLog.lastTime - jobLog.firstSample - jobLog.pausedTotal;
  char buf[96];
  int n = snprintf(buf, sizeof(buf), "FILAMENT CONSUMED %.2f m", jobLog.extruded / 1000);
  if (active > 0) {
    double rate = jobLog.extruded / active * 3600;  // mm/h
    double area = M_PI * pow(FILAMENT_DIAMETER / 2, 2);
    snprintf(buf + n, sizeof(buf) - n, " | avg %.2f m/h (%.1f cm3/h)", rate / 1000,
             rate * area / 1000);
  }
  jobWrite(buf);
}

void jobLogEnd(JsonVariantConst job, time_t now) {
  if (jobLog.pauseStart) jobLogResume(now);
  jobLogConsumption();
  const char* result = job["lastFileCancelled"].as<bool>() ? "cancelled"
                       : job["lastFileAborted"].as<bool>() ? "aborted"
                                                           : "finished";
  double total = job["lastDuration"].as<double>();
  if (!total) total = difftime(now, jobLog.start);
  jobWrite("END     " + fmtDt(now) + "  " + result + " | total " + fmtDuration(total) +
           " | paused " + fmtDuration(jobLog.pausedTotal));
  jobLog.active = false;
}

// One log per print: START, every PAUSE/RESUME, END. A job = job.file.fileName not null.
void checkJobLog(const String& status) {
  if (!status.length() || !timeValid()) return;
  JsonDocument filter;
  JsonObject f = filter["result"].to<JsonObject>();
  f["file"]["fileName"] = true;
  f["duration"] = true;
  f["rawExtrusion"] = true;
  f["lastDuration"] = true;
  f["lastFileCancelled"] = true;
  f["lastFileAborted"] = true;
  JsonDocument doc;
  if (!duetModel("job", "d99vno", doc, true, &filter)) return;
  JsonVariantConst job = doc["result"];
  if (job.isNull()) return;
  time_t now = time(nullptr);
  const char* fn = job["file"]["fileName"];
  String fileName = fn ? fn : "";

  if (jobLog.active && (!fn || fileName != jobLog.file)) jobLogEnd(job, now);

  xSemaphoreTake(stateLock, portMAX_DELAY);
  shared.file = fileName;
  xSemaphoreGive(stateLock);

  if (!fn) return;
  if (!jobLog.active) jobLogStart(fileName, job["duration"].as<double>(), now);

  if (isPaused(status) && !jobLog.pauseStart) {
    jobLog.pauseStart = now;
    jobWrite("PAUSE   " + fmtDt(now));
  } else if (!isPaused(status) && jobLog.pauseStart) {
    jobLogResume(now);
  }
  if (!job["rawExtrusion"].isNull()) jobLogSample(job["rawExtrusion"].as<double>(), now);
}

// Alert once per spool when the time left drops below FILAMENT_LOW_HOURS
void checkFilamentTime() {
  if (!cellsOk() || !spoolReady() || !timeValid()) return;
  uint32_t now = time(nullptr);
  for (int c = 0; c < NUM_CELLS; c++) {
    CellView v = spoolView(c, now);
    // Only spools that are actually being consumed (live rate). No reference-rate fallback:
    // we can't tell which of the two cells feeds the printer.
    if (!v.consuming) continue;  // idle or no live rate yet: keep the alert state as is
    if (!v.hasCurrent || !v.cfg.hasEmpty) continue;
    if (v.current + v.tensionOffset <= v.cfg.empty) continue;  // empty reel or no reel
    if (!v.hasLeftLive) continue;
    String cell = "cell" + String(c + 1);
    String problem = "filament_low:" + cell;
    if (v.leftLive < FILAMENT_LOW_HOURS) {
      long minutes = lround(v.leftLive * 60);
      char left[32];
      snprintf(left, sizeof(left), "%ldh %02ldmin", minutes / 60, minutes % 60);
      notifyOnce(problem, "Filament Low",
                 String(PRINTER_NAME) + " " + cell + "\nTime left: " + left, "high", "warning");
    } else if (v.leftLive > FILAMENT_CLEAR_HOURS) {
      clearProblem(problem);
    }
  }
}

// ============================ Tasks ============================
void replyTask(void*) {
  for (;;) {
    if (WiFi.isConnected()) checkReply();
    vTaskDelay(pdMS_TO_TICKS(REQUEST_REPLY_MS));
  }
}

void stateTask(void*) {
  for (;;) {
    if (WiFi.isConnected()) {
      checkLayer();
      String status = checkStateStatus();
      checkJobLog(status);
      checkFilamentTime();
    }
    vTaskDelay(pdMS_TO_TICKS(REQUEST_STATE_MS));
  }
}

}  // namespace

void printerBegin() {
  stateLock = xSemaphoreCreateMutex();
  notifiedLock = xSemaphoreCreateMutex();
  xTaskCreatePinnedToCore(replyTask, "reply", 8192, nullptr, 1, nullptr, 0);
  xTaskCreatePinnedToCore(stateTask, "state", 12288, nullptr, 1, nullptr, 0);
}

void printerStatusJson(JsonObject out) {
  xSemaphoreTake(stateLock, portMAX_DELAY);
  out["url"] = PRINTER_URL;
  out["online"] = shared.online;
  if (shared.status.length()) out["status"] = shared.status; else out["status"] = nullptr;
  if (shared.hasLayer) out["layer"] = shared.layer; else out["layer"] = nullptr;
  if (shared.file.length()) out["file"] = shared.file; else out["file"] = nullptr;
  if (shared.lastSeen) out["last_seen"] = shared.lastSeen; else out["last_seen"] = nullptr;
  xSemaphoreGive(stateLock);
}
