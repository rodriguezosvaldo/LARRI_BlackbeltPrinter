# BB_printer

Monitoring, alerting and configuration for the lab's two **BlackBelt 3D printers**
(Duet 3 MB 6HC + 3HC expansion board, RepRapFirmware 3).

| Printer | IP              | ntfy topic env var |
|---------|-----------------|--------------------|
| 1       | `192.168.1.243` | `NTFY1`            |
| 2       | `192.168.1.188` | `NTFY2`            |

The project has four independent pieces:

| Part | What it does | Runs on |
|------|--------------|---------|
| [`alarm.py`](alarm.py) | Polls both printers, sends push notifications, writes per-print logs | PC on the printer network (Python) |
| [`dht22.cpp`](dht22.cpp) | Temperature/humidity monitoring and heater control for the print enclosure, with its own web UI | ESP32 |
| [`load cel/`](load%20cel/) | Weighs the filament spools with load cells and alerts when a spool is running low | ESP32 + 2× HX711 |
| [`config.g/`](config.g/), [`macros/`](macros/) | RepRapFirmware config and macros stored on each printer's SD card (reference copies) | Duet 3 board |

```
BB_printer/
├── alarm.py                 # printer monitor + ntfy alerts + job logs
├── run_alarm.bat            # Windows launcher (uses .venv)
├── requirements.txt
├── legacy_alarm.py          # older, larger version of alarm.py (reference only, not run)
├── dht22.cpp                # ESP32 enclosure temperature/humidity + heater controller
├── load cel/
│   ├── calibration.py       # serial/WiFi reader to calibrate load-cell thresholds
│   ├── load_cell.png        # wiring/reference image
│   └── hx711/               # PlatformIO project for the ESP32 + HX711 firmware
├── config.g/                # config.g for each printer
├── macros/                  # homing/jog macros and filament load/unload/pause/resume
└── GCODE_CHEATSHEET.md      # G/M-codes used in the config and macros
```

---

## 1. Printer monitor (`alarm.py`)

Polls each printer's Duet HTTP object model (`/rr_model`) and sends phone notifications
through [ntfy](https://ntfy.sh) when:

- a print **starts, finishes, pauses, is cancelled or halts** (state transitions),
- the printer emits a **message, warning or error** (e.g. `M118` from a macro).

Filament-low alerts are not handled here; they come from the load cells (`load cel/`).

Repeating alerts are sent once per problem so your phone isn't spammed while it persists.

### Setup

1. Install Python 3.10+ and the dependencies:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate        # Windows  (source .venv/bin/activate on Linux/macOS)
   pip install -r requirements.txt
   ```

2. Create a `.env` file in the repo root:

   ```
   PRINTER1_IP=http://192.168.1.243
   PRINTER2_IP=http://192.168.1.188
   NTFY1=https://ntfy.sh/<your-topic-for-printer-1>
   NTFY2=https://ntfy.sh/<your-topic-for-printer-2>
   ```

3. Subscribe to both topics in the ntfy app on your phone.

### Run

```bash
python alarm.py
```

or double-click `run_alarm.bat` on Windows (it also starts the spool monitor on
**http://localhost:5051**). Per-print logs and printer replies are written to `logs/`.

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

## 3. Load-cell filament monitor (`load cel/`)

Each printer has its own set: one ESP32, two HX711 amplifiers and two load cells (one under
each of that printer's two spools). The ESP32 sends an ntfy alert when a spool's weight drops
below a threshold.

| Printer | Board (PlatformIO env) | Cell 1 DOUT / SCK | Cell 2 DOUT / SCK |
|---------|------------------------|-------------------|-------------------|
| P1 (.243) | ESP32-S3-WROOM-1 (`esp32-s3`, active) | GPIO 17 / 18 | GPIO 15 / 16 |
| P2 (.188) | classic ESP32 (`esp32dev`, commented out) | GPIO 21 / 22 | GPIO 18 / 19 |

`platformio.ini` and `main.cpp` are currently set up for P1. To flash P2, switch to the
commented `esp32dev` env and P2 pins.

### Firmware (`load cel/hx711/`, PlatformIO)

1. Create `load cel/hx711/include/secrets.h` (gitignored):

   ```cpp
   #define WIFI_SSID   "<wifi-name>"
   #define WIFI_PASS   "<wifi-password>"
   #define P1_NTFY_URL "https://ntfy.sh/<topic-printer-1>"
   #define P2_NTFY_URL "https://ntfy.sh/<topic-printer-2>"
   ```

2. Build and upload with PlatformIO (VS Code extension or CLI):

   ```bash
   cd "load cel/hx711"
   pio run -t upload
   pio device monitor        # 115200 baud
   ```

The firmware reads both cells every 2 s, prints `HX711 #<n> reading: <value>` over serial,
and sends an alert after 3 consecutive low readings. Each cell has two levels: ~2 h left (~50 m,
high priority) and ~1 h left (~25 m, urgent). Each level alerts once and won't alert again
until the reading goes back above its threshold (i.e. a new spool is loaded).

### Calibration

Thresholds (`CELL{1,2}_LOW_THRESHOLD_2HOURSLEFT` / `_1HOURLEFT` in `src/main.cpp`) are raw
HX711 counts: `threshold = empty_reel + counts_per_m × meters`, so meters left =
`(reading − empty_reel) / counts_per_m`. A 4 kg spool is ~1565 m (measured on `p1_cell1` and
`p2_cell1`, see `load cel/calibration_results.txt`).

Cells are named `p1_cell1`, `p1_cell2` (printer 1, .243) and `p2_cell1`, `p2_cell2` (printer 2,
.188); each printer has its own ESP32, whose `cell1`/`cell2` are that printer's cells.

| Cell       | counts/m | Empty reel | Source |
|------------|----------|------------|--------|
| `p1_cell1` | ~538     | 932,225    | slope from print drop (13,100 counts/h); empty reel 2026-10-24 |
| `p1_cell2` | pending  | 88,770     | empty reel 2026-10-23/24; slope = (full spool − 88,770) / 1565 |
| `p2_cell1` | ~530     | pending    | fitted against a P2 print's extrusion CSV (P2 classic ESP32) |
| `p2_cell2` | ~636     | −314,500   | fitted against a P2 print CSV; full spool +542,900 (P2 classic ESP32) |

To measure a reference, put the spool (or empty reel) on the cell and run:

```bash
python "load cel/calibration.py"                  # both printers (P1 and P2) over WiFi
python "load cel/calibration.py" p1               # only P1 over WiFi
python "load cel/calibration.py" p1=COM3 p2       # P1 over serial (your ESP32's port), P2 over WiFi
python "load cel/calibration.py" p2=192.168.1.50  # P2 from another IP (printed on serial at boot)
```

Over WiFi it polls `http://<esp32-ip>/data`, which returns the latest readings as JSON
(`{"seq":..,"ok":true,"cell1":..,"cell2":..,"threshold1":..,"threshold1_1h":..,"threshold2":..,"threshold2_1h":..}`). The PC must be
on the same network as the ESP32.

It prints the mean and standard deviation of every 100 readings per cell.

### Spool monitor (time left per spool)

```bash
python "load cel/spool_monitor.py"     # web page at http://localhost:5051
```

Polls both ESP32s (`/data`) and shows P1 and P2 side by side. For each cell: current reading,
full spool, empty reel and reference rate (from `load cel/spools.json`, which is reloaded when
edited), the **live rate** (least-squares slope of the last hour of readings, counts/h) and the
**time left** = `(reading − empty) / rate`, with the estimated run-out time. The live rate
resets itself when a spool is changed (jump of more than 100,000 counts). Readings are logged to
`logs/load_cells/<p1|p2>_<date>.csv` and the last hour is reloaded on restart.

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
