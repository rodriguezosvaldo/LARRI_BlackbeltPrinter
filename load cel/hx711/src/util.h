#pragma once
#include <Arduino.h>
#include <time.h>

// True once NTP has set the clock (needed for job logs, CSV rows and the live rate)
inline bool timeValid() { return time(nullptr) > 1700000000; }

// e.g. "10/11/2026 11:44 am" (alarm.py _fmt_dt); sep = "\n" for notification timestamps
String fmtDt(time_t t, const char* sep = " ");

// e.g. "1h 05m 03s" or "5m 03s" (alarm.py _fmt_duration)
String fmtDuration(double seconds);

// strftime on local time
String fmtTime(time_t t, const char* fmt);
