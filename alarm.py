import os
import threading
from dotenv import load_dotenv
from pathlib import Path
import requests
import time
from datetime import datetime

from flask import Flask, jsonify, send_from_directory

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

TOTAL_FILAMENT_LENGTH = 1498000 # mm - New filanent (4kg)
FILAMENT_LEFT_ALERT = 20000 # mm - 20 meters
raw_extrusion_reference1 = 0 # mm
raw_extrusion_reference2 = 0 # mm

WEB_HOST = "0.0.0.0"
WEB_PORT = 5050

ui_lock = threading.Lock()
ui_state = {
    "date_time": None,
    "printer1": {
        "ip": PRINTER1_IP,
        "label": "",
        "state_status": None,
        "raw_extrusion": None,
        "raw_extrusion_reference": 0,
        "layer": None,
    },
    "printer2": {
        "ip": PRINTER2_IP,
        "label": "",
        "state_status": None,
        "raw_extrusion": None,
        "raw_extrusion_reference": 0,
        "layer": None,
    },
}

REQUEST_REPLY = 0.5        # monitor seqs.reply, if changes get rr_reply which value is updated after ~1 seconds. So we need to check it every 0.5s
REQUEST_TEN_SECONDS = 10   # check every 10 seconds
MAX_PROBLEM_NOTIFICATIONS = 2  # normal alerts before the final "no more" message

MESSAGE_START_PRINTING = "selected for printing"
MESSAGE_END_PRINTING = "printing finished" # This value is not checked in real reply

# See: https://github.com/Duet3D/RepRapFirmware/wiki/Object-Model-Documentation
PRINTING_STATES = {"processing", "simulating"}
STOPPED_STATES = {"paused", "pausing", "halted", "cancelling", "off", "idle"}

LOG_DIR = Path(__file__).resolve().parent / "logs"


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


def _problem_notifications(printer_ip: str) -> dict:
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
            f"{message}\n\n"
            "This will be the last notification, but the problem is very likely to continue.",
            priority=priority,
            tags=tags,
            ntfy=ntfy,
        )
        counts[problem_key] = "suppressed"
        return True

    return False


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

def get_raw_extrusion(printer_ip: str) -> float:
    try:
        r = requests.get(
            f"{printer_ip}/rr_model", 
            params={"key": "job.rawExtrusion", "flags": "d99nfo"}, 
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        print(f"[get_raw_extrusion error] {printer_ip}: {e}")
        return None

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


def _printer_ui_key(printer_ip: str) -> str:
    if printer_ip == PRINTER1_IP:
        return "printer1"
    return "printer2"


_UI_UNSET = object()


def _init_ui_state() -> None:
    with ui_lock:
        for key, ip in (("printer1", PRINTER1_IP), ("printer2", PRINTER2_IP)):
            ui_state[key]["ip"] = ip
            ui_state[key]["label"] = _printer_slug(ip) if ip else key


def _update_ui(printer_ip: str, *, state_status=None, raw_extrusion=None, layer=_UI_UNSET) -> None:
    key = _printer_ui_key(printer_ip)
    ref = raw_extrusion_reference1 if key == "printer1" else raw_extrusion_reference2
    with ui_lock:
        if state_status is not None:
            ui_state[key]["state_status"] = state_status
        if raw_extrusion is not None:
            ui_state[key]["raw_extrusion"] = raw_extrusion
        if layer is not _UI_UNSET:
            ui_state[key]["layer"] = layer
        ui_state[key]["raw_extrusion_reference"] = ref


def reset_raw_extrusion_reference(printer_num: int) -> bool:
    global raw_extrusion_reference1, raw_extrusion_reference2
    printer_ip = PRINTER1_IP if printer_num == 1 else PRINTER2_IP
    current = get_raw_extrusion(printer_ip)
    if current is None:
        return False
    if printer_num == 1:
        raw_extrusion_reference1 = current
    else:
        raw_extrusion_reference2 = current
    _update_ui(printer_ip, raw_extrusion=current)
    return True

# ====================== Previous state (edge detection) ======================
def _default_prev() -> dict:
    return {
        # Static values
        "job_seq": None,
        "seqs_reply": None,
        # Dynamic values
        "status": None,
        "layer": None,
    }

prev_by_printer: dict[str, dict] = {}

def _prev(printer_ip: str) -> dict:
    if printer_ip not in prev_by_printer:
        prev_by_printer[printer_ip] = _default_prev()
    return prev_by_printer[printer_ip]

# ============================ Save Last Reply ============================
def save_last_reply(printer_ip: str, reply: str) -> None:
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
                stop_notifications(
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

def check_raw_extrusion(printer_ip: str, ntfy: str, raw_extrusion_reference: float) -> None:
    raw_extrusion = get_raw_extrusion(printer_ip)
    _update_ui(printer_ip, raw_extrusion=raw_extrusion)
    if raw_extrusion is not None:
        if raw_extrusion > TOTAL_FILAMENT_LENGTH and raw_extrusion_reference == 0:
            raw_extrusion = raw_extrusion - TOTAL_FILAMENT_LENGTH
        extrusion_from_reference = raw_extrusion - raw_extrusion_reference
        filament_left = TOTAL_FILAMENT_LENGTH - extrusion_from_reference
        key_filament = "filament_low"
        if filament_left < FILAMENT_LEFT_ALERT:
            stop_notifications(
                printer_ip,
                key_filament,
                "Filament Low",
                f"{_printer_slug(printer_ip)}\n"
                f"Filament Consumed: {extrusion_from_reference:.2f} mm\n"
                f"Filament Left: {filament_left:.2f} mm",
                tags="warning",
                ntfy=ntfy,
            )
        else:
            clear_problem_notification(printer_ip, key_filament)

def check_state_status(printer_ip: str, ntfy: str) -> str:
    # state.status: main indicator of pause/stop/halt (edge detection)
    prev = _prev(printer_ip)
    status = get_state_status(printer_ip)
    _update_ui(printer_ip, state_status=status)
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

def check_layer(printer_ip: str) -> None:
    prev = _prev(printer_ip)
    layer = get_layer(printer_ip)
   
    prev["layer"] = layer
    _update_ui(printer_ip, layer=layer)


# ============================ Web UI ============================
STATIC_DIR = Path(__file__).resolve().parent / "static"
app = Flask(__name__, static_folder=str(STATIC_DIR))


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/status")
def api_status():
    with ui_lock:
        return jsonify(ui_state)


@app.post("/api/reset-reference/<int:printer_id>")
def api_reset_reference(printer_id: int):
    if printer_id not in (1, 2):
        return jsonify({"ok": False, "error": "printer_id must be 1 or 2"}), 400
    ok = reset_raw_extrusion_reference(printer_id)
    if not ok:
        return jsonify({"ok": False, "error": "could not read raw extrusion"}), 502
    return jsonify({"ok": True})


def _run_web_server() -> None:
    app.run(host=WEB_HOST, port=WEB_PORT, threaded=True, use_reloader=False)


# ============================ Main ============================
def main() -> None:
    _init_ui_state()
    web_thread = threading.Thread(target=_run_web_server, daemon=True)
    web_thread.start()
    print(f"Web UI: http://localhost:{WEB_PORT}")
    print(f"Checking {PRINTER1_IP} and {PRINTER2_IP}...")
    def loop_check_seqs_reply() -> None:
        while True:
            check_reply(PRINTER1_IP, NTFY1)
            check_reply(PRINTER2_IP, NTFY2)
            time.sleep(REQUEST_REPLY)

    def loop_check_extrusion_and_state() -> None:
        while True:
            check_raw_extrusion(PRINTER1_IP, NTFY1, raw_extrusion_reference1)
            check_layer(PRINTER1_IP)
            check_state_status(PRINTER1_IP, NTFY1)

            check_raw_extrusion(PRINTER2_IP, NTFY2, raw_extrusion_reference2)
            check_layer(PRINTER2_IP)
            check_state_status(PRINTER2_IP, NTFY2)

            with ui_lock:
                ui_state["date_time"] = datetime.now().strftime('%Y-%m-%d  %H:%M:%S')

            time.sleep(REQUEST_TEN_SECONDS)
        

    seqs_thread = threading.Thread(target=loop_check_seqs_reply, name="seqs-reply")
    extrusion_and_state_thread = threading.Thread(target=loop_check_extrusion_and_state, name="extrusion-and-state")
    seqs_thread.start()
    extrusion_and_state_thread.start()
    seqs_thread.join()
    extrusion_and_state_thread.join()
if __name__ == "__main__":
    main()











