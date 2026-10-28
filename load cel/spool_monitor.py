"""Spool monitor: polls each printer's load-cell ESP32 and estimates filament time left.

    python "load cel/spool_monitor.py"      -> http://localhost:5051

Per cell the page shows the empty reel, the current reading, the reference rate (from
spools.json) and the live rate, plus time left = (current reading - empty reel) / rate. The rate is the live
one (least-squares slope over the last RATE_WINDOW seconds) while consuming, else ref_rate.
While consuming, the reading is the fitted line's value now (else the last-minute mean, see
FIT_MAX_DEVIATION), and TENSION_OFFSET is added to it (filament tension lowers it).

Readings are also appended to logs/load_cells/<printer>_<date>.csv, and the last RATE_WINDOW
of them is reloaded at startup so a restart doesn't reset the live rate.
"""

import csv
import json
import statistics
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import Flask, jsonify, send_from_directory

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "spools.json"
LOG_DIR = HERE.parent / "logs" / "load_cells"  # logs/ is gitignored

WEB_PORT = 5051
POLL_SECONDS = 2        # firmware reads every 2 s
REQUEST_TIMEOUT = 3
RATE_WINDOW = 7200      # s - live rate = slope over the last 2 hours
MIN_RATE_SPAN = 600     # s - need at least 10 min of data before showing a live rate
CURRENT_WINDOW = 60     # s - fallback "current" = mean of the last minute (printing noise ~3.5k)
# While consuming, "current" = the fitted line's value now (uses the whole window, no lag).
# If it differs from the last-minute mean by more than this, the window doesn't follow one
# straight line (print start/stop, pause, tension step) and the last-minute mean is used.
FIT_MAX_DEVIATION = 5000  # counts
MIN_CONSUMING_RATE = 1000  # counts/h - below this the spool is considered idle (~2 m/h)
JUMP_THRESHOLD = 100000  # counts - a jump this big vs. the recent median = spool changed/removed
JUMP_CONFIRM = 3        # consecutive jumped readings before resetting the history
# While printing, filament tension pulls the reel up and lowers the reading by ~10k-20k counts.
# full/empty in spools.json are measured at rest, so while consuming we add this back to the
# reading before computing % and time left (otherwise time left comes out ~1 h short).
TENSION_OFFSET = 15000  # counts
CELLS = ("cell1", "cell2")
CONSOLE_REPORT_SECONDS = 300  # print each active cell's end time / time left to the console


# ============================ Config ============================
_config = {}
_config_mtime = None
_config_lock = threading.Lock()


def get_config() -> dict:
    """spools.json, reloaded when the file changes (so it can be edited while running)."""
    global _config, _config_mtime
    with _config_lock:
        try:
            mtime = CONFIG_PATH.stat().st_mtime
            if mtime != _config_mtime:
                _config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                _config_mtime = mtime
        except (OSError, json.JSONDecodeError) as e:
            print(f"[config] could not load {CONFIG_PATH.name}: {e}")
        return _config


# ============================ Per-cell history ============================
class CellHistory:
    def __init__(self):
        self.samples = deque()  # (unix time, reading)
        self.jumps = 0
        self.lock = threading.Lock()

    def add(self, t: float, value: int) -> bool:
        """Add a reading; returns True if a spool change was detected (history reset)."""
        with self.lock:
            recent = [v for _, v in list(self.samples)[-15:]]
            reset = False
            if len(recent) >= 5 and abs(value - statistics.median(recent)) > JUMP_THRESHOLD:
                self.jumps += 1
                if self.jumps >= JUMP_CONFIRM:
                    # keep only the readings taken after the jump
                    kept = list(self.samples)[-(JUMP_CONFIRM - 1):]
                    self.samples = deque(kept)
                    self.jumps = 0
                    reset = True
            else:
                self.jumps = 0
            self.samples.append((t, value))
            while self.samples and t - self.samples[0][0] > RATE_WINDOW:
                self.samples.popleft()
            return reset

    def stats(self, now: float) -> dict:
        with self.lock:
            samples = list(self.samples)
        if not samples:
            return {"current": None, "current_source": None, "live_rate": None, "span": 0, "n": 0}
        last = [v for t, v in samples if now - t <= CURRENT_WINDOW] or [samples[-1][1]]
        last_mean = statistics.fmean(last)
        current, source = last_mean, "last_minute"
        span = samples[-1][0] - samples[0][0]
        live_rate = None
        if span >= MIN_RATE_SPAN and len(samples) >= 10:
            ts = [t for t, _ in samples]
            vs = [v for _, v in samples]
            mt, mv = statistics.fmean(ts), statistics.fmean(vs)
            sxx = sum((t - mt) ** 2 for t in ts)
            sxy = sum((t - mt) * (v - mv) for t, v in samples)
            slope = sxy / sxx  # counts/s
            live_rate = -slope * 3600  # counts/h dropped (positive = consuming)
            fit_now = mv + slope * (min(now, ts[-1]) - mt)
            if live_rate >= MIN_CONSUMING_RATE and abs(fit_now - last_mean) <= FIT_MAX_DEVIATION:
                current, source = fit_now, "fit"
        return {
            "current": current,
            "current_source": source,
            "live_rate": live_rate,
            "span": span,
            "n": len(samples),
        }


history = {p: {c: CellHistory() for c in CELLS} for p in ("p1", "p2")}
status = {p: {"online": False, "last_seen": None, "error": None, "ip": None} for p in ("p1", "p2")}
status_lock = threading.Lock()


# ============================ CSV log ============================
def _log_path(printer: str, day: datetime) -> Path:
    return LOG_DIR / f"{printer}_{day:%Y-%m-%d}.csv"


def log_reading(printer: str, t: float, readings: dict) -> None:
    dt = datetime.fromtimestamp(t)
    path = _log_path(printer, dt)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "unix", *CELLS])
            w.writerow([dt.strftime("%Y-%m-%d %H:%M:%S"), f"{t:.1f}", *(readings[c] for c in CELLS)])
    except OSError as e:
        print(f"[{printer}] [log error] {e}")


def load_recent_history() -> None:
    """Reload the last RATE_WINDOW of readings from today's/yesterday's CSV."""
    now = time.time()
    today = datetime.now()
    for printer in history:
        for day in (today - timedelta(days=1), today):
            path = _log_path(printer, day)
            if not path.exists():
                continue
            try:
                with path.open(newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        t = float(row["unix"])
                        if now - t > RATE_WINDOW:
                            continue
                        for cell in CELLS:
                            history[printer][cell].add(t, int(row[cell]))
            except (OSError, KeyError, ValueError) as e:
                print(f"[{printer}] [history] skipping {path.name}: {e}")


# ============================ Polling ============================
def poll_printer(printer: str) -> None:
    last_seq = None
    while True:
        ip = get_config().get(printer, {}).get("esp32")
        if not ip:
            time.sleep(POLL_SECONDS)
            continue
        url = (ip if ip.startswith("http") else f"http://{ip}").rstrip("/") + "/data"
        try:
            data = requests.get(url, timeout=REQUEST_TIMEOUT).json()
            if not data.get("ok"):
                raise RuntimeError("ESP32 reports HX711 not ready")
            with status_lock:
                status[printer].update(online=True, error=None, ip=ip, last_seen=time.time())
            if data.get("seq") != last_seq:
                last_seq = data.get("seq")
                t = time.time()
                readings = {c: int(data[c]) for c in CELLS}
                for cell in CELLS:
                    if history[printer][cell].add(t, readings[cell]):
                        print(f"[{printer}] {cell}: spool change detected, live rate reset")
                log_reading(printer, t, readings)
        except Exception as e:
            with status_lock:
                status[printer].update(online=False, error=str(e), ip=ip)
        time.sleep(POLL_SECONDS)


# ============================ Estimates ============================
def _hours_left(current, empty, rate):
    if current is None or empty is None or not rate or rate < MIN_CONSUMING_RATE:
        return None
    return max(current - empty, 0) / rate


def cell_view(printer: str, cell: str, cfg: dict, now: float) -> dict:
    s = history[printer][cell].stats(now)
    full, empty, ref_rate = cfg.get("full"), cfg.get("empty"), cfg.get("ref_rate")
    current = s["current"]
    live = s["live_rate"]
    consuming = live is not None and live >= MIN_CONSUMING_RATE
    # at-rest equivalent of the reading (undo the filament tension while printing)
    level = current + TENSION_OFFSET if current is not None and consuming else current
    percent = None
    if level is not None and full is not None and empty is not None and full != empty:
        percent = max(0.0, min(100.0, (level - empty) / (full - empty) * 100))
    return {
        "current": current,
        "current_source": s["current_source"],
        "full": full,
        "empty": empty,
        "ref_rate": ref_rate,
        "live_rate": live,
        "live_span": s["span"],
        "consuming": consuming,
        "tension_offset": TENSION_OFFSET if consuming else 0,
        "percent": percent,
        "hours_left_live": _hours_left(level, empty, live),
        "hours_left_ref": _hours_left(level, empty, ref_rate),
        "note": cfg.get("note", ""),
    }


# ============================ Console report ============================
def _fmt_end(t: datetime) -> str:
    # 10/27/2026, 3:17pm
    hour = t.hour % 12 or 12
    return f"{t:%m/%d/%Y}, {hour}:{t:%M}{'am' if t.hour < 12 else 'pm'}"


def console_report() -> None:
    # Every CONSOLE_REPORT_SECONDS: end time and time left of each active (non-idle) cell,
    # same estimate as the page (live rate while consuming, else reference rate)
    while True:
        time.sleep(CONSOLE_REPORT_SECONDS)
        now = time.time()
        cfg = get_config()
        blocks = []
        for printer in history:
            for cell in CELLS:
                v = cell_view(printer, cell, cfg.get(printer, {}).get("cells", {}).get(cell, {}), now)
                if v["live_rate"] is not None and not v["consuming"]:
                    continue  # idle
                h = v["hours_left_live"] if v["consuming"] else v["hours_left_ref"]
                if h is None:
                    continue
                minutes = round(h * 60)
                basis = "" if v["consuming"] else "  (reference rate, collecting live data)"
                blocks.append(
                    f"{printer.upper()} {cell}\n"
                    f"ends: {_fmt_end(datetime.fromtimestamp(now + h * 3600))}\n"
                    f"time left: {minutes // 60}h {minutes % 60:02d}min{basis}"
                )
        sep = "-" * 39
        print(f"\n=========== {datetime.now():%m/%d/%Y %H:%M:%S} ===========")
        print(f"\n{sep}\n".join(blocks) if blocks else "No active spools")


# ============================ Web ============================
app = Flask(__name__)


@app.route("/")
def index():
    return send_from_directory(HERE, "spool_monitor.html")


@app.get("/api/spools")
def api_spools():
    cfg = get_config()
    now = time.time()
    out = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "rate_window": RATE_WINDOW, "min_rate_span": MIN_RATE_SPAN, "printers": {}}
    for printer in history:
        pcfg = cfg.get(printer, {})
        with status_lock:
            st = dict(status[printer])
        out["printers"][printer] = {
            "label": pcfg.get("label", printer.upper()),
            **st,
            "cells": {c: cell_view(printer, c, pcfg.get("cells", {}).get(c, {}), now) for c in CELLS},
        }
    return jsonify(out)


def main() -> None:
    get_config()
    load_recent_history()
    for printer in history:
        threading.Thread(target=poll_printer, args=(printer,), name=f"poll-{printer}", daemon=True).start()
    threading.Thread(target=console_report, name="console-report", daemon=True).start()
    print(f"Spool monitor: http://localhost:{WEB_PORT}")
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
