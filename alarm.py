import os
import re
import threading
from dotenv import load_dotenv
from pathlib import Path
import requests
import math
import time
from collections import deque
from datetime import datetime

load_dotenv()

PRINTER1_IP = os.getenv("PRINTER1_IP")
PRINTER2_IP = os.getenv("PRINTER2_IP")
NTFY1 = os.getenv("NTFY1")
NTFY2 = os.getenv("NTFY2")
# Create a .env file in the root directory with the following content:
# PRINTER1_IP=http://YourPrinter1IP
# PRINTER2_IP=http://YourPrinter2IP
# NTFY1=https://ntfy.sh/YourNTFY1Topic
# NTFY2=https://ntfy.sh/YourNTFY2Topic

# ============================ Config ============================
REQUEST_TIMEOUT = 10       # seconds per HTTP request

# Filament-low alerts are handled by the load cells (load cel/), not here.
FILAMENT_DIAMETER = 1.75 # mm - for volumetric rate (mm3 per mm of filament)
RATE_WINDOW = 600 # seconds - rolling window for the per-sample consumption rate

REQUEST_REPLY = 0.5        # monitor seqs.reply, if changes get rr_reply which value is updated after ~1 seconds. So we need to check it every 0.5s
REQUEST_TEN_SECONDS = 10   # check every 10 seconds

MESSAGE_START_PRINTING = "selected for printing"
MESSAGE_END_PRINTING = "printing finished" # This value is not checked in real reply

# See: https://github.com/Duet3D/RepRapFirmware/wiki/Object-Model-Documentation
PRINTING_STATES = {"processing", "simulating"}
STOPPED_STATES = {"paused", "pausing", "halted", "cancelling", "off", "idle"}

LOG_DIR = Path(__file__).resolve().parent / "logs"


def _fmt_dt(dt: datetime, sep: str = " ") -> str:
    # e.g. "10/11/2026 11:44 am"
    hour = dt.hour % 12 or 12
    return f"{dt:%m/%d/%Y}{sep}{hour}:{dt:%M} {'am' if dt.hour < 12 else 'pm'}"


def _timestamp() -> str:
    # e.g. "10/11/2026\n11:44 am"
    return _fmt_dt(datetime.now(), sep="\n")


def notify(title: str, message: str, priority: str = "default", tags: str = "", ntfy: str = NTFY1) -> None:
    message = f"{message}\n\n{_timestamp()}"
    print(f"[ALARM] {title}: {message}")
    try:
        headers = {"Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = tags
        requests.post(
            ntfy,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        print(f"[notify error] {e}")


def _notified_problems(printer_ip: str) -> set:
    prev = _prev(printer_ip)
    if "notified_problems" not in prev:
        prev["notified_problems"] = set()
    return prev["notified_problems"]


def clear_problem_notification(printer_ip: str, problem_key: str) -> None:
    _notified_problems(printer_ip).discard(problem_key)


def notify_once(
    printer_ip: str,
    problem_key: str,
    title: str,
    message: str,
    *,
    priority: str = "default",
    tags: str = "",
    ntfy: str,
) -> bool:
    # Send a single alert per problem until clear_problem_notification() is called
    notified = _notified_problems(printer_ip)
    if problem_key in notified:
        return False
    notify(title, message, priority=priority, tags=tags, ntfy=ntfy)
    notified.add(problem_key)
    return True


def get_seqs_reply(printer_ip: str) -> dict:
    try:
        r = requests.get(
            f"{printer_ip}/rr_model",
            params={"key": "seqs.reply", "flags": "d99n"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        print(f"[get_seqs.reply error] {printer_ip}: {e}")
        return None

def get_reply(printer_ip: str) -> str:
    # Get the next pending reply from the firmware (M118, errors, warnings)
    try:
        r = requests.get(f"{printer_ip}/rr_reply", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.text.strip()
    except Exception:
        return ""

def get_state_status(printer_ip: str) -> str:
    try:
        r = requests.get(
            f"{printer_ip}/rr_model",
            params={"key": "state.status", "flags": "d99nfo"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        print(f"[get_state_status error] {printer_ip}: {e}")
        return None

def get_job(printer_ip: str) -> dict:
    # file.fileName is null when no job is running; duration = seconds since the job started
    try:
        r = requests.get(
            f"{printer_ip}/rr_model",
            params={"key": "job", "flags": "d99vno"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        print(f"[get_job error] {printer_ip}: {e}")
        return None

def get_layer(printer_ip: str):
    try:
        r = requests.get(
            f"{printer_ip}/rr_model",
            params={"key": "job.layer", "flags": "d99nfo"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        print(f"[get_layer error] {printer_ip}: {e}")
        return None

# ============================ Helpers ============================
def _printer_slug(printer_ip: str) -> str:
    return printer_ip.rstrip("/").split("/")[-1] or "printer"


def _printer_label(printer_ip: str) -> str:
    if not printer_ip:
        return "printer"
    normalized = printer_ip.rstrip("/")
    if normalized.endswith("192.168.1.243"):
        return "Printer 1"
    if normalized.endswith("192.168.1.188"):
        return "Printer 2"
    return _printer_slug(printer_ip)

# ====================== Previous state (edge detection) ======================
def _default_prev() -> dict:
    return {
        # Static values
        "job_seq": None,
        "seqs_reply": None,
        # Dynamic values
        "status": None,
        "layer": None,
        "job_log": None,  # active print log: path, file, start, pause_start, paused_total
    }

prev_by_printer: dict[str, dict] = {}

def _prev(printer_ip: str) -> dict:
    if printer_ip not in prev_by_printer:
        prev_by_printer[printer_ip] = _default_prev()
    return prev_by_printer[printer_ip]

# ============================ Save Last Reply ============================
def save_last_reply(printer_ip: str, reply: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"replies_{_printer_slug(printer_ip)}.jsonl"
    with open(log_path, "a") as f:
        f.write(f"{datetime.now().strftime('%Y-%m-%d  %H:%M:%S')},{reply}\n")

# ============================ Checks ============================
def check_reply(printer_ip: str, ntfy: str) -> None:
    prev = _prev(printer_ip)
    seqs_reply = get_seqs_reply(printer_ip)
    if seqs_reply is not None:
        if seqs_reply != prev["seqs_reply"] and prev["seqs_reply"] is not None:
            reply = get_reply(printer_ip)
            if reply:
                notify_once(
                    printer_ip,
                    f"m118:{reply}",
                    "New M118 Message",
                    f"{_printer_slug(printer_ip)}\n"
                    f"Message: {reply}",
                    tags="warning",
                    ntfy=ntfy,
                )
                save_last_reply(printer_ip, reply)
            prev["seqs_reply"] = seqs_reply

def check_state_status(printer_ip: str, ntfy: str) -> str:
    # state.status: main indicator of pause/stop/halt (edge detection)
    prev = _prev(printer_ip)
    status = get_state_status(printer_ip)
    last = prev["status"]
    if status is not None and status != last and last is not None:
        slug = _printer_slug(printer_ip)
        if status == "halted":
            notify(
                "Printer Halted",
                f"{slug}\n"
                f"state {last} -> halted (emergency stop)",
                priority="urgent",
                tags="rotating_light",
                ntfy=ntfy,
            )
        elif status == "paused" and last not in {"pausing", "paused"}:
            layer = prev.get("layer")
            notify(
                "Print Paused",
                f"{slug}\n"
                f"state {last} -> paused\n"
                f"Current layer: {layer}\n",
                priority="high",
                tags="pause_button",
                ntfy=ntfy,
            )
        elif status == "resuming":
            notify(
                "Print Resuming",
                f"{slug}\n"
                f"state {last} -> resuming",
                tags="arrow_forward",
                ntfy=ntfy,
            )
        elif last in PRINTING_STATES and status in STOPPED_STATES:
            notify(
                "Print Stopped",
                f"{slug}\n"
                f"state {last} -> {status}",
                priority="urgent",
                tags="octagonal_sign,warning",
                ntfy=ntfy,
            )
    if status is not None:
        prev["status"] = status
    print(f"{_printer_slug(printer_ip)}_state_status: {status}")  # Debug
    return status

# ============================ Print Job Log ============================
PAUSED_STATES = {"pausing", "paused"}


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def _job_log_write(log: dict, line: str) -> None:
    try:
        with open(log["path"], "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[job log error] {log['path']}: {e}")


def _job_log_start(printer_ip: str, file_name: str, duration) -> dict:
    # p1_243_2026-10-11_<job>.log (_2, _3... if the same job prints again that day)
    now = datetime.now()
    start = datetime.fromtimestamp(now.timestamp() - (duration or 0))
    num = "p1" if printer_ip == PRINTER1_IP else "p2"
    octet = _printer_slug(printer_ip).split(".")[-1]
    job = re.sub(r"[^\w.-]+", "_", Path(file_name.replace("\\", "/")).stem)
    LOG_DIR.mkdir(exist_ok=True)
    base = f"{num}_{octet}_{start:%Y-%m-%d}_{job}"
    path = LOG_DIR / f"{base}.log"
    n = 2
    while path.exists():
        path = LOG_DIR / f"{base}_{n}.log"
        n += 1
    log = {
        "path": path,
        "csv_path": path.with_suffix(".csv"),
        "file": file_name,
        "start": start,
        "pause_start": None,
        "paused_total": 0.0,
        # Filament consumption: summed positive deltas of job.rawExtrusion
        "first_sample": None,
        "last_raw": None,
        "last_time": None,
        "extruded": 0.0,
        "window": deque(),  # (time, extruded) while printing, for the rolling rate
    }
    _job_log_write(log, f"Job: {file_name}")
    _job_log_write(log, f"Printer: {_printer_label(printer_ip)} ({_printer_slug(printer_ip)})")
    note = "  (log started mid-print)" if (duration or 0) > 2 * REQUEST_TEN_SECONDS else ""
    _job_log_write(log, f"START   {_fmt_dt(start)}{note}")
    return log


def _job_log_resume(log: dict, now: datetime) -> None:
    paused = (now - log["pause_start"]).total_seconds()
    log["paused_total"] += paused
    log["pause_start"] = None
    _job_log_write(log, f"RESUME  {_fmt_dt(now)}  (paused {_fmt_duration(paused)})")


def _job_log_sample(log: dict, raw, status: str, now: datetime) -> None:
    # One CSV row per poll: time, raw extrusion, state, rolling rate (mm/min)
    if raw is None:
        return
    t = now.timestamp()
    if log["first_sample"] is None:
        log["first_sample"] = t
        try:
            with open(log["csv_path"], "w", encoding="utf-8") as f:
                f.write("timestamp,elapsed_s,state,raw_extrusion_mm,extruded_mm,rate_mm_min\n")
        except Exception as e:
            print(f"[job csv error] {log['csv_path']}: {e}")
    elif raw >= log["last_raw"]:
        log["extruded"] += raw - log["last_raw"]
    else:
        # rawExtrusion went backwards (firmware reset): restart the rolling window
        log["window"].clear()
    log["last_raw"] = raw
    log["last_time"] = t

    window = log["window"]
    rate = None
    if status in PRINTING_STATES:
        window.append((t, log["extruded"]))
        while window and t - window[0][0] > RATE_WINDOW:
            window.popleft()
        if len(window) >= 2 and t > window[0][0]:
            rate = (log["extruded"] - window[0][1]) / (t - window[0][0]) * 60
    else:
        # Don't let pauses drag the rolling rate down
        window.clear()

    try:
        with open(log["csv_path"], "a", encoding="utf-8") as f:
            f.write(
                f"{now:%Y-%m-%d %H:%M:%S},{t - log['first_sample']:.0f},{status},"
                f"{raw:.2f},{log['extruded']:.2f},{'' if rate is None else f'{rate:.2f}'}\n"
            )
    except Exception as e:
        print(f"[job csv error] {log['csv_path']}: {e}")


def _job_log_consumption(log: dict) -> None:
    # Average rate over printing time (paused time excluded) since the first sample
    if log["first_sample"] is None:
        return
    active = log["last_time"] - log["first_sample"] - log["paused_total"]
    extruded = log["extruded"]
    line = f"FILAMENT {extruded:.0f} mm"
    if active > 0:
        rate = extruded / active * 60
        area = math.pi * (FILAMENT_DIAMETER / 2) ** 2
        line += f" | avg {rate:.2f} mm/min ({rate * area * 60 / 1000:.1f} cm3/h)"
    _job_log_write(log, line)


def _job_log_end(log: dict, job: dict, now: datetime) -> None:
    if log["pause_start"] is not None:
        _job_log_resume(log, now)
    _job_log_consumption(log)
    if job.get("lastFileCancelled"):
        result = "cancelled"
    elif job.get("lastFileAborted"):
        result = "aborted"
    else:
        result = "finished"
    total = job.get("lastDuration") or (now - log["start"]).total_seconds()
    _job_log_write(
        log,
        f"END     {_fmt_dt(now)}  {result} | total {_fmt_duration(total)}"
        f" | paused {_fmt_duration(log['paused_total'])}",
    )


def check_job_log(printer_ip: str, status: str) -> None:
    # One log file per print: START, every PAUSE/RESUME, END
    if status is None:
        return
    job = get_job(printer_ip)
    if job is None:
        return
    prev = _prev(printer_ip)
    log = prev["job_log"]
    now = datetime.now()
    file_name = (job.get("file") or {}).get("fileName")

    if log is not None and file_name != log["file"]:
        _job_log_end(log, job, now)
        log = prev["job_log"] = None

    if file_name is None:
        return

    if log is None:
        log = prev["job_log"] = _job_log_start(printer_ip, file_name, job.get("duration"))

    if status in PAUSED_STATES and log["pause_start"] is None:
        log["pause_start"] = now
        _job_log_write(log, f"PAUSE   {_fmt_dt(now)}")
    elif status not in PAUSED_STATES and log["pause_start"] is not None:
        _job_log_resume(log, now)

    _job_log_sample(log, job.get("rawExtrusion"), status, now)


def check_layer(printer_ip: str) -> None:
    prev = _prev(printer_ip)
    layer = get_layer(printer_ip)
    prev["layer"] = layer


# ============================ Main ============================
def main() -> None:
    print(f"Checking {PRINTER1_IP} and {PRINTER2_IP}...")
    def loop_check_seqs_reply() -> None:
        while True:
            check_reply(PRINTER1_IP, NTFY1)
            check_reply(PRINTER2_IP, NTFY2)
            time.sleep(REQUEST_REPLY)

    def loop_check_state() -> None:
        while True:
            check_layer(PRINTER1_IP)
            status1 = check_state_status(PRINTER1_IP, NTFY1)
            check_job_log(PRINTER1_IP, status1)

            check_layer(PRINTER2_IP)
            status2 = check_state_status(PRINTER2_IP, NTFY2)
            check_job_log(PRINTER2_IP, status2)

            time.sleep(REQUEST_TEN_SECONDS)
        

    seqs_thread = threading.Thread(target=loop_check_seqs_reply, name="seqs-reply")
    state_thread = threading.Thread(target=loop_check_state, name="state")
    seqs_thread.start()
    state_thread.start()
    seqs_thread.join()
    state_thread.join()
if __name__ == "__main__":
    main()











