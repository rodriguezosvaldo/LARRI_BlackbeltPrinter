"""Log receiver for the printer ESP32s (load cel/hx711 firmware). Standard library only.

    python log_server.py          -> listens on port 5052 (LOG_SERVER_PORT to change it)

The ESP32s do all the monitoring (alarms, spool monitor, dashboard) and just send their logs
here, written to logs/ in the same format the old alarm.py / spool_monitor.py used:

    POST /job          {"key", "base", "line"}   -> logs/<base>.log  (_2, _3... for a repeat print that day)
    POST /reply        {"slug", "line"}          -> logs/replies_<slug>.jsonl
    POST /cells        {"printer", "rows": [[unix, cell1, cell2], ...]}
                                                 -> logs/load_cells/<printer>_<date>.csv
    GET  /cells/recent?printer=p1&seconds=7200   -> "unix,cell1,cell2" lines (history reload at boot)

If this PC is off, the ESP32s keep working and send what's pending when it comes back.
On Windows, allow python through the firewall (private networks) for port 5052.
"""

import csv
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(os.getenv("LOG_SERVER_PORT", "5052"))
LOG_DIR = Path(__file__).resolve().parent / "logs"  # gitignored
CELL_DIR = LOG_DIR / "load_cells"
JOB_INDEX = LOG_DIR / ".job_index.json"  # print key -> log file name
JOB_INDEX_MAX = 500
PRINTERS = {"p1", "p2"}
CELLS = ("cell1", "cell2")
SAFE_NAME = re.compile(r"^[\w.-]+$")

lock = threading.Lock()
job_index: dict[str, str] = {}
last_cell_time: dict[str, float] = {}  # per printer, to skip rows resent after a lost reply


# ============================ Job logs ============================
def load_job_index() -> None:
    global job_index
    try:
        job_index = json.loads(JOB_INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        job_index = {}


def job_path(key: str, base: str) -> Path:
    # One file per print: the first line of a new print picks the name (as alarm.py did)
    name = job_index.get(key)
    if name is None:
        name = f"{base}.log"
        n = 2
        while (LOG_DIR / name).exists():
            name = f"{base}_{n}.log"
            n += 1
        job_index[key] = name
        while len(job_index) > JOB_INDEX_MAX:
            job_index.pop(next(iter(job_index)))
        JOB_INDEX.write_text(json.dumps(job_index, indent=1), encoding="utf-8")
    return LOG_DIR / name


def write_job(data: dict) -> None:
    key, base, line = str(data["key"]), str(data["base"]), str(data["line"])
    if not SAFE_NAME.match(base):
        raise ValueError(f"bad base {base!r}")
    with lock:
        LOG_DIR.mkdir(exist_ok=True)
        path = job_path(key, base)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    print(f"[{path.name}] {line}")


def write_reply(data: dict) -> None:
    slug, line = str(data["slug"]), str(data["line"])
    if not SAFE_NAME.match(slug):
        raise ValueError(f"bad slug {slug!r}")
    with lock:
        LOG_DIR.mkdir(exist_ok=True)
        with (LOG_DIR / f"replies_{slug}.jsonl").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    print(f"[reply {slug}] {line}")


# ============================ Load-cell CSV ============================
def cell_path(printer: str, day: datetime) -> Path:
    return CELL_DIR / f"{printer}_{day:%Y-%m-%d}.csv"


def last_logged_time(printer: str) -> float:
    # Last unix time in today's CSV (only read once per printer)
    if printer not in last_cell_time:
        t = 0.0
        path = cell_path(printer, datetime.now())
        try:
            with path.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    t = float(row["unix"])
        except (OSError, KeyError, ValueError):
            pass
        last_cell_time[printer] = t
    return last_cell_time[printer]


def write_cells(data: dict) -> None:
    printer = data["printer"]
    if printer not in PRINTERS:
        raise ValueError(f"bad printer {printer!r}")
    rows = [(float(r[0]), int(r[1]), int(r[2])) for r in data["rows"]]
    with lock:
        CELL_DIR.mkdir(parents=True, exist_ok=True)
        last = last_logged_time(printer)
        for t, c1, c2 in rows:
            if t <= last:
                continue  # already written (ESP32 resent after a lost reply)
            dt = datetime.fromtimestamp(t)
            path = cell_path(printer, dt)
            new = not path.exists()
            with path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["time", "unix", *CELLS])
                w.writerow([dt.strftime("%Y-%m-%d %H:%M:%S"), f"{t:.1f}", c1, c2])
            last = t
        last_cell_time[printer] = last


def recent_cells(printer: str, seconds: float) -> str:
    # Readings from the last `seconds`, from yesterday's and today's CSV, oldest first
    now = time.time()
    today = datetime.now()
    out = []
    for day in (today - timedelta(days=1), today):
        path = cell_path(printer, day)
        if not path.exists():
            continue
        try:
            with path.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    t = float(row["unix"])
                    if now - t <= seconds:
                        out.append(f"{int(t)},{int(row['cell1'])},{int(row['cell2'])}\n")
        except (OSError, KeyError, ValueError) as e:
            print(f"[{printer}] [history] skipping {path.name}: {e}")
    return "".join(out)


# ============================ HTTP ============================
POST_ROUTES = {"/job": write_job, "/reply": write_reply, "/cells": write_cells}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str, ctype: str = "text/plain") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        route = POST_ROUTES.get(urlparse(self.path).path)
        if route is None:
            self._send(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            route(json.loads(self.rfile.read(length)))
        except (ValueError, KeyError, TypeError, IndexError) as e:
            print(f"[bad request] {self.path} from {self.client_address[0]}: {e}")
            self._send(400, str(e))
            return
        except OSError as e:
            print(f"[write error] {self.path}: {e}")
            self._send(500, str(e))
            return
        self._send(200, "ok")

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/cells/recent":
            q = parse_qs(url.query)
            printer = q.get("printer", [""])[0]
            if printer not in PRINTERS:
                self._send(400, "bad printer")
                return
            seconds = float(q.get("seconds", ["7200"])[0])
            with lock:
                body = recent_cells(printer, seconds)
            print(f"[{printer}] history reload: {body.count(chr(10))} readings")
            self._send(200, body)
        elif url.path == "/":
            self._send(200, f"BB_printer log server, writing to {LOG_DIR}\n")
        else:
            self._send(404, "not found")

    def log_message(self, format, *args) -> None:
        pass  # quiet: only the lines written are printed


def main() -> None:
    load_job_index()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Log server on port {PORT}, writing to {LOG_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
