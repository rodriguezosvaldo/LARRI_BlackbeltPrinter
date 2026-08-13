# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Monitoring and configuration for two BlackBelt 3D printers (Duet 3 MB 6HC mainboard + 3HC
expansion board, RepRapFirmware) at `192.168.1.243` and `192.168.1.188`. It has three mostly
independent parts:

1. **`alarm.py`** — the live Python service: polls both printers' HTTP object-model APIs,
   sends push notifications via [ntfy](https://ntfy.sh), and serves a small status web UI.
2. **`dht22.cpp`** (and older `dht11.cpp`) — standalone ESP32 Arduino sketches for a
   temperature/humidity + heater-control web UI, unrelated to `alarm.py` except that it
   monitors the same print environment.
3. **`config.g/` and `macros/`** — RepRapFirmware config and G-code macro files that live on
   each printer's SD card, kept here for reference/version control. `GCODE_CHEATSHEET.md`
   documents the G/M-codes used in them.

## Running

```bash
pip install -r requirements.txt
python alarm.py                 # or run_alarm.bat on Windows (uses .venv)
```

Requires a `.env` in the repo root (see comment block at the top of `alarm.py`):

```
PRINTER1_IP=http://192.168.1.243
PRINTER2_IP=http://192.168.1.188
NTFY1=https://ntfy.sh/<topic1>
NTFY2=https://ntfy.sh/<topic2>
```

The web UI (status dashboard, `static/index.html`) is served at `http://localhost:5050` once
`alarm.py` is running. There is no test suite, linter, or build step in this repo.

## `alarm.py` architecture

Two daemon-ish threads run forever in `main()`, plus a Flask server thread:

- **`loop_check_seqs_reply`** (every `REQUEST_REPLY` = 0.5s): polls `seqs.reply` on the Duet
  object model to detect *new* M118/error/warning messages (`check_reply`). Polled fast
  because the firmware only exposes the latest reply for ~1s.
- **`loop_check_extrusion_and_state`** (every `REQUEST_TEN_SECONDS` = 10s): checks filament
  remaining (`check_raw_extrusion`), current layer (`check_layer`), and print state
  (`check_state_status`), for both printers in sequence.
- **Flask app** (`_run_web_server`, port 5050): exposes `GET /api/status` (dumps `ui_state`)
  and `POST /api/reset-reference/<1|2>` (zeroes the filament-used counter), backing
  `static/index.html`'s live dashboard.

Key patterns to preserve when editing:

- **Edge-detection via `prev_by_printer`**: each check function compares the freshly polled
  value against a per-printer `_prev()` dict and only notifies on a *transition* (e.g.
  `processing -> paused`), not on every poll. Update `prev[...]` even when not notifying, or
  edge detection breaks.
- **`stop_notifications()` de-dupe**: for problems that recur every poll (filament low, new
  M118 message), don't call `notify()` directly — route through `stop_notifications(printer_ip,
  problem_key, ...)`. It sends up to `MAX_PROBLEM_NOTIFICATIONS` (2) alerts, then one final
  "last notification" message, then goes silent until `clear_problem_notification()` is called
  (when the condition clears).
- **`ui_state`/`ui_lock`**: the web UI reads a shared dict guarded by `ui_lock`. Any new
  per-printer value that should show up in the dashboard needs to flow through `_update_ui()`
  under the lock, mirroring `raw_extrusion`/`layer`/`state_status`.
- **Duet object model access**: all printer reads go through `GET {printer_ip}/rr_model?key=...`
  (see `get_raw_extrusion`, `get_state_status`, `get_layer`, `get_seqs_reply`). See
  `GCODE_CHEATSHEET.md` and the [Object Model docs](https://github.com/Duet3D/RepRapFirmware/wiki/Object-Model-Documentation)
  for available keys, and `PRINTING_STATES`/`STOPPED_STATES` in `alarm.py` for the state
  vocabulary.
- Notification failures (`notify()`, `get_*` HTTP calls) are caught and logged to stdout, never
  raised — a printer being briefly unreachable must not kill the polling loops.

`legacy_alarm.py` and `legacy_deleted_alarm_functions.py` are a superset of `alarm.py`'s
checks (stall detection, MCU temp, VIN, heater/filament-sensor faults) kept for reference —
they are not run. When porting a check back in, follow `alarm.py`'s current conventions
(`_update_ui`, `stop_notifications`), not the legacy file's.

`encoder_motor_belt.py` is an unimplemented stub for a planned belt-motor encoder feature (see
its docstring for the intended io4.in/io5.in wiring).

## `dht22.cpp` (ESP32 sketch)

Single-file Arduino sketch: reads two DHT22 sensors, drives a relay-controlled heater with
software PWM (period/pulse in ms), and serves its own embedded-HTML dashboard + JSON API
(`/data`, `/set`, `/mode`, `/manual`, `/pwm`) on port 80. `dht11.cpp` is an older variant for
the DHT11 sensor — prefer editing `dht22.cpp`; the two are not kept in sync automatically.
Control logic: `updateControl()` decides the logical `heating` state (auto bang-bang on
`controlTemp()` = sensor 2 by default, or manual), `relayStatus()`/`pwmUpdate()` chop that
into the physical PWM signal on `HEATER_PIN`.

## `config.g/` and `macros/`

Per-printer RepRapFirmware startup config (`config.g/<ip>_config.g`, with `originals/` backups)
and macros (`macros/macros_<ip>/` for jogging/homing, `macros/load/<printer-num>/` for
filament load/unload and pause/resume G-code run on the Duet board itself). These are
firmware-side files edited for the physical printers, not code invoked by `alarm.py`. Printer
"188"/"243" numbering in macro folder names refers to the last IP octet.
