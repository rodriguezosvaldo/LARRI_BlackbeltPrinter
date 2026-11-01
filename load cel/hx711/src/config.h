// Per-printer settings (picked by -D PRINTER_NUM in platformio.ini) and the tuning constants
// ported from alarm.py and spool_monitor.py.
#pragma once

#include "secrets.h"  // WIFI_SSID, WIFI_PASS, NTFY1, NTFY2, optional LOG_SERVER_URL / PEER_URL

#ifndef PRINTER_NUM
#error "Build with -D PRINTER_NUM=1 or 2 (pio run -e p1 / -e p2)"
#endif

#if !defined(NTFY1) || !defined(NTFY2)
#error "secrets.h must define NTFY1 and NTFY2 (see secrets.example.h)"
#endif

#ifndef LOG_SERVER_URL
#define LOG_SERVER_URL ""  // no PC: logs are not saved
#endif

#if PRINTER_NUM == 1
// --- P1-243: ESP32-S3-WROOM-1 ---
// Avoid GPIO 19/20 (native USB), 26-37 (flash/PSRAM) and 0/3/45/46 (strapping pins).
const int HX1_DOUT = 17;
const int HX1_SCK  = 18;
const int HX2_DOUT = 15;
const int HX2_SCK  = 16;
#define PRINTER_KEY   "p1"
#define PRINTER_NAME  "Printer 1"
#define PRINTER_LABEL "Printer 1 (243)"
#define PRINTER_SLUG  "192.168.1.243"
#define NTFY_URL      NTFY1
#define HOSTNAME      "bb-p1"
#ifndef PEER_URL
#define PEER_URL      "http://192.168.1.226"  // P2's ESP32
#endif
// Defaults for the /config page (values from spools.json, 2026-10-27)
constexpr long DEF_FULL[2]  = {1778018, 933147};
constexpr long DEF_EMPTY[2] = {930897, 88798};
constexpr long DEF_REF[2]   = {13150, 12900};
constexpr const char* DEF_NOTE[2] = {
    "empty = reel on it since 10-26 (other reel: 932,223)",
    "full ESTIMATED = empty + avg (full - empty) of other cells (844,349); weigh a new spool to confirm"};

#elif PRINTER_NUM == 2
// --- P2-188: classic ESP32 (ESP32-WROOM-32 DevKit) ---
const int HX1_DOUT = 21;
const int HX1_SCK  = 22;
const int HX2_DOUT = 18;
const int HX2_SCK  = 19;
#define PRINTER_KEY   "p2"
#define PRINTER_NAME  "Printer 2"
#define PRINTER_LABEL "Printer 2 (188)"
#define PRINTER_SLUG  "192.168.1.188"
#define NTFY_URL      NTFY2
#define HOSTNAME      "bb-p2"
#ifndef PEER_URL
#define PEER_URL      "http://192.168.1.132"  // P1's ESP32
#endif
constexpr long DEF_FULL[2]  = {1332526, 542885};
constexpr long DEF_EMPTY[2] = {504732, -315248};
constexpr long DEF_REF[2]   = {13150, 13860};
constexpr const char* DEF_NOTE[2] = {
    "",
    "empty from one sample (10-27 08:49), matches -314,500 from 10-23"};

#else
#error "PRINTER_NUM must be 1 or 2"
#endif

#define PRINTER_URL "http://" PRINTER_SLUG

// Louisville, KY (US Eastern with DST)
#define TIMEZONE "EST5EDT,M3.2.0,M11.1.0"

// ============================ Load cells ============================
const unsigned long READ_INTERVAL_MS = 2000;  // HX711 read period

// --- Calibration button ---
// External 4-leg tactile push button between GPIO 4 and GND (internal pull-up, active LOW).
// GPIO 4 is free and not a strapping/USB/flash pin on both the S3 (P1) and classic ESP32 (P2).
// Press = start a calibration, press again while running = cancel.
const int CALIB_BUTTON_PIN = 4;
const unsigned long BUTTON_DEBOUNCE_MS = 50;
const int CALIB_WINDOW = 100;  // readings per cell, same WINDOW as calibration.py (~200 s at 2 s/reading)

// ============================ Spool monitor ============================
const long RATE_WINDOW = 7200;        // s - live rate = slope over the last 2 hours
const long MIN_RATE_SPAN = 600;       // s - need at least 10 min of data before showing a live rate
const long CURRENT_WINDOW = 60;       // s - fallback "current" = mean of the last minute
// While consuming, "current" = the fitted line's value now. If it differs from the last-minute
// mean by more than this, the window doesn't follow one straight line (print start/stop, pause,
// tension step) and the last-minute mean is used.
const double FIT_MAX_DEVIATION = 5000;   // counts
const double MIN_CONSUMING_RATE = 1000;  // counts/h - below this the spool is idle (~2 m/h)
const double JUMP_THRESHOLD = 100000;    // counts - jump vs. recent median = spool changed/removed
const int JUMP_CONFIRM = 3;              // consecutive jumped readings before resetting the history
// While printing, filament tension lowers the reading by ~10k-20k counts. full/empty are
// measured at rest, so while consuming this is added back before computing % and time left.
const long TENSION_OFFSET = 15000;       // counts

// ============================ Printer monitor (alarm.py) ============================
const unsigned long REQUEST_REPLY_MS = 500;    // seqs.reply poll (firmware keeps a reply ~1 s)
const unsigned long REQUEST_STATE_MS = 10000;  // layer / state / job log / filament checks
const double FILAMENT_LOW_HOURS = 2.0;    // alert when a spool has less than this left
const double FILAMENT_CLEAR_HOURS = 2.5;  // re-arm the alert above this (new spool)
const double FILAMENT_DIAMETER = 1.75;    // mm - for volumetric rate in the job log
