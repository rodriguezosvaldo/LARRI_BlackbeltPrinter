// Web server on port 80:
//   GET  /                 dashboard (both printers: this ESP32 + the peer's /api/spools)
//   GET  /api/spools       this printer's spools + printer state (JSON, CORS open)
//   GET  /api/config       per-cell full/empty/ref_rate/note + peer URL
//   POST /api/config       save them (flash)
//   GET  /config           page to edit them
//   GET  /calib            calibration page
//   GET  /data, /calibration, POST /calibration/start|cancel   (used by calibration.py)
//   GET  /update           firmware update page
//   POST /update firmware .bin (multipart, Basic auth ota:OTA_PASS; used by ota_upload.py)
#pragma once

void webBegin();
void webLoop();
