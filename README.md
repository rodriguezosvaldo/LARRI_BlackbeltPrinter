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
| [`alarm.py`](alarm.py) + [`static/`](static/) | Polls both printers, sends push notifications, serves a status dashboard | PC on the printer network (Python) |
| [`dht22.cpp`](dht22.cpp) | Temperature/humidity monitoring and heater control for the print enclosure, with its own web UI | ESP32 |
| [`load cel/`](load%20cel/) | Weighs the filament spools with load cells and alerts when a spool is running low | ESP32 + 2× HX711 |
| [`config.g/`](config.g/), [`macros/`](macros/) | RepRapFirmware config and macros stored on each printer's SD card (reference copies) | Duet 3 board |

```
BB_printer/
├── alarm.py                 # printer monitor + ntfy alerts + web dashboard
├── static/index.html        # dashboard served by alarm.py
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
- the printer emits a **message, warning or error** (e.g. `M118` from a macro),
- the **remaining filament** on the spool (estimated from total extrusion) drops below 20 m.

Repeating alerts are capped (2 notifications + a final "last notification") so your phone
isn't spammed while a problem persists.

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

or double-click `run_alarm.bat` on Windows. Then open **http://localhost:5050** for the
dashboard (live state, current layer and filament used for each printer).

### Spool change

The filament estimate is based on the total extruded length since the last reset, assuming a
new 4 kg spool (`TOTAL_FILAMENT_LENGTH` in `alarm.py`). After loading a new spool, press the
**Reset reference** button for that printer in the dashboard (or `POST /api/reset-reference/<1|2>`).

### API

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/status` | JSON with the current state of both printers |
| `POST` | `/api/reset-reference/<1\|2>` | Reset the filament-used counter for a printer |

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

An ESP32 reads two load cells (one under each printer's spool) through HX711 amplifiers and
sends an ntfy alert when a spool's weight drops below a threshold.

Board: ESP32-S3-WROOM-1 (PlatformIO `env:esp32-s3`). The old classic-ESP32 env and pins are
kept commented out in `platformio.ini` and `main.cpp`.

| Cell | HX711 DOUT | HX711 SCK |
|------|-----------|-----------|
| 1    | GPIO 17   | GPIO 18   |
| 2    | GPIO 15   | GPIO 16   |

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
HX711 counts, computed from each cell's counts per meter of filament (cell 1 ~530, cell 2 ~636,
fitted against a print's extrusion CSV) and a reference reading (cell 1: full spool; cell 2:
empty reel). A 4 kg spool is ~1350 m. To pick them, put a nearly-empty spool on each cell and run:

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
