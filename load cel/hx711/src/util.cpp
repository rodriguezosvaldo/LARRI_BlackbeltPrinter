#include "util.h"

String fmtTime(time_t t, const char* fmt) {
  struct tm tm;
  localtime_r(&t, &tm);
  char buf[64];
  strftime(buf, sizeof(buf), fmt, &tm);
  return String(buf);
}

String fmtDt(time_t t, const char* sep) {
  struct tm tm;
  localtime_r(&t, &tm);
  int hour = tm.tm_hour % 12;
  if (hour == 0) hour = 12;
  char buf[48];
  snprintf(buf, sizeof(buf), "%02d/%02d/%04d%s%d:%02d %s", tm.tm_mon + 1, tm.tm_mday,
           tm.tm_year + 1900, sep, hour, tm.tm_min, tm.tm_hour < 12 ? "am" : "pm");
  return String(buf);
}

String fmtDuration(double seconds) {
  long s = (long)seconds;
  if (s < 0) s = 0;
  long h = s / 3600, m = (s % 3600) / 60, sec = s % 60;
  char buf[32];
  if (h) snprintf(buf, sizeof(buf), "%ldh %02ldm %02lds", h, m, sec);
  else snprintf(buf, sizeof(buf), "%ldm %02lds", m, sec);
  return String(buf);
}
