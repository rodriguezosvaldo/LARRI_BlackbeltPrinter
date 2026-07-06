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
FILAMENT_LEFT_ALERT = 10000 # mm
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
    },
    "printer2": {
        "ip": PRINTER2_IP,
        "label": "",
        "state_status": None,
        "raw_extrusion": None,
        "raw_extrusion_reference": 0,
    },
}

REQUEST_REPLY = 0.5        # monitor seqs.reply, if changes get rr_reply which value is updated after ~1 seconds. So we need to check it every 0.5s
MESSAGE_START_PRINTING = "selected for printing"
MESSAGE_END_PRINTING = "printing finished" # This value is not checked in real reply

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
    
# ============================ Helpers ============================
def _printer_slug(printer_ip: str) -> str:
    return printer_ip.rstrip("/").split("/")[-1] or "printer"


def _printer_ui_key(printer_ip: str) -> str:
    if printer_ip == PRINTER1_IP:
        return "printer1"
    return "printer2"


def _init_ui_state() -> None:
    with ui_lock:
        for key, ip in (("printer1", PRINTER1_IP), ("printer2", PRINTER2_IP)):
            ui_state[key]["ip"] = ip
            ui_state[key]["label"] = _printer_slug(ip) if ip else key


def _update_ui(printer_ip: str, *, state_status=None, raw_extrusion=None) -> None:
    key = _printer_ui_key(printer_ip)
    ref = raw_extrusion_reference1 if key == "printer1" else raw_extrusion_reference2
    with ui_lock:
        if state_status is not None:
            ui_state[key]["state_status"] = state_status
        if raw_extrusion is not None:
            ui_state[key]["raw_extrusion"] = raw_extrusion
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
                notify(
                    "New M118 Message",
                    f"{_printer_slug(printer_ip)}\n"
                    f"Message: {reply}",
                    tags="warning",
                    ntfy=ntfy,
                )
                save_last_reply(printer_ip, reply)
            prev["seqs_reply"] = seqs_reply
            print(f"{_printer_slug(printer_ip)}_seqs_reply: {seqs_reply}") # Debug
    else:
        print(f"{_printer_slug(printer_ip)}_seqs_reply: None") # Debug

def check_raw_extrusion(printer_ip: str, ntfy: str, raw_extrusion_reference: float) -> None:
    raw_extrusion = get_raw_extrusion(printer_ip)
    _update_ui(printer_ip, raw_extrusion=raw_extrusion)
    print(f"{_printer_slug(printer_ip)}_raw_extrusion: {raw_extrusion}") # Debug
    print(f"{_printer_slug(printer_ip)}_raw_extrusion_reference: {raw_extrusion_reference}") # Debug
    if raw_extrusion is not None:
        extrusion_from_reference = raw_extrusion - raw_extrusion_reference
        filament_left = TOTAL_FILAMENT_LENGTH - extrusion_from_reference
        if filament_left < FILAMENT_LEFT_ALERT:
            notify(
                "Filament Low",
                f"{_printer_slug(printer_ip)}\n"
                f"Filament Consumed: {extrusion_from_reference} mm\n"
                f"Filament Left: {filament_left} mm",
                tags="warning",
                ntfy=ntfy,
            )

def check_state_status(printer_ip: str) -> str:
    state_status = get_state_status(printer_ip)
    _update_ui(printer_ip, state_status=state_status)
    print(f"{_printer_slug(printer_ip)}_state_status: {state_status}") # Debug
    return state_status


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
    while True:
        check_reply(PRINTER1_IP, NTFY1)
        check_raw_extrusion(PRINTER1_IP, NTFY1, raw_extrusion_reference1)
        check_state_status(PRINTER1_IP)

        check_reply(PRINTER2_IP, NTFY2)
        check_raw_extrusion(PRINTER2_IP, NTFY2, raw_extrusion_reference2)
        check_state_status(PRINTER2_IP)

        with ui_lock:
            ui_state["date_time"] = datetime.now().strftime('%Y-%m-%d  %H:%M:%S')
        
        time.sleep(REQUEST_REPLY)

if __name__ == "__main__":
    main()











