# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Monitoring and configuration for two BlackBelt 3D printers (Duet 3 MB 6HC mainboard + 3HC
expansion board, RepRapFirmware) at `192.168.1.243` (printer 1) and `192.168.1.188`
(printer 2). It has four mostly independent parts:

1. **`alarm.py`** — the live Python service: polls both printers' HTTP object-model APIs,
   sends push notifications via [ntfy](https://ntfy.sh), and serves a small status web UI.
2. **`dht22.cpp`** — standalone ESP32 Arduino sketch for a temperature/humidity +
   heater-control web UI for the print environment. Unrelated to `alarm.py` at the code level.
3. **`load cel/`** — ESP32 + two HX711 load cells (PlatformIO project) that weigh the filament
   spools and send their own ntfy "low filament" alerts, plus a serial calibration script.
4. **`config.g/` and `macros/`** — RepRapFirmware config and G-code macro files that live on
   each printer's SD card, kept here for reference/version control. `GCODE_CHEATSHEET.md`
   documents the G/M-codes used in them.

See `README.md` for the user-facing overview and setup steps.

## Running

```bash
pip install -r requirements.txt
python alarm.py                 # or run_alarm.bat on Windows (expects .venv/ in repo root)
```

Requires a `.env` in the repo root (see comment block at the top of `alarm.py`):

```
PRINTER1_IP=http://192.168.1.243
PRINTER2_IP=http://192.168.1.188
NTFY1=https://ntfy.sh/<topic1>
NTFY2=https://ntfy.sh/<topic2>
```

The web UI (status dashboard, `static/index.html`) is served at `http://localhost:5050` once
`alarm.py` is running. Last printer replies are written under `logs/` (gitignored). There is
no test suite, linter, or build step for the Python side. `requirements.txt` also includes
`pyserial`, which is only used by `load cel/calibration.py` (serial mode).

## `alarm.py` architecture

Two daemon-ish threads run forever in `main()`, plus a Flask server thread:

- **`loop_check_seqs_reply`** (every `REQUEST_REPLY` = 0.5s): polls `seqs.reply` on the Duet
  object model to detect *new* M118/error/warning messages (`check_reply`). Polled fast
  because the firmware only exposes the latest reply for ~1s.
- **`loop_check_extrusion_and_state`** (every `REQUEST_TEN_SECONDS` = 10s): checks filament
  remaining (`check_raw_extrusion`, against `TOTAL_FILAMENT_LENGTH` / `FILAMENT_LEFT_ALERT`),
  current layer (`check_layer`), and print state (`check_state_status`), for both printers in
  sequence.
- **Flask app** (`_run_web_server`, port 5050): exposes `GET /api/status` (dumps `ui_state`)
  and `POST /api/reset-reference/<1|2>` (zeroes the filament-used counter after a spool
  change), backing `static/index.html`'s live dashboard.

Key patterns to preserve when editing:

- **Edge-detection via `prev_by_printer`**: each check function compares the freshly polled
  value against a per-printer `_prev()` dict and only notifies on a *transition* (e.g.
  `processing -> paused`), not on every poll. Update `prev[...]` even when not notifying, or
  edge detection breaks.
- **`notify_once()` de-dupe**: for problems that recur every poll (filament low, new M118
  message), don't call `notify()` directly — route through `notify_once(printer_ip,
  problem_key, ...)`. It sends a single alert, then stays silent for that key until
  `clear_problem_notification()` is called (when the condition clears).
- **`ui_state`/`ui_lock`**: the web UI reads a shared dict guarded by `ui_lock`. Any new
  per-printer value that should show up in the dashboard needs to flow through `_update_ui()`
  under the lock, mirroring `raw_extrusion`/`layer`/`state_status`.
- **Duet object model access**: all printer reads go through `GET {printer_ip}/rr_model?key=...`
  (see `get_raw_extrusion`, `get_state_status`, `get_layer`, `get_seqs_reply`). See
  `GCODE_CHEATSHEET.md` and the [Object Model docs](https://github.com/Duet3D/RepRapFirmware/wiki/Object-Model-Documentation)
  for available keys, and `PRINTING_STATES`/`STOPPED_STATES` in `alarm.py` for the state
  vocabulary.
- **Print job logs** (`check_job_log`, called after `check_state_status` in the 10s loop): one
  file per print in `logs/` (`p1_243_<start date>_<job>.log`, `_2`, `_3`... for repeats that day), with START,
  every PAUSE/RESUME (with pause length) and END (finished/cancelled/aborted, total and paused
  time). A job is detected by `job.file.fileName` being non-null; start time is
  `now - job.duration`, and the job ends when `fileName` goes null or changes.
  Each job also gets a sibling `.csv` (`_job_log_sample`, one row per 10s poll: state, raw
  extrusion, cumulative extruded mm, rolling `RATE_WINDOW` mm/min rate while printing). Extruded
  length sums only positive `job.rawExtrusion` deltas (a drop = firmware reset). The END entry
  adds a `FILAMENT` line (total mm, avg mm/min and cm³/h over non-paused time, using
  `FILAMENT_DIAMETER`).
- Notification failures (`notify()`, `get_*` HTTP calls) are caught and logged to stdout, never
  raised — a printer being briefly unreachable must not kill the polling loops.

`legacy_alarm.py` is a superset of `alarm.py`'s checks (stall detection, MCU temp, VIN,
heater/filament-sensor faults) kept for reference — it is not run. When porting a check back
in, follow `alarm.py`'s current conventions (`_update_ui`, `notify_once`), not the
legacy file's.

## `dht22.cpp` (ESP32 sketch)

Single-file Arduino sketch: reads two DHT22 sensors, drives a relay-controlled heater with
software PWM (period/pulse in ms), and serves its own embedded-HTML dashboard + JSON API
(`/data`, `/set`, `/mode`, `/manual`, `/pwm`) on port 80. Control logic: `updateControl()`
decides the logical `heating` state (auto bang-bang with `HYSTERESIS` on `controlTemp()` =
sensor 2 by default, or manual), `relayStatus()`/`pwmUpdate()` chop that into the physical PWM
signal on `HEATER_PIN`. WiFi credentials come from `dht22_secrets.h` (gitignored; template in
`dht22_secrets.example.h`, which must sit next to the sketch). Never hardcode credentials in
tracked files.

## `load cel/` (load-cell filament monitor)

Note the directory name has a space (`load cel`) — quote it in shell commands.

- **`hx711/`** — PlatformIO project (`env:esp32-s3`, board ESP32-S3-WROOM-1; the old
  `esp32dev` env and pins are kept commented out; Arduino framework, `bogde/HX711` lib,
  115200 baud). `src/main.cpp` reads two HX711 amplifiers (cell 1: DOUT 17 / SCK 18; cell 2:
  DOUT 15 / SCK 16 — avoid GPIO 19/20 on the S3, they are native USB) every 2s (non-blocking `millis()` timer, not `delay()`, so the web server
  stays responsive), prints `HX711 #<n> reading: <raw>` over serial, serves the latest readings
  as JSON at `GET http://<esp32-ip>/data` (`seq`, `ok`, `cell1`, `cell2`, `threshold1`/`threshold2` (2 h levels), `threshold1_1h`/`threshold2_1h`; the IP
  is printed over serial once WiFi connects), and POSTs an ntfy alert when a raw reading stays below a threshold for 3 consecutive polls
  (`checkLow()` on a `LowAlert` struct). Each cell has two levels, `CELLn_LOW_THRESHOLD_2HOURSLEFT`
  (~50 m) and `CELLn_LOW_THRESHOLD_1HOURLEFT` (~25 m), derived from counts/m (cell 1 ~530, cell 2
  ~636) and a reference reading (cell 1 full spool, cell 2 empty reel); see the comments in
  `main.cpp`. Each level's `notified` flag prevents repeat
  alerts until the reading goes back above its threshold. Thresholds are raw HX711 counts (no tare/scale factor applied) and must be tuned
  per cell.
- **`hx711/include/secrets.h`** (gitignored) — must define `WIFI_SSID`, `WIFI_PASS`,
  `P1_NTFY_URL`, `P2_NTFY_URL`. Currently `main.cpp` sends both cells' alerts to
  `P1_NTFY_URL`. Never commit or echo this file's contents.
- **`calibration.py`** — `python calibration.py [p1[=SOURCE]] [p2[=SOURCE]]`, SOURCE = COM port
  or ESP32 IP/URL (default: both printers over WiFi, `P1_IP` = `192.168.1.132`, `P2_IP` =
  `192.168.1.226`). One reader thread per printer (serial, or WiFi polling `/data`, skipping repeated `seq`)
  feeds a shared queue. Parses the serial output format above and prints mean / std-dev per printer/cell every 100 readings
  (also appended to `load cel/calibration_<P1|P2>_cell<N>.log`), for picking
  thresholds. If you change the serial print format in `main.cpp`, update the regex here too.
- `hx711/.pio/` is PlatformIO build output/library cache — don't edit.

## `config.g/` and `macros/`

Per-printer RepRapFirmware startup config (`config.g/<ip>_config.g`) and macros
(`macros/macros_<ip>/` for jogging/homing, `macros/load/<printer-num>/` for filament
load/unload and pause/resume G-code run on the Duet board itself). These are firmware-side
files edited for the physical printers and uploaded via the Duet Web Control UI, not code
invoked by `alarm.py`. Printer "188"/"243" numbering in macro folder names refers to the last
IP octet.
