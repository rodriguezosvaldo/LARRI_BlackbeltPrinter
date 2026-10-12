import re
import statistics
import sys

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM3"
BAUD = 115200
WINDOW = 100

# Matches main.cpp output: "HX711 #1 reading: 123456"
pattern = re.compile(r"HX711 #(\d+) reading:\s*(-?\d+)")

with serial.Serial(PORT, BAUD, timeout=2) as ser:
    print(f"Reading {PORT} @ {BAUD}... (Ctrl+C to exit)")
    try:
        values = {1: [], 2: []}
        while True:
            line = ser.readline().decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            match = pattern.search(line)
            if match:
                cell = int(match.group(1))
                value = int(match.group(2))
                print(f"cell {cell}: {value}")
                cell_values = values.setdefault(cell, [])
                cell_values.append(value)
                if len(cell_values) == WINDOW:
                    mean = statistics.mean(cell_values)
                    std_dev = statistics.stdev(cell_values)
                    print(f"Cell {cell} last {WINDOW} readings -> mean: {mean:.2f}, std dev: {std_dev:.2f}")
                    cell_values.clear()
            else:
                print(f"[msg] {line}")
    except KeyboardInterrupt:
        pass
