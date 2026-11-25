// BlackBelt twin dashboard: polls /api/state, draws each printer's piece with Three.js.
//
// Part frame (what is drawn): X = G-code X (across the belt), Y = height above the belt,
// Z = position along the belt. A G-code point (x, y, z) with gantry angle A is at
//   height h = y·sin(A), along the belt d = z − y·cos(A)
// (checked against a real Cura BlackBelt file: walls come out straight). Everything printed
// so far is the set of layers with G-code Z ≤ current belt Z, i.e. the points with
// d + h·cot(A) ≤ Z — a plane tilted like the gantry, drawn as the translucent "gantry plane".
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";

const POLL_MS = 2000;
const DEFAULT_GANTRY_ANGLE = 35;
const COLORS = { printed: 0xff8a3d, ghost: 0x6b7785, belt: 0x20262d, plane: 0x4ea8f2,
                 dimTotal: 0x6b7785, dimDone: 0xff8a3d }; // same as ghost / printed piece
const AXES = { x: 0, y: 1, z: 2 };
// Tooltip of "end ≈" in the job line (&#10; = line break inside the title attribute)
const END_HINT = "Now + Duet job.timesLeft.filament (filament-based estimate).&#10;" +
  "fraction = rawExtrusion / filament total from the G-code header&#10;" +
  "left ≈ elapsed × (1 − fraction) / fraction&#10;" +
  "Adapts to the real speed; unreliable in the first minutes. Updated every 2 s.";

// ============================ formatting ============================
const num = (v, d = 1) => (v == null || isNaN(v) ? "—" : Number(v).toFixed(d));
// Durations: "2d 13h 09min", "13h 09min" or "9min"
function fmtDur(s) {
  if (s == null) return "—";
  const t = Math.round(s / 60), d = Math.floor(t / 1440), h = Math.floor((t % 1440) / 60), m = t % 60;
  const mm = `${String(m).padStart(2, "0")}min`;
  return d ? `${d}d ${h}h ${mm}` : h ? `${h}h ${mm}` : `${m}min`;
}
const escHtml = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const cnt = (v) => (v == null ? "—" : Math.round(v).toLocaleString("en-US"));
// When the spool runs out, e.g. "Today 18:40" or "Wed, Nov 19 22:25"
function fmtEta(h) {
  if (h == null) return "—";
  const t = new Date(Date.now() + h * 3600e3);
  const time = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (t.toDateString() === new Date().toDateString()) return `Today ${time}`;
  return `${t.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })} ${time}`;
}
const fmtDate = (unix) => new Date(unix * 1000).toLocaleString(undefined, {
  month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit" });
const prefs = {
  get(k) { try { return JSON.parse(localStorage.getItem("twin:" + k)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem("twin:" + k, JSON.stringify(v)); } catch {} },
};

// ============================ 3D viewer ============================
class Viewer {
  constructor(el) {
    this.el = el;
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.localClippingEnabled = true;
    el.prepend(this.renderer.domElement);
    this.labels = new CSS2DRenderer();
    Object.assign(this.labels.domElement.style, { position: "absolute", inset: "0", pointerEvents: "none" });
    this.renderer.domElement.after(this.labels.domElement);
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0a0c0f);
    this.camera = new THREE.PerspectiveCamera(40, 1, 1, 50000);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x223344, 1.2));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(1, 2, -1);
    this.scene.add(sun);

    this.world = new THREE.Group();
    this.scene.add(this.world);
    this.angle = DEFAULT_GANTRY_ANGLE;
    this.z = null;
    this.toolpath = null; // {printed, ghost, runZ, runStart, box}
    this.stl = null; // {printed, ghost, box}
    this.source = "toolpath";
    this.clipPrinted = new THREE.Plane();
    this.clipGhost = new THREE.Plane();

    new ResizeObserver(() => this.resize()).observe(el);
    this.resize();
    const loop = () => {
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
      this.labels.render(this.scene, this.camera);
      requestAnimationFrame(loop);
    };
    loop();
  }

  resize() {
    const w = this.el.clientWidth, h = this.el.clientHeight;
    this.renderer.setSize(w, h, false);
    this.labels.setSize(w, h);
    this.camera.aspect = w / Math.max(h, 1);
    this.camera.updateProjectionMatrix();
  }

  clear() {
    for (const o of [...this.world.children]) {
      this.world.remove(o);
      o.traverse((c) => { c.geometry?.dispose(); c.material?.dispose?.(); });
    }
    this.toolpath = null;
    this.stl = null;
    this.belt = this.plane = this.stage = null;
    this.onChange?.();
  }

  // piece = /api/piece JSON: runs [[z, [x0, y0, x1, y1, ...]], ...] in G-code coordinates
  setToolpath(piece) {
    this.clear();
    this.angle = piece.gantry_angle ?? DEFAULT_GANTRY_ANGLE;
    const a = (this.angle * Math.PI) / 180, sin = Math.sin(a), cos = Math.cos(a);
    let n = 0;
    for (const [, p] of piece.runs) n += p.length / 2 - 1;
    const pos = new Float32Array(n * 6);
    const runZ = new Float64Array(piece.runs.length), runStart = new Uint32Array(piece.runs.length);
    let v = 0;
    piece.runs.forEach(([z, p], i) => {
      runZ[i] = z;
      runStart[i] = v / 3;
      for (let k = 0; k + 3 < p.length; k += 2) {
        for (const [x, y] of [[p[k], p[k + 1]], [p[k + 2], p[k + 3]]]) {
          pos[v++] = x; pos[v++] = y * sin; pos[v++] = z - y * cos;
        }
      }
    });
    const attr = new THREE.BufferAttribute(pos, 3);
    // printed lines shaded by height so the shape reads (dark at the belt, bright on top)
    let hMax = 1;
    for (let i = 1; i < pos.length; i += 3) hMax = Math.max(hMax, pos[i]);
    const col = new Float32Array(pos.length), lo = new THREE.Color(0x7a3a12), hi = new THREE.Color(0xffc58a), c = new THREE.Color();
    for (let i = 0; i < pos.length; i += 3) {
      c.lerpColors(lo, hi, pos[i + 1] / hMax);
      col[i] = c.r; col[i + 1] = c.g; col[i + 2] = c.b;
    }
    const mk = (color, opacity) => {
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", attr);
      if (color == null) g.setAttribute("color", new THREE.BufferAttribute(col, 3));
      const m = new THREE.LineBasicMaterial({ color: color ?? 0xffffff, vertexColors: color == null,
        transparent: opacity < 1, opacity, depthWrite: opacity >= 1 });
      return new THREE.LineSegments(g, m);
    };
    const printed = mk(null, 1), ghost = mk(COLORS.ghost, 0.18);
    printed.geometry.computeBoundingBox();
    const box = printed.geometry.boundingBox.clone();
    this.toolpath = { printed, ghost, runZ, runStart, total: v / 3, box };
    this.world.add(ghost, printed);
    this.addStage(box);
    this.showSource();
    this.update(this.z);
    this.fit();
  }

  // STL from twin/models/: cfg = {belt: "x"|"y"|"z", up: "x"|"y"|"z"} (defaults y, z); flip swaps ends
  removeStl() {
    if (!this.stl) return;
    this.world.remove(this.stl.printed, this.stl.ghost);
    this.stl.printed.geometry.dispose();
    this.stl = null;
    this.showSource();
  }

  setStl(geometry, cfg, flip) {
    this.removeStl();
    const belt = AXES[cfg.belt] ?? 1, up = AXES[cfg.up] ?? 2;
    const across = [0, 1, 2].find((i) => i !== belt && i !== up);
    const src = geometry.getAttribute("position").array;
    const out = new Float32Array(src.length);
    for (let i = 0; i < src.length; i += 3) {
      out[i] = src[i + across]; out[i + 1] = src[i + up]; out[i + 2] = src[i + belt];
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(out, 3));
    g.computeBoundingBox();
    const b = g.boundingBox;
    // Align with the toolpath when there is one (same size in mm), else start on the belt at 0
    const ref = this.toolpath?.box;
    const dx = ref ? (ref.min.x + ref.max.x) / 2 - (b.min.x + b.max.x) / 2 : -(b.min.x + b.max.x) / 2;
    const d0 = ref ? ref.min.z : 0;
    const len = b.max.z - b.min.z;
    for (let i = 0; i < out.length; i += 3) {
      out[i] += dx;
      out[i + 1] -= b.min.y;
      const d = out[i + 2] - b.min.z;
      out[i + 2] = d0 + (flip ? len - d : d);
    }
    if (flip) flipWinding(out); // STLLoader geometry is non-indexed
    g.attributes.position.needsUpdate = true;
    g.computeVertexNormals();
    g.computeBoundingBox();
    const printed = new THREE.Mesh(g, new THREE.MeshStandardMaterial({
      color: COLORS.printed, roughness: 0.6, metalness: 0.05, clippingPlanes: [this.clipPrinted], side: THREE.DoubleSide }));
    const ghost = new THREE.Mesh(g, new THREE.MeshStandardMaterial({
      color: COLORS.ghost, transparent: true, opacity: 0.18, depthWrite: false, clippingPlanes: [this.clipGhost], side: THREE.DoubleSide }));
    this.stl = { printed, ghost, box: g.boundingBox.clone() };
    this.world.add(ghost, printed);
    if (!this.toolpath) this.addStage(this.stl.box);
    this.showSource();
    this.update(this.z);
  }

  addStage(box) {
    if (this.belt) return;
    const len = box.max.z - box.min.z, wid = box.max.x - box.min.x;
    const belt = new THREE.Mesh(
      new THREE.PlaneGeometry(wid + 160, len + 400),
      new THREE.MeshStandardMaterial({ color: COLORS.belt, roughness: 1 }));
    belt.rotation.x = -Math.PI / 2;
    belt.position.set((box.min.x + box.max.x) / 2, -0.5, (box.min.z + box.max.z) / 2);
    const grid = new THREE.GridHelper(Math.max(len, wid) + 400, Math.round((Math.max(len, wid) + 400) / 50), 0x2c343d, 0x252b32);
    grid.position.copy(belt.position).setY(-0.2);
    this.belt = belt;
    this.world.add(belt, grid);
    // gantry plane: d = Z − h·cot(A), tilted at the gantry angle
    const hMax = Math.max(box.max.y * 1.15, 50);
    const pg = new THREE.PlaneGeometry(wid + 120, hMax / Math.sin((this.angle * Math.PI) / 180));
    this.plane = new THREE.Mesh(pg, new THREE.MeshBasicMaterial({
      color: COLORS.plane, transparent: true, opacity: 0.12, side: THREE.DoubleSide, depthWrite: false }));
    this.planeH = hMax;
    this.planeX = (box.min.x + box.max.x) / 2;
    this.world.add(this.plane);

    // on the belt, beside the piece (camera side): total length and length printed so far
    const x = box.min.x;
    this.stage = {
      box: box.clone(),
      total: dimension(x - 170, COLORS.dimTotal, "dim"),
      done: dimension(x - 90, COLORS.dimDone, "dim done"),
      pct: label("pct"),
    };
    for (const k of ["total", "done", "pct"]) this.world.add(this.stage[k].obj ?? this.stage[k]);
  }

  showSource() {
    const stl = this.source === "stl" && this.stl;
    if (this.toolpath) this.toolpath.printed.visible = this.toolpath.ghost.visible = !stl;
    if (this.stl) this.stl.printed.visible = this.stl.ghost.visible = !!stl || !this.toolpath;
    this.onChange?.();
  }

  // z = current belt position in G-code coordinates (Duet machinePosition)
  update(z, percent = this.percent) {
    this.z = z;
    this.percent = percent;
    const zz = z ?? -1e9;
    const tp = this.toolpath;
    if (tp) {
      let lo = 0, hi = tp.runZ.length; // first run with z > current
      while (lo < hi) { const m = (lo + hi) >> 1; if (tp.runZ[m] <= zz) lo = m + 1; else hi = m; }
      const count = lo < tp.runZ.length ? tp.runStart[lo] : tp.total;
      tp.printed.geometry.setDrawRange(0, count);
      tp.ghost.geometry.setDrawRange(count, tp.total - count);
      // extent of the layer being printed (last G-code Z ≤ belt Z), part frame: the area on the
      // gantry plane (used by the nozzle animation)
      tp.layer = null;
      if (lo > 0) {
        let k = lo - 1;
        while (k > 0 && tp.runZ[k - 1] === tp.runZ[lo - 1]) k--;
        const p = tp.printed.geometry.attributes.position.array;
        let h0 = Infinity, h1 = -Infinity, x0 = Infinity, x1 = -Infinity;
        for (let i = tp.runStart[k] * 3; i < count * 3; i += 3) {
          x0 = Math.min(x0, p[i]); x1 = Math.max(x1, p[i]);
          h0 = Math.min(h0, p[i + 1]); h1 = Math.max(h1, p[i + 1]);
        }
        if (h1 >= h0) tp.layer = { h0, h1, x0, x1 };
      }
    }
    const a = (this.angle * Math.PI) / 180, cot = Math.cos(a) / Math.sin(a);
    // printed: Z − d − h·cot(A) ≥ 0
    const n = new THREE.Vector3(0, -cot, -1).normalize(), len = Math.hypot(cot, 1);
    this.clipPrinted.set(n, zz / len);
    this.clipGhost.set(n.clone().negate(), -zz / len);
    if (this.plane) {
      this.plane.visible = z != null;
      const h = this.planeH / 2;
      this.plane.position.set(this.planeX, h, zz - h * cot);
      this.plane.rotation.set(-(Math.PI / 2 - a), 0, 0);
    }
    const st = this.stage;
    if (st) {
      const d0 = st.box.min.z, d1 = st.box.max.z;
      // at belt level (h = 0) everything up to d = Z is printed
      const dz = z == null ? d0 : Math.min(Math.max(zz, d0), d1);
      st.total.set(d0, d1, `${Math.round(d1 - d0)} mm`);
      st.done.set(d0, dz, `${Math.round(dz - d0)} mm`);
      st.done.obj.visible = z != null && dz > d0;
      // % in the middle of the printed part (half height, behind the gantry plane)
      const h = st.box.max.y / 2;
      const dm = Math.min(Math.max(zz - h * cot, d0), d1);
      st.pct.position.set((st.box.min.x + st.box.max.x) / 2, h, (d0 + dm) / 2);
      st.pct.visible = z != null && percent != null && dm > d0;
      st.pct.element.textContent = percent != null ? `${percent.toFixed(1)}%` : "";
    }
    this.onChange?.();
  }

  fit() {
    const tp = this.toolpath, s = this.stl;
    const box = (this.source === "stl" && s ? s.box : tp?.box) || s?.box;
    if (!box) return;
    const c = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3());
    const r = Math.max(size.x, size.y, size.z) * 0.75 + 60;
    this.controls.target.copy(c);
    this.camera.position.set(c.x - r * 1.0, c.y + r * 0.6, c.z - r * 0.45);
    this.camera.near = r / 100;
    this.camera.far = r * 20;
    this.camera.updateProjectionMatrix();
  }
}

function label(cls) {
  const div = document.createElement("div");
  div.className = `lbl ${cls}`;
  return new CSS2DObject(div);
}

// Dimension line along the belt (Z) at a fixed x and height y: main line + end ticks + label
// (label `off` mm from the line in x)
function dimension(x, color, cls, { y = 1, tick = 14, off = -28 } = {}) {
  const pos = new Float32Array(18);
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  const obj = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color }));
  const text = label(cls);
  obj.add(text);
  return {
    obj, text,
    set(d0, d1, txt) {
      pos.set([x, y, d0, x, y, d1, x - tick, y, d0, x + tick, y, d0, x - tick, y, d1, x + tick, y, d1]);
      g.attributes.position.needsUpdate = true;
      g.computeBoundingSphere();
      text.position.set(x + off, y, (d0 + d1) / 2);
      text.element.textContent = txt;
    },
  };
}

// ============================ printer view (3D / top) ============================
// Printer frame, mm: X across the belt (left → right seen from the front), Y up from the floor,
// Z along the belt: 0 = nozzle line at the belt (green line), + toward the front. The belt carries
// the piece toward the front, so a part-frame point (x, h, d) is at Z = belt Z − d: the last layer
// is at the nozzle, the first one has travelled Z − d0. G-code +Z points to the back (the nozzle
// moves back relative to the piece), so with Y up a right-handed G-code X points left seen from the
// front: X = −x (+ centring). Sizes are rough, from the sketch.
const PRINTER = {
  width: 900, back: 270, front: 800, // back of the frame → nozzle line → front edge (0.8 m)
  lowerH: 530, upperH: 570,          // spool cabinet / upper section
  chamberW: 480, chamberH: 285, chamberY: 90, // build chamber, bottom chamberY above the cabinet
  beltW: 340, beltBack: 60,          // belt starts beltBack behind the nozzle line
  spoolR: 120, spoolCore: 45, spoolW: 110, spoolX: 225, spoolY: 240, spoolZ: -150,
};
const BELT_Y = PRINTER.lowerH + PRINTER.chamberY;
const PRINTER_H = PRINTER.lowerH + PRINTER.upperH;
const VIEW_MODES = ["top", "3d"];

function outlineBox(w, h, d, x, y, z, color) {
  const g = new THREE.EdgesGeometry(new THREE.BoxGeometry(w, h, d));
  const o = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color }));
  o.position.set(x, y, z);
  return o;
}

class PrinterView {
  // src = the small Viewer (part frame): its toolpath / STL geometry is shared, printed part only
  constructor(el, src) {
    this.el = el;
    this.src = src;
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.localClippingEnabled = true;
    el.prepend(this.renderer.domElement);
    this.labels = new CSS2DRenderer();
    Object.assign(this.labels.domElement.style, { position: "absolute", inset: "0", pointerEvents: "none" });
    this.renderer.domElement.after(this.labels.domElement);
    const scene = (this.scene = new THREE.Scene());
    scene.background = new THREE.Color(0x0a0c0f);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x223344, 1.2));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(1, 2, 1.5);
    scene.add(sun);

    const P = PRINTER, depth = P.back + P.front, zc = (P.front - P.back) / 2;
    const frameCol = 0x8a94a0;
    scene.add(outlineBox(P.width, P.lowerH, depth, 0, P.lowerH / 2, zc, frameCol));
    scene.add(outlineBox(P.width, P.upperH, depth, 0, P.lowerH + P.upperH / 2, zc, frameCol));
    const bl = P.beltBack + P.front;
    // build chamber: only its opening on the front face of the printer body
    const cw = P.chamberW / 2, c0 = BELT_Y, c1 = BELT_Y + P.chamberH;
    this.chamber = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(
      [[-cw, c0], [cw, c0], [cw, c1], [-cw, c1]].map(([x, y]) => new THREE.Vector3(x, y, P.front))),
      new THREE.LineBasicMaterial({ color: frameCol }));
    const belt = new THREE.Mesh(new THREE.BoxGeometry(P.beltW, 6, bl),
      new THREE.MeshStandardMaterial({ color: COLORS.belt, roughness: 1 }));
    belt.position.set(0, BELT_Y - 3.2, (P.front - P.beltBack) / 2);
    // green line: the nozzle, as wide as the gantry plane. At the belt with no piece; while printing
    // it sweeps from the belt up the plane to the top of the layer being printed (animation only)
    const nl = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(-0.5, 0, 0), new THREE.Vector3(0.5, 0, 0)]);
    this.nozzleLine = new THREE.Line(nl, new THREE.LineBasicMaterial({ color: 0x3ccf7a }));
    this.sweep = null; // {h1: how high it goes on the plane} while there is a printed piece
    this.cot = 1 / Math.tan((DEFAULT_GANTRY_ANGLE * Math.PI) / 180);
    this.gantry = new THREE.Mesh(new THREE.PlaneGeometry(P.chamberW, 1), new THREE.MeshBasicMaterial({
      color: 0x9ff2bd, transparent: true, opacity: 0.14, side: THREE.DoubleSide, depthWrite: false }));
    scene.add(this.chamber, belt, this.nozzleLine, this.gantry);

    // spools in the cabinet, axis front-back: rims + filament left (radius from the load cell %)
    this.spools = [1, 2].map((i) => {
      const x = (i === 1 ? -1 : 1) * P.spoolX;
      const g = new THREE.Group();
      g.position.set(x, P.spoolY, P.spoolZ);
      const rim = new THREE.LineSegments(
        new THREE.EdgesGeometry(new THREE.CylinderGeometry(P.spoolR, P.spoolR, P.spoolW, 48), 30),
        new THREE.LineBasicMaterial({ color: frameCol }));
      const core = new THREE.Mesh(new THREE.CylinderGeometry(P.spoolCore, P.spoolCore, P.spoolW * 0.9, 32),
        new THREE.MeshStandardMaterial({ color: 0x2a3038 }));
      const fil = new THREE.Mesh(new THREE.CylinderGeometry(1, 1, P.spoolW * 0.85, 48),
        new THREE.MeshStandardMaterial({ color: COLORS.ghost, roughness: 0.7 }));
      for (const o of [rim, core, fil]) { o.rotation.x = Math.PI / 2; g.add(o); }
      const lbl = label("plbl spool");
      lbl.position.set(0, 0, P.spoolW / 2 + 1);
      g.add(lbl);
      scene.add(g);
      return { fil, lbl, i };
    });

    // part of the piece already out of the printer: front edge → first printed end (top left,
    // outside the frame; only shown when something is out)
    this.dimOut = dimension(-P.width / 2 - 60, COLORS.dimDone, "dim done", { y: PRINTER_H, tick: 30, off: -70 });
    this.out = 0;
    this.pct = label("pct");
    scene.add(this.dimOut.obj, this.pct);

    // printed piece: part frame → printer frame = 180° turn about the vertical (X and Z reversed,
    // NOT a mirror: reversing only Z mirrored the piece left↔right), moved with the belt
    this.piece = new THREE.Group();
    this.piece.scale.set(-1, 1, -1);
    scene.add(this.piece);
    this.clip = new THREE.Plane();
    this.tpRef = this.stlRef = this.tpObj = this.stlObj = null;

    // cameras: top orthographic (pan + zoom), 3D perspective (orbit)
    const top = new THREE.OrthographicCamera();
    top.up.set(0, 0, -1); // back of the printer at the top of the screen
    this.cams = { top, "3d": new THREE.PerspectiveCamera(40, 1, 10, 30000) };
    this.rects = { // what the orthographic view frames: center + size on screen
      top: { cx: -50, cy: zc + 20, w: P.width + 300, h: depth + 140 },
    };
    this.ctrls = {};
    this.moved = {}; // mode → the user panned/zoomed since the last fit (then don't refit on its own)
    for (const [m, cam] of Object.entries(this.cams)) {
      const c = new OrbitControls(cam, this.renderer.domElement);
      c.enableDamping = true;
      c.enabled = false;
      c.addEventListener("start", () => (this.moved[m] = true));
      if (m !== "3d") {
        c.enableRotate = false;
        c.mouseButtons = { LEFT: THREE.MOUSE.PAN, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.PAN };
        c.touches = { ONE: THREE.TOUCH.PAN, TWO: THREE.TOUCH.DOLLY_PAN };
      }
      this.ctrls[m] = c;
    }
    this.mode = "3d";
    for (const m of VIEW_MODES) this.fit(m);

    new ResizeObserver(() => this.resize()).observe(el);
    this.resize();
    this.printing = false; // set by render(): the nozzle only moves while the Duet is printing
    let phase = 0, last = 0; // animation time (ms), only advances while printing
    const loop = (t = 0) => {
      const cam = this.cams[this.mode];
      this.ctrls[this.mode].update();
      if (this.printing) phase += Math.min(t - last, 100);
      last = t;
      // nozzle line: smooth up/down along the gantry plane (period 4 s), belt → top of the layer;
      // frozen where it is when not printing (paused, idle...)
      const s = this.sweep, nlo = this.nozzleLine;
      const h = s ? s.h1 * (0.5 - 0.5 * Math.cos((2 * Math.PI * phase) / 4000)) : 0;
      nlo.position.set(0, BELT_Y + 0.5 + h, h * this.cot);
      nlo.scale.x = P.chamberW;
      this.renderer.render(scene, cam);
      this.labels.render(scene, cam);
      requestAnimationFrame(loop);
    };
    loop();
  }

  setMode(mode) {
    if (!this.cams[mode]) mode = "3d";
    this.ctrls[this.mode].enabled = false;
    this.mode = mode;
    this.ctrls[mode].enabled = true;
    this.chamber.visible = mode === "3d";
    this.gantry.visible = mode === "3d" && this.src.z != null;
    this.showOut();
    this.resize();
  }

  // dimension of the piece out of the printer (top / 3D, only when something is out)
  showOut() {
    const P = PRINTER, out = this.out;
    this.dimOut.obj.visible = this.dimOut.text.visible = out > 0;
    if (out > 0) this.dimOut.set(P.front, P.front + out, `${(out / 1000).toFixed(2)} m`);
    // top view: frame the piece out of the printer too, unless the user moved the view
    const zc = (P.front - P.back) / 2, ext = Math.max(out, 0);
    const r = { cx: -50, cy: zc + 20 + ext / 2, w: P.width + 300, h: P.back + P.front + 140 + ext };
    const old = this.rects.top;
    if (Math.abs(r.h - old.h) > 20) {
      this.rects.top = r;
      if (!this.moved.top) this.fit("top");
    }
  }

  frustum(mode) {
    const cam = this.cams[mode], r = this.rects[mode], a = this.aspect || 1;
    const halfH = Math.max(r.h, r.w / a) / 2;
    Object.assign(cam, { left: -halfH * a, right: halfH * a, top: halfH, bottom: -halfH, near: 1, far: 20000 });
    cam.updateProjectionMatrix();
  }

  fit(mode = this.mode) {
    const cam = this.cams[mode], c = this.ctrls[mode];
    this.moved[mode] = false;
    if (mode === "3d") {
      const t = new THREE.Vector3(0, PRINTER_H * 0.45, (PRINTER.front - PRINTER.back) / 2);
      cam.position.set(t.x + 1300, t.y + 900, t.z + 1500);
      c.target.copy(t);
      cam.near = 10; cam.far = 30000;
      cam.updateProjectionMatrix();
      return;
    }
    const r = this.rects[mode];
    const t = new THREE.Vector3(r.cx, 0, r.cy);
    cam.position.copy(t).add(new THREE.Vector3(0, 5000, 0));
    cam.zoom = 1;
    c.target.copy(t);
    cam.lookAt(t);
    this.frustum(mode);
  }

  resize() {
    const w = this.el.clientWidth, h = this.el.clientHeight;
    this.renderer.setSize(w, h, false);
    this.labels.setSize(w, h);
    this.aspect = w / Math.max(h, 1);
    const p = this.cams["3d"];
    p.aspect = this.aspect;
    p.updateProjectionMatrix();
    this.frustum("top");
  }

  updateSpools(spools) {
    const P = PRINTER;
    for (const s of this.spools) {
      const c = spools?.[`cell${s.i}`];
      const pct = c?.percent;
      s.fil.visible = pct != null;
      const r = P.spoolCore + (P.spoolR - P.spoolCore) * Math.max(0, Math.min(1, (pct ?? 0) / 100));
      s.fil.scale.set(r, 1, r);
      // green = feeding the printer, red = not feeding
      s.fil.material.color.setHex(c?.consuming ? 0x3ccf7a : 0xef5b5b);
      s.lbl.element.textContent = pct != null ? `${num(pct, 0)}%` : "—";
      s.lbl.element.classList.toggle("feeding", !!c?.consuming);
      s.lbl.element.classList.toggle("idle", c != null && !c.consuming);
    }
  }

  // Called by the small Viewer whenever its piece, source or belt Z changes
  sync() {
    const v = this.src;
    if (v.toolpath !== this.tpRef) {
      if (this.tpObj) { this.piece.remove(this.tpObj); this.tpObj.material.dispose(); }
      this.tpRef = v.toolpath;
      this.tpObj = v.toolpath && new THREE.LineSegments(v.toolpath.printed.geometry,
        new THREE.LineBasicMaterial({ vertexColors: true }));
      if (this.tpObj) this.piece.add(this.tpObj);
    }
    if (v.stl !== this.stlRef) {
      if (this.stlObj) { this.piece.remove(this.stlObj); this.stlObj.material.dispose(); }
      this.stlRef = v.stl;
      this.stlObj = v.stl && new THREE.Mesh(v.stl.printed.geometry, new THREE.MeshStandardMaterial({
        color: COLORS.printed, roughness: 0.6, metalness: 0.05, clippingPlanes: [this.clip], side: THREE.DoubleSide }));
      if (this.stlObj) this.piece.add(this.stlObj);
    }
    const a = (v.angle * Math.PI) / 180, cot = Math.cos(a) / Math.sin(a), len = Math.hypot(cot, 1);
    // printed = in front of the gantry plane: Z − (Y − belt)·cot(A) ≥ 0 (fixed in the printer frame)
    this.clip.set(new THREE.Vector3(0, -cot / len, 1 / len), (cot * BELT_Y) / len);
    const gh = PRINTER.chamberH;
    this.gantry.scale.y = gh / Math.sin(a);
    this.gantry.position.set(0, BELT_Y + gh / 2, (gh / 2) * cot);
    this.gantry.rotation.set(Math.PI / 2 - a, 0, 0);
    this.gantry.visible = this.mode === "3d" && v.z != null;

    const z = v.z, on = z != null;
    if (this.tpObj) this.tpObj.visible = on && v.toolpath.printed.visible;
    if (this.stlObj) this.stlObj.visible = on && v.stl.printed.visible;
    const useStl = this.stlObj?.visible;
    const box = useStl ? v.stl.box : v.toolpath?.box;
    // piece centred across the belt / chamber
    const cx = box ? (box.min.x + box.max.x) / 2 : 0; // printer X = cx − x: centred, turned
    this.piece.position.set(cx, BELT_Y, z ?? 0);
    this.cot = cot;
    // nozzle sweep height: top of the layer being printed (toolpath), else the whole STL height
    const L = v.toolpath?.layer;
    this.sweep = !on || !box ? null : { h1: L ? L.h1 : box.max.y };
    // first printed end is at Z − d0: whatever is past the front edge is out of the printer
    const out = on && box ? z - box.min.z - PRINTER.front : 0;
    if (Math.abs(out - this.out) > 0.5 || (out > 0) !== (this.out > 0)) { this.out = out; this.showOut(); }
    this.pct.visible = false;
    if (on && box && v.percent != null) {
      const d0 = box.min.z, d1 = box.max.z, h = box.max.y / 2;
      const dm = Math.min(Math.max(z - h * cot, d0), d1);
      if (dm > d0) {
        this.pct.position.set(0, BELT_Y + h, z - (d0 + dm) / 2);
        this.pct.element.textContent = `${v.percent.toFixed(1)}%`;
        this.pct.visible = true;
      }
    }
  }
}

// Non-indexed triangles: mirroring one axis inverts the winding, swap 2 vertices per triangle
function flipWinding(a) {
  for (let i = 0; i < a.length; i += 9) {
    for (let k = 0; k < 3; k++) { const t = a[i + 3 + k]; a[i + 3 + k] = a[i + 6 + k]; a[i + 6 + k] = t; }
  }
}

// ============================ printer cards ============================
const stlLoader = new STLLoader();

class Card {
  constructor(key) {
    this.key = key;
    this.el = document.getElementById("tpl").content.firstElementChild.cloneNode(true);
    document.getElementById("printers").append(this.el);
    this.f = {};
    this.el.querySelectorAll("[data-f]").forEach((e) => (this.f[e.dataset.f] = e));
    this.partEl = this.el.querySelector(".partview");
    this.mainEl = this.el.querySelector(".main3d");
    this.viewer = new Viewer(this.partEl);
    this.main = new PrinterView(this.mainEl, this.viewer);
    this.viewer.onChange = () => this.main.sync();
    this.pieceId = null;
    this.modelUrl = null;
    this.modelGeom = null;
    this.btn = {};
    this.el.querySelectorAll("[data-act]").forEach((b) => (this.btn[b.dataset.act] = b));
    this.btn.fit.onclick = () => (this.mode === "part" ? this.viewer.fit() : this.main.fit());
    this.el.querySelectorAll("[data-mode]").forEach((b) => (b.onclick = () => this.setMode(b.dataset.mode)));
    this.setMode("3d"); // always open in 3D, same camera on both printers
    this.btn.source.onclick = () => {
      this.viewer.source = this.viewer.source === "stl" ? "toolpath" : "stl";
      prefs.set(`source:${key}`, this.viewer.source);
      this.viewer.showSource();
      this.syncButtons();
    };
    this.btn.flip.onclick = () => {
      const k = `flip:${this.modelUrl}`;
      prefs.set(k, !prefs.get(k));
      this.applyStl();
    };
    this.viewer.source = prefs.get(`source:${key}`) || "stl";
  }

  set(name, text) { if (this.f[name]) this.f[name].textContent = text; }

  // front / top / 3d = printer view, part = the whole piece (printed orange, rest grey)
  setMode(mode) {
    const part = mode === "part";
    this.partEl.hidden = !part;
    this.mainEl.hidden = part;
    if (!part) this.main.setMode(mode);
    this.mode = part ? "part" : this.main.mode;
    this.el.querySelectorAll("[data-mode]").forEach((b) => b.classList.toggle("on", b.dataset.mode === this.mode));
  }

  syncButtons() {
    const hasStl = !!this.viewer.stl, hasTp = !!this.viewer.toolpath;
    this.btn.source.hidden = !(hasStl && hasTp);
    this.btn.source.textContent = this.viewer.source === "stl" ? "Show toolpath" : "Show STL";
    this.btn.flip.hidden = !hasStl || (hasTp && this.viewer.source !== "stl");
    const showing = this.viewer.source === "stl" && hasStl ? "STL model" : hasTp ? "G-code outer walls" : hasStl ? "STL model" : "";
    this.set("vlabel", showing);
  }

  async loadPiece(piece) {
    const id = piece.id;
    try {
      const r = await fetch(`/api/piece/${this.key}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      if (this.pieceId !== id) return;
      this.viewer.setToolpath(data);
      if (this.modelGeom) this.applyStl();
      this.syncButtons();
    } catch (e) {
      this.pieceId = null; // retry next poll
      console.warn(this.key, "piece", e);
    }
  }

  async loadModel(url, cfg) {
    try {
      const buf = await (await fetch(url)).arrayBuffer();
      if (this.modelUrl !== url) return;
      this.modelGeom = stlLoader.parse(buf);
      this.modelCfg = cfg || {};
      this.applyStl();
    } catch (e) {
      console.warn(this.key, "model", e);
    }
  }

  applyStl() {
    if (!this.modelGeom) return;
    const flip = !!prefs.get(`flip:${this.modelUrl}`) !== !!this.modelCfg.flip;
    this.viewer.setStl(this.modelGeom, this.modelCfg, flip);
    if (!this.viewer.toolpath) this.viewer.fit();
    this.syncButtons();
  }

  spoolEl(name) {
    this.spoolEls ||= {};
    if (!this.spoolEls[name]) {
      const el = document.getElementById("spool-tpl").content.firstElementChild.cloneNode(true);
      el.querySelector(".s-name").textContent = `${this.key}_${name}`;
      this.f.spools.append(el);
      this.spoolEls[name] = el;
    }
    return this.spoolEls[name];
  }

  renderSpool(name, c, esp) {
    const el = this.spoolEl(name), q = (sel) => el.querySelector(sel);
    const val = (sel, text, muted) => { q(sel).textContent = text; q(sel).classList.toggle("muted", !!muted); };
    q(".s-calib").href = `${esp}/calib`;
    q(".s-config").href = `${esp}/config`;
    const u = c.config_updated || {};
    const tip = u.updated ? `last update: ${fmtDate(u.updated)}`
      : u.since ? `last update: before ${fmtDate(u.since)}` : "last update: unknown";
    q(".s-calib").title = q(".s-config").title = tip;
    el.title = c.note || "";
    q(".s-feed").hidden = !c.consuming;
    el.classList.toggle("feeding", !!c.consuming);
    q(".s-pct").textContent = c.percent != null ? `${num(c.percent, 1)} %` : "";

    val(".s-current", cnt(c.current), c.current == null);
    val(".s-empty", cnt(c.empty), c.empty == null);
    val(".s-ref", c.ref_rate ? `${cnt(c.ref_rate)} /h` : "—", !c.ref_rate);
    val(".s-live", c.live_rate == null
      ? `collecting (${Math.round((c.live_span || 0) / 60)} min)`
      : `${cnt(c.live_rate)} /h` + (c.consuming ? "" : " (idle)"), !c.consuming);

    // Headline: when the spool runs out (live estimate when feeding, reference while collecting)
    const idle = c.live_rate != null && !c.consuming;
    const h = idle ? null : c.consuming ? c.hours_left_live : c.hours_left_ref;
    const big = q(".s-eta");
    big.textContent = idle ? "not feeding" : fmtEta(h);
    big.className = "big s-eta " + (h == null ? "muted" : h < 1 ? "bad" : h < 2 ? "warn" : "");
    let sub;
    if (idle) sub = "idle";
    else if (c.empty == null) sub = "empty reel not calibrated";
    else if (c.current == null) sub = "no reading";
    else if (h == null) sub = "no rate yet";
    else sub = `${fmtDur(h * 3600)} left · ${c.consuming ? "at live rate" : "at reference rate (collecting live data)"}`;
    q(".s-sub").textContent = sub;
    const bar = q(".bar > i");
    bar.style.width = `${Math.max(0, Math.min(100, c.percent ?? 0))}%`;
    bar.style.background = h != null && h < 1 ? "var(--bad)" : h != null && h < 2 ? "var(--warn)" : "var(--ok)";
  }

  render(p) {
    this.set("label", p.label);
    const job = p.job;
    this.main.updateSpools(p.spools);
    this.main.printing = p.online && (p.status === "processing" || p.status === "simulating");
    const status = p.online ? p.status || "?" : "offline";
    this.f.status.textContent = status;
    this.f.status.className = `badge ${status}`;
    this.set("error", p.online ? "" : `Duet: ${p.error || "offline"}`);

    // piece
    const piece = p.piece || {};
    if (!job || piece.status === "none") {
      if (this.pieceId) { this.viewer.clear(); this.syncButtons(); }
      this.pieceId = null;
    } else if (piece.status === "ready" && piece.id !== this.pieceId) {
      this.pieceId = piece.id;
      this.loadPiece(piece);
    }
    if (piece.model !== this.modelUrl) {
      this.modelUrl = piece.model || null;
      this.modelGeom = null;
      this.viewer.removeStl();
      if (this.modelUrl) this.loadModel(this.modelUrl, piece.model_cfg);
    }
    let overlay = "";
    if (!p.online) overlay = "Printer offline";
    else if (!job) overlay = "No job running";
    else if (piece.status === "loading" && !this.viewer.stl)
      overlay = `Reading G-code from the printer… ${num(piece.progress, 0)}%`;
    else if (piece.status === "error" && !this.viewer.stl) overlay = `Could not read the G-code (${piece.error}), retrying in a minute…`;
    this.f.overlay.textContent = overlay;
    this.f.overlay.style.display = overlay ? "" : "none";
    this.viewer.update(job ? p.z : null, job?.percent ?? null);
    this.syncButtons();

    // job
    this.set("file", job ? job.file.replace(/\.gcode$/i, "") : "—");
    const fmtEnd = (s) => new Date(Date.now() + s * 1000).toLocaleString(undefined, {
      weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const endTxt = job?.time_left != null ? fmtEnd(job.time_left) : null;
    // Historic end: average real printing time of this part (logs/print_time_history.csv) minus
    // the printing time so far (Duet job.duration, which excludes pauses like print_min does)
    const h = job?.history && { ...job.history, part: escHtml(job.history.part) };
    let histHtml = "";
    if (h && !h.count) {
      histHtml = `<span class="hist none" title="No completed ${h.part} print in ` +
        `print_time_history.csv yet: the historic end appears after the first one.">` +
        `no history for ${h.part}</span>`;
    } else if (h && job.duration != null) {
      const left = h.avg_print_s - job.duration;
      const tip = `Average real printing time (pauses excluded) of ${h.count} completed ` +
        `${h.part} print${h.count > 1 ? "s" : ""} in print_time_history.csv: ` +
        `${fmtDur(h.avg_print_s)}. End = now + average − printing time so far ` +
        `(${fmtDur(job.duration)}); pauses from now on push it later.`;
      histHtml = `<b class="hist" title="${tip}">` + (left > 0
        ? `historic end ≈ ${fmtEnd(left)}`
        : `historic avg passed (${fmtDur(-left)} over)`) + "</b>";
    }
    const info = job
      ? `<span>elapsed ${fmtDur(job.duration)} · left ≈ ${fmtDur(job.time_left)}` +
        (endTxt ? ` · <b class="end" title="${END_HINT}">end ≈ ${endTxt}</b>` : "") +
        `</span>` + histHtml
      : "";
    // Only when it changed (about once a minute): rewriting it every poll closes the tooltip
    if (info !== this.jobInfo) this.f.jobinfo.innerHTML = this.jobInfo = info;

    // spools (same logic as the ESP32 spool monitor, load cel/hx711/src/pages.h), always shown
    for (const [name, c] of Object.entries(p.spools || {})) this.renderSpool(name, c, p.esp);
    this.set("spoolerr", p.spools_online ? "" : `Load cells: ${p.spools_error || "offline"}`);
  }
}

// ============================ main ============================
const cards = {};
async function poll() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    const s = await r.json();
    // P2 on the left, P1 on the right (cards are appended in this order on the first poll)
    for (const key of Object.keys(s.printers).sort().reverse()) {
      (cards[key] ||= new Card(key)).render(s.printers[key]);
    }
    document.getElementById("updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    document.getElementById("updated").textContent = `twin server unreachable (${e.message})`;
  }
  setTimeout(poll, POLL_MS);
}
poll();
