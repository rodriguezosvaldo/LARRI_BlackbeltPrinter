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
#   python calibration.py report [DIR]            -> only rebuild calibration_results.txt from the logs
# calibration_results.txt is also rebuilt when you stop the reader with Ctrl+C.
DEFAULT_SOURCES = {"p1": P1_IP, "p2": P2_IP}
BAUD = 115200
WINDOW = 100
POLL_SECONDS = 1  # main.cpp reads every 2s; polling faster only skips duplicate seqs
LOG_DIR = Path(__file__).parent  # calibration_<P1|P2>_cell<N>.log: one line per WINDOW of readings

RESULTS_FILE = "calibration_results.txt"
MANUAL_MARKER = "Manual notes (kept when the file is regenerated)"
# Report heuristics (raw counts; one log line = WINDOW readings, ~200 s over WiFi)
REST_STD = 1000          # std_dev above this = spool moving/vibrating (e.g. filament tension), not at rest
LEVEL_TOL = 2000         # samples within this of a level's first value belong to the same rest level
LEVEL_MIN_SAMPLES = 3    # rest levels need at least this many log lines (~10 min)
RUN_MAX_GAP_MIN = 15     # a gap longer than this splits a level or a printing run
RUN_MAX_STEP = 10000     # a step bigger than this splits a printing run (tension at print start is ~15k)
RUN_MIN_HOURS = 1.0      # printing runs shorter than this are not used for the rate
RUN_MIN_RATE = 1000      # counts/h; slower drops are drift, not printing
SPOOL_JUMP = 500000      # rise between levels bigger than this = new spool put on
REEL_COUNTS = (150000, 260000)  # drop between levels in this range = reel taken off (empty reel ~205k counts)

LOG_LINE = re.compile(r"(\d{4}-\d{2}-\d{2}) \| (\d{2}:\d{2}:\d{2}) \| mean=(-?[\d.]+) \| std_dev=([\d.]+)")

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


def read_log(path):
    """Return [(datetime, mean, std)] from one calibration_<P>_cell<N>.log."""
    samples = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = LOG_LINE.search(line)
        if m:
            t = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
            samples.append((t, float(m.group(3)), float(m.group(4))))
    return sorted(samples)


def slope_per_hour(points):
    """Least-squares slope (counts/h) of [(datetime, value)], or 0 for fewer than 2 points."""
    if len(points) < 2:
        return 0.0
    t0 = points[0][0]
    xs = [(t - t0).total_seconds() / 3600 for t, _ in points]
    ys = [v for _, v in points]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0


def is_flat(samples, i):
    """True if the reading around sample i (+-RUN_MAX_GAP_MIN, no jumps) is not dropping/rising."""
    t, v, _ = samples[i]
    near = [(tt, vv) for tt, vv, _ in samples
            if abs((tt - t).total_seconds()) <= RUN_MAX_GAP_MIN * 60 and abs(vv - v) < RUN_MAX_STEP]
    return abs(slope_per_hour(near)) < RUN_MIN_RATE


def rest_levels(samples):
    """Group consecutive at-rest samples with the same value. Returns (levels, indexes used)."""
    levels, used, group = [], set(), []
    rest = [std < REST_STD and is_flat(samples, i) for i, (_, _, std) in enumerate(samples)]

    def close():
        if len(group) >= LEVEL_MIN_SAMPLES:
            levels.append({
                "start": samples[group[0]][0], "end": samples[group[-1]][0], "n": len(group),
                "mean": statistics.mean(samples[i][1] for i in group),
                "std": statistics.mean(samples[i][2] for i in group),
                "tag": "",
            })
            used.update(group)

    for i, (t, mean, std) in enumerate(samples):
        fits = (group and rest[i] and abs(mean - samples[group[0]][1]) < LEVEL_TOL
                and (t - samples[group[-1]][0]).total_seconds() < RUN_MAX_GAP_MIN * 60)
        if not fits:
            close()
            group = []
        if rest[i]:
            group.append(i)
    close()
    return levels, used


def printing_runs(samples, used):
    """Least-squares rate (counts/h, positive = consuming) of each run of non-rest samples."""
    runs, run = [], []

    def close():
        if len(run) < 3:
            return
        hours = (run[-1][0] - run[0][0]).total_seconds() / 3600
        rate = -slope_per_hour(run)
        if hours >= RUN_MIN_HOURS and rate >= RUN_MIN_RATE:
            runs.append({"start": run[0][0], "hours": hours, "rate": rate})

    for i, (t, mean, std) in enumerate(samples):
        if i in used or std >= REST_STD:
            close()
            run = []
            continue
        if run and ((t - run[-1][0]).total_seconds() > RUN_MAX_GAP_MIN * 60 or abs(mean - run[-1][1]) > RUN_MAX_STEP):
            close()
            run = []
        run.append((t, mean))
    close()
    return runs


def analyze(samples):
    """Guess empty reel / no reel / full spool levels and the printing rate for one cell."""
    levels, used = rest_levels(samples)
    runs = printing_runs(samples, used)
    for a, b in zip(levels, levels[1:]):
        step = b["mean"] - a["mean"]
        if step > SPOOL_JUMP:  # new spool put on
            a["tag"] = a["tag"] or "empty reel?"
            b["tag"] = "full spool?"
        elif REEL_COUNTS[0] <= -step <= REEL_COUNTS[1]:  # reel taken off
            a["tag"] = "empty reel?"
            b["tag"] = "no reel?"
        elif REEL_COUNTS[0] <= step <= REEL_COUNTS[1] and a["tag"] == "no reel?":  # empty reel put back
            b["tag"] = b["tag"] or "empty reel?"

    def last(tag):
        found = [lv["mean"] for lv in levels if lv["tag"] == tag]
        return found[-1] if found else None

    total_h = sum(r["hours"] for r in runs)
    rate = sum(r["rate"] * r["hours"] for r in runs) / total_h if runs else None
    return {"levels": levels, "runs": runs, "rate": rate,
            "empty": last("empty reel?"), "full": last("full spool?"), "no_reel": last("no reel?")}


def write_report(log_dir=LOG_DIR):
    """Rebuild calibration_results.txt from calibration_*.log, keeping the manual notes section."""
    log_dir = Path(log_dir)
    cells = {}
    for path in sorted(log_dir.glob("calibration_P*_cell*.log")):
        m = re.search(r"calibration_(P\d)_cell(\d)", path.name)
        samples = read_log(path)
        if m and samples:
            cells[f"{m.group(1).lower()}_cell{m.group(2)}"] = (samples, analyze(samples))
    if not cells:
        print(f"No readings in {log_dir / 'calibration_*.log'}, {RESULTS_FILE} not changed.")
        return

    def fmt(v):
        return "-" if v is None else f"{v:,.0f}"

    first = min(s[0][0] for s, _ in cells.values())
    last = max(s[-1][0] for s, _ in cells.values())
    out = [
        "Load cell calibration results",
        "=============================",
        f"Generated by calibration.py on {datetime.now():%Y-%m-%d %H:%M}; edit only the manual notes section.",
        f"Source: calibration_*.log ({first:%Y-%m-%d %H:%M} -> {last:%Y-%m-%d %H:%M})",
        "Units: raw HX711 counts (no tare). The reading goes up with weight.",
        "Rate = counts/h the reading drops while printing (depends on the job).",
        "Values with '?' are automatic guesses: check them against the levels below.",
        "",
        "Cell        Empty reel?   Rate (counts/h)   Full spool?      No reel?",
        "---------   -----------   ---------------   -----------   -----------",
    ]
    for name, (_, r) in cells.items():
        out.append(f"{name:<9}   {fmt(r['empty']):>11}   {fmt(r['rate']):>15}   "
                   f"{fmt(r['full']):>11}   {fmt(r['no_reel']):>11}")
    out += ["", "", "Per-cell details", "----------------"]
    for name, (samples, r) in cells.items():
        out.append(f"{name} ({len(samples)} log lines)")
        out.append(f"  Rest levels (std < {REST_STD}, >= {LEVEL_MIN_SAMPLES} lines):")
        for lv in r["levels"]:
            out.append(f"    {lv['start']:%m-%d %H:%M} -> {lv['end']:%m-%d %H:%M}  {lv['mean']:>12,.0f}"
                       f"  (std {lv['std']:,.0f}, {lv['n']} lines)  {lv['tag']}".rstrip())
        if not r["levels"]:
            out.append("    none")
        out.append(f"  Printing runs (>= {RUN_MIN_HOURS:g} h):")
        for run in r["runs"]:
            out.append(f"    {run['start']:%m-%d %H:%M}  {run['hours']:5.1f} h  {run['rate']:>8,.0f} counts/h")
        if not r["runs"]:
            out.append("    none")
        out.append("")

    path = log_dir / RESULTS_FILE
    manual = ""
    if path.exists():
        old = path.read_text(encoding="utf-8")
        # first run on a hand-written file: keep all of it as notes
        manual = old.split(MANUAL_MARKER, 1)[1].lstrip("-\n") if MANUAL_MARKER in old else old
    out += ["", MANUAL_MARKER, "-" * len(MANUAL_MARKER), manual.rstrip()]
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {path}")


def main():
    if sys.argv[1:2] == ["report"]:
        write_report(sys.argv[2] if len(sys.argv) > 2 else LOG_DIR)
        return
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
        write_report()


if __name__ == "__main__":
    main()
