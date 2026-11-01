#include "spool.h"

#include <Preferences.h>
#include <algorithm>
#include <math.h>

#include "config.h"

namespace {

const char* CELL_NAMES[NUM_CELLS] = {"cell1", "cell2"};

// One reading every READ_INTERVAL_MS, RATE_WINDOW kept, plus some slack
const int CAP = RATE_WINDOW * 1000 / READ_INTERVAL_MS + 200;

// Ring buffer of (unix time, reading) for one cell (CellHistory in spool_monitor.py)
struct History {
  uint32_t t[CAP];
  int32_t v[CAP];
  int head = 0;  // index of the oldest sample
  int count = 0;
  int jumps = 0;

  int idx(int i) const { return (head + i) % CAP; }
  uint32_t T(int i) const { return t[idx(i)]; }
  int32_t V(int i) const { return v[idx(i)]; }
  void popFront() {
    head = (head + 1) % CAP;
    count--;
  }
  void push(uint32_t tt, int32_t vv) {
    if (count == CAP) popFront();
    int k = (head + count) % CAP;
    t[k] = tt;
    v[k] = vv;
    count++;
  }
  void keepLast(int n) {
    while (count > n) popFront();
  }
};

History hist[NUM_CELLS];
SemaphoreHandle_t lock;  // history + config (written by loop/net task, read by web/printer task)
volatile bool ready = false;

CellCfg cfg[NUM_CELLS];
String peer = PEER_URL;

void lockTake() { xSemaphoreTake(lock, portMAX_DELAY); }
void lockGive() { xSemaphoreGive(lock); }

bool hoursLeft(double level, const CellCfg& c, bool hasRate, double rate, double& out) {
  if (!c.hasEmpty || !hasRate || rate < MIN_CONSUMING_RATE) return false;
  out = std::max(level - c.empty, 0.0) / rate;
  return true;
}

// Reads one optional number field: null -> has = false
bool readField(JsonVariantConst v, bool& has, long& value, const char* name, String& error) {
  if (v.isNull()) {
    has = false;
    return true;
  }
  if (!v.is<double>() && !v.is<long>()) {
    error = String(name) + " must be a number or null";
    return false;
  }
  has = true;
  value = lround(v.as<double>());
  return true;
}

void setDefaults() {
  for (int c = 0; c < NUM_CELLS; c++) {
    cfg[c].hasFull = cfg[c].hasEmpty = cfg[c].hasRef = true;
    cfg[c].full = DEF_FULL[c];
    cfg[c].empty = DEF_EMPTY[c];
    cfg[c].refRate = DEF_REF[c];
    cfg[c].note = DEF_NOTE[c];
  }
  peer = PEER_URL;
}

// Applies a config JSON (does not lock; caller holds the lock or is in setup)
bool applyConfig(JsonVariantConst in, String& error) {
  CellCfg next[NUM_CELLS];
  for (int c = 0; c < NUM_CELLS; c++) next[c] = cfg[c];
  String nextPeer = peer;

  if (!in["peer"].isNull()) nextPeer = in["peer"].as<String>();
  nextPeer.trim();
  while (nextPeer.endsWith("/")) nextPeer.remove(nextPeer.length() - 1);
  if (nextPeer.length() && !nextPeer.startsWith("http")) nextPeer = "http://" + nextPeer;

  JsonVariantConst cells = in["cells"];
  for (int c = 0; c < NUM_CELLS; c++) {
    JsonVariantConst x = cells[CELL_NAMES[c]];
    if (x.isNull()) continue;
    if (!readField(x["full"], next[c].hasFull, next[c].full, "full", error)) return false;
    if (!readField(x["empty"], next[c].hasEmpty, next[c].empty, "empty", error)) return false;
    if (!readField(x["ref_rate"], next[c].hasRef, next[c].refRate, "ref_rate", error)) return false;
    if (!x["note"].isNull()) next[c].note = x["note"].as<String>();
  }
  for (int c = 0; c < NUM_CELLS; c++) cfg[c] = next[c];
  peer = nextPeer;
  return true;
}

void configToJson(JsonObject out) {
  out["label"] = PRINTER_LABEL;
  out["peer"] = peer;
  JsonObject cells = out["cells"].to<JsonObject>();
  for (int c = 0; c < NUM_CELLS; c++) {
    JsonObject x = cells[CELL_NAMES[c]].to<JsonObject>();
    if (cfg[c].hasFull) x["full"] = cfg[c].full; else x["full"] = nullptr;
    if (cfg[c].hasEmpty) x["empty"] = cfg[c].empty; else x["empty"] = nullptr;
    if (cfg[c].hasRef) x["ref_rate"] = cfg[c].refRate; else x["ref_rate"] = nullptr;
    x["note"] = cfg[c].note;
  }
}

}  // namespace

void spoolBegin() {
  lock = xSemaphoreCreateMutex();
  setDefaults();
  Preferences prefs;
  if (prefs.begin("spool", true)) {
    String saved = prefs.getString("cfg", "");
    prefs.end();
    if (saved.length()) {
      JsonDocument doc;
      String error;
      if (deserializeJson(doc, saved) || !applyConfig(doc.as<JsonVariantConst>(), error)) {
        Serial.println("[config] saved config invalid, using defaults");
        setDefaults();
      } else {
        Serial.println("[config] loaded from flash");
      }
    }
  }
}

bool spoolReady() { return ready; }
void spoolSetReady() { ready = true; }

bool spoolAdd(int cell, uint32_t t, long value) {
  lockTake();
  History& h = hist[cell];
  bool reset = false;
  int nr = std::min(h.count, 15);
  if (nr >= 5) {
    double recent[15];
    for (int i = 0; i < nr; i++) recent[i] = h.V(h.count - nr + i);
    std::sort(recent, recent + nr);
    double median = nr % 2 ? recent[nr / 2] : (recent[nr / 2 - 1] + recent[nr / 2]) / 2;
    if (fabs(value - median) > JUMP_THRESHOLD) {
      if (++h.jumps >= JUMP_CONFIRM) {
        h.keepLast(JUMP_CONFIRM - 1);  // keep only the readings taken after the jump
        h.jumps = 0;
        reset = true;
      }
    } else {
      h.jumps = 0;
    }
  } else {
    h.jumps = 0;
  }
  h.push(t, value);
  while (h.count && (int64_t)t - (int64_t)h.T(0) > RATE_WINDOW) h.popFront();
  lockGive();
  return reset;
}

CellView spoolView(int cell, uint32_t now) {
  CellView out;
  lockTake();
  out.cfg = cfg[cell];
  const History& h = hist[cell];
  out.n = h.count;
  double lastMean = 0;
  if (h.count) {
    double sum = 0;
    int k = 0;
    for (int i = 0; i < h.count; i++) {
      if ((int64_t)now - (int64_t)h.T(i) <= CURRENT_WINDOW) {
        sum += h.V(i);
        k++;
      }
    }
    lastMean = k ? sum / k : h.V(h.count - 1);
    out.hasCurrent = true;
    out.current = lastMean;
    out.source = "last_minute";
    uint32_t t0 = h.T(0), tLast = h.T(h.count - 1);
    out.span = tLast - t0;
    if (out.span >= MIN_RATE_SPAN && h.count >= 10) {
      // Least-squares slope, times relative to the first sample
      double mt = 0, mv = 0;
      for (int i = 0; i < h.count; i++) {
        mt += h.T(i) - t0;
        mv += h.V(i);
      }
      mt /= h.count;
      mv /= h.count;
      double sxx = 0, sxy = 0;
      for (int i = 0; i < h.count; i++) {
        double dt = (double)(h.T(i) - t0) - mt;
        sxx += dt * dt;
        sxy += dt * (h.V(i) - mv);
      }
      if (sxx > 0) {
        double slope = sxy / sxx;  // counts/s
        out.hasLive = true;
        out.liveRate = -slope * 3600;  // counts/h dropped (positive = consuming)
        double tNow = (double)(std::min(now, tLast) - t0);
        double fitNow = mv + slope * (tNow - mt);
        if (out.liveRate >= MIN_CONSUMING_RATE && fabs(fitNow - lastMean) <= FIT_MAX_DEVIATION) {
          out.current = fitNow;
          out.source = "fit";
        }
      }
    }
  }
  lockGive();

  out.consuming = out.hasLive && out.liveRate >= MIN_CONSUMING_RATE;
  out.tensionOffset = out.consuming ? TENSION_OFFSET : 0;
  if (!out.hasCurrent) return out;
  // at-rest equivalent of the reading (undo the filament tension while printing)
  double level = out.current + out.tensionOffset;
  const CellCfg& c = out.cfg;
  if (c.hasFull && c.hasEmpty && c.full != c.empty) {
    out.hasPercent = true;
    out.percent = std::max(0.0, std::min(100.0, (level - c.empty) / (double)(c.full - c.empty) * 100));
  }
  out.hasLeftLive = hoursLeft(level, c, out.hasLive, out.liveRate, out.leftLive);
  out.hasLeftRef = hoursLeft(level, c, c.hasRef, c.refRate, out.leftRef);
  return out;
}

String spoolPeerUrl() {
  lockTake();
  String p = peer;
  lockGive();
  return p;
}

void spoolConfigJson(JsonObject out) {
  lockTake();
  configToJson(out);
  lockGive();
}

bool spoolSetConfig(JsonVariantConst in, String& error) {
  lockTake();
  bool ok = applyConfig(in, error);
  String saved;
  if (ok) {
    JsonDocument doc;
    configToJson(doc.to<JsonObject>());
    serializeJson(doc, saved);
  }
  lockGive();
  if (!ok) return false;
  Preferences prefs;
  if (!prefs.begin("spool", false)) {
    error = "could not open flash storage";
    return false;
  }
  prefs.putString("cfg", saved);
  prefs.end();
  Serial.println("[config] saved");
  return true;
}
