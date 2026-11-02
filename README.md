# BB_printer

Monitoring, alerting and configuration for the lab's two **BlackBelt 3D printers**
(Duet 3 MB 6HC + 3HC expansion board, RepRapFirmware 3).

| Printer | IP              | Its ESP32 (load cells + monitor) | ntfy topic |
|---------|-----------------|----------------------------------|------------|
| 1       | `192.168.1.243` | ESP32-S3, `192.168.1.132` (`bb-p1.local`) | `NTFY1` |
| 2       | `192.168.1.188` | classic ESP32, `192.168.1.226` (`bb-p2.local`) | `NTFY2` |

**Branch `all_in_esp32`:** each printer's ESP32 does all the monitoring by itself (alarms,
spool monitor, dashboard). No PC is needed for alerts; a PC on the same network only stores
the logs (`log_server.py`). The previous PC version (`alarm.py` + `load cel/spool_monitor.py`)
is still in the repo for reference but is not run.

| Part | What it does | Runs on |
|------|--------------|---------|
| [`load cel/hx711/`](load%20cel/hx711/) | Printer monitor + ntfy alerts + job logs, load cells, time left per spool, web dashboard | One ESP32 per printer |
| [`log_server.py`](log_server.py) | Receives the ESP32s' job logs, replies and load-cell CSVs into `logs/` | PC (optional) |
| [`dht22.cpp`](dht22.cpp) | Temperature/humidity monitoring and heater control for the print enclosure, with its own web UI | ESP32 |
| [`config.g/`](config.g/), [`macros/`](macros/) | RepRapFirmware config and macros stored on each printer's SD card (reference copies) | Duet 3 board |

```
BB_printer/
├── log_server.py            # PC log receiver for the ESP32s (stdlib only)
├── run_log_server.bat       # Windows launcher
├── alarm.py                 # previous PC printer monitor (reference, not run)
├── legacy_alarm.py          # older, larger version of alarm.py (reference only)
├── dht22.cpp                # ESP32 enclosure temperature/humidity + heater controller
├── load cel/
│   ├── hx711/               # PlatformIO project: the ESP32 firmware (monitor + load cells)
│   ├── calibration.py       # serial/WiFi reader to calibrate load-cell thresholds
│   ├── spool_monitor.py     # previous PC spool monitor (reference, not run)
│   └── load_cell.png        # wiring/reference image
├── config.g/                # config.g for each printer
├── macros/                  # homing/jog macros and filament load/unload/pause/resume
└── GCODE_CHEATSHEET.md      # G/M-codes used in the config and macros
```

---

## 1. Printer monitor on the ESP32 (`load cel/hx711/`)

Each ESP32 watches **its own printer** (P1's ESP32 → `.243`, P2's → `.188`) through the Duet
HTTP object model (`/rr_model`) and its two load cells, and sends phone notifications through
[ntfy](https://ntfy.sh) when:

- the printer **pauses, resumes, is cancelled/stops or halts** (state transitions),
- the printer emits a **message, warning or error** (e.g. `M118` from a macro),
- a spool that is being consumed has **less than 2 h of filament left** (once per spool,
  re-armed above 2.5 h, i.e. a new spool).

| Printer | Board (PlatformIO env) | Cell 1 DOUT / SCK | Cell 2 DOUT / SCK |
|---------|------------------------|-------------------|-------------------|
| P1 (.243) | ESP32-S3-WROOM-1 (`p1`) | GPIO 17 / 18 | GPIO 15 / 16 |
| P2 (.188) | classic ESP32 (`p2`) | GPIO 21 / 22 | GPIO 18 / 19 |

Calibration push button: GPIO 4 to GND on both boards.

### Setup

1. Copy `load cel/hx711/include/secrets.example.h` to `secrets.h` (gitignored) and fill in WiFi,
   both ntfy topics and, if you want logs on a PC, `LOG_SERVER_URL` (`http://<pc-ip>:5052`).
2. Build and upload with PlatformIO (VS Code extension or CLI), one environment per printer:

   ```bash
   cd "load cel/hx711"
   pio run -e p1 -t upload -t monitor     # P1 ESP32
   pio run -e p2 -t upload -t monitor     # P2 ESP32
   ```

3. Subscribe to both topics in the ntfy app on your phone.

The serial monitor (115200 baud) prints the IP, every 10 s the printer state, the alerts sent
and the job-log lines. The clock comes from NTP (US Eastern time), so the network needs
internet access (also needed for ntfy).

### Web pages (port 80 of either ESP32)

| URL | What |
|-----|------|
| `/` | Dashboard: both printers (state, job, layer) and, per spool, run-out time, time left, %, readings and rates. The page also asks the other ESP32 for its data. |
| `/config` | Per-cell `full` / `empty` / `ref_rate` / note (what `spools.json` used to hold) and the other ESP32's URL. Saved in the ESP32's flash. |
| `/calib` | Calibration: 100 readings per cell -> mean / std dev (also with the push button). A mean within 100k counts of the saved empty reel / full spool replaces it (saved to flash). |
| `/api/spools`, `/api/config`, `/data`, `/calibration` | JSON APIs |

**Time left** = `(reading − empty) / rate`. The live rate is the least-squares slope of the last
2 h of readings (shown after 10 min of data); while consuming, 15,000 counts are added to the
reading to undo the filament tension. A jump of more than 100,000 counts = spool changed, the
history restarts.

### Logs on the PC (`log_server.py`)

```bash
python log_server.py        # or run_log_server.bat; port 5052, standard library only
```

The ESP32s send to it (same files as before, in `logs/`):

- one log per print: `p1_243_<date>_<job>.log` with START, every PAUSE/RESUME, FILAMENT
  CONSUMED and END,
- printer messages: `replies_<printer-ip>.jsonl`,
- load-cell readings every 2 s: `load_cells/<p1|p2>_<date>.csv` (sent once a minute).

If the PC is off, alerts and the dashboard keep working; the ESP32 keeps the logs in RAM and
sends them later (load-cell rows: up to 2 h on P1, 1 h on P2). At boot the ESP32 reloads the
last 2 h of readings from the PC so the live rate isn't lost. Give the PC a fixed IP (DHCP
reservation) and allow Python through the Windows firewall on private networks.

---

## 2. Enclosure climate controller (`dht22.cpp`)

Arduino sketch for an ESP32 with two DHT22 sensors and a relay-driven heater.

- Reads temperature and humidity from both sensors every 2 s and keeps a history for charts.
- **Auto mode**: bang-bang control with hysteresis around a target temperature (using sensor 2).
- **Manual mode**: force the heater on/off.
- Software **PWM** on the heater relay (configurable period/on-time in ms).
- Built-in web dashboard on port 80, plus a JSON API: `/data`, `/set`, `/mode`, `/manual`, `/pwm`.

Default pins: DHT22 on GPIO 17 and 22, heater relay on GPIO 26. Copy
`dht22_secrets.example.h` to `dht22_secrets.h` (gitignored) and fill in your WiFi SSID/password
(keep it in the same folder as the sketch), then flash it with the Arduino IDE (ESP32 board package + *DHT sensor
library*), and open the IP printed on the serial monitor.

---

## 3. Load-cell calibration (`load cel/`)

Cells are named `p1_cell1`, `p1_cell2` (printer 1, .243) and `p2_cell1`, `p2_cell2` (printer 2,
.188); each printer has its own ESP32, whose `cell1`/`cell2` are that printer's cells. Values
are raw HX711 counts (no tare); the reading goes up with weight. A 4 kg spool is ~1565 m
(measured on `p1_cell1` and `p2_cell1`, see `load cel/calibration_results.txt`).

| Cell       | counts/m | Empty reel | Source |
|------------|----------|------------|--------|
| `p1_cell1` | ~538     | 932,225    | slope from print drop (13,100 counts/h); empty reel 2026-10-24 |
| `p1_cell2` | pending  | 88,770     | empty reel 2026-10-23/24; slope = (full spool − 88,770) / 1565 |
| `p2_cell1` | ~530     | pending    | fitted against a P2 print's extrusion CSV (P2 classic ESP32) |
| `p2_cell2` | ~636     | −314,500   | fitted against a P2 print CSV; full spool +542,900 (P2 classic ESP32) |

To measure a reference, put the spool (or empty reel) on the cell and either press the
calibration button / open `/calib` on the ESP32, or run `calibration.py` (below). The ESP32
calibration (~200 s, spool at rest, not printing) updates the cell's `empty` or `full` by itself
when the mean is within 100k counts of the saved value, and keeps it in flash (NVS) across
reboots and reflashes; `config.h` only holds the starting values. A value further off (e.g. the
estimated `full` of `p1_cell2`) has to be set once at `/config`.

```bash
python "load cel/calibration.py"                  # both printers (P1 and P2) over WiFi
python "load cel/calibration.py" p1               # only P1 over WiFi
python "load cel/calibration.py" p1=COM3 p2       # P1 over serial (your ESP32's port), P2 over WiFi
python "load cel/calibration.py" p2=192.168.1.50  # P2 from another IP (printed on serial at boot)
```

Over WiFi it polls `http://<esp32-ip>/data` (`{"seq":..,"ok":true,"cell1":..,"cell2":..}`). It
prints the mean and standard deviation of every 100 readings per cell. Then enter the new
`full` / `empty` / `ref_rate` on the ESP32's `/config` page.

---

## 4. Printer firmware files (`config.g/`, `macros/`)

Reference copies of what lives on each printer's SD card — edit here, then upload through
Duet Web Control.

- `config.g/<ip>_config.g` — machine configuration (drives, steps/mm, speeds, currents,
  heaters, sensors) for each printer.
- `macros/macros_<ip>/` — homing (`homeall.g`, `homex.g`, …) and jog macros (`x+10.g`, …).
- `macros/load/<188|243>/` — filament load/unload and `pause.g` / `resume.g` for each printer.

See [`GCODE_CHEATSHEET.md`](GCODE_CHEATSHEET.md) for an explanation of the G/M-codes used.

---

## References

- [Duet3D RepRapFirmware Object Model](https://github.com/Duet3D/RepRapFirmware/wiki/Object-Model-Documentation)
- [RepRapFirmware G-code reference](https://docs.duet3d.com/User_manual/Reference/Gcodes)
- [ntfy documentation](https://docs.ntfy.sh)
- [bogde/HX711 library](https://github.com/bogde/HX711)
