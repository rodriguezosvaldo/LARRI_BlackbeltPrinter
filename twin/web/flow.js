// Animated data-flow diagram: who asks whom, what travels and how often.
// Hand-placed SVG (viewBox 1200x750): Printer 2 on the left, Printer 1 on the right (like the
// Twin), the PC in the middle. Dots move along each line in the direction the data travels;
// a hollow ring going the other way is a question (a request). Timings come from the code:
// config.h (REQUEST_*_MS, READ_INTERVAL_MS), pclog.cpp, twin_server.py (DUET_POLL_S, ESP_POLL_S).
"use strict";

const NS = "http://www.w3.org/2000/svg";
const W = 1200;
const HW = 100, HH = 32; // node half width / half height
const SPEED = 260; // packet speed, px per (simulated) second

const CATS = {
  status: { name: "Printer status & messages", color: "var(--c-status)" },
  gcode: { name: "Print file (G-code)", color: "var(--c-gcode)" },
  alert: { name: "Alerts", color: "var(--c-alert)" },
  logs: { name: "Logs & history", color: "var(--c-logs)" },
  weight: { name: "Spool weight", color: "var(--c-weight)" },
};

// 36x36 line icons
const ICONS = {
  board: "M4 8h28v20H4z M13 13h10v10H13z M8 4v4 M14 4v4 M22 4v4 M28 4v4 M8 28v4 M14 28v4 M22 28v4 M28 28v4",
  esp: "M8 13h20v19H8z M12 13V5h4v4h4V5h4v8 M13 19h10v8H13z M5 18h3 M5 24h3 M28 18h3 M28 24h3",
  spool: "M28 14a10 10 0 1 1-20 0a10 10 0 1 1 20 0z M21 14a3 3 0 1 1-6 0a3 3 0 1 1 6 0z M18 24v6 M4 30h28 M8 30v3 M28 30v3",
  twin: "M3 5h30v20H3z M13 31h10 M18 25v6 M18 8.5l6 3.5v7L18 22.5l-6-3.5v-7z M12 12l6 3.5 6-3.5 M18 15.5v7",
  server: "M6 4h24v11H6z M6 19h24v11H6z M11 9.5h2 M11 24.5h2 M18 9.5h7 M18 24.5h7 M10 34h16",
  folder: "M3 9h11l3 3h16v19H3z M3 16h30",
  cloud: "M10 28h17a7 7 0 0 0 1-14 9 9 0 0 0-17-2 7 7 0 0 0-1 16z",
  phone: "M11 3h14a2 2 0 0 1 2 2v26a2 2 0 0 1-2 2H11a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z M16 29h4",
};

// ---------------------------------------------------------------- nodes and flows

const NODES = {};
const FLOWS = [];

function addNode(id, x, y, icon, color, t1, t2, desc) {
  NODES[id] = { id, x, y, icon, color, t1, t2, desc };
}

function addFlow(f) {
  f.ping = f.period == null || f.period >= 10; // flash the receiver (not for the fast ones)
  FLOWS.push(f);
}

const IPS = { 1: "192.168.1.243", 2: "192.168.1.188" };

for (const p of [2, 1]) {
  const k = `p${p}`;
  const mx = (x) => (p === 2 ? x : W - x);
  const an = (a) => (p === 2 || a === "middle" ? a : a === "end" ? "start" : "end");
  const pts = (...xy) => xy.map(([x, y]) => [mx(x), y]);
  const lab = (x, y, anchor) => ({ lx: mx(x), ly: y, anchor: an(anchor) });
  const duet = `${k}-duet`, esp = `${k}-esp`, cells = `${k}-cells`;

  addNode(duet, mx(200), 90, "board", "var(--muted)", `Printer ${p} · Duet 3`, IPS[p],
    "Printer control board. Only answers requests (ESP32, Twin); never sends data on its own.");
  addNode(esp, mx(200), 400, "esp", "var(--accent)", `ESP32 · Printer ${p}`, `bb-p${p}.local`,
    `Monitors its printer: polls the Duet, reads the load cells, sends alerts and logs. Works
     without the PC. Dashboard: <b>bb-p${p}.local</b>.`);
  addNode(cells, mx(200), 585, "spool", "var(--c-weight)", "Load cells", "2 spool scales",
    "Scales under the 2 spools, wired to the ESP32. Used for filament left and usage rate.");

  addFlow({ id: `${k}-esp-model`, a: esp, b: duet, kind: "pull", cat: "status", period: 10,
    label: "status · 10 s", ...lab(170, 200, "end"), pts: pts([180, 368], [180, 122]),
    title: "Printer status",
    detail: "State (printing / paused / idle), file, layer, filament used, belt position. Source of all alerts and job logs." });
  addFlow({ id: `${k}-esp-reply`, a: esp, b: duet, kind: "pull", cat: "status", period: 0.5,
    label: "messages · 0.5 s", ...lab(230, 200, "start"), pts: pts([220, 368], [220, 122]),
    title: "Printer messages",
    detail: "New Duet messages (e.g. pause reason). Saved on the PC and added to the “Print Paused” alert." });
  addFlow({ id: `${k}-cells-esp`, a: cells, b: esp, kind: "push", cat: "weight", period: 2,
    label: "weight · 2 s", ...lab(210, 495, "start"), pts: pts([200, 553], [200, 432]),
    title: "Spool weight",
    detail: "Weight readings, used to compute usage rate and time left per spool." });
  addFlow({ id: `${k}-esp-ntfy`, a: esp, b: "ntfy", kind: "push", cat: "alert", period: null,
    when: "on event", label: "alerts", ...lab(360, 530, "middle"),
    pts: pts([265, 432], [500, 585]),
    title: "Alerts",
    detail: "Print started / paused / finished / stopped, part reaching edge, filament low." });
  addFlow({ id: `${k}-esp-log`, a: esp, b: "logsrv", kind: "push", cat: "logs", period: 60,
    when: "on event + every minute",
    label: "job log · weights", ...lab(400, 382, "middle"), pts: pts([300, 390], [500, 390]),
    title: "Logs to the PC",
    detail: "Job events (start, pause, end, filament used), Duet messages and spool weights. Weights are buffered while the PC is off." });
  addFlow({ id: `${k}-esp-boot`, a: esp, b: "logsrv", kind: "pull", cat: "logs", period: null,
    when: "on boot", label: "history at start-up", ...lab(400, 432, "middle"),
    pts: pts([300, 414], [500, 414]),
    title: "Weight history",
    detail: "Last 2 h of spool weights, so time left is accurate right after a restart." });
  addFlow({ id: `${k}-twin-model`, a: "twin", b: duet, kind: "pull", cat: "status", period: 2,
    label: "status · 2 s", ...lab(400, 72, "middle"), pts: pts([500, 80], [300, 80]),
    title: "Printer status",
    detail: "State, job progress and belt position, to update the 3D view." });
  addFlow({ id: `${k}-twin-gcode`, a: "twin", b: duet, kind: "pull", cat: "gcode", period: null,
    when: "once per print", label: "G-code · new print", ...lab(400, 122, "middle"),
    pts: pts([500, 104], [300, 104]),
    title: "G-code file",
    detail: "Downloaded once per print to build the 3D part." });
  addFlow({ id: `${k}-twin-spools`, a: "twin", b: esp, kind: "pull", cat: "weight", period: 5,
    label: "spools · 5 s", ...lab(347, 282, "end"), pts: pts([520, 122], [270, 368]),
    title: "Spool levels",
    detail: "% left, feeding or not, time left, for the spools in the 3D view." });
}

addFlow({ id: "twin-ntfy", a: "twin", b: "ntfy", kind: "push", cat: "alert", period: null,
  when: "only while an ESP32 is offline", label: "backup alerts", lx: 722, ly: 505, anchor: "end",
  pts: [[700, 106], [732, 106], [732, 572], [700, 588]],
  title: "Backup alerts",
  detail: "If an ESP32 doesn't answer for 2 min while its Duet does, the Twin sends “ESP32 Offline” and that printer's alerts (backup_monitor.py) until the ESP32 is back." });

addNode("twin", 600, 90, "twin", "var(--c-gcode)", "Digital Twin", "PC · port 5053",
  "Live 3D view of both printers. Read-only: requests data, never sends commands.");
addNode("logs", 600, 250, "folder", "var(--c-logs)", "logs/ folder", "files on the PC",
  "Job logs, printer messages, spool-weight CSVs and print-time history.");
addNode("logsrv", 600, 400, "server", "var(--c-logs)", "Log Server", "PC · port 5052",
  "Receives the ESP32 logs and writes them to <b>logs/</b>. Optional: alerts work without it.");
addNode("ntfy", 600, 590, "cloud", "var(--c-alert)", "ntfy.sh", "internet service",
  "Notification service. Forwards the ESP32 alerts to subscribed phones.");
addNode("phone", 600, 705, "phone", "var(--c-alert)", "Phone", "ntfy app",
  "Receives the alerts as push notifications.");

addFlow({ id: "ntfy-phone", a: "ntfy", b: "phone", kind: "push", cat: "alert", period: null,
  when: "on each alert", label: "push notification", lx: 612, ly: 652, anchor: "start",
  pts: [[600, 622], [600, 673]],
  title: "Push notification", detail: "Delivered within seconds." });
addFlow({ id: "log-files", a: "logsrv", b: "logs", kind: "push", cat: "logs", period: null,
  when: "on each log", label: "saves files", lx: 612, ly: 330, anchor: "start",
  pts: [[600, 368], [600, 282]],
  title: "Writing logs",
  detail: "One log per print, messages, weight CSVs, print-time history." });
addFlow({ id: "files-twin", a: "twin", b: "logs", kind: "pull", cat: "logs", period: null,
  when: "on new job", label: "past print times", lx: 612, ly: 175, anchor: "start",
  pts: [[600, 122], [600, 218]],
  title: "Print-time history",
  detail: "Average time of the same part, used for the “historic end” estimate." });

const F = Object.fromEntries(FLOWS.map((f) => [f.id, f]));

const STORIES = [
  { title: "Print starts", steps: [
    { text: "ESP32 polls the Duet and detects a new job.", flows: ["p1-esp-model"] },
    { text: "Sends “Print Started” → ntfy → phone.", flows: ["p1-esp-ntfy", "ntfy-phone"] },
    { text: "Sends START to the Log Server → new job log.", flows: ["p1-esp-log", "log-files"] },
    { text: "Twin detects the job and downloads the G-code once.", flows: ["p1-twin-model", "p1-twin-gcode"] },
    { text: "Twin reads past print times to estimate the end.", flows: ["files-twin"] },
  ] },
  { title: "Print pauses", steps: [
    { text: "ESP32 fetches the new Duet message (e.g. filament runout).", flows: ["p1-esp-reply"] },
    { text: "Message saved on the PC.", flows: ["p1-esp-log", "log-files"] },
    { text: "Next poll: state = paused.", flows: ["p1-esp-model"] },
    { text: "Sends “Print Paused” with the message as reason, and PAUSE to the job log.",
      flows: ["p1-esp-ntfy", "ntfy-phone", "p1-esp-log", "log-files"] },
  ] },
  { title: "Spool runs low", steps: [
    { text: "ESP32 reads the load cells every 2 s.", flows: ["p1-cells-esp", "p1-cells-esp"] },
    { text: "Time left below the limit → “Filament Low” alert.", flows: ["p1-esp-ntfy", "ntfy-phone"] },
    { text: "Weights saved on the PC every minute.", flows: ["p1-esp-log", "log-files"] },
    { text: "Twin requests the spool levels and updates the 3D view.", flows: ["p1-twin-spools"] },
  ] },
  { title: "Part reaches the edge", steps: [
    { text: "ESP32 reads the belt position on each poll.", flows: ["p1-esp-model"] },
    { text: "Part near the front → “Part Reaching Edge” (once per print).", flows: ["p1-esp-ntfy", "ntfy-phone"] },
    { text: "EDGE added to the job log.", flows: ["p1-esp-log", "log-files"] },
  ] },
  { title: "Print finishes", steps: [
    { text: "Poll shows no active job.", flows: ["p1-esp-model"] },
    { text: "Sends “Print Finished” with the total time.", flows: ["p1-esp-ntfy", "ntfy-phone"] },
    { text: "Sends END + filament used; complete prints go to the time history.", flows: ["p1-esp-log", "log-files"] },
    { text: "Twin uses that history for future estimates of the same part.", flows: ["files-twin"] },
  ] },
  { title: "ESP32 restarts", steps: [
    { text: "On boot, ESP32 requests the last 2 h of spool weights.", flows: ["p1-esp-boot"] },
    { text: "Time left is available immediately; new readings continue.", flows: ["p1-cells-esp"] },
    { text: "Duet polling resumes.", flows: ["p1-esp-model"] },
  ] },
  { title: "ESP32 goes offline", steps: [
    { text: "Twin asks the ESP32 for its spools every 5 s: no answer for 2 min.", flows: ["p1-twin-spools"] },
    { text: "The Duet still answers the Twin, so the printer is on.", flows: ["p1-twin-model"] },
    { text: "Twin sends “ESP32 Offline” and from now on the printer alerts.", flows: ["twin-ntfy", "ntfy-phone"] },
    { text: "ESP32 answers twice in a row → “ESP32 Back Online”, it sends the alerts again.",
      flows: ["p1-twin-spools", "twin-ntfy", "p1-esp-ntfy"] },
  ] },
];

// ---------------------------------------------------------------- drawing

const svg = document.getElementById("map");

function el(tag, attrs = {}, parent = null) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

function draw() {
  const defs = el("defs", {}, svg);
  const glow = el("filter", { id: "glow", x: "-100%", y: "-100%", width: "300%", height: "300%" }, defs);
  el("feGaussianBlur", { stdDeviation: "2.5", result: "b" }, glow);
  const merge = el("feMerge", {}, glow);
  el("feMergeNode", { in: "b" }, merge);
  el("feMergeNode", { in: "SourceGraphic" }, merge);
  for (const [k, c] of Object.entries(CATS)) {
    const m = el("marker", { id: `arr-${k}`, viewBox: "0 0 10 10", refX: 9, refY: 5,
      markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, defs);
    el("path", { d: "M0,0 L10,5 L0,10 z" }, m).style.fill = c.color;
  }

  const groups = el("g", { class: "group" }, svg);
  for (const [x, w, name] of [[85, 230, "PRINTER 2"], [885, 230, "PRINTER 1"], [445, 310, "LAB PC (optional)"]]) {
    el("rect", { x, y: 18, width: w, height: name.startsWith("LAB") ? 448 : 608, rx: 16 }, groups);
    el("text", { x: x + w / 2, y: 44, "text-anchor": "middle" }, groups).textContent = name;
  }

  const flowLayer = el("g", {}, svg);
  window.pktLayer = el("g", {}, svg);
  const nodeLayer = el("g", {}, svg);

  for (const f of FLOWS) {
    const g = el("g", { class: "flow", "data-id": f.id }, flowLayer);
    const d = "M" + f.pts.map((p) => p.join(" ")).join(" L");
    const line = el("path", { d, class: "line" }, g);
    line.style.stroke = CATS[f.cat].color;
    line.setAttribute(f.kind === "push" ? "marker-end" : "marker-start", `url(#arr-${f.cat})`);
    el("path", { d, class: "hit" }, g);
    el("text", { x: f.lx, y: f.ly, "text-anchor": f.anchor, class: "flabel" }, g).textContent = f.label;
    f.g = g;
    f.path = line;
    f.len = line.getTotalLength();
    g.addEventListener("mouseenter", (e) => showTip(f, e));
    g.addEventListener("mousemove", moveTip);
    g.addEventListener("mouseleave", hideTip);
    g.addEventListener("click", (e) => { e.stopPropagation(); exitStory(); selectFlow(f); });
  }

  for (const n of Object.values(NODES)) {
    const g = el("g", { class: "node", transform: `translate(${n.x - HW} ${n.y - HH})` }, nodeLayer);
    el("rect", { class: "card", width: HW * 2, height: HH * 2, rx: 12 }, g);
    const icon = el("path", { class: "icon", d: ICONS[n.icon], transform: "translate(12 14)" }, g);
    icon.style.stroke = n.color;
    el("text", { class: "t1", x: 58, y: 28 }, g).textContent = n.t1;
    el("text", { class: "t2", x: 58, y: 46 }, g).textContent = n.t2;
    n.g = g;
    g.addEventListener("click", (e) => { e.stopPropagation(); exitStory(); selectNode(n); });
  }

  svg.addEventListener("click", () => { exitStory(); clearSelection(); });
}

// ---------------------------------------------------------------- animation

let simT = 0, last = performance.now(), paused = false, speed = 0.5;
let packets = [];
let timers = [];

const visPeriod = (p) => 0.9 + 1.25 * Math.log2(p / 0.5); // 0.5 s → 0.9, 10 s → 6.3, 60 s → 9.5
const interval = (f) => (f.period ? visPeriod(f.period) : 14 + Math.random() * 16);

function after(s, fn) { timers.push({ at: simT + s, fn }); }

function spawn(f, phase, onDone) {
  const c = el("circle", { r: phase === "req" ? 4 : 6, class: phase === "req" ? "pkt-req" : "pkt-data",
    cx: -20, cy: -20 }, window.pktLayer);
  c.style[phase === "req" ? "stroke" : "fill"] = CATS[f.cat].color;
  packets.push({ f, phase, fwd: phase === "req" || f.kind === "push", t0: simT, dur: f.len / SPEED, el: c, onDone });
}

// One exchange: a push sends data a→b; a pull sends a question a→b and the data comes back b→a.
function trigger(f, onDone) {
  if (f.kind === "push") spawn(f, "data", onDone);
  else spawn(f, "req", () => spawn(f, "data", onDone));
}

function ping(nodeId, color) {
  const g = NODES[nodeId].g;
  g.style.setProperty("--pc", color);
  g.classList.remove("ping");
  void g.getBoundingClientRect();
  g.classList.add("ping");
}

function clearPackets() {
  for (const p of packets) p.el.remove();
  packets = [];
  timers = [];
}

function update() {
  const due = timers.filter((t) => t.at <= simT);
  timers = timers.filter((t) => t.at > simT);
  due.forEach((t) => t.fn());

  if (!story) {
    for (const f of FLOWS) {
      if (f.next == null) f.next = simT + Math.random() * interval(f);
      if (simT >= f.next) { trigger(f); f.next = simT + interval(f); }
    }
  }

  const arrived = [];
  packets = packets.filter((p) => {
    const u = (simT - p.t0) / p.dur;
    if (u >= 1) { p.el.remove(); arrived.push(p); return false; }
    const pt = p.f.path.getPointAtLength(p.f.len * (p.fwd ? u : 1 - u));
    p.el.setAttribute("cx", pt.x.toFixed(1));
    p.el.setAttribute("cy", pt.y.toFixed(1));
    p.el.style.opacity = !focus || focus.has(p.f.id) ? 1 : 0.08;
    return true;
  });
  for (const p of arrived) {
    if (p.phase === "data" && (p.f.ping || story)) ping(p.fwd ? p.f.b : p.f.a, CATS[p.f.cat].color);
    if (p.onDone) p.onDone();
  }
}

function frame(now) {
  const dt = Math.min(0.1, (now - last) / 1000);
  last = now;
  if (!paused) { simT += dt * speed; update(); }
  requestAnimationFrame(frame);
}

// ---------------------------------------------------------------- focus, panel, tooltip

let focus = null; // Set of flow ids, or null = everything

function setFocus(flowIds, nodeIds = []) {
  focus = flowIds ? new Set(flowIds) : null;
  svg.classList.toggle("focus", !!focus);
  const onNodes = new Set(nodeIds);
  for (const f of FLOWS) {
    const on = !!focus && focus.has(f.id);
    f.g.classList.toggle("on", on);
    if (on) { onNodes.add(f.a); onNodes.add(f.b); }
  }
  for (const n of Object.values(NODES)) n.g.classList.toggle("on", onNodes.has(n.id));
}

function markSel(node, flow) {
  for (const n of Object.values(NODES)) n.g.classList.toggle("sel", n === node);
  for (const f of FLOWS) f.g.classList.toggle("sel", f === flow);
}

const fmtPeriod = (p) => (p < 60 ? `every ${p} s` : p === 60 ? "every minute" : `every ${p / 60} min`);
const whenText = (f) => f.when || fmtPeriod(f.period);
const whoText = (f) => f.kind === "pull"
  ? `${NODES[f.a].t1} requests from ${NODES[f.b].t1}`
  : `${NODES[f.a].t1} → ${NODES[f.b].t1}`;
const chip = (cat) => `<span class="chip" style="background:${CATS[cat].color}"></span>`;

const panel = document.getElementById("panel");

function flowItem(f) {
  return `<li>${chip(f.cat)}<span class="flowlink" data-flow="${f.id}">${f.title}</span>
    <span class="muted">· ${whenText(f)}</span></li>`;
}

function bindLinks() {
  panel.querySelectorAll("[data-flow]").forEach((a) =>
    a.addEventListener("click", () => selectFlow(F[a.dataset.flow])));
  panel.querySelectorAll("[data-node]").forEach((a) =>
    a.addEventListener("click", () => selectNode(NODES[a.dataset.node])));
}

function showIntro() {
  panel.innerHTML = `
    <h2>Who requests what</h2>
    <p><span class="ring"></span>request<br>
       <span class="chip" style="background:var(--text)"></span>data · the arrow shows its direction</p>
    <ul>
      <li><b>Duet</b>: only answers requests.</li>
      <li><b>ESP32</b>: monitors its printer, sends alerts and logs.</li>
      <li><b>PC</b>: optional; alerts work without it.</li>
      <li><b>Twin</b>: read-only.</li>
    </ul>
    <h3>Use</h3>
    <ul>
      <li>Click a box: show its flows.</li>
      <li>Hover a line: details.</li>
      <li>Pick a scenario below: step by step.</li>
    </ul>
    <p class="muted">Animation slowed down; real intervals are on each line.</p>`;
}

function selectNode(n) {
  const mine = FLOWS.filter((f) => f.a === n.id || f.b === n.id);
  setFocus(mine.map((f) => f.id), [n.id]);
  markSel(n, null);
  const list = (title, fl) => (fl.length ? `<h3>${title}</h3><ul>${fl.map(flowItem).join("")}</ul>` : "");
  panel.innerHTML = `<h2>${n.t1}</h2><p class="muted">${n.t2}</p><p>${n.desc}</p>
    ${list("Asks for", mine.filter((f) => f.kind === "pull" && f.a === n.id))}
    ${list("Answers", mine.filter((f) => f.kind === "pull" && f.b === n.id))}
    ${list("Sends", mine.filter((f) => f.kind === "push" && f.a === n.id))}
    ${list("Receives", mine.filter((f) => f.kind === "push" && f.b === n.id))}`;
  bindLinks();
}

function selectFlow(f) {
  setFocus([f.id]);
  markSel(null, f);
  panel.innerHTML = `<h2>${chip(f.cat)}${f.title}</h2>
    <p><b>${whoText(f)}</b><br><span class="muted">${whenText(f)}</span></p>
    <p>${f.detail}</p>
    <h3>Devices</h3><ul>
      <li><span class="flowlink" data-node="${f.a}">${NODES[f.a].t1}</span></li>
      <li><span class="flowlink" data-node="${f.b}">${NODES[f.b].t1}</span></li></ul>`;
  bindLinks();
}

function clearSelection() {
  setFocus(null);
  markSel(null, null);
  showIntro();
}

const tip = document.getElementById("tip");
function showTip(f, e) {
  tip.innerHTML = `<b>${chip(f.cat)}${f.title}</b><span class="muted">${whoText(f)} · ${whenText(f)}</span>
    <div style="margin-top:4px">${f.detail}</div>`;
  tip.style.display = "block";
  moveTip(e);
}
function moveTip(e) {
  const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
  const y = Math.min(e.clientY + 14, window.innerHeight - tip.offsetHeight - 8);
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}
function hideTip() { tip.style.display = "none"; }

// ---------------------------------------------------------------- stories

let story = null; // { s, i, btn }
const cap = document.getElementById("caption");
const capText = document.getElementById("capText");
const capStep = document.getElementById("capStep");
const capNext = document.getElementById("capNext");
const capPrev = document.getElementById("capPrev");

function chain(ids, k, done) {
  if (k >= ids.length) return done();
  trigger(F[ids[k]], () => after(0.25, () => chain(ids, k + 1, done)));
}

function showStep(i) {
  clearPackets();
  story.i = i;
  const steps = story.s.steps, st = steps[i];
  setFocus(st.flows);
  markSel(null, null);
  capStep.textContent = `${story.s.title} · ${i + 1}/${steps.length}`;
  capText.textContent = st.text;
  capPrev.disabled = i === 0;
  capNext.textContent = i === steps.length - 1 ? "Replay" : "Next";
  const s = story.s;
  chain(st.flows, 0, () => after(2.2, () => {
    if (story && story.s === s && story.i === i && i + 1 < steps.length) showStep(i + 1);
  }));
}

function startStory(s, btn) {
  exitStory();
  story = { s, i: 0, btn };
  btn.classList.add("on");
  cap.classList.add("show");
  panel.innerHTML = `<h2>${s.title}</h2><ol>${s.steps.map((st) => `<li>${st.text}</li>`).join("")}</ol>
    <p class="muted">Auto-advances; use Back / Next.</p>`;
  showStep(0);
}

function exitStory() {
  if (!story) return;
  story.btn.classList.remove("on");
  story = null;
  cap.classList.remove("show");
  clearPackets();
  for (const f of FLOWS) f.next = simT + Math.random() * 2;
  clearSelection();
}

capPrev.addEventListener("click", () => story && story.i > 0 && showStep(story.i - 1));
capNext.addEventListener("click", () => {
  if (!story) return;
  showStep(story.i + 1 < story.s.steps.length ? story.i + 1 : 0);
});
document.getElementById("capExit").addEventListener("click", exitStory);

// ---------------------------------------------------------------- controls, legend, start

function setup() {
  const stories = document.getElementById("stories");
  for (const s of STORIES) {
    const b = document.createElement("button");
    b.textContent = s.title;
    b.addEventListener("click", () => startStory(s, b));
    stories.appendChild(b);
  }

  document.getElementById("legend").innerHTML =
    Object.values(CATS).map((c) => `<span><span class="chip" style="background:${c.color}"></span>${c.name}</span>`).join("") +
    `<span><span class="ring"></span>request</span><span>arrow = data direction</span>`;

  const pauseBtn = document.getElementById("pause");
  const setPaused = (v) => { paused = v; pauseBtn.textContent = v ? "Play" : "Pause"; pauseBtn.classList.toggle("on", v); };
  pauseBtn.addEventListener("click", () => setPaused(!paused));
  document.getElementById("speed").addEventListener("input", (e) => { speed = +e.target.value; });
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) setPaused(true);

  draw();
  showIntro();
  // flow.html#story-N opens story N (1-based) directly
  const m = location.hash.match(/^#story-(\d+)$/);
  const btns = stories.querySelectorAll("button");
  if (m && btns[+m[1] - 1]) startStory(STORIES[+m[1] - 1], btns[+m[1] - 1]);
  requestAnimationFrame(frame);
}

setup();
