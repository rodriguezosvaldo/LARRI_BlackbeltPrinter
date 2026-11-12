"""G-code parser for the twin: reduces a (BlackBelt Cura) G-code to what the viewer needs.

Only extrusion moves of the outer walls are kept (;TYPE:WALL-OUTER, or every extrusion if the
file has no ;TYPE: comments), as polylines in raw G-code coordinates (X, Y = along the gantry,
Z = belt). Points closer than MIN_STEP_MM to the previous one are dropped; if the result is
still bigger than MAX_POINTS, only every n-th layer is kept.

Also returns the Z range of the extrusion (belt travel = length of the part on the belt), the
total extruded length (sum of positive E deltas, G92 aware) and header info (gantry angle,
layer count, filament used).
"""

import re
from typing import Iterable

MIN_STEP_MM = 1.5
MAX_POINTS = 400_000

WORD = re.compile(r"([XYZEF])(-?\d*\.?\d+)")
HEADER = {
    "gantry_angle": re.compile(r";\s*Gantry Angle:\s*(-?\d+\.?\d*)"),
    "layer_count": re.compile(r";LAYER_COUNT:(\d+)"),
    "filament_m": re.compile(r";Filament used:\s*(\d+\.?\d*)m"),
}
OUTER_TYPES = ("WALL-OUTER",)


class Parser:
    def __init__(self) -> None:
        self.pos = {"X": 0.0, "Y": 0.0, "Z": 0.0, "E": 0.0}
        self.abs_xyz = True
        self.abs_e = True
        self.type = None
        self.has_types = False
        self.layer = -1
        self.info: dict = {}
        self.extruded = 0.0
        self.z_min = None
        self.z_max = None
        # polylines: (layer, z, [x0, y0, x1, y1, ...]) for outer walls, all for the fallback
        self.outer: list = []
        self.all: list = []
        self._run_outer = None
        self._run_all = None

    def _end_runs(self) -> None:
        self._run_outer = None
        self._run_all = None

    def _add(self, runs: list, attr: str, x0, y0, z, x1, y1) -> None:
        run = getattr(self, attr)
        if run is None or run[1] != z:
            run = (self.layer, z, [round(x0, 2), round(y0, 2)])
            runs.append(run)
            setattr(self, attr, run)
        pts = run[2]
        if abs(x1 - pts[-2]) + abs(y1 - pts[-1]) >= MIN_STEP_MM:
            pts.append(round(x1, 2))
            pts.append(round(y1, 2))

    def line(self, raw: str) -> None:
        s = raw.strip()
        if not s:
            return
        if s[0] == ";":
            if s.startswith(";TYPE:"):
                self.type = s[6:].strip()
                self.has_types = True
                self._end_runs()
            elif s.startswith(";LAYER:"):
                try:
                    self.layer = int(s[7:])
                except ValueError:
                    pass
                self._end_runs()
            else:
                for key, rx in HEADER.items():
                    if key not in self.info:
                        m = rx.match(s)
                        if m:
                            self.info[key] = float(m.group(1))
            return
        code = s.split(";", 1)[0].strip().upper()
        if not code:
            return
        cmd = code.split(None, 1)[0]
        if cmd in ("G0", "G1"):
            self._move(code, cmd == "G1")
        elif cmd == "G92":
            for k, v in WORD.findall(code):
                if k in self.pos:
                    self.pos[k] = float(v)
                    if k != "E":
                        self._end_runs()  # G92 E (belt wall compensation) keeps the run
        elif cmd == "G90":
            self.abs_xyz = True
            self.abs_e = True
        elif cmd == "G91":
            self.abs_xyz = False
            self.abs_e = False
        elif cmd == "M82":
            self.abs_e = True
        elif cmd == "M83":
            self.abs_e = False

    def _move(self, code: str, g1: bool) -> None:
        old = dict(self.pos)
        for k, v in WORD.findall(code):
            if k == "F":
                continue
            v = float(v)
            if k == "E":
                self.pos["E"] = v if self.abs_e else old["E"] + v
            else:
                self.pos[k] = v if self.abs_xyz else old[k] + v
        de = self.pos["E"] - old["E"]
        moved = any(self.pos[k] != old[k] for k in "XYZ")
        if de <= 0 or not moved or not g1:
            if moved:
                self._end_runs()
            if de > 0:
                self.extruded += de
            return
        self.extruded += de
        if self.layer < 0:
            return  # start G-code (purge on the belt), not part of the piece
        z = self.pos["Z"]
        self.z_min = z if self.z_min is None else min(self.z_min, z)
        self.z_max = z if self.z_max is None else max(self.z_max, z)
        args = (old["X"], old["Y"], z, self.pos["X"], self.pos["Y"])
        self._add(self.all, "_run_all", *args)
        if self.type in OUTER_TYPES:
            self._add(self.outer, "_run_outer", *args)

    def result(self) -> dict:
        runs = self.outer if self.outer else self.all
        total = sum(len(r[2]) // 2 for r in runs)
        stride = max(1, -(-total // MAX_POINTS))  # ceil
        if stride > 1:
            runs = [r for r in runs if r[0] % stride == 0]
        info = self.info
        return {
            "gantry_angle": info.get("gantry_angle"),
            "layer_count": int(info["layer_count"]) if "layer_count" in info else self.layer + 1,
            "filament_m": info.get("filament_m"),
            "extruded_mm": round(self.extruded, 1),
            "z_min": self.z_min,
            "z_max": self.z_max,
            "walls": "outer" if self.outer else "all",
            "layer_stride": stride,
            # [z, [x0, y0, x1, y1, ...]] per continuous extrusion run, in file (= Z) order
            "runs": [[r[1], r[2]] for r in runs if len(r[2]) >= 4],
        }


def parse(lines: Iterable[str]) -> dict:
    p = Parser()
    for raw in lines:
        p.line(raw)
    return p.result()


if __name__ == "__main__":
    import json
    import sys

    with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
        r = parse(f)
    pts = sum(len(x[1]) // 2 for x in r["runs"])
    print(json.dumps({k: v for k, v in r.items() if k != "runs"}, indent=1))
    print(f"{len(r['runs'])} runs, {pts} points, ~{len(json.dumps(r['runs'])) / 1e6:.1f} MB JSON")
