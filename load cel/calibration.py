import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

P2_IP = "192.168.1.226"  # ESP32 IP address for P2 (default WiFi source)

# Usage:
#   python calibration.py                     -> read over WiFi from P2_IP (GET /data)
#   python calibration.py <COM_PORT>          -> read over serial, e.g. COM3
#   python calibration.py http://<ESP32_IP>   -> read over WiFi from another ESP32
#   python calibration.py <ESP32_IP>          -> same, http:// is added
SOURCE = sys.argv[1] if len(sys.argv) > 1 else P2_IP
BAUD = 115200
WINDOW = 100
POLL_SECONDS = 1  # main.cpp reads every 2s; polling faster only skips duplicate seqs
LOG_DIR = Path(__file__).parent  # calibration_cell<N>.log: one line per WINDOW of readings

# Matches main.cpp output: "HX711 #1 reading: 123456"
pattern = re.compile(r"HX711 #(\d+) reading:\s*(-?\d+)")


def is_wifi(source):
    return source.startswith("http") or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", source) is not None


def serial_readings(port):
    """Yield (cell, value) parsed from the ESP32 serial output."""
    import serial

    with serial.Serial(port, BAUD, timeout=2) as ser:
        print(f"Reading {port} @ {BAUD}... (Ctrl+C to exit)")
        while True:
            line = ser.readline().decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            match = pattern.search(line)
            if match:
                yield int(match.group(1)), int(match.group(2))
            else:
                print(f"[msg] {line}")


def wifi_readings(source):
    """Yield (cell, value) polled from the ESP32's GET /data endpoint."""
    import requests

    url = source if source.startswith("http") else f"http://{source}"
    url = url.rstrip("/")
    if not url.endswith("/data"):
        url += "/data"
    print(f"Polling {url}... (Ctrl+C to exit)")
    last_seq = None
    while True:
        try:
            data = requests.get(url, timeout=3).json()
        except Exception as e:
            print(f"[error] {e}")
            time.sleep(POLL_SECONDS)
            continue
        if not data.get("ok"):
            print("[msg] HX711 not found.")
        elif data["seq"] != last_seq:
            last_seq = data["seq"]
            yield 1, data["cell1"]
            yield 2, data["cell2"]
        time.sleep(POLL_SECONDS)


readings = wifi_readings(SOURCE) if is_wifi(SOURCE) else serial_readings(SOURCE)

try:
    values = {1: [], 2: []}
    for cell, value in readings:
        print(f"cell {cell}: {value}")
        cell_values = values.setdefault(cell, [])
        cell_values.append(value)
        if len(cell_values) == WINDOW:
            mean = statistics.mean(cell_values)
            std_dev = statistics.stdev(cell_values)
            print(f"Cell {cell} last {WINDOW} readings -> mean: {mean:.2f}, std dev: {std_dev:.2f}")
            now = datetime.now()
            with (LOG_DIR / f"calibration_cell{cell}.log").open("a", encoding="utf-8") as log:
                log.write(f"{now:%Y-%m-%d} | {now:%H:%M:%S} | mean={mean:.2f} | std_dev={std_dev:.2f}\n")
            cell_values.clear()
except KeyboardInterrupt:
    pass
