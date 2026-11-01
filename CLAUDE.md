# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Monitoring and configuration for two BlackBelt 3D printers (Duet 3 MB 6HC mainboard + 3HC
expansion board, RepRapFirmware) at `192.168.1.243` (printer 1) and `192.168.1.188`
(printer 2). On branch `all_in_esp32` all monitoring runs on the ESP32s (one per printer);
the PC only stores logs. Parts:

1. **`load cel/hx711/`** — PlatformIO firmware, one build per printer (`pio run -e p1` / `-e p2`):
   printer monitor (port of `alarm.py`: Duet polling, ntfy alerts, job logs), load cells (two
   HX711), spool monitor (port of `spool_monitor.py`: live rate, time left, low-filament alert)
   and the web dashboard.
2. **`log_server.py`** — PC log receiver (stdlib only, port 5052): the ESP32s POST job-log lines,
   printer replies and load-cell rows; written to `logs/` in the old formats.
3. **`dht22.cpp`** — standalone ESP32 Arduino sketch for a temperature/humidity +
   heater-control web UI for the print environment. Unrelated to the rest at the code level.
4. **`config.g/` and `macros/`** — RepRapFirmware config and G-code macro files that live on
   each printer's SD card, kept here for reference/version control. `GCODE_CHEATSHEET.md`
   documents the G/M-codes used in them.
5. **Reference only, not run:** `alarm.py`, `load cel/spool_monitor.py` (+ `.html`,
   `spools.json`), `run_alarm.bat` — the previous PC version; `legacy_alarm.py` — an older,
   larger `alarm.py` (stall detection, MCU temp, VIN, heater/filament-sensor faults). When
   porting a check from them, follow the firmware's conventions (`prev*` edge detection,
   `notifyOnce`).

See `README.md` for the user-facing overview and setup steps.

## Building / running

```bash
cd "load cel/hx711"
pio run -e p1 -t upload -t monitor   # P1: ESP32-S3; -e p2 = P2: classic ESP32
python log_server.py                 # on the PC (or run_log_server.bat)
```

`pio` may not be on PATH: it is at `~/.platformio/penv/Scripts/pio.exe`. Both envs must compile
(P2's flash is ~80% used). There is no test suite or linter.

**`load cel/hx711/include/secrets.h`** (gitignored; template `secrets.example.h`) defines
`WIFI_SSID`, `WIFI_PASS`, `NTFY1`, `NTFY2`, optional `LOG_SERVER_URL` (`http://<pc-ip>:5052`,
empty/undefined = no PC logs) and optional `PEER_URL`. Never commit or echo its contents.
`calibration.py` uses `pyserial` (in `requirements.txt`).

## Firmware architecture (`load cel/hx711/src/`)

`config.h` holds everything per printer, chosen by `-D PRINTER_NUM` (pins, `PRINTER_URL`,
`NTFY_URL`, `PRINTER_KEY` p1/p2, `PEER_URL`, cell defaults from the old `spools.json`) and the
constants ported from the Python (`RATE_WINDOW`, `JUMP_THRESHOLD`, `TENSION_OFFSET`,
`FILAMENT_LOW_HOURS`, ...). Keep both printers' blocks in sync when adding a setting.

Tasks: `loop()` (core 1) runs the web server, button and HX711 reads; on core 0, `reply`
(every `REQUEST_REPLY_MS` = 0.5 s), `state` (every 10 s) and `net` (sends ntfy + PC logs) tasks.
HTTP calls that may be slow (ntfy over TLS, the PC) only happen in `net`, through queues, so a
slow/offline PC or ntfy never delays polling or the web page.

- **`printer.cpp`** (port of `alarm.py`, for this ESP32's printer only): `checkReply`
  (`seqs.reply` change → `/rr_reply` → `notifyOnce("m118:<text>")`), and every 10 s `checkLayer`
  → `checkStateStatus` → `checkJobLog` → `checkFilamentTime`. All Duet reads go through
  `duetModel()` = `GET {PRINTER_URL}/rr_model?key=...` (ArduinoJson, with a filter for `job`).
  See the [Object Model docs](https://github.com/Duet3D/RepRapFirmware/wiki/Object-Model-Documentation)
  and `isPrinting`/`isStopped` for the state vocabulary.
  - **Edge detection**: each check compares the polled value with its `prev*` variable and only
    notifies on a *transition*. Update `prev*` even when not notifying.
  - **`notifyOnce(problem, ...)` / `clearProblem()`**: for conditions that recur every poll.
    Bounded list (`MAX_NOTIFIED`), `m118:` keys evicted first.
  - **Job logs**: a job = `job.file.fileName` non-null; start = now − `job.duration`; ends when
    `fileName` goes null or changes. Lines (START, PAUSE, RESUME, FILAMENT CONSUMED, END) go to
    `pclogJobLine(key, base, line)`; the PC picks the file name (`<base>.log`, `_2`, `_3`...)
    and maps `key` (base + start unix) to it. Extruded length = sum of positive
    `job.rawExtrusion` deltas.
  - `checkFilamentTime` reads the spool module directly: only cells with `consuming`, using
    `hours_left_live`, skipping cells at/below `empty` (+ tension offset); key
    `filament_low:cellN`, re-armed above `FILAMENT_CLEAR_HOURS`.
  - Shared state for the dashboard is in `shared` under `stateLock` (`printerStatusJson`).
- **`spool.cpp`** (port of `spool_monitor.py` `CellHistory` + `cell_view`): per-cell ring buffer
  of (unix, counts) over `RATE_WINDOW`, jump detection (median of last 15), least-squares live
  rate, current = fitted value or last-minute mean (`FIT_MAX_DEVIATION`), `TENSION_OFFSET` added
  while consuming. Per-cell config (`full`/`empty`/`ref_rate`/`note`) + peer URL are stored in
  NVS (`Preferences`, edited at `/config`), defaults in `config.h`. Samples are only added once
  `spoolReady()` (clock set by NTP and the PC history reload attempted).
- **`cells.cpp`**: HX711 reads every 2 s (non-blocking `millis()` timer), serial
  `HX711 #<n> reading: <raw>` (parsed by `calibration.py` — update its regex if this changes),
  on-board calibration (button GPIO 4 or `/calib`, Welford mean/std over `CALIB_WINDOW` = 100,
  `[calib] ...` serial lines). Feeds `spoolAdd` and `pclogCells`.
- **`ntfy.cpp`**: `notify()` queues (adds the timestamp like `alarm.py`), `ntfyProcess()` sends
  over HTTPS (`setInsecure()`); failures are logged, never retried or raised.
- **`pclog.cpp`**: queues for the PC (`/job`, `/reply` events in order; `/cells` rows once a
  minute, max `PCLOG_MAX_ROWS` kept while the PC is down). `pclogLoadHistory()` at boot streams
  `GET /cells/recent` into the spool history.
- **`web.cpp` + `pages.h`**: `/` dashboard (fetches its own `/api/spools` and the peer's, CORS
  open), `/config`, `/calib`, `/api/spools` (same shape as the old `spool_monitor.py` API with
  only this printer, plus `printer`, `self`, `peer`), `/api/config` GET/POST, `/data`,
  `/calibration`, `/calibration/start|cancel`.

Pins: P1 (S3) cell 1 DOUT 17 / SCK 18, cell 2 DOUT 15 / SCK 16 — avoid GPIO 19/20 on the S3
(native USB); P2 (classic) 21/22, 18/19. The ESP32s get their IP by DHCP; `PEER_URL` (or
`/config`) and `calibration.py` (`P1_IP` = `192.168.1.132`, `P2_IP` = `192.168.1.226`) must
match. mDNS names: `bb-p1.local`, `bb-p2.local`. Cells are named `p1_cell1`/`p1_cell2`,
`p2_cell1`/`p2_cell2`. `hx711/.pio/` is build output — don't edit.

## `log_server.py`

`ThreadingHTTPServer` on `LOG_SERVER_PORT` (5052). Writes `logs/<base>.log` (index in
`logs/.job_index.json`), `logs/replies_<slug>.jsonl`, `logs/load_cells/<printer>_<date>.csv`
(header `time,unix,cell1,cell2`, rows not newer than the last written are skipped). Names are
validated with `SAFE_NAME`. `logs/` is gitignored.

## `load cel/calibration.py`

`python calibration.py [p1[=SOURCE]] [p2[=SOURCE]]`, SOURCE = COM port or ESP32 IP/URL (default:
both printers over WiFi). One reader thread per printer (serial, or WiFi polling `/data`,
skipping repeated `seq`) feeds a shared queue; prints mean / std-dev per printer/cell every 100
readings (also appended to `load cel/calibration_<P1|P2>_cell<N>.log`).
`python calibration.py report [DIR]` (also run on Ctrl+C) rebuilds `calibration_results.txt`
from the logs; text below its "Manual notes" marker is kept.

## `dht22.cpp` (ESP32 sketch)

Single-file Arduino sketch: reads two DHT22 sensors, drives a relay-controlled heater with
software PWM (period/pulse in ms), and serves its own embedded-HTML dashboard + JSON API
(`/data`, `/set`, `/mode`, `/manual`, `/pwm`) on port 80. Control logic: `updateControl()`
decides the logical `heating` state (auto bang-bang with `HYSTERESIS` on `controlTemp()` =
sensor 2 by default, or manual), `relayStatus()`/`pwmUpdate()` chop that into the physical PWM
signal on `HEATER_PIN`. WiFi credentials come from `dht22_secrets.h` (gitignored; template in
`dht22_secrets.example.h`, which must sit next to the sketch). Never hardcode credentials in
tracked files.

## `config.g/` and `macros/`

Per-printer RepRapFirmware startup config (`config.g/<ip>_config.g`) and macros
(`macros/macros_<ip>/` for jogging/homing, `macros/load/<printer-num>/` for filament
load/unload and pause/resume G-code run on the Duet board itself). These are firmware-side
files edited for the physical printers and uploaded via the Duet Web Control UI. Printer
"188"/"243" numbering in macro folder names refers to the last IP octet.
