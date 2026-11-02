// Embedded web pages (served by web.cpp)
#pragma once
#include <Arduino.h>

// Dashboard: load cells (time left per spool) + printer state for BOTH printers. Data comes from
// this ESP32's /api/spools and from the peer ESP32's (URL in /api/config, CORS open).
const char DASHBOARD_HTML[] PROGMEM = R"rawliteral(<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Spool Monitor</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Patrick+Hand&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0d0d0d; --panel: #141414; --line: #555; --text: #f0f0f0; --muted: #888;
      --value: #d8ffd8; --ok: #3fae5a; --warn: #e0a030; --bad: #e05050;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      min-height: 100vh; background: var(--bg); color: var(--text);
      font-family: "Patrick Hand", "Segoe Print", cursive; font-size: 1.15rem;
      display: flex; justify-content: center; padding: 1.5rem 1rem;
    }
    a { color: var(--muted); }
    .dashboard {
      width: min(1200px, 100%); border: 1px solid var(--line); border-radius: 14px;
      background: var(--panel); overflow: hidden;
    }
    .header { border-bottom: 1px solid var(--line); padding: 0.75rem 1rem; text-align: center; letter-spacing: 0.04em; }
    .header small { color: var(--muted); display: block; font-size: 0.95rem; }
    .printers { display: grid; grid-template-columns: 1fr 1fr; }
    .printer { padding: 1.25rem; display: flex; flex-direction: column; gap: 1rem; }
    .printer + .printer { border-left: 1px solid var(--line); }
    .printer-head {
      border: 1px solid var(--line); border-radius: 12px; padding: 0.7rem 1rem;
      display: flex; flex-direction: column; gap: 0.4rem;
    }
    .row { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
    .job { color: var(--muted); font-size: 0.95rem; overflow-wrap: anywhere; }
    .badge { font-size: 0.95rem; padding: 0.1rem 0.6rem; border-radius: 999px; border: 1px solid currentColor; }
    .badge.ok { color: var(--ok); }
    .badge.warn { color: var(--warn); }
    .badge.bad { color: var(--bad); }
    .badge.muted { color: var(--muted); }
    .cell { border: 1px solid var(--line); border-radius: 12px; padding: 1rem; display: flex; flex-direction: column; gap: 0.75rem; }
    .cell-title { display: flex; justify-content: space-between; align-items: baseline; }
    .cell-title h3 { font-weight: normal; font-size: 1.35rem; }
    .time-left { text-align: center; padding: 0.6rem 0.4rem; border-radius: 10px; background: #1c1c1c; }
    .time-left .big { font-size: 2rem; color: var(--value); line-height: 1.1; }
    .time-left .big.warn { color: var(--warn); }
    .time-left .big.bad { color: var(--bad); }
    .time-left .big.muted { color: var(--muted); }
    .time-left .sub { color: var(--muted); font-size: 0.95rem; }
    .bar { height: 10px; border-radius: 5px; background: #262626; overflow: hidden; }
    .bar > div { height: 100%; width: 0; background: var(--ok); transition: width 0.4s; }
    dl { display: grid; grid-template-columns: auto 1fr; gap: 0.25rem 1rem; }
    dt { color: var(--muted); }
    dd { text-align: right; color: var(--value); font-variant-numeric: tabular-nums; }
    dd.muted { color: var(--muted); }
    .note { color: var(--muted); font-size: 0.9rem; }
    .all-idle { text-align: center; padding: 1rem 0; }
    [hidden] { display: none !important; }
    @media (max-width: 800px) {
      .printers { grid-template-columns: 1fr; }
      .printer + .printer { border-left: none; border-top: 1px solid var(--line); }
    }
  </style>
</head>
<body>
  <div class="dashboard">
    <header class="header">
      Spool Monitor — <span id="clock">—</span>
      <small id="window-info">Live rate = slope over the last hours</small>
    </header>
    <div class="printers" id="printers"></div>
  </div>

  <template id="printer-tpl">
    <section class="printer">
      <div class="printer-head">
        <div class="row"><span class="p-label">—</span><span class="badge p-state muted">—</span></div>
        <div class="row">
          <span class="job p-job">—</span>
          <span class="note">cells <span class="badge p-cells muted">—</span></span>
        </div>
        <div class="note"><a class="p-calib" href="#">calibration</a> · <a class="p-config" href="#">config</a></div>
      </div>
      <div class="note p-msg" hidden></div>
    </section>
  </template>

  <template id="cell-tpl">
    <div class="cell">
      <div class="cell-title"><h3 class="c-name">—</h3><span class="c-percent note">—</span></div>
      <div class="bar"><div></div></div>
      <div class="time-left">
        <div class="big c-left">—</div>
        <div class="sub c-left-sub">—</div>
      </div>
      <dl>
        <dt>Current reading</dt><dd class="c-current">—</dd>
        <dt>Empty reel</dt><dd class="c-empty">—</dd>
        <dt>Live rate</dt><dd class="c-live">—</dd>
        <dt>Reference rate</dt><dd class="c-ref">—</dd>
      </dl>
    </div>
  </template>

  <script>
    const ORDER = ["p1", "p2"];
    const LABELS = { p1: "Printer 1 (243)", p2: "Printer 2 (188)" };

    const nums = (v, d = 0) =>
      v === null || v === undefined ? "—" : v.toLocaleString("en-US", { maximumFractionDigits: d });

    function fmtHours(h) {
      if (h === null || h === undefined) return "—";
      const total = Math.round(h * 60);
      const hh = Math.floor(total / 60), mm = total % 60;
      return hh > 0 ? `${hh} h ${String(mm).padStart(2, "0")} min` : `${mm} min`;
    }

    // Date/time the spool runs out, e.g. "Today 18:40" or "Wed 29 Oct 22:25"
    function fmtEta(h) {
      if (h === null || h === undefined) return "—";
      const t = new Date(Date.now() + h * 3600e3);
      const sameDay = t.toDateString() === new Date().toDateString();
      const time = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      const day = t.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
      return sameDay ? `Today ${time}` : `${day} ${time}`;
    }

    function setVal(el, text, muted) {
      el.textContent = text;
      el.classList.toggle("muted", !!muted);
    }

    function setBadge(el, text, cls) {
      el.textContent = text;
      el.className = el.className.replace(/\b(ok|warn|bad|muted)\b/g, "").trim() + " " + cls;
    }

    const built = {};

    function build(key) {
      const p = document.getElementById("printer-tpl").content.firstElementChild.cloneNode(true);
      p.querySelector(".p-label").textContent = LABELS[key] || key;
      const cells = {};
      for (const c of ["cell1", "cell2"]) {
        const el = document.getElementById("cell-tpl").content.firstElementChild.cloneNode(true);
        el.querySelector(".c-name").textContent = `${key}_${c}`;
        el.hidden = true;
        p.appendChild(el);
        cells[c] = el;
      }
      const none = document.createElement("div");
      none.className = "note all-idle";
      none.textContent = "All spools idle";
      none.hidden = true;
      p.appendChild(none);
      document.getElementById("printers").appendChild(p);
      built[key] = { root: p, cells };
    }
    ORDER.forEach(build);

    const STATE_CLASS = {
      processing: "ok", simulating: "ok", resuming: "ok", busy: "ok",
      paused: "warn", pausing: "warn", changingTool: "warn",
      halted: "bad", cancelling: "bad", off: "bad",
    };

    // Shows a message instead of the cells (ESP32 unreachable, not configured...)
    function message(key, text) {
      const { root, cells } = built[key];
      const msg = root.querySelector(".p-msg");
      msg.hidden = !text;
      msg.textContent = text || "";
      if (text) {
        for (const el of Object.values(cells)) el.hidden = true;
        root.querySelector(".all-idle").hidden = true;
        setBadge(root.querySelector(".p-cells"), "unreachable", "bad");
        setBadge(root.querySelector(".p-state"), "unknown", "muted");
      }
    }

    function renderPrinter(key, p, base) {
      if (!built[key]) build(key);
      const { root, cells } = built[key];
      message(key, "");
      root.querySelector(".p-label").textContent = `${p.label}${p.ip ? " · " + p.ip : ""}`;
      root.querySelector(".p-calib").href = `${base}/calib`;
      root.querySelector(".p-config").href = `${base}/config`;
      const cb = root.querySelector(".p-cells");
      setBadge(cb, p.online ? "online" : "offline", p.online ? "ok" : "bad");
      cb.title = p.error || "";

      const pr = p.printer || {};
      const st = root.querySelector(".p-state");
      if (!pr.online) setBadge(st, "printer offline", "bad");
      else setBadge(st, pr.status || "—", STATE_CLASS[pr.status] || "muted");
      const job = pr.file ? `${pr.file.split("/").pop()}${pr.layer != null ? " · layer " + pr.layer : ""}` : "no job";
      root.querySelector(".p-job").textContent = job;

      let shown = 0;
      for (const [c, v] of Object.entries(p.cells)) {
        const el = cells[c];
        if (!el) continue;
        const q = (s) => el.querySelector(s);

        setVal(q(".c-empty"), nums(v.empty), v.empty === null);
        setVal(q(".c-current"), nums(v.current), v.current === null);
        setVal(q(".c-ref"), v.ref_rate ? `${nums(v.ref_rate)} /h` : "—", !v.ref_rate);
        const live = v.live_rate === null
          ? `collecting (${Math.round(v.live_span / 60)} min)`
          : `${nums(v.live_rate)} /h` + (v.consuming ? "" : " (idle)");
        setVal(q(".c-live"), live, !v.consuming);

        // Idle = live rate known but below the consuming threshold: hidden, no countdown
        const idle = v.live_rate !== null && !v.consuming;
        el.hidden = idle;
        if (idle) continue;
        shown++;
        // Headline: when the spool runs out (live estimate when consuming, else reference)
        const h = v.consuming ? v.hours_left_live : v.hours_left_ref;
        const big = q(".c-left");
        big.textContent = fmtEta(h);
        big.className = "big c-left " + (h === null ? "muted" : h < 1 ? "bad" : h < 2 ? "warn" : "");
        let sub;
        if (v.empty === null) sub = "empty reel not calibrated";
        else if (v.current === null) sub = "no reading";
        else if (h === null) sub = "no rate yet";
        else {
          const basis = v.consuming ? "at live rate" : "at reference rate (collecting live data)";
          sub = `${fmtHours(h)} left · ${basis}`;
        }
        q(".c-left-sub").textContent = sub;

        q(".c-percent").textContent = v.percent === null ? "" : `${nums(v.percent, 1)} %`;
        const bar = q(".bar > div");
        bar.style.width = `${v.percent ?? 0}%`;
        bar.style.background = h !== null && h < 1 ? "var(--bad)" : h !== null && h < 2 ? "var(--warn)" : "var(--ok)";
      }
      root.querySelector(".all-idle").hidden = shown > 0;
    }

    function render(data, base) {
      for (const [key, p] of Object.entries(data.printers)) renderPrinter(key, p, base);
    }

    async function getJson(url, ms = 4000) {
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), ms);
      try {
        const res = await fetch(url, { cache: "no-store", signal: ctl.signal });
        if (!res.ok) throw new Error(res.status);
        return await res.json();
      } finally {
        clearTimeout(timer);
      }
    }

    let busy = false;
    async function refresh() {
      if (busy) return;
      busy = true;
      try {
        let local;
        try { local = await getJson("/api/spools"); } catch (_) { return; }  // restarting
        document.getElementById("clock").textContent = local.time || "waiting for clock (NTP)";
        document.getElementById("window-info").textContent =
          `Live rate = slope over the last ${Math.round(local.rate_window / 60)} min ` +
          `(needs ${Math.round(local.min_rate_span / 60)} min of data) · time left = (reading − empty) / rate`;
        render(local, "");

        const peerKey = ORDER.find((k) => k !== local.self);
        if (!local.peer) {
          message(peerKey, "Other printer's ESP32 not set (config page).");
        } else {
          try { render(await getJson(local.peer + "/api/spools"), local.peer); }
          catch (_) { message(peerKey, `Other printer's ESP32 not reachable (${local.peer}).`); }
        }
      } finally {
        busy = false;
      }
    }

    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>)rawliteral";

// Calibration page (button + live mean / std dev), same as before on "/"
const char CALIB_HTML[] PROGMEM = R"rawliteral(<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Load cell calibration</title>
<style>
body{font-family:sans-serif;max-width:480px;margin:24px auto;padding:0 16px}
button{font-size:1.1em;padding:10px 18px;margin-right:8px}
table{border-collapse:collapse;margin-top:16px;width:100%}
td,th{border:1px solid #ccc;padding:6px 10px;text-align:right}
th:first-child,td:first-child{text-align:left}
</style></head><body>
<p><a href="/">&larr; Dashboard</a></p>
<h2>Load cell calibration</h2>
<p>Live: cell 1 <b id="l1">-</b>, cell 2 <b id="l2">-</b></p>
<button id="start" onclick="post('start')">Start calibration</button>
<button id="cancel" onclick="post('cancel')">Cancel</button>
<p id="status">-</p>
<table><tr><th>Cell</th><th>Mean</th><th>Std dev</th><th>Result</th></tr>
<tr><td>1</td><td id="m1">-</td><td id="s1">-</td><td id="r1">-</td></tr>
<tr><td>2</td><td id="m2">-</td><td id="s2">-</td><td id="r2">-</td></tr></table>
<p style="color:#666">When it finishes, a mean close to the saved empty reel or full spool value
(see <a href="/config">config</a>) replaces it. Calibrate with the spool at rest, not printing.</p>
<script>
const f=v=>Math.round(v).toLocaleString();
const msg={no_match:'not near empty or full, not saved',unstable:'not stable (std dev too high), not saved',
  printing:'printing, not saved'};
const res=r=>!r.result?'-':r.changed?'saved as '+r.result+' (was '+f(r.before)+')'+(r.saved?'':' - flash error!'):msg[r.result];
function show(c){
  document.getElementById('status').textContent=
    c.state+' - '+c.readings+'/'+c.window+' readings, '+c.elapsed_s+' s';
  const has=c.readings>0;
  m1.textContent=has?f(c.cell1.mean):'-'; s1.textContent=has?f(c.cell1.std_dev):'-';
  m2.textContent=has?f(c.cell2.mean):'-'; s2.textContent=has?f(c.cell2.std_dev):'-';
  r1.textContent=res(c.cell1); r2.textContent=res(c.cell2);
  document.getElementById('cancel').disabled=c.state!='running';
}
function post(a){fetch('/calibration/'+a,{method:'POST'}).then(r=>r.json()).then(show);}
function poll(){
  fetch('/calibration').then(r=>r.json()).then(show).catch(()=>{});
  fetch('/data').then(r=>r.json()).then(d=>{l1.textContent=f(d.cell1);l2.textContent=f(d.cell2);}).catch(()=>{});
}
poll();setInterval(poll,2000);
</script></body></html>)rawliteral";

// Config page: per-cell full / empty / ref_rate / note (what spools.json used to hold) + peer URL
const char CONFIG_HTML[] PROGMEM = R"rawliteral(<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Spool config</title>
<style>
body{font-family:sans-serif;max-width:640px;margin:24px auto;padding:0 16px}
fieldset{margin:12px 0;padding:8px 12px}
label{display:block;margin:6px 0}
input{width:100%;padding:6px;font-size:1em;box-sizing:border-box}
button{font-size:1.1em;padding:10px 18px;margin-top:8px}
.help{color:#666;font-size:.9em}
</style></head><body>
<p><a href="/">&larr; Dashboard</a></p>
<h2 id="title">Spool config</h2>
<p class="help">Raw HX711 counts (no tare); the reading goes up with weight. ref_rate = counts/h the
reading drops while printing. Leave a field empty = not measured yet. Saved in the ESP32's flash.</p>
<label>Other printer's ESP32 (for the dashboard)<input id="peer" placeholder="http://192.168.1.x"></label>
<div id="cells"></div>
<button onclick="save()">Save</button> <span id="msg"></span>
<script>
const CELLS=['cell1','cell2'], FIELDS=['full','empty','ref_rate','note'];
function load(){
  fetch('/api/config').then(r=>r.json()).then(c=>{
    document.getElementById('title').textContent='Spool config — '+c.label;
    document.getElementById('peer').value=c.peer||'';
    document.getElementById('cells').innerHTML=CELLS.map(n=>'<fieldset><legend>'+n+'</legend>'+
      FIELDS.map(f=>'<label>'+f+'<input id="'+n+'_'+f+'"></label>').join('')+'</fieldset>').join('');
    for(const n of CELLS)for(const f of FIELDS){
      const v=c.cells[n][f];document.getElementById(n+'_'+f).value=v===null||v===undefined?'':v;}
  });
}
function save(){
  const out={peer:document.getElementById('peer').value.trim(),cells:{}};
  for(const n of CELLS){
    out.cells[n]={};
    for(const f of FIELDS){
      const s=document.getElementById(n+'_'+f).value.trim();
      out.cells[n][f]=f==='note'?s:(s===''?null:Number(s.replace(/,/g,'')));
    }
  }
  const msg=document.getElementById('msg');
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(out)})
    .then(r=>r.json().then(j=>{msg.textContent=r.ok?'Saved':'Error: '+j.error;if(r.ok)load();}))
    .catch(e=>msg.textContent='Error: '+e);
}
load();
</script></body></html>)rawliteral";
