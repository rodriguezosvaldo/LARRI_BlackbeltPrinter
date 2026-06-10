import json
import time
import os
from pathlib import Path
import requests
from typing import Any, Optional
from dotenv import load_dotenv

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

POLL_INTERVAL = 5          # seconds between polls
REQUEST_TIMEOUT = 10       # seconds per HTTP request
STALL_SECONDS = 60         # job.filePosition without changes -> print stuck
MCU_TEMP_MAX = 80.0        # alarm if MCU temp > X
VIN_MIN = 11.0             # alarm if VIN < X V
LOG_INTERVAL = 1 * 60      # seconds between periodic logs
LOG_PATH = Path(__file__).resolve().parent / "logs" / "om_snapshots.jsonl"
CHECKED_LOG_PATH = Path(__file__).resolve().parent / "logs" / "checked_values.jsonl"
FINISH_LOG_PATH = Path(__file__).resolve().parent / "logs" / "finish_snapshot.jsonl"

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

def get_model_key(printer_ip: str, key: str) -> Any:
    try:
        r = requests.get(
            f"{printer_ip}/rr_model",
            params={"key": key},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        print(f"[get_model_key error] {key}: {e}")
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
        "status": None,
        "filament": {},                # index -> last status
        "heaters": {},                 # index -> last state
        "analog": {},                  # index -> last state
        "file_position": None,
        "file_position_changed_at": time.monotonic(),
        "stalled_alerted": False,
        "low_vin_alerted": False,
        "mcu_hot_alerted": False,
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
def check_state(m: dict, printer_ip: str, ntfy: str) -> Optional[str]:
    # state.status: main indicator of pause/stop/halt
    prev = _prev(printer_ip)
    status = _safe(m, "state", "status")
    last = prev["status"]
    if status != last and last is not None:
        if last in PRINTING_STATES and status in STOPPED_STATES:
            cause = derive_cause(m, printer_ip)
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
                f"state {last} -> paused. Cause: {derive_cause(m, printer_ip)}",
                priority="high",
                tags="pause_button",
                ntfy=ntfy,
            )
        elif status == "halted":
            notify(
                "Printer Halted",
                f"state {last} -> halted (emergency stop). Cause: {derive_cause(m, printer_ip)}",
                priority="urgent",
                tags="rotating_light",
                ntfy=ntfy,
            )
        elif status == "resuming":
            notify("Print Resuming", f"state {last} -> resuming", tags="arrow_forward", ntfy=ntfy)
    prev["status"] = status
    return status


def check_filament(m: dict, printer_ip: str, ntfy: str) -> None:
    # sensors.filamentMonitors[n].status: filament out / sensor error
    prev = _prev(printer_ip)
    monitors = _safe(m, "sensors", "filamentMonitors", default=[]) or []
    status = _safe(m, "state", "status")
    for i, fm in enumerate(monitors):
        if not isinstance(fm, dict):
            continue
        s = fm.get("status")
        last = prev["filament"].get(i, "ok")
        if s and s != "ok" and last == "ok":
            during_print = status in (PRINTING_STATES | {"paused", "pausing"})
            notify(
                "Filament Issue" + (" (during print)" if during_print else ""),
                f"Filament monitor #{i} status: {s}",
                priority="urgent" if during_print else "high",
                tags="warning,scroll",
                ntfy=ntfy,
            )
        elif s == "ok" and last != "ok":
            notify("Filament OK", f"Filament monitor #{i} recovered", tags="white_check_mark", ntfy=ntfy)
        prev["filament"][i] = s or "unknown"


def check_heaters(m: dict, printer_ip: str, ntfy: str) -> None:
    # heat.heaters[n].state: fault or offline
    prev = _prev(printer_ip)
    heaters = _safe(m, "heat", "heaters", default=[]) or []
    for i, h in enumerate(heaters):
        if not isinstance(h, dict):
            continue
        st = h.get("state")
        last = prev["heaters"].get(i)
        if st in HEATER_FAULT_STATES and last not in HEATER_FAULT_STATES:
            cur = h.get("current")
            act = h.get("active")
            notify(
                "Heater Fault",
                f"Heater #{i} state={st} current={cur} setpoint={act}",
                priority="urgent",
                tags="fire,rotating_light",
                ntfy=ntfy,
            )
        prev["heaters"][i] = st


def check_analog_sensors(m: dict, printer_ip: str, ntfy: str) -> None:
    # sensors.analog[n].state: openCircuit, shortCircuit, timeout, hardwareError, etc
    prev = _prev(printer_ip)
    sensors = _safe(m, "sensors", "analog", default=[]) or []
    for i, s in enumerate(sensors):
        if not isinstance(s, dict):
            continue
        st = s.get("state")
        last = prev["analog"].get(i)
        if st and st != "ok" and last in (None, "ok"):
            notify(
                "Temperature Sensor Fault",
                f"Analog sensor #{i} ({s.get('name', '?')}) state: {st}",
                priority="urgent",
                tags="thermometer,warning",
                ntfy=ntfy,
            )
        prev["analog"][i] = st


def check_stall(m: dict, printer_ip: str, ntfy: str) -> None:
    # job.filePosition + move.currentMove: detect print stuck
    prev = _prev(printer_ip)
    status = _safe(m, "state", "status")
    pos = _safe(m, "job", "filePosition")
    now = time.monotonic()
    if status in PRINTING_STATES and pos is not None:
        if prev["file_position"] != pos:
            prev["file_position"] = pos
            prev["file_position_changed_at"] = now
            prev["stalled_alerted"] = False
        elif (now - prev["file_position_changed_at"]) > STALL_SECONDS and not prev["stalled_alerted"]:
            req_speed = _safe(m, "move", "currentMove", "requestedSpeed", default=0) or 0
            ext_rate = _safe(m, "move", "currentMove", "extrusionRate", default=0) or 0
            notify(
                "Print Stalled",
                f"filePosition={pos} no changes for {STALL_SECONDS}s "
                f"(status={status}, requestedSpeed={req_speed}, extrusionRate={ext_rate})",
                priority="urgent",
                tags="hourglass,warning",
                ntfy=ntfy,
            )
            prev["stalled_alerted"] = True
    else:
        prev["file_position"] = pos
        prev["file_position_changed_at"] = now
        prev["stalled_alerted"] = False


def check_board(m: dict, printer_ip: str, ntfy: str) -> None:
    # boards[0].vIn.current and boards[0].mcuTemp.current
    prev = _prev(printer_ip)
    boards = _safe(m, "boards", default=[]) or []
    for i, b in enumerate(boards):
        if not isinstance(b, dict):
            continue
        vin = _safe(b, "vIn", "current")
        if isinstance(vin, (int, float)):
            if vin < VIN_MIN and not prev["low_vin_alerted"]:
                notify(
                    "Low VIN",
                    f"Board #{i} VIN={vin:.2f}V (< {VIN_MIN}V)",
                    priority="urgent",
                    tags="electric_plug,warning",
                    ntfy=ntfy,
                )
                prev["low_vin_alerted"] = True
            elif vin >= VIN_MIN + 0.5:
                prev["low_vin_alerted"] = False
        mcu = _safe(b, "mcuTemp", "current")
        if isinstance(mcu, (int, float)):
            if mcu > MCU_TEMP_MAX and not prev["mcu_hot_alerted"]:
                notify(
                    "MCU Overheat",
                    f"Board #{i} MCU={mcu:.1f}C (> {MCU_TEMP_MAX}C)",
                    priority="urgent",
                    tags="fire,warning",
                    ntfy=ntfy,
                )
                prev["mcu_hot_alerted"] = True
            elif mcu < MCU_TEMP_MAX - 5:
                prev["mcu_hot_alerted"] = False

def sync_job_static_fields(m: dict, printer_ip: str, ntfy: str) -> None:
    # seqs.job increments when non-live job fields change (new file, print finished, etc.)
    # job.lastDuration is null while a job runs and becomes a float when it completes
    prev = _prev(printer_ip)
    job_seq = _safe(m, "seqs", "job")
    if job_seq is None or job_seq == prev["job_seq"]:
        return

    last_seq = prev["job_seq"]
    prev["job_seq"] = job_seq

    file_name = get_model_key(printer_ip, "job.file.fileName")
    if isinstance(file_name, str):
        prev["file_name"] = file_name

    raw_duration = get_model_key(printer_ip, "job.lastDuration")
    old_duration = prev["last_duration"]
    if isinstance(raw_duration, (int, float)):
        new_duration = float(raw_duration)
        prev["last_duration"] = new_duration
        if last_seq is not None and new_duration != old_duration:
            check_job_finished(m, printer_ip, ntfy, new_duration, prev.get("file_name"))

    else:
        prev["last_duration"] = None



def check_job_finished(
    m: dict,
    printer_ip: str,
    ntfy: str,
    last_duration: float,
    file_name: Optional[str],
) -> None:
    name_part = f" file={file_name}" if file_name else ""
    notify(
        "Print Finished",
        f"Duration={last_duration}s{name_part}",
        priority="high",
        tags="hourglass,success",
        ntfy=ntfy,
    )
    log_om_snapshot(m, printer_ip)
    log_finish_snapshot(m, printer_ip, last_duration, file_name)

def derive_cause(m: dict, printer_ip: str) -> str:
    # Cross filament + heaters + sensors + autopause + reply to infer the cause
    reasons = []

    for i, fm in enumerate(_safe(m, "sensors", "filamentMonitors", default=[]) or []):
        s = (fm or {}).get("status")
        if s and s != "ok":
            reasons.append(f"filament[{i}]={s}")

    for i, h in enumerate(_safe(m, "heat", "heaters", default=[]) or []):
        st = (h or {}).get("state")
        if st in HEATER_FAULT_STATES:
            reasons.append(f"heater[{i}]={st}")

    for i, s in enumerate(_safe(m, "sensors", "analog", default=[]) or []):
        st = (s or {}).get("state")
        if st and st != "ok":
            reasons.append(f"analog[{i}]={st}")

    for inp in _safe(m, "inputs", default=[]) or []:
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


def log_om_snapshot(m: dict, printer_ip: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "model": m}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{printer_ip}] {json.dumps(row, ensure_ascii=False)}\n")

def log_finish_snapshot(
    m: dict,
    printer_ip: str,
    last_duration: float,
    file_name: Optional[str] = None,
) -> None:
    row = {
        "ts": time.time(),
        "printer_ip": printer_ip,
        "duration": last_duration,
        "file_name": file_name,
        "paused_duration": {
            i: fm.get("duration")
            for i, fm in enumerate(_safe(m, "job", "pauseDuration", default=[]) or [])
            if isinstance(fm, dict)
        },
        "warmup_duration": {
            i: fm.get("duration")
            for i, fm in enumerate(_safe(m, "job", "warmUpDuration", default=[]) or [])
            if isinstance(fm, dict)
        },
        "filament_usage": {
            i: fm.get("usage")
            for i, fm in enumerate(_safe(m, "job", "rawExtrusion", default=[]) or [])
            if isinstance(fm, dict)
        }
    }
    FINISH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FINISH_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{printer_ip}] {json.dumps(row, ensure_ascii=False)}\n")

def log_checked_snapshot(m: dict, printer_ip: str) -> None:
    prev = _prev(printer_ip)
    row = {
        "ts": time.time(),
        "status": _safe(m, "state", "status"),
        "filament": {
            i: fm.get("status")
            for i, fm in enumerate(_safe(m, "sensors", "filamentMonitors", default=[]) or [])
            if isinstance(fm, dict)
        },
        "heaters": {
            i: h.get("state")
            for i, h in enumerate(_safe(m, "heat", "heaters", default=[]) or [])
            if isinstance(h, dict)
        },
        "analog": {
            i: s.get("state")
            for i, s in enumerate(_safe(m, "sensors", "analog", default=[]) or [])
            if isinstance(s, dict)
        },
        "file_position": _safe(m, "job", "filePosition"),
        "seconds_since_file_position_change": round(time.monotonic() - prev["file_position_changed_at"], 1),
        "stalled_alerted": prev["stalled_alerted"],
        "low_vin_alerted": prev["low_vin_alerted"],
        "mcu_hot_alerted": prev["mcu_hot_alerted"],
    }
    CHECKED_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CHECKED_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{printer_ip}] {json.dumps(row, ensure_ascii=False)}\n")

def check_printer(m: dict, last_log_at: float, printer_ip: str, ntfy: str) -> bool:
    sync_job_static_fields(m, printer_ip, ntfy)
    status = check_state(m, printer_ip, ntfy)
    check_filament(m, printer_ip, ntfy)
    check_heaters(m, printer_ip, ntfy)
    check_analog_sensors(m, printer_ip, ntfy)
    check_stall(m, printer_ip, ntfy)
    check_board(m, printer_ip, ntfy)
    now = time.monotonic()
    print(f"[{printer_ip}] status={status}")
    if now - last_log_at >= LOG_INTERVAL:
        log_om_snapshot(m, printer_ip)
        log_checked_snapshot(m, printer_ip)
        print(f"[{printer_ip}] [log] OM snapshot -> {LOG_PATH}")
        print(f"[{printer_ip}] [log] checked values -> {CHECKED_LOG_PATH}")
        return True
    return False

# ============================ Main loop ============================
def main() -> None:
    print(f"Starting Duet alarm monitor against {PRINTER1_IP} and {PRINTER2_IP}")
    last_log_at = time.monotonic()
    while True:
        m1 = get_model(PRINTER1_IP)
        m2 = get_model(PRINTER2_IP)
        logged = False
        if m1 is not None:
            logged = check_printer(m1, last_log_at, PRINTER1_IP, NTFY1) or logged
        if m2 is not None:
            logged = check_printer(m2, last_log_at, PRINTER2_IP, NTFY2) or logged
        if logged:
            last_log_at = time.monotonic()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
