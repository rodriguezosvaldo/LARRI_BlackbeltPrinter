import queue
import re
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

P1_IP = "192.168.1.132"  # ESP32 IP address for P1
P2_IP = "192.168.1.226"  # ESP32 IP address for P2

# Usage (each printer's ESP32 has two cells):
#   python calibration.py                         -> both printers over WiFi (P1_IP and P2_IP)
#   python calibration.py p1                      -> only P1 over WiFi (same for p2)
#   python calibration.py p1=COM3 p2              -> P1 over serial, P2 over WiFi
#   python calibration.py p2=192.168.1.50         -> P2 over WiFi from another IP (http:// optional)
DEFAULT_SOURCES = {"p1": P1_IP, "p2": P2_IP}
BAUD = 115200
WINDOW = 100
POLL_SECONDS = 1  # main.cpp reads every 2s; polling faster only skips duplicate seqs
LOG_DIR = Path(__file__).parent  # calibration_<P1|P2>_cell<N>.log: one line per WINDOW of readings

# Matches main.cpp output: "HX711 #1 reading: 123456"
pattern = re.compile(r"HX711 #(\d+) reading:\s*(-?\d+)")


def parse_sources(args):
    """Return {printer: source} from args like 'p1', 'p2=COM3', 'p1=192.168.1.50'."""
    if not args:
        return dict(DEFAULT_SOURCES)
    sources = {}
    for arg in args:
        name, _, source = arg.partition("=")
        name = name.lower()
        if name not in DEFAULT_SOURCES:
            sys.exit(f"Unknown printer '{name}' (use p1 or p2), e.g. p1=COM3")
        sources[name] = source or DEFAULT_SOURCES[name]
    return sources


def is_wifi(source):
    return source.startswith("http") or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", source) is not None


def serial_readings(printer, port):
    """Yield (cell, value) parsed from the ESP32 serial output."""
    import serial

    with serial.Serial(port, BAUD, timeout=2) as ser:
        print(f"[{printer}] Reading {port} @ {BAUD}...")
        while True:
            line = ser.readline().decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            match = pattern.search(line)
            if match:
                yield int(match.group(1)), int(match.group(2))
            else:
                print(f"[{printer}] [msg] {line}")


def wifi_readings(printer, source):
    """Yield (cell, value) polled from the ESP32's GET /data endpoint."""
    import requests

    url = source if source.startswith("http") else f"http://{source}"
    url = url.rstrip("/")
    if not url.endswith("/data"):
        url += "/data"
    print(f"[{printer}] Polling {url}...")
    last_seq = None
    while True:
        try:
            data = requests.get(url, timeout=3).json()
        except Exception as e:
            print(f"[{printer}] [error] {e}")
            time.sleep(POLL_SECONDS)
            continue
        if not data.get("ok"):
            print(f"[{printer}] [msg] HX711 not found.")
        elif data["seq"] != last_seq:
            last_seq = data["seq"]
            yield 1, data["cell1"]
            yield 2, data["cell2"]
        time.sleep(POLL_SECONDS)


def reader(printer, source, out):
    """Thread body: push (printer, cell, value) from one ESP32 into the shared queue."""
    readings = wifi_readings(printer, source) if is_wifi(source) else serial_readings(printer, source)
    try:
        for cell, value in readings:
            out.put((printer, cell, value))
    except Exception as e:
        print(f"[{printer}] [error] reader stopped: {e}")


def main():
    sources = parse_sources(sys.argv[1:])
    out = queue.Queue()
    for printer, source in sources.items():
        threading.Thread(target=reader, args=(printer, source, out), daemon=True).start()
    print("Ctrl+C to exit")

    values = {}  # (printer, cell) -> readings in the current window
    try:
        while True:
            try:
                printer, cell, value = out.get(timeout=0.5)  # timeout keeps Ctrl+C responsive
            except queue.Empty:
                continue
            print(f"{printer} cell {cell}: {value}")
            cell_values = values.setdefault((printer, cell), [])
            cell_values.append(value)
            if len(cell_values) == WINDOW:
                mean = statistics.mean(cell_values)
                std_dev = statistics.stdev(cell_values)
                print(f"{printer.upper()} cell {cell} last {WINDOW} readings -> mean: {mean:.2f}, std dev: {std_dev:.2f}")
                now = datetime.now()
                with (LOG_DIR / f"calibration_{printer.upper()}_cell{cell}.log").open("a", encoding="utf-8") as log:
                    log.write(f"{now:%Y-%m-%d} | {now:%H:%M:%S} | mean={mean:.2f} | std_dev={std_dev:.2f}\n")
                cell_values.clear()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
