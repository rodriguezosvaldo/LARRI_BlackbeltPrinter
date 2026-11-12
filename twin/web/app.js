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

// ============================ formatting ============================
const num = (v, d = 1) => (v == null || isNaN(v) ? "—" : Number(v).toFixed(d));
// Durations: "2d 13h 09min", "13h 09min" or "9min"
function fmtDur(s) {
  if (s == null) return "—";
  const t = Math.round(s / 60), d = Math.floor(t / 1440), h = Math.floor((t % 1440) / 60), m = t % 60;
  const mm = `${String(m).padStart(2, "0")}min`;
  return d ? `${d}d ${h}h ${mm}` : h ? `${h}h ${mm}` : `${m}min`;
}
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

// Dimension line along the belt at a fixed x, lying on the belt: main line + end ticks + label
function dimension(x, color, cls) {
  const pos = new Float32Array(18);
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  const obj = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color }));
  const text = label(cls);
  obj.add(text);
  const y = 1, tick = 14;
  return {
    obj,
    set(d0, d1, txt) {
      pos.set([x, y, d0, x, y, d1, x - tick, y, d0, x + tick, y, d0, x - tick, y, d1, x + tick, y, d1]);
      g.attributes.position.needsUpdate = true;
      g.computeBoundingSphere();
      text.position.set(x - 2 * tick, y, (d0 + d1) / 2);
      text.element.textContent = txt;
    },
  };
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
    const end = job?.time_left != null ? new Date(Date.now() + job.time_left * 1000) : null;
    const endTxt = end ? end.toLocaleString(undefined, {
      weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : null;
    this.f.jobinfo.innerHTML = job
      ? `elapsed ${fmtDur(job.duration)} · left ≈ ${fmtDur(job.time_left)}` +
        (endTxt ? ` · <b class="end">end ≈ ${endTxt}</b>` : "")
      : "";

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
