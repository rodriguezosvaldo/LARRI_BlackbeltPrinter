#include "cells.h"

#include "HX711.h"
#include "config.h"
#include "pclog.h"
#include "spool.h"
#include "util.h"

namespace {

// Latest readings, served at GET /data
volatile long last1 = 0;
volatile long last2 = 0;
volatile unsigned long seq = 0;  // increments on every new reading, so clients can skip duplicates
volatile bool sensorOk = false;
volatile uint32_t lastSeenUnix = 0;
unsigned long lastRead = 0;

// On-board calibration (same mean / std dev as calibration.py, for this ESP32's two cells).
// Welford's running mean/variance, so no reading buffer is needed.
struct CalibStats {
  long n = 0;
  double mean = 0;
  double m2 = 0;  // sum of squared differences from the mean
  void reset() { n = 0; mean = 0; m2 = 0; }
  void add(long x) {
    n++;
    double d = x - mean;
    mean += d / n;
    m2 += d * (x - mean);
  }
  double stdDev() const { return n > 1 ? sqrt(m2 / (n - 1)) : 0; }  // sample std dev, like statistics.stdev
};

enum CalibState { CALIB_IDLE, CALIB_RUNNING, CALIB_DONE, CALIB_CANCELLED };
CalibState calibState = CALIB_IDLE;
CalibStats calib1, calib2;
CalibUpdate calibUpd[NUM_CELLS];  // what each cell's result did to empty/full
unsigned long calibStartMs = 0;
unsigned long calibEndMs = 0;

bool buttonStable = HIGH;  // debounced level (pull-up: HIGH = released)
bool buttonLastRaw = HIGH;
unsigned long buttonChangedMs = 0;

HX711 scale1;
HX711 scale2;

const char* calibStateName() {
  switch (calibState) {
    case CALIB_RUNNING:   return "running";
    case CALIB_DONE:      return "done";
    case CALIB_CANCELLED: return "cancelled";
    default:              return "idle";
  }
}

// Replaces the cell's empty or full with the calibration mean if it is close to it
void calibApply(int cell, const CalibStats& s) {
  CalibUpdate& u = calibUpd[cell];
  u = spoolCalibrate(cell, s.mean, s.stdDev());
  const char* r = u.result;
  if (u.changed)
    Serial.printf("[calib] cell %d: %s %ld -> %ld (%s)\n", cell + 1, r, u.before, u.after,
                  u.saved ? "saved" : "NOT saved, flash error");
  else if (!strcmp(r, "unstable"))
    Serial.printf("[calib] cell %d: std dev above %.0f, not at rest, nothing saved\n", cell + 1, CALIB_MAX_STD);
  else if (!strcmp(r, "printing"))
    Serial.printf("[calib] cell %d: spool is being consumed, nothing saved\n", cell + 1);
  else
    Serial.printf("[calib] cell %d: mean not within %.0f of empty or full, nothing saved\n", cell + 1,
                  CALIB_MATCH_TOLERANCE);
}

// Called after every good reading
void calibAddReading(long reading1, long reading2) {
  if (calibState != CALIB_RUNNING) return;
  calib1.add(reading1);
  calib2.add(reading2);
  if (calib1.n >= CALIB_WINDOW) {
    calibState = CALIB_DONE;
    calibEndMs = millis();
    Serial.printf("[calib] cell 1 last %d readings -> mean: %.2f, std dev: %.2f\n",
                  CALIB_WINDOW, calib1.mean, calib1.stdDev());
    Serial.printf("[calib] cell 2 last %d readings -> mean: %.2f, std dev: %.2f\n",
                  CALIB_WINDOW, calib2.mean, calib2.stdDev());
    calibApply(0, calib1);
    calibApply(1, calib2);
  }
}

void checkButton() {
  bool raw = digitalRead(CALIB_BUTTON_PIN);
  if (raw != buttonLastRaw) {
    buttonLastRaw = raw;
    buttonChangedMs = millis();
  }
  if (raw != buttonStable && millis() - buttonChangedMs >= BUTTON_DEBOUNCE_MS) {
    buttonStable = raw;
    if (buttonStable == LOW) {  // pressed
      if (calibState == CALIB_RUNNING) calibCancel();
      else calibStart();
    }
  }
}

String cellJson(const CalibStats& c, const CalibUpdate& u) {
  String s = "{\"mean\":" + String(c.mean, 2) + ",\"std_dev\":" + String(c.stdDev(), 2) + ",\"result\":";
  s += u.result ? "\"" + String(u.result) + "\"" : String("null");
  if (u.changed)
    s += ",\"before\":" + String(u.before) + ",\"after\":" + String(u.after) +
         ",\"saved\":" + (u.saved ? "true" : "false");
  return s + "}";
}

void readCells() {
  if (scale1.wait_ready_timeout(1000) && scale2.wait_ready_timeout(1000)) {
    long reading1 = scale1.read();
    long reading2 = scale2.read();
    // calibration.py parses these two lines
    Serial.printf("HX711 #1 reading: %ld\n", reading1);
    Serial.printf("HX711 #2 reading: %ld\n", reading2);
    last1 = reading1;
    last2 = reading2;
    sensorOk = true;
    seq++;
    calibAddReading(reading1, reading2);

    // Spool monitor + PC CSV log, once the clock is set and the history was reloaded
    if (spoolReady() && timeValid()) {
      uint32_t t = time(nullptr);
      lastSeenUnix = t;
      long readings[NUM_CELLS] = {reading1, reading2};
      for (int c = 0; c < NUM_CELLS; c++) {
        if (spoolAdd(c, t, readings[c]))
          Serial.printf("[%s] cell%d: spool change detected, live rate reset\n", PRINTER_KEY, c + 1);
      }
      pclogCells(t, reading1, reading2);
    }
  } else {
    sensorOk = false;
    Serial.println("HX711 not found.");
  }
}

}  // namespace

void cellsBegin() {
  pinMode(CALIB_BUTTON_PIN, INPUT_PULLUP);
  scale1.begin(HX1_DOUT, HX1_SCK);
  scale2.begin(HX2_DOUT, HX2_SCK);
}

void cellsLoop() {
  checkButton();
  // Non-blocking timer instead of delay(), so the web server keeps responding between reads
  if (millis() - lastRead >= READ_INTERVAL_MS) {
    lastRead = millis();
    readCells();
  }
}

bool cellsOk() { return sensorOk; }
uint32_t cellsLastSeenUnix() { return lastSeenUnix; }

String cellsDataJson() {
  return "{\"seq\":" + String(seq) +
         ",\"ok\":" + (sensorOk ? "true" : "false") +
         ",\"cell1\":" + String(last1) +
         ",\"cell2\":" + String(last2) +
         "}";
}

void calibStart() {
  calib1.reset();
  calib2.reset();
  for (auto& u : calibUpd) u = CalibUpdate();
  calibStartMs = millis();
  calibState = CALIB_RUNNING;
  Serial.printf("[calib] started: %d readings per cell (~%lu s)\n",
                CALIB_WINDOW, CALIB_WINDOW * READ_INTERVAL_MS / 1000);
}

void calibCancel() {
  if (calibState != CALIB_RUNNING) return;
  calibState = CALIB_CANCELLED;
  calibEndMs = millis();
  Serial.printf("[calib] cancelled after %ld readings\n", calib1.n);
}

// state, progress and (partial while running) mean / std dev per cell
String calibJson() {
  unsigned long end = calibState == CALIB_RUNNING ? millis() : calibEndMs;
  return "{\"state\":\"" + String(calibStateName()) + "\"" +
         ",\"readings\":" + String(calib1.n) +
         ",\"window\":" + String(CALIB_WINDOW) +
         ",\"elapsed_s\":" + String(calibState == CALIB_IDLE ? 0 : (end - calibStartMs) / 1000) +
         ",\"cell1\":" + cellJson(calib1, calibUpd[0]) +
         ",\"cell2\":" + cellJson(calib2, calibUpd[1]) +
         "}";
}
