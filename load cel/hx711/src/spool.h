// Spool monitor (port of load cel/spool_monitor.py): per-cell reading history, live consumption
// rate (least-squares slope) and time left, plus the per-cell config (full/empty/ref_rate).
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

const int NUM_CELLS = 2;

struct CellCfg {
  bool hasFull = false, hasEmpty = false, hasRef = false;
  long full = 0, empty = 0, refRate = 0;  // raw counts, counts/h
  String note;
};

// Same fields as spool_monitor.py cell_view(); has* = false means null
struct CellView {
  CellCfg cfg;
  bool hasCurrent = false;
  double current = 0;
  const char* source = nullptr;  // "fit" or "last_minute"
  bool hasLive = false;
  double liveRate = 0;  // counts/h, positive = consuming
  double span = 0;      // s of history
  int n = 0;
  bool consuming = false;
  long tensionOffset = 0;
  bool hasPercent = false;
  double percent = 0;
  bool hasLeftLive = false, hasLeftRef = false;
  double leftLive = 0, leftRef = 0;  // hours
};

void spoolBegin();  // load config from NVS

// History only starts once the clock is set and the PC history (if any) was reloaded
bool spoolReady();
void spoolSetReady();

// Add a reading (unix time, raw counts) for cell 0/1. Returns true if a spool change was
// detected (history reset).
bool spoolAdd(int cell, uint32_t t, long value);
CellView spoolView(int cell, uint32_t now);

// Config (GET/POST /api/config): {"label","peer","cells":{"cell1":{full,empty,ref_rate,note},...}}
String spoolPeerUrl();
void spoolConfigJson(JsonObject out);
bool spoolSetConfig(JsonVariantConst in, String& error);
