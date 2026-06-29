import csv
import json
import time
import os
from pathlib import Path
import requests
from typing import Any, Optional
from dotenv import load_dotenv
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
# 
TIME_LEFT_FILAMENT_ALERT = 5*60 # 5 minutes - job.timesLeft.filament
TOTAL_FILAMENT_LENGTH = 1498000 # mm - Unused filanent (4kg)
FILAMENT_USAGE_ALERT = 10000 # mm - Alert if filament length is less than this value
POLL_INTERVAL = 5          # seconds between polls
REQUEST_TIMEOUT = 10       # seconds per HTTP request
STALL_SECONDS = 60         # job.filePosition without changes -> print stuck
MCU_TEMP_MAX = 80.0        # alarm if MCU temp > X
VIN_MIN = 11.0             # alarm if VIN < X V
MAX_PROBLEM_NOTIFICATIONS = 2  # normal alerts before the final "no more" message
LOG_INTERVAL = 10      # seconds between periodic logs
LOG_PATH = Path(__file__).resolve().parent / "logs" / "om_snapshots.jsonl"
CHECKED_LOG_PATH = Path(__file__).resolve().parent / "logs" / "checked_values.jsonl"
FINISH_LOG_PATH = Path(__file__).resolve().parent / "logs" / "finish_snapshot.jsonl"
EXTRUSION_RATE_LOG_DIR = Path(__file__).resolve().parent / "logs" / "extrusion_rate"

# ===================== Sets of the Object Model =====================
# See: https://github.com/Duet3D/RepRapFirmware/wiki/Object-Model-Documentation
PRINTING_STATES = {"processing", "simulating"}
STOPPED_STATES = {"paused", "pausing", "halted", "cancelling", "off", "idle"}
FILAMENT_FAULT_STATUSES = {
    "noFilament",
    "tooLittleMovement",
    "tooMuchMovement",
    "sensorError",
    "noDataReceived",
}
HEATER_FAULT_STATES = {"fault", "offline"}


# ============================ Helpers ============================
def _safe(d: Any, *path, default=None):
    # Traverse nested dict/list without breaking if a key is missing
    cur = d
    for p in path:
        if cur is None:
            return default
        if isinstance(p, int):
            if isinstance(cur, list) and -len(cur) <= p < len(cur):
                cur = cur[p]
            else:
                return default
        else:
            if isinstance(cur, dict):
                cur = cur.get(p)
            else:
                return default
    return cur if cur is not None else default


def notify(title: str, message: str, priority: str = "default", tags: str = "", ntfy: str = NTFY1) -> None:
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


def _problem_notifications(printer_ip: str) -> dict[str, int | str]:
    prev = _prev(printer_ip)
    if "problem_notifications" not in prev:
        prev["problem_notifications"] = {}
    return prev["problem_notifications"]


def clear_problem_notification(printer_ip: str, problem_key: str) -> None:
    _problem_notifications(printer_ip).pop(problem_key, None)


def stop_notifications(
    printer_ip: str,
    problem_key: str,
    title: str,
    message: str,
    *,
    priority: str = "default",
    tags: str = "",
    ntfy: str,
) -> bool:
    # Up to MAX_PROBLEM_NOTIFICATIONS normal alerts, then one final message; then suppress.
    counts = _problem_notifications(printer_ip)
    state = counts.get(problem_key)

    if state == "suppressed":
        return False

    n = state if isinstance(state, int) else 0

    if n < MAX_PROBLEM_NOTIFICATIONS:
        notify(title, message, priority=priority, tags=tags, ntfy=ntfy)
        counts[problem_key] = n + 1
        return True

    if n == MAX_PROBLEM_NOTIFICATIONS:
        notify(
            f"{title} (Last Notification)",
            f"{message}\n\nNo more notifications will be sent for this problem, "
            "but it is likely that the problem still continues",
            priority=priority,
            tags=tags,
            ntfy=ntfy,
        )
        counts[problem_key] = "suppressed"
        return True

    return False

def get_model_static_values(printer_ip: str) -> Optional[dict]:
    # Full static model (flags=d99n, no key) is too large and returns HTTP 503 
    try:
        def _key(key: str) -> Any:
            r = requests.get(
                f"{printer_ip}/rr_model",
                params={"key": key, "flags": "d99n"},
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            return r.json().get("result")

        return {
            "seqs": {"job": _key("seqs.job")},
            "job": _key("job"),
        }
    except Exception as e:
        print(f"[get_model_static_values error] {e}")
        return None

def get_model(printer_ip: str) -> Optional[dict]:
    # Flag d99fno:
    # d99: max tree depth = 99 levels
    # f: only values that change frequently
    # n: includes null values
    # o: includes obsolete fields
    try:
        r = requests.get(
            f"{printer_ip}/rr_model",
            params={"flags": "d99fno"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        print(f"[get_model error] {e}")
        return None


def get_reply(printer_ip: str) -> str:
    # Get the next pending reply from the firmware (M118, errors, warnings)
    try:
        r = requests.get(f"{printer_ip}/rr_reply", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.text.strip()
    except Exception:
        return ""


# ====================== Previous state (edge detection) ======================
def _default_prev() -> dict:
    return {
        # Dynamic values
        "time_monotonic": time.monotonic(),
        "status": None,
        "filament": {},                # index -> last status
        "heaters": {},                 # index -> last state
        "analog": {},                  # index -> last state
        "file_position": None,
        "file_position_changed_at": time.monotonic(),
        "pause_duration": None,
        "warmup_duration": None,
        "raw_extrusion": None,
        "extrusion_rate_log_path": None,
        "extrusion_rate_started_at": None,
        # Static values
        "job_seq": None,
        "last_duration": None,
        "file_name": None,
    }


prev_by_printer: dict[str, dict] = {}


def _prev(printer_ip: str) -> dict:
    if printer_ip not in prev_by_printer:
        prev_by_printer[printer_ip] = _default_prev()
    return prev_by_printer[printer_ip]


# ============================ Checks ============================
def check_state(model_dynamic_values: dict, printer_ip: str, ntfy: str) -> Optional[str]:
    # state.status: main indicator of pause/stop/halt
    prev = _prev(printer_ip)
    status = _safe(model_dynamic_values, "state", "status")
    last = prev["status"]
    if status != last and last is not None:
        if last in PRINTING_STATES and status in STOPPED_STATES:
            cause = derive_cause(model_dynamic_values, printer_ip)
            notify(
                "Print Stopped",
                f"state {last} -> {status}. Possible cause: {cause}",
                priority="urgent",
                tags="octagonal_sign,warning",
                ntfy=ntfy,
            )
        elif status == "paused" and last not in {"pausing", "paused"}:
            notify(
                "Print Paused",
                f"state {last} -> paused. Cause: {derive_cause(model_dynamic_values, printer_ip)}",
                priority="high",
                tags="pause_button",
                ntfy=ntfy,
            )
        elif status == "halted":
            notify(
                "Printer Halted",
                f"state {last} -> halted (emergency stop). Cause: {derive_cause(model_dynamic_values, printer_ip)}",
                priority="urgent",
                tags="rotating_light",
                ntfy=ntfy,
            )
        elif status == "resuming":
            notify("Print Resuming", f"state {last} -> resuming", tags="arrow_forward", ntfy=ntfy)
        elif last in (PRINTING_STATES | {"paused", "pausing"}) and status in {"idle", "off"}:
            prev["extrusion_rate_log_path"] = None
            prev["extrusion_rate_started_at"] = None
    prev["status"] = status
    return status


def check_filament(model_dynamic_values: dict, printer_ip: str, ntfy: str) -> None:
    prev = _prev(printer_ip)
    status = _safe(model_dynamic_values, "state", "status")
    time_left_filament = _safe(model_dynamic_values, "job", "timesLeft", "filament") # job.timesLeft.filament in seconds
    key_time = "filament_low_time"
    if time_left_filament is not None and time_left_filament < TIME_LEFT_FILAMENT_ALERT:
        stop_notifications(
            printer_ip,
            key_time,
            "Filament Low (based on job.timesLeft.filament)",
            f"Filament out in {time_left_filament} seconds",
            priority="urgent",
            tags="warning,scroll",
            ntfy=ntfy,
        )
    else:
        clear_problem_notification(printer_ip, key_time)

    monitors = _safe(model_dynamic_values, "sensors", "filamentMonitors", default=[]) or [] # sensors.filamentMonitors[n].status: filament out / sensor error
    
    for i, fm in enumerate(monitors):
        if not isinstance(fm, dict):
            continue
        s = fm.get("status")
        last = prev["filament"].get(i, "ok")
        key_fm = f"filament_monitor_{i}"
        if s and s != "ok":
            during_print = status in (PRINTING_STATES | {"paused", "pausing"})
            stop_notifications(
                printer_ip,
                key_fm,
                "Filament Issue" + (" (during print)" if during_print else ""),
                f"Filament monitor #{i} status: {s}",
                priority="urgent" if during_print else "high",
                tags="warning,scroll",
                ntfy=ntfy,
            )
        elif s == "ok":
            clear_problem_notification(printer_ip, key_fm)
            if last != "ok":
                notify("Filament OK", f"Filament monitor #{i} recovered", tags="white_check_mark", ntfy=ntfy)
        prev["filament"][i] = s or "unknown"


def check_heaters(model_dynamic_values: dict, printer_ip: str, ntfy: str) -> None:
    # heat.heaters[n].state: fault or offline
    prev = _prev(printer_ip)
    heaters = _safe(model_dynamic_values, "heat", "heaters", default=[]) or []
    for i, h in enumerate(heaters):
        if not isinstance(h, dict):
            continue
        st = h.get("state")
        last = prev["heaters"].get(i)
        key = f"heater_fault_{i}"
        if st in HEATER_FAULT_STATES:
            cur = h.get("current")
            act = h.get("active")
            stop_notifications(
                printer_ip,
                key,
                "Heater Fault",
                f"Heater #{i} state={st} current={cur} setpoint={act}",
                priority="urgent",
                tags="fire,rotating_light",
                ntfy=ntfy,
            )
        else:
            clear_problem_notification(printer_ip, key)
        prev["heaters"][i] = st


def check_analog_sensors(model_dynamic_values: dict, printer_ip: str, ntfy: str) -> None:
    # sensors.analog[n].state: openCircuit, shortCircuit, timeout, hardwareError, etc
    prev = _prev(printer_ip)
    sensors = _safe(model_dynamic_values, "sensors", "analog", default=[]) or []
    for i, s in enumerate(sensors):
        if not isinstance(s, dict):
            continue
        st = s.get("state")
        last = prev["analog"].get(i)
        key = f"analog_fault_{i}"
        if st and st != "ok":
            stop_notifications(
                printer_ip,
                key,
                "Temperature Sensor Fault",
                f"Analog sensor #{i} ({s.get('name', '?')}) state: {st}",
                priority="urgent",
                tags="thermometer,warning",
                ntfy=ntfy,
            )
        else:
            clear_problem_notification(printer_ip, key)
        prev["analog"][i] = st


def check_stall(model_dynamic_values: dict, printer_ip: str, ntfy: str) -> None:
    # job.filePosition + move.currentMove: detect print stuck
    prev = _prev(printer_ip)
    status = _safe(model_dynamic_values, "state", "status")
    pos = _safe(model_dynamic_values, "job", "filePosition")
    now = time.monotonic()
    if status in PRINTING_STATES and pos is not None:
        if prev["file_position"] != pos:
            prev["file_position"] = pos
            prev["file_position_changed_at"] = now
            clear_problem_notification(printer_ip, "print_stalled")
        elif (now - prev["file_position_changed_at"]) > STALL_SECONDS:
            req_speed = _safe(model_dynamic_values, "move", "currentMove", "requestedSpeed", default=0) or 0
            ext_rate = _safe(model_dynamic_values, "move", "currentMove", "extrusionRate", default=0) or 0
            stop_notifications(
                printer_ip,
                "print_stalled",
                "Print Stalled",
                f"filePosition={pos} no changes for {STALL_SECONDS}s "
                f"(status={status}, requestedSpeed={req_speed}, extrusionRate={ext_rate})",
                priority="urgent",
                tags="hourglass,warning",
                ntfy=ntfy,
            )
    else:
        prev["file_position"] = pos
        prev["file_position_changed_at"] = now
        clear_problem_notification(printer_ip, "print_stalled")


def check_board(model_dynamic_values: dict, printer_ip: str, ntfy: str) -> None:
    # boards[0].vIn.current and boards[0].mcuTemp.current
    prev = _prev(printer_ip)
    boards = _safe(model_dynamic_values, "boards", default=[]) or []
    for i, b in enumerate(boards):
        if not isinstance(b, dict):
            continue
        vin = _safe(b, "vIn", "current")
        key_vin = f"low_vin_{i}"
        if isinstance(vin, (int, float)):
            if vin < VIN_MIN:
                stop_notifications(
                    printer_ip,
                    key_vin,
                    "Low VIN",
                    f"Board #{i} VIN={vin:.2f}V (< {VIN_MIN}V)",
                    priority="urgent",
                    tags="electric_plug,warning",
                    ntfy=ntfy,
                )
            else:
                clear_problem_notification(printer_ip, key_vin)
        mcu = _safe(b, "mcuTemp", "current")
        key_mcu = f"mcu_overheat_{i}"
        if isinstance(mcu, (int, float)):
            if mcu > MCU_TEMP_MAX:
                stop_notifications(
                    printer_ip,
                    key_mcu,
                    "MCU Overheat",
                    f"Board #{i} MCU={mcu:.1f}C (> {MCU_TEMP_MAX}C)",
                    priority="urgent",
                    tags="fire,warning",
                    ntfy=ntfy,
                )
            else:
                clear_problem_notification(printer_ip, key_mcu)

def save_last_pause_warmup_rawExtrusion(model_dynamic_values: dict, printer_ip: str, ntfy: str) -> None:
    # Save the last pause duration, warmup duration, and raw extrusion values
    # This is useful to get those values when the print is finished
    prev = _prev(printer_ip)
    pause_duration = _safe(model_dynamic_values, "job", "pauseDuration")
    if pause_duration is not None:
        prev["pause_duration"] = float(pause_duration)
    warmup_duration = _safe(model_dynamic_values, "job", "warmUpDuration")
    if warmup_duration is not None:
        prev["warmup_duration"] = float(warmup_duration)

    status = _safe(model_dynamic_values, "state", "status")
    during_print = status in (PRINTING_STATES | {"paused", "pausing"})
    raw_extrusion = _safe(model_dynamic_values, "job", "rawExtrusion")
    if not during_print or raw_extrusion is None:
        return

    file_name = _safe(model_dynamic_values, "job", "file", "fileName") or prev.get("file_name")
    log_path = prev.get("extrusion_rate_log_path")
    if log_path is None:
        log_path = start_extrusion_rate_log(printer_ip, file_name)
        prev["extrusion_rate_log_path"] = log_path
        now = time.monotonic()
        prev["extrusion_rate_started_at"] = now
        prev["time_monotonic"] = now
        prev["raw_extrusion"] = float(raw_extrusion)
        save_extrusion_rate_csv(
            log_path,
            printer_ip,
            file_name,
            now,
            now,
            float(raw_extrusion),
            0.0,
        )
        return

    prev_time_monotonic = prev["time_monotonic"]
    prev_raw_extrusion = prev["raw_extrusion"]
    now = time.monotonic()
    current_raw_extrusion = float(raw_extrusion)
    if (
        prev_time_monotonic is not None
        and prev_raw_extrusion is not None
        and now > prev_time_monotonic
    ):
        extrusion_rate = (current_raw_extrusion - prev_raw_extrusion) / (now - prev_time_monotonic)
        save_extrusion_rate_csv(
            log_path,
            printer_ip,
            file_name,
            prev["extrusion_rate_started_at"],
            now,
            current_raw_extrusion,
            extrusion_rate,
        )

    prev["time_monotonic"] = now
    prev["raw_extrusion"] = current_raw_extrusion

    filament_rolls_used = current_raw_extrusion / TOTAL_FILAMENT_LENGTH
    current_filament_roll_extruded = current_raw_extrusion % TOTAL_FILAMENT_LENGTH
    current_filament_roll_left = TOTAL_FILAMENT_LENGTH - current_filament_roll_extruded
    if current_filament_roll_left < FILAMENT_USAGE_ALERT:
        stop_notifications(
            printer_ip,
            "filament_low",
            "Filament Low",
            f"(Based on job.rawExtrusion)\n"
            f"SUPPOSING WE USE NEW FILAMENT ROLLS EVERY TIME A ROLL IS FINISHED\n\n"
            f"Filament Rolls Used: {filament_rolls_used:.2f}\n"
            f"Current Filament Roll left: {current_filament_roll_left:.0f} mm < {FILAMENT_USAGE_ALERT} mm",
            priority="urgent",
            tags="warning",
            ntfy=ntfy,
        )
    

def check_static_values(model_static_values: dict, printer_ip: str, ntfy: str) -> None:
    # seqs.job increments when non-live job fields change (new file, print finished, etc.)
    # job.lastDuration is null while a job runs and becomes a float when it completes
    prev = _prev(printer_ip)
    seqs = _safe(model_static_values, "seqs", "job") or []
    if seqs is None or seqs == prev["job_seq"]:
        return
    # If seqs has changed, set value to prev["job_seq"] and check lastDuration
    prev["job_seq"] = seqs
    
    file_name = _safe(model_static_values, "job", "file", "fileName")
    if file_name is not None:
        prev["file_name"] = file_name
    last_duration = _safe(model_static_values, "job", "lastDuration")

    if last_duration is not None:
        prev["last_duration"] = float(last_duration)
        pause_duration = prev["pause_duration"]
        warmup_duration = prev["warmup_duration"]
        raw_extrusion = prev["raw_extrusion"]

        log_finish_snapshot(printer_ip, prev["last_duration"], file_name, pause_duration, warmup_duration, raw_extrusion)
        prev["extrusion_rate_log_path"] = None
        prev["extrusion_rate_started_at"] = None
        notify(
            "Print Finished",
            f"File Name => {file_name}\n"
            f"--------------------------------\n"
            f"Duration: {prev["last_duration"]}s\n"
            f"Pause Duration: {pause_duration}s\n"
            f"Warmup Duration: {warmup_duration}s\n"
            f"Raw Extrusion: {raw_extrusion}mm\n",
            priority="Urgent",
            tags="hourglass,success",
            ntfy=ntfy,
        )
    
def derive_cause(model_dynamic_values: dict, printer_ip: str) -> str:
    # Cross filament + heaters + sensors + autopause + reply to infer the cause
    reasons = []

    for i, fm in enumerate(_safe(model_dynamic_values, "sensors", "filamentMonitors", default=[]) or []):
        s = (fm or {}).get("status")
        if s and s != "ok":
            reasons.append(f"filament[{i}]={s}")

    for i, h in enumerate(_safe(model_dynamic_values, "heat", "heaters", default=[]) or []):
        st = (h or {}).get("state")
        if st in HEATER_FAULT_STATES:
            reasons.append(f"heater[{i}]={st}")

    for i, s in enumerate(_safe(model_dynamic_values, "sensors", "analog", default=[]) or []):
        st = (s or {}).get("state")
        if st and st != "ok":
            reasons.append(f"analog[{i}]={st}")

    for inp in _safe(model_dynamic_values, "inputs", default=[]) or []:
        if not isinstance(inp, dict):
            continue
        name = (inp.get("name") or "").lower()
        st = inp.get("state")
        if name == "autopause" and st not in (None, "idle"):
            reasons.append(f"autopause={st}")

    reply = get_reply(printer_ip)
    if reply:
        reasons.append(f"reply='{reply[:200]}'")

    return "; ".join(reasons) if reasons else "unknown"

def _printer_slug(printer_ip: str) -> str:
    return printer_ip.rstrip("/").split("/")[-1] or "printer"

def _safe_filename(name: str) -> str:
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name.strip() or "unknown"

def start_extrusion_rate_log(printer_ip: str, file_name: Optional[str]) -> Path:
    EXTRUSION_RATE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = _safe_filename(file_name or "unknown")
    return EXTRUSION_RATE_LOG_DIR / f"{_printer_slug(printer_ip)}_{ts}_{safe_name}.csv"

EXTRUSION_RATE_CSV_FIELDS = (
    "date_time",
    "printer_ip",
    "file_name",
    "elapsed_s",
    "raw_extrusion",
    "extrusion_rate",
)

def save_extrusion_rate_csv(
    log_path: Path,
    printer_ip: str,
    file_name: Optional[str],
    started_at: float,
    now: float,
    current_raw_extrusion: float,
    extrusion_rate: float,
) -> None:
    row = {
        "date_time": datetime.now().isoformat(timespec="seconds"),
        "printer_ip": printer_ip,
        "file_name": file_name or "",
        "elapsed_s": round(now - started_at, 1),
        "raw_extrusion": current_raw_extrusion,
        "extrusion_rate": round(extrusion_rate, 2),
    }
    write_header = not log_path.exists() or log_path.stat().st_size == 0
    with log_path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EXTRUSION_RATE_CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)

def log_om_snapshot(model_dynamic_values: dict, printer_ip: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "date_time": datetime.now().isoformat(timespec="seconds"),
        "ts": time.time(),
        "model": model_dynamic_values,
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{printer_ip}] {json.dumps(row, ensure_ascii=False)}\n")

def log_finish_snapshot(
    printer_ip: str,
    last_duration: float,
    file_name: Optional[str] = None,
    pause_duration: Optional[float] = None,
    warmup_duration: Optional[float] = None,
    raw_extrusion: Optional[float] = None,
) -> None:
    row = {
        "date_time": datetime.now().isoformat(timespec="seconds"),
        "ts": time.time(),
        "printer_ip": printer_ip,
        "duration": last_duration,
        "file_name": file_name,
        "pause_duration": pause_duration,
        "warmup_duration": warmup_duration,
        "filament_usage": raw_extrusion,
    }
    FINISH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FINISH_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{printer_ip}] {json.dumps(row, ensure_ascii=False)}\n")

def log_checked_snapshot(model_dynamic_values: dict, printer_ip: str) -> None:
    prev = _prev(printer_ip)
    row = {
        "date_time": datetime.now().isoformat(timespec="seconds"),
        "ts": time.time(),
        "status": _safe(model_dynamic_values, "state", "status"),
        "filament": {
            i: fm.get("status")
            for i, fm in enumerate(_safe(model_dynamic_values, "sensors", "filamentMonitors", default=[]) or [])
            if isinstance(fm, dict)
        },
        "heaters": {
            i: h.get("state")
            for i, h in enumerate(_safe(model_dynamic_values, "heat", "heaters", default=[]) or [])
            if isinstance(h, dict)
        },
        "analog": {
            i: s.get("state")
            for i, s in enumerate(_safe(model_dynamic_values, "sensors", "analog", default=[]) or [])
            if isinstance(s, dict)
        },
        "file_position": _safe(model_dynamic_values, "job", "filePosition"),
        "seconds_since_file_position_change": round(time.monotonic() - prev["file_position_changed_at"], 1),
        "problem_notifications": dict(_problem_notifications(printer_ip)),
    }
    CHECKED_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CHECKED_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{printer_ip}] {json.dumps(row, ensure_ascii=False)}\n")

def check_printer(model_dynamic_values: dict, model_static_values: dict, last_log_at: float, printer_ip: str, ntfy: str) -> bool:
    # Checking dynamic values
    now = time.monotonic()
    status = check_state(model_dynamic_values, printer_ip, ntfy)
    check_filament(model_dynamic_values, printer_ip, ntfy)
    check_heaters(model_dynamic_values, printer_ip, ntfy)
    check_analog_sensors(model_dynamic_values, printer_ip, ntfy)
    check_stall(model_dynamic_values, printer_ip, ntfy)
    check_board(model_dynamic_values, printer_ip, ntfy)
    save_last_pause_warmup_rawExtrusion(model_dynamic_values, printer_ip, ntfy) # To get the last pause duration, warmup duration, and raw extrusion values when the print is finished. Also, alert if the filament left is less than FILAMENT_USAGE_ALERT
    check_static_values(model_static_values, printer_ip, ntfy)
    print(f"[{printer_ip}] status={status}")
    if now - last_log_at >= LOG_INTERVAL:
        log_om_snapshot(model_dynamic_values, printer_ip)
        log_checked_snapshot(model_dynamic_values, printer_ip)
        return True
    return False

# ============================ Main loop ============================
def main() -> None:
    print(f"Starting Duet alarm monitor against {PRINTER1_IP} and {PRINTER2_IP}")
    notify(
        "Starting Duet alarm monitor",
        f"Starting Duet alarm monitor against {PRINTER1_IP} and {PRINTER2_IP}",
        priority="default",
        tags="info",
        ntfy=NTFY1,
    )
    last_log_at = time.monotonic()
    while True:
        model_dynamic_values1 = get_model(PRINTER1_IP)
        model_static_values1 = get_model_static_values(PRINTER1_IP)
        model_dynamic_values2 = get_model(PRINTER2_IP)
        model_static_values2 = get_model_static_values(PRINTER2_IP)
        logged = False
        if model_dynamic_values1 is not None:
            logged = check_printer(model_dynamic_values1, model_static_values1, last_log_at, PRINTER1_IP, NTFY1) or logged
        if model_dynamic_values2 is not None:
            logged = check_printer(model_dynamic_values2, model_static_values2, last_log_at, PRINTER2_IP, NTFY2) or logged
        if logged:
            last_log_at = time.monotonic()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
