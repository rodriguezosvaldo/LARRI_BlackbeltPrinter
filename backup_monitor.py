"""Backup printer monitor for ONE printer, while its ESP32 is offline. Standard library only.

Normally started by the twin (twin/twin_server.py): when an ESP32's /api/spools hasn't answered
for ESP_DOWN_S while its Duet does, the twin runs a Monitor for that printer in a thread and
stops it once the ESP32 answers again. It can also be run by hand:

    python backup_monitor.py p2              # or p1
    python backup_monitor.py p2 --esp32 http://192.168.1.187 --keep

It sends the same ntfy alerts as the firmware's printer monitor (printer.cpp), to that printer's
topic: Print Started / Paused (with the Duet's reason) / Resuming / Stopped / Halted / Finished /
Cancelled / Aborted and Part Reaching Edge, marked "(PC backup monitor)". Not covered: Filament
Low (the load cells are on the ESP32) and job logs (log_server.py only gets them from the ESP32).
Run by hand, it exits by itself once the ESP32 answers ESP_BACK_OK times in a row (--keep: never).

ntfy topics: NTFY1 / NTFY2 from the environment, else .env, else load cel/hx711/include/secrets.h.
"""
import argparse
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

PRINTERS = {
    # key: Duet, ESP32 (DHCP, same as calibration.py / twin_server.py), ntfy variable, label
    "p1": {"duet": "http://192.168.1.243", "esp": "http://192.168.1.132", "ntfy": "NTFY1",
           "short": "P1 (243)"},
    "p2": {"duet": "http://192.168.1.188", "esp": "http://192.168.1.187", "ntfy": "NTFY2",
           "short": "P2 (188)"},
}

# Same values as load cel/hx711/src/config.h
REQUEST_REPLY_S = 0.5
REQUEST_STATE_S = 10
PAUSE_REPLY_WINDOW_S = REQUEST_STATE_S + 5
PRINTER_FRONT_MM = 800
PART_EDGE_WARN_MM = 10
TIMEOUT = 10

# ESP32 watchdog (twin_server.py and the stand-alone run)
ESP_DOWN_S = 120  # no /api/spools answer for this long = offline (an OTA reboot takes ~10 s)
ESP_BACK_OK = 2   # answers in a row before handing the alerts back to the ESP32

PAUSED = {"pausing", "paused"}
PRINTING = {"processing", "simulating"}
STOPPED = {"paused", "pausing", "halted", "cancelling", "off", "idle"}


# ============================ Config / HTTP ============================
def read_ntfy(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name and v.strip():
                return v.strip().strip('"').strip("'")
    secrets = ROOT / "load cel" / "hx711" / "include" / "secrets.h"
    if secrets.exists():
        m = re.search(rf'#define\s+{name}\s+"([^"]+)"', secrets.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    raise RuntimeError(f"{name} not found (environment, .env or secrets.h)")


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        return json.load(r)


def get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def esp32_alive(esp: str, key: str) -> bool:
    """The ESP32's /api/spools answers for this printer (same request the twin polls)."""
    try:
        return key in (get_json(f"{esp.rstrip('/')}/api/spools").get("printers") or {})
    except Exception:
        return False


def fmt_dt(dt: datetime, sep: str = " ") -> str:
    hour = dt.hour % 12 or 12
    return f"{dt:%m/%d/%Y}{sep}{hour}:{dt:%M} {'am' if dt.hour < 12 else 'pm'}"


def fmt_duration(seconds) -> str:
    s = max(0, int(seconds or 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m {sec:02d}s" if h else f"{m}m {sec:02d}s"


def job_name(file_name: str) -> str:
    return file_name.replace("\\", "/").rsplit("/", 1)[-1]


# ============================ Monitor ============================
class Monitor:
    """Port of printer.cpp's alerts for one printer. start() / stop() run it in two threads."""

    def __init__(self, key: str, duet: str | None = None, verbose: bool = True):
        cfg = PRINTERS[key]
        self.key = key
        self.duet = (duet or cfg["duet"]).rstrip("/")
        self.short = cfg["short"]
        self.ntfy = read_ntfy(cfg["ntfy"])
        self.verbose = verbose
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.lock = threading.Lock()
        self.replies: list[tuple[float, str]] = []  # (time.monotonic(), text), last few
        self.duet_ok = True  # log Duet errors once per outage
        # prev* edge detection (state thread only)
        self.prev_seqs = None
        self.prev_status = None
        self.layer = None
        self.job_polled = False
        self.job = None  # {file, start, pause_start, paused_total, edge_*, mid_print}

    def log(self, text: str) -> None:
        print(f"[backup {self.key}] {text}")

    # ---------------- ntfy ----------------
    def notify(self, title: str, message: str, priority: str = "default", tags: str = "") -> None:
        message = f"{message}\n(PC backup monitor)\n\n{fmt_dt(datetime.now(), sep=chr(10))}"
        self.log(f"[ALARM] {title}: {message}")
        headers = {"Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = tags
        req = urllib.request.Request(self.ntfy, data=message.encode("utf-8"), headers=headers)
        try:
            urllib.request.urlopen(req, timeout=TIMEOUT).close()
        except Exception as e:
            self.log(f"[notify error] {e}")

    # ---------------- Duet ----------------
    def model(self, key: str, flags: str):
        q = urllib.parse.urlencode({"key": key, "flags": flags})
        try:
            result = get_json(f"{self.duet}/rr_model?{q}").get("result")
        except Exception as e:
            if self.duet_ok:
                self.log(f"[duet error] {key}: {e}")
            self.duet_ok = False
            return None
        if not self.duet_ok:
            self.log("Duet answering again")
        self.duet_ok = True
        return result

    def check_reply(self) -> None:
        seqs = self.model("seqs.reply", "d99n")
        if seqs is None:
            return
        if self.prev_seqs is not None and seqs != self.prev_seqs:
            try:
                reply = get_text(f"{self.duet}/rr_reply").strip()
            except Exception:
                reply = ""
            if reply:
                self.log(f"[reply] {reply}")
                with self.lock:
                    self.replies = (self.replies + [(time.monotonic(), reply[:200])])[-4:]
        self.prev_seqs = seqs

    def recent_replies(self) -> str:
        now = time.monotonic()
        with self.lock:
            return "\n".join(t for ms, t in self.replies if now - ms <= PAUSE_REPLY_WINDOW_S)

    def check_state(self) -> str | None:
        layer = self.model("job.layer", "d99nfo")
        self.layer = layer if isinstance(layer, int) else None
        status = self.model("state.status", "d99nfo")
        last = self.prev_status
        if status is not None and last is not None and status != last:
            if status == "halted":
                self.notify("Printer Halted", f"{self.short}\nstate {last} -> halted (emergency stop)",
                            "urgent", "rotating_light")
            elif status in PAUSED and last not in PAUSED:
                reason = self.recent_replies()
                self.notify("Print Paused",
                            f"{self.short}\nstate {last} -> {status}\nCurrent layer: {self.layer}\n"
                            + (f"Duet: {reason}" if reason else "No Duet message"),
                            "high", "pause_button")
            elif status == "resuming" or (status in PRINTING and last in PAUSED):
                self.notify("Print Resuming", f"{self.short}\nstate {last} -> {status}",
                            tags="arrow_forward")
            elif last in PRINTING and status in STOPPED and status != "idle":
                self.notify("Print Stopped", f"{self.short}\nstate {last} -> {status}",
                            "urgent", "octagonal_sign,warning")
        if status is not None:
            self.prev_status = status
        if self.verbose:
            self.log(f"{datetime.now():%H:%M:%S} state: {status}  layer: {self.layer}")
        return status

    def check_job(self, status) -> None:
        if status is None:
            return
        job = self.model("job", "d99vno")
        if job is None:
            return
        now = time.time()
        file_name = (job.get("file") or {}).get("fileName")
        first_poll = not self.job_polled
        self.job_polled = True
        if self.job and file_name != self.job["file"]:
            self.job_end(job, now)
        if not file_name:
            return
        log = self.job
        if log is None:
            # A job already running when the backup starts isn't announced (like the ESP32 at boot)
            log = self.job = {"file": file_name, "start": now - (job.get("duration") or 0),
                              "pause_start": None, "paused_total": 0.0, "edge_warned": False,
                              "edge_checked": False, "mid_print": first_poll}
            if not first_poll:
                self.notify("Print Started", f"{self.short}\nJob: {job_name(file_name)}",
                            tags="printer")
        if status in PAUSED and log["pause_start"] is None:
            log["pause_start"] = now
        elif status not in PAUSED and log["pause_start"] is not None:
            log["paused_total"] += now - log["pause_start"]
            log["pause_start"] = None

    def job_end(self, job: dict, now: float) -> None:
        log, self.job = self.job, None
        if log["pause_start"] is not None:
            log["paused_total"] += now - log["pause_start"]
        if job.get("lastFileCancelled"):
            title, prio, tags = "Print Cancelled", "high", "octagonal_sign"
        elif job.get("lastFileAborted"):
            title, prio, tags = "Print Aborted", "high", "octagonal_sign"
        else:
            title, prio, tags = "Print Finished", "default", "white_check_mark"
        total = job.get("lastDuration") or now - log["start"]
        self.notify(title, f"{self.short}\nJob: {job_name(log['file'])}\n"
                    f"Total {fmt_duration(total)} | paused {fmt_duration(log['paused_total'])}",
                    prio, tags)

    def check_part_edge(self, status) -> None:
        log = self.job
        if not log or log["edge_warned"] or status not in PRINTING:
            return
        if self.layer is None or self.layer < 1:
            return  # start G-code: Z still holds the previous print's position until G92 Z0
        axes = self.model("move.axes", "d99vno") or []
        z = next((a.get("machinePosition") for a in axes if a.get("letter") == "Z"), None)
        if z is None:
            return
        first_read = not log["edge_checked"]
        log["edge_checked"] = True
        if z < PRINTER_FRONT_MM - PART_EDGE_WARN_MM:
            return
        log["edge_warned"] = True
        if first_read and log["mid_print"]:
            return  # already past it when the backup started
        self.notify("Part Reaching Edge",
                    f"{self.short}\nThe part is about to leave the printer: it will need a support behind soon",
                    "high", "straight_ruler")

    def duet_status(self) -> str | None:
        return self.model("state.status", "d99nfo")

    # ---------------- threads ----------------
    def _reply_loop(self) -> None:
        while not self._stop.is_set():
            self.check_reply()
            self._stop.wait(REQUEST_REPLY_S)

    def _state_loop(self) -> None:
        while not self._stop.is_set():
            try:
                status = self.check_state()
                self.check_job(status)
                self.check_part_edge(status)
            except Exception as e:  # never let one bad poll stop the backup
                self.log(f"error: {e!r}")
            self._stop.wait(REQUEST_STATE_S)

    def start(self) -> None:
        self._threads = [threading.Thread(target=self._reply_loop, daemon=True, name=f"backup-{self.key}-reply"),
                         threading.Thread(target=self._state_loop, daemon=True, name=f"backup-{self.key}-state")]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()


# ============================ Stand-alone ============================
def main() -> None:
    ap = argparse.ArgumentParser(description="Backup PC monitor for one printer (ESP32 offline).")
    ap.add_argument("printer", choices=sorted(PRINTERS))
    ap.add_argument("--esp32", help="that printer's ESP32 URL (default: its usual DHCP address)")
    ap.add_argument("--keep", action="store_true", help="don't exit when the ESP32 is back")
    a = ap.parse_args()
    esp = a.esp32 or PRINTERS[a.printer]["esp"]
    if not esp.startswith("http"):
        esp = "http://" + esp
    try:
        mon = Monitor(a.printer)
    except RuntimeError as e:
        raise SystemExit(str(e))
    print(f"Backup monitor for {mon.short}: Duet {mon.duet}, ESP32 {esp}"
          + ("" if a.keep else " (exits when the ESP32 answers again)"))
    mon.notify("Backup Monitor Started",
               f"{mon.short}\nThe PC sends this printer's alerts until its ESP32 is back", tags="computer")
    mon.start()
    ok_in_row = 0
    try:
        while True:
            time.sleep(30)
            if a.keep:
                continue
            ok_in_row = ok_in_row + 1 if esp32_alive(esp, a.printer) else 0
            if ok_in_row >= ESP_BACK_OK:
                break
    except KeyboardInterrupt:
        print("Stopped (Ctrl+C)")
        mon.stop()
        return
    mon.stop()
    mon.notify("Backup Monitor Stopped",
               f"{mon.short}\nESP32 answering again: it sends the alerts from now on",
               tags="white_check_mark")


if __name__ == "__main__":
    main()
