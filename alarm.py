import json
import time
import os
from pathlib import Path
import requests
from typing import Any, Optional
from dotenv import load_dotenv

load_dotenv()

PRINTER_IP = os.getenv("PRINTER_IP")
NTFY = os.getenv("NTFY")
# Create a .env file in the root directory with the following content:
# PRINTER_IP=http://YourPrinterIP
# NTFY=https://ntfy.sh/YourNTFYTopic

# ============================ Config ============================

POLL_INTERVAL = 5          # seconds between polls
REQUEST_TIMEOUT = 10       # seconds per HTTP request
STALL_SECONDS = 60         # job.filePosition without changes -> print stuck
MCU_TEMP_MAX = 80.0        # alarm if MCU temp > X
VIN_MIN = 11.0             # alarm if VIN < X V
LOG_INTERVAL = 5 * 60      # seconds between full OM snapshots
LOG_PATH = Path(__file__).resolve().parent / "logs" / "om_snapshots.jsonl"

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


def notify(title: str, message: str, priority: str = "default", tags: str = "") -> None:
    print(f"[ALARM] {title}: {message}")
    try:
        headers = {"Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = tags
        requests.post(
            NTFY,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        print(f"[notify error] {e}")


def get_model() -> Optional[dict]:
    try:
        r = requests.get(
            f"{PRINTER_IP}/rr_model",
            params={"flags": "d99fno"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        print(f"[get_model error] {e}")
        return None


def get_reply() -> str:
    # Get the next pending reply from the firmware (M118, errors, warnings)
    try:
        r = requests.get(f"{PRINTER_IP}/rr_reply", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.text.strip()
    except Exception:
        return ""


# ====================== Previous state (edge detection) ======================
prev = {
    "status": None,
    "filament": {},                # index -> last status
    "heaters": {},                 # index -> last state
    "analog": {},                  # index -> last state
    "file_position": None,
    "file_position_changed_at": time.monotonic(),
    "stalled_alerted": False,
    "low_vin_alerted": False,
    "mcu_hot_alerted": False,
}


# ============================ Checks ============================
def check_state(m: dict) -> Optional[str]:
    # state.status: main indicator of pause/stop/halt
    status = _safe(m, "state", "status")
    last = prev["status"]
    if status != last and last is not None:
        if last in PRINTING_STATES and status in STOPPED_STATES:
            cause = derive_cause(m)
            notify(
                "Print Stopped",
                f"state {last} -> {status}. Possible cause: {cause}",
                priority="urgent",
                tags="octagonal_sign,warning",
            )
        elif status == "paused" and last not in {"pausing", "paused"}:
            notify(
                "Print Paused",
                f"state {last} -> paused. Cause: {derive_cause(m)}",
                priority="high",
                tags="pause_button",
            )
        elif status == "halted":
            notify(
                "Printer Halted",
                f"state {last} -> halted (emergency stop). Cause: {derive_cause(m)}",
                priority="urgent",
                tags="rotating_light",
            )
        elif status == "resuming":
            notify("Print Resuming", f"state {last} -> resuming", tags="arrow_forward")
    prev["status"] = status
    return status


def check_filament(m: dict) -> None:
    # sensors.filamentMonitors[n].status: filament out / sensor error
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
            )
        elif s == "ok" and last != "ok":
            notify("Filament OK", f"Filament monitor #{i} recovered", tags="white_check_mark")
        prev["filament"][i] = s or "unknown"


def check_heaters(m: dict) -> None:
    # heat.heaters[n].state: fault or offline
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
            )
        prev["heaters"][i] = st


def check_analog_sensors(m: dict) -> None:
    # sensors.analog[n].state: openCircuit, shortCircuit, timeout, hardwareError, etc
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
            )
        prev["analog"][i] = st


def check_stall(m: dict) -> None:
    # job.filePosition + move.currentMove: detect print stuck
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
            )
            prev["stalled_alerted"] = True
    else:
        prev["file_position"] = pos
        prev["file_position_changed_at"] = now
        prev["stalled_alerted"] = False


def check_board(m: dict) -> None:
    # boards[0].vIn.current and boards[0].mcuTemp.current
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
                )
                prev["mcu_hot_alerted"] = True
            elif mcu < MCU_TEMP_MAX - 5:
                prev["mcu_hot_alerted"] = False


def derive_cause(m: dict) -> str:
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

    reply = get_reply()
    if reply:
        reasons.append(f"reply='{reply[:200]}'")

    return "; ".join(reasons) if reasons else "unknown"


def log_om_snapshot(m: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "model": m}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ============================ Main loop ============================
def main() -> None:
    print(f"Starting Duet alarm monitor against {PRINTER_IP}")
    last_log_at = 0.0
    while True:
        m = get_model()
        if m is not None:
            status = check_state(m)
            check_filament(m)
            check_heaters(m)
            check_analog_sensors(m)
            check_stall(m)
            check_board(m)
            now = time.monotonic()
            if now - last_log_at >= LOG_INTERVAL:
                log_om_snapshot(m)
                last_log_at = now
                print(f"[log] OM snapshot -> {LOG_PATH}")
            t = _safe(m, "state", "time")
            print(f"[{t}] status={status}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
