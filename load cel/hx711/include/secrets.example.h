// Copy to secrets.h (gitignored) and fill in. Never commit secrets.h.
#pragma once

#define WIFI_SSID "your-wifi"
#define WIFI_PASS "your-password"

// ntfy topics, one per printer (same values as NTFY1/NTFY2 in the repo's .env)
#define NTFY1 "https://ntfy.sh/your-topic-1"
#define NTFY2 "https://ntfy.sh/your-topic-2"

// PC running log_server.py (job logs, replies, load-cell CSVs). Leave commented out to
// disable: alarms and the dashboard work without it.
// #define LOG_SERVER_URL "http://192.168.1.XX:5052"

// Password for firmware updates over WiFi (/update page, pio run -e p1_ota -t upload).
// Leave commented out to disable them (USB only).
// #define OTA_PASS "choose-a-password"

// Optional: the other printer's ESP32, for the dashboard (default in config.h).
// Can also be changed at runtime from the /config page.
// #define PEER_URL "http://192.168.1.187"
