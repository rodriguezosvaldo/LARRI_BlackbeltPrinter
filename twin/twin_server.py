"""Digital twin of the two BlackBelt printers (read only). Standard library only.

    python twin/twin_server.py     -> http://localhost:5053 (TWIN_PORT to change it)

Polls each Duet (rr_model) and each ESP32 (/api/spools) and serves twin/web/:
    GET /api/state           both printers: job, % done, Z, spools, piece status
    GET /api/piece/<p1|p2>   the current job's piece (outer-wall toolpath from the G-code, see gcode.py)
    GET /models/<file>       STL files from twin/models/ (shown instead of the toolpath when one matches)

The G-code of a new job is downloaded once from the Duet (rr_download, ~2 min for 30 MB) and the
reduced piece is cached in twin/cache/. Nothing is ever sent to the printers besides these GETs.
"""

import gzip
import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import gcode

PORT = int(os.getenv("TWIN_PORT", "5053"))
PRINTERS = {
    "p1": {
        "label": "Printer 1 (243)",
        "duet": os.getenv("TWIN_P1_DUET", "http://192.168.1.243"),
        "esp": os.getenv("TWIN_P1_ESP", "http://192.168.1.132"),  # = P1_IP in calibration.py
    },
    "p2": {
        "label": "Printer 2 (188)",
        "duet": os.getenv("TWIN_P2_DUET", "http://192.168.1.188"),
        "esp": os.getenv("TWIN_P2_ESP", "http://192.168.1.226"),  # = P2_IP in calibration.py
    },
}
DUET_POLL_S = 2  # the ESP32 also polls the Duet: don't go much lower
ESP_POLL_S = 5
PIECE_RETRY_S = 60

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
MODEL_DIR = ROOT / "models"
CACHE_DIR = ROOT / "cache"  # gitignored
# When each cell's empty/full/ref_rate last changed (calibration or /config), as seen by this
# server: the ESP32s don't keep a date. gitignored.
CELL_LOG = ROOT / "cell_updates.json"
CELL_FIELDS = ("empty", "full", "ref_rate")
SAFE_NAME = re.compile(r"^[\w .()+-]+\.(stl|json)$", re.IGNORECASE)
STATIC_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}

lock = threading.Lock()
download_lock = threading.Lock()  # one G-code download at a time (two at once got reset by a Duet)
state: dict = {}  # printer key -> state dict for /api/state
pieces: dict = {}  # printer key -> gzipped JSON of the current piece
cell_log: dict = {}  # "p1/cell1" -> {"values": {...}, "updated": unix | None, "since": unix}
cell_log_lock = threading.Lock()


def load_cell_log() -> None:
    global cell_log
    try:
        cell_log = json.loads(CELL_LOG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cell_log = {}
    except (OSError, ValueError) as e:
        print(f"[cells] can't read {CELL_LOG.name}: {e}")
        cell_log = {}


def track_cell(key: str, name: str, cell: dict) -> dict:
    """Records when the cell's calibration values change; returns {updated, since}."""
    values = {k: cell.get(k) for k in CELL_FIELDS}
    now = time.time()
    with cell_log_lock:
        entry = cell_log.get(f"{key}/{name}")
        if entry is None:  # first time seen: the last change happened before now, date unknown
            entry = {"values": values, "updated": None, "since": now}
        elif all(entry["values"].get(k) == values[k] for k in CELL_FIELDS):
            return {"updated": entry["updated"], "since": entry["since"]}
        else:
            changed = [k for k in CELL_FIELDS if entry["values"].get(k) != values[k]]
            print(f"[{key}] {name} updated: " +
                  ", ".join(f"{k} {entry['values'].get(k)} -> {values[k]}" for k in changed))
            entry = {"values": values, "updated": now, "since": entry["since"]}
        cell_log[f"{key}/{name}"] = entry
        try:
            CELL_LOG.write_text(json.dumps(cell_log, indent=1), encoding="utf-8")
        except OSError as e:
            print(f"[cells] can't write {CELL_LOG.name}: {e}")
        return {"updated": entry["updated"], "since": entry["since"]}


def get_json(url: str, timeout: float = 4) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def duet_model(base: str, key: str):
    return get_json(f"{base}/rr_model?key={key}&flags=d99vno")["result"]


# ============================ Duet polling ============================
def job_view(job: dict) -> dict | None:
    f = job.get("file") or {}
    path = f.get("fileName")
    if not path:
        return None
    size = f.get("size") or 0
    pos = job.get("filePosition") or 0
    total_mm = sum(x for x in (f.get("filament") or []) if x) or None
    used_mm = job.get("rawExtrusion")
    file_pct = round(pos / size * 100, 2) if size else None
    # Same as DWC: progress by filament used when the slicer gave the total, else by file position
    if total_mm and used_mm is not None:
        percent, source = round(min(used_mm / total_mm, 1) * 100, 2), "filament"
    else:
        percent, source = file_pct, "file"
    return {
        "path": path,
        "file": path.rsplit("/", 1)[-1],
        "size": size,
        "last_modified": f.get("lastModified"),
        "percent": percent,
        "percent_source": source,
        "layer": job.get("layer"),
        "duration": job.get("duration"),
        "time_left": (job.get("timesLeft") or {}).get("filament"),  # same basis as percent
        "filament_total_mm": total_mm,
        "filament_used_mm": used_mm,
        "objects": [o.get("name") for o in ((job.get("build") or {}).get("objects") or []) if o],
    }


def poll_duet(key: str) -> None:
    base = PRINTERS[key]["duet"]
    while True:
        upd: dict = {}
        try:
            upd["status"] = duet_model(base, "state.status")
            upd["job"] = job_view(duet_model(base, "job"))
            axes = duet_model(base, "move.axes")
            upd["z"] = next((a.get("machinePosition") for a in axes if a.get("letter") == "Z"), None)
            upd["online"], upd["error"] = True, None
            upd["last_seen"] = time.time()
        except (OSError, ValueError, KeyError, TypeError) as e:
            upd = {"online": False, "error": str(e)}
        with lock:
            state[key].update(upd)
            job = state[key].get("job") if state[key]["online"] else None
        if upd.get("online"):
            update_piece(key, job)
        time.sleep(DUET_POLL_S)


def poll_esp(key: str) -> None:
    base = PRINTERS[key]["esp"]
    while True:
        try:
            p = get_json(f"{base}/api/spools")["printers"][key]
            cells = {
                name: {k: c.get(k) for k in ("percent", "hours_left_live", "hours_left_ref",
                                             "consuming", "note", "current", "full", "empty",
                                             "live_rate", "live_span", "ref_rate")}
                for name, c in (p.get("cells") or {}).items()
            }
            for name, c in cells.items():
                c["config_updated"] = track_cell(key, name, c)
            upd = {"spools": cells, "spools_online": bool(p.get("online")),
                   "spools_error": p.get("error")}
        except (OSError, ValueError, KeyError, TypeError) as e:
            upd = {"spools_online": False, "spools_error": str(e)}
        with lock:
            state[key].update(upd)
        time.sleep(ESP_POLL_S)


# ============================ Piece (G-code / STL) ============================
def find_model(job: dict) -> tuple[str | None, dict]:
    """STL in twin/models/ named like a build object (Cura mesh name) or like the G-code file."""
    if not MODEL_DIR.is_dir():
        return None, {}
    stls = {p.stem.lower(): p for p in MODEL_DIR.iterdir() if p.suffix.lower() == ".stl"}
    names = [Path(o).stem for o in job.get("objects") or []] + [Path(job["file"]).stem]
    for n in names:
        p = stls.get(n.lower())
        if p:
            cfg_path = p.with_suffix(".json")
            cfg = {}
            if cfg_path.is_file():
                try:
                    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as e:
                    print(f"[model] bad {cfg_path.name}: {e}")
            return p.name, cfg
    return None, {}


def piece_id(job: dict) -> str:
    raw = f"{job['path']}|{job['size']}|{job.get('last_modified')}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def set_piece(key: str, **kw) -> None:
    with lock:
        state[key]["piece"].update(kw)


def download_piece(key: str, job: dict, pid: str, cache: Path) -> bytes:
    """Stream the job's G-code from the Duet through gcode.Parser; returns the gzipped piece."""
    with download_lock:
        set_piece(key, progress=0)
        url = f"{PRINTERS[key]['duet']}/rr_download?name={urllib.parse.quote(job['path'])}"
        print(f"[{key}] downloading {job['file']} ({job['size'] / 1e6:.1f} MB)")
        size = job["size"] or 1
        p = gcode.Parser()
        read = 0
        with urllib.request.urlopen(url, timeout=30) as r:
            for i, raw in enumerate(r):
                read += len(raw)
                p.line(raw.decode("utf-8", "replace"))
                if i % 5000 == 0:
                    set_piece(key, progress=round(min(read / size, 1) * 100, 1))
    if read < size * 0.99:
        raise OSError(f"download incomplete ({read} of {size} bytes)")
    res = p.result()
    res["id"] = pid
    data = gzip.compress(json.dumps(res, separators=(",", ":")).encode())
    CACHE_DIR.mkdir(exist_ok=True)
    cache.write_bytes(data)
    print(f"[{key}] piece ready: {len(res['runs'])} runs, {len(data) / 1e6:.1f} MB gz")
    return data


def load_piece(key: str, job: dict, pid: str) -> None:
    cache = CACHE_DIR / f"{pid}.json.gz"
    try:
        data = cache.read_bytes() if cache.is_file() else download_piece(key, job, pid, cache)
        res = json.loads(gzip.decompress(data))
        with lock:
            pieces[key] = data
        set_piece(key, status="ready", progress=100, error=None, z_min=res["z_min"],
                  z_max=res["z_max"], layer_count=res["layer_count"],
                  gantry_angle=res["gantry_angle"], extruded_mm=res["extruded_mm"])
    except (OSError, ValueError, KeyError) as e:
        print(f"[{key}] piece error: {e}")
        set_piece(key, status="error", error=str(e), retry_at=time.time() + PIECE_RETRY_S)


def update_piece(key: str, job: dict | None) -> None:
    """Called after every Duet poll: (re)load the piece when the job changes."""
    with lock:
        piece = state[key]["piece"]
        if job is None:
            if piece.get("id"):
                state[key]["piece"] = {"status": "none"}
                pieces.pop(key, None)
            return
        pid = piece_id(job)
        model, cfg = find_model(job)  # re-checked every poll: an STL can be added mid-print
        piece["model"] = f"/models/{urllib.parse.quote(model)}" if model else None
        piece["model_cfg"] = cfg
        start = piece.get("id") != pid or (
            piece.get("status") == "error" and time.time() >= piece.get("retry_at", 0))
        if not start:
            return
        state[key]["piece"] = {"status": "loading", "id": pid, "progress": 0,
                               "model": piece["model"], "model_cfg": cfg}
        pieces.pop(key, None)
    threading.Thread(target=load_piece, args=(key, job, pid), daemon=True).start()


# ============================ HTTP ============================
class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str, gz: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str) -> None:
        if not path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, path.read_bytes(), ctype)

    def do_GET(self) -> None:
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if path == "/api/state":
            with lock:
                body = json.dumps({"time": time.time(), "printers": state}).encode()
            self._send(200, body, "application/json")
        elif path.startswith("/api/piece/"):
            with lock:
                data = pieces.get(path[len("/api/piece/"):])
            if data is None:
                self._send(404, b"no piece", "text/plain")
            elif "gzip" in self.headers.get("Accept-Encoding", ""):
                self._send(200, data, "application/json", gz=True)
            else:
                self._send(200, gzip.decompress(data), "application/json")
        elif path.startswith("/models/"):
            name = path[len("/models/"):]
            if not SAFE_NAME.match(name):
                self._send(400, b"bad name", "text/plain")
                return
            self._file(MODEL_DIR / name, "model/stl")
        else:
            name = "index.html" if path == "/" else path.lstrip("/")
            ext = Path(name).suffix
            if "/" in name or ".." in name or ext not in STATIC_TYPES:
                self._send(404, b"not found", "text/plain")
                return
            self._file(WEB_DIR / name, STATIC_TYPES[ext] + "; charset=utf-8")

    def log_message(self, format, *args) -> None:
        pass


def main() -> None:
    load_cell_log()
    for key, cfg in PRINTERS.items():
        state[key] = {"key": key, "label": cfg["label"], "esp": cfg["esp"],
                      "online": False, "error": "not polled yet",
                      "spools_online": False, "piece": {"status": "none"}}
        threading.Thread(target=poll_duet, args=(key,), daemon=True).start()
        threading.Thread(target=poll_esp, args=(key,), daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Twin on http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
