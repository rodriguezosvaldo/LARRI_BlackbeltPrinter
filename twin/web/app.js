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

const POLL_MS = 2000;
const DEFAULT_GANTRY_ANGLE = 35;
const COLORS = { printed: 0xff8a3d, ghost: 0x6b7785, belt: 0x20262d, plane: 0x4ea8f2 };
const AXES = { x: 0, y: 1, z: 2 };

// ============================ formatting ============================
const num = (v, d = 1) => (v == null || isNaN(v) ? "—" : Number(v).toFixed(d));
function fmtLen(mm) {
  if (mm == null) return "—";
  const m = mm / 1000;
  return m >= 100 ? `${Math.round(m).toLocaleString()} m` : `${m.toFixed(1)} m`;
}
const fmtKg = (kg) => (kg == null ? "" : kg >= 1 ? `≈ ${kg.toFixed(2)} kg` : `≈ ${Math.round(kg * 1000)} g`);
function fmtDur(s) {
  if (s == null) return "—";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m} min`;
}
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
      requestAnimationFrame(loop);
    };
    loop();
  }

  resize() {
    const w = this.el.clientWidth, h = this.el.clientHeight;
    this.renderer.setSize(w, h, false);
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
    this.belt = this.plane = null;
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
  }

  showSource() {
    const stl = this.source === "stl" && this.stl;
    if (this.toolpath) this.toolpath.printed.visible = this.toolpath.ghost.visible = !stl;
    if (this.stl) this.stl.printed.visible = this.stl.ghost.visible = !!stl || !this.toolpath;
  }

  // z = current belt position in G-code coordinates (Duet machinePosition)
  update(z) {
    this.z = z;
    const zz = z ?? -1e9;
    const tp = this.toolpath;
    if (tp) {
      let lo = 0, hi = tp.runZ.length; // first run with z > current
      while (lo < hi) { const m = (lo + hi) >> 1; if (tp.runZ[m] <= zz) lo = m + 1; else hi = m; }
      const count = lo < tp.runZ.length ? tp.runStart[lo] : tp.total;
      tp.printed.geometry.setDrawRange(0, count);
      tp.ghost.geometry.setDrawRange(count, tp.total - count);
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
    this.viewer = new Viewer(this.el.querySelector(".view"));
    this.pieceId = null;
    this.modelUrl = null;
    this.modelGeom = null;
    this.btn = {};
    this.el.querySelectorAll("[data-act]").forEach((b) => (this.btn[b.dataset.act] = b));
    this.btn.fit.onclick = () => this.viewer.fit();
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

  render(p) {
    this.set("label", p.label);
    const job = p.job;
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
    this.viewer.update(job ? p.z : null);
    this.syncButtons();

    // job
    this.set("file", job ? job.file.replace(/\.gcode$/i, "") : "—");
    const layers = piece.layer_count;
    this.set("jobinfo", job
      ? [job.objects?.length ? `Model: ${job.objects.join(", ")}` : null,
         `Layer ${job.layer ?? "—"}${layers ? " / " + layers : ""}`,
         `elapsed ${fmtDur(job.duration)}`, `left ≈ ${fmtDur(job.time_left)}`].filter(Boolean).join(" · ")
      : "");
    this.set("percent", job?.percent != null ? `${num(job.percent, 1)}%` : "—");
    this.f.percentbar.style.width = `${job?.percent ?? 0}%`;

    // belt Z
    const z0 = piece.z_min, z1 = piece.z_max;
    const done = job && p.z != null ? (z0 != null ? Math.max(0, p.z - z0) : p.z) : null;
    const total = z0 != null && z1 != null ? z1 - z0 : null;
    this.set("z", done != null ? `${num(done, 1)} mm` : "—");
    this.set("ztotal", total != null ? `of ${num(total, 0)} mm · belt Z ${num(p.z, 1)}` : p.z != null ? `belt Z ${num(p.z, 1)}` : "");
    const zpct = done != null && total ? Math.min(100, (done / total) * 100) : null;
    this.set("zpct", zpct != null ? `${num(zpct, 1)}%` : "");
    this.f.zbar.style.width = `${zpct ?? 0}%`;

    // filament
    const tot = job?.filament_total_mm ?? piece.extruded_mm, used = job?.filament_used_mm;
    const left = tot != null && used != null ? Math.max(0, tot - used) : null;
    const kg = (mm) => (mm == null || !tot || job?.filament_total_kg == null ? null : (job.filament_total_kg * mm) / tot);
    this.set("filtotal", fmtLen(tot)); this.set("filtotalkg", fmtKg(kg(tot)));
    this.set("filused", fmtLen(used)); this.set("filusedkg", fmtKg(kg(used)));
    this.set("filleft", fmtLen(left)); this.set("filleftkg", fmtKg(kg(left)));

    // temperatures
    for (const name of ["E0", "Bed0", "Bed1"]) {
      const tile = this.el.querySelector(`[data-tile="${name}"]`), t = p.temps?.[name];
      tile.querySelector(".v").textContent = t ? `${num(t.current)}°C` : "—";
      tile.querySelector(".t").textContent = t ? (t.target ? `→ ${num(t.target, 0)}°C` : t.state || "off") : "";
      tile.classList.toggle("hot", !!t && t.current > 50);
    }
    this.set("dhtt", p.dht?.temp != null ? `${num(p.dht.temp)}°C` : "—");
    this.set("dhth", p.dht?.humidity != null ? `${num(p.dht.humidity)}%` : "—");

    // spools
    const cells = p.spools || {};
    this.f.spools.innerHTML = "";
    for (const [name, c] of Object.entries(cells)) {
      const pct = c.percent;
      const div = document.createElement("div");
      div.className = "tile spool" + (pct != null && pct < 10 ? " low" : "");
      const hours = c.hours_left_live ?? null;
      div.innerHTML = `<div class="row"><span class="k">${name.replace("cell", "Cell ")}</span>
          ${c.consuming ? '<span class="tag">feeding</span>' : ""}</div>
        <div class="v">${pct != null ? num(pct, 0) + "%" : "—"}</div>
        <div class="bar"><i style="width:${Math.max(0, Math.min(100, pct ?? 0))}%"></i></div>
        <div class="t">${hours != null ? `≈ ${num(hours, 1)} h left at current rate` : c.consuming ? "" : "not feeding"}</div>`;
      if (c.note) div.title = c.note;
      this.f.spools.append(div);
    }
    this.set("spoolerr", p.spools_online ? "" : `Load cells: ${p.spools_error || "offline"}`);
  }
}

// ============================ main ============================
const cards = {};
async function poll() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    const s = await r.json();
    for (const [key, p] of Object.entries(s.printers)) {
      (cards[key] ||= new Card(key)).render(p);
    }
    document.getElementById("updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    document.getElementById("updated").textContent = `twin server unreachable (${e.message})`;
  }
  setTimeout(poll, POLL_MS);
}
poll();
