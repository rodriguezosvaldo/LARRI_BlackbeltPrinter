#include <WiFi.h>
#include <WebServer.h>
#include <DHT.h>

// Network credentials
const char* ssid = "thecage";
const char* password = "12345678";

// ---- Sensors -------------------------------------------------------------
// Two DHT22s. Sensor 1 is the primary (drives the thermostat by default).
// Wire the second sensor's data line to DHTPIN2; both share 3V3 + GND and
// each wants its own ~10k pull-up on the data line.
#define DHTPIN1 33
#define DHTPIN2 32                 // second sensor data pin (change as needed)
#define DHTTYPE DHT22
DHT dht1(DHTPIN1, DHTTYPE);
DHT dht2(DHTPIN2, DHTTYPE);

// Heater / relay control
#define HEATER_PIN 26              // GPIO driving the relay/heater (change as needed)
float targetTemp = NAN;            // setpoint in °C; NAN = control disabled
bool heating = false;              // current LOGICAL output state (thermostat/manual decision)
const float HYSTERESIS = 0.5;      // °C deadband; set to 0.0 for exact on/off at target

// Soft-PWM "dimmer" timing, applied in BOTH auto and manual modes.
// These do not decide *whether* to heat (that's `heating`); they decide how the output
// is chopped while heating. The effective power is simply pulse / period.
//   - pwmPeriodMs: full cycle length. The "every N seconds" knob.
//   - pwmPulseMs:  on-time within each cycle. The "on for N seconds" knob.
// Both are set from the web UI in seconds and stored here as ms. Accuracy is loose.
//
// Relay wear note: a mechanical relay switches ~2x per period, so longer periods are
// gentler; a heater's thermal mass makes a slow period (several seconds) fine. Shorten
// only if you're driving an SSR.
unsigned long pwmPeriodMs = 5000;  // default 5 s cycle
unsigned long pwmPulseMs  = 5000;  // default = full period -> always on while heating
const unsigned long PWM_PERIOD_MIN_MS = 100UL;     // floor: avoid modulo-by-zero / silly rates
const unsigned long PWM_TIME_MAX_MS   = 600000UL;  // 10 min ceiling on either field

// Control mode
enum HeaterMode { MODE_AUTO, MODE_MANUAL };
HeaterMode heaterMode = MODE_AUTO; // start in thermostat (auto) mode
bool manualOn = false;             // desired output while in manual mode

WebServer server(80);

// ---- Per-sensor state ----------------------------------------------------
// Moving-average ring buffer (smoothing) + a longer ring buffer for plotting.
// One struct per sensor so the two stay independent; reads happen on the same
// cadence so their plot histories advance in lockstep (shared X axis).
const int SAMPLE_COUNT = 10;
const int HISTORY_SIZE = 1200;

struct SensorState {
  DHT*  dht = nullptr;
  // moving average
  float tempSamples[SAMPLE_COUNT];
  float humSamples[SAMPLE_COUNT];
  int   sampleIndex = 0;
  int   sampleCount = 0;
  // plotting history
  float tempHistory[HISTORY_SIZE];
  float humHistory[HISTORY_SIZE];
  int   historyIndex = 0;
  int   historyCount = 0;
};
SensorState s1, s2;

unsigned long lastReadMs = 0;
const unsigned long READ_INTERVAL_MS = 2000;

const char index_html[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta charset="utf-8">
    <title>Printer Climate</title>
    <style>
        :root {
            --bg:    #0e0e0e;
            --card:  #181818;
            --inset: #222222;
            --border:#2b2b2b;
            --text:  #ededed;
            --text2: #9b9b9b;
            --text3: #6a6a6a;
            --accent:#e8e8e8;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            padding: 20px 16px 32px;
            background: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            -webkit-font-smoothing: antialiased;
        }
        .app {
            max-width: 720px;
            margin: 0 auto;
            display: flex;
            flex-direction: column;
            gap: 14px;
        }
        header {
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            padding: 2px 2px 4px;
        }
        h1 { font-size: 17px; font-weight: 600; letter-spacing: .3px; margin: 0; }
        #status { font-size: 12px; color: var(--text3); font-variant-numeric: tabular-nums; }

        .card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 18px;
        }
        .card-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 14px;
        }
        .card-title {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 1.4px;
            color: var(--text2);
            font-weight: 600;
        }
        .legend { display: flex; gap: 16px; }
        .lg { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--text2); }
        .lg i { width: 16px; height: 3px; border-radius: 2px; display: inline-block; }

        /* Top row: temps (left) + control (right); collapses on narrow screens */
        .top {
            display: grid;
            grid-template-columns: minmax(0, 0.8fr) minmax(0, 1fr);
            gap: 14px;
        }
        .temp-readout { display: flex; flex-direction: column; }
        .temp-readout .stat-stack { display: flex; flex-direction: column; gap: 12px; flex: 1; }
        .temp-readout .stat { flex: 1; display: flex; flex-direction: column; justify-content: center; }
        @media (max-width: 560px) {
            .top { grid-template-columns: 1fr; }
            .temp-readout .stat { flex: initial; }
        }

        .stats { display: flex; gap: 12px; margin-bottom: 4px; }
        .stat {
            flex: 1;
            background: var(--inset);
            border-radius: 9px;
            padding: 13px 15px;
        }
        .stat-val { font-size: 34px; font-weight: 600; line-height: 1; letter-spacing: -1px; font-variant-numeric: tabular-nums; }
        .stat-val .u { font-size: 15px; color: var(--text3); font-weight: 500; margin-left: 3px; letter-spacing: 0; }
        .stat-sub { display: flex; align-items: center; gap: 6px; margin-top: 8px; font-size: 11px; color: var(--text2); }
        .stat-sub i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }

        canvas { width: 100%; height: 150px; display: block; margin-top: 8px; }

        .controls { display: flex; flex-direction: column; gap: 12px; }
        .controls .card-head { margin-bottom: 0; }
        .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
        .row .k {
            font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
            color: var(--text2); min-width: 60px;
        }
        input[type=number] {
            background: var(--inset); color: var(--text);
            border: 1px solid var(--border); border-radius: 7px;
            padding: 7px 8px; width: 74px; font-size: 14px; text-align: center;
            font-family: inherit; font-variant-numeric: tabular-nums;
        }
        input[type=number]:focus { outline: none; border-color: #454545; }
        .suffix { font-size: 12px; color: var(--text3); }
        button {
            background: var(--inset); color: var(--text);
            border: 1px solid var(--border); border-radius: 7px;
            padding: 7px 14px; font-size: 13px; cursor: pointer; font-family: inherit;
        }
        button:hover:not(:disabled) { border-color: #454545; }
        button:disabled { opacity: .32; cursor: not-allowed; }
        .mode-btn.active { background: var(--accent); color: #111; border-color: var(--accent); }

        .heat { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--text2); margin-left: auto; }
        .heat .dot { width: 9px; height: 9px; border-radius: 50%; background: #444; transition: background .2s; }
        .heat.on .dot { background: #ff5252; box-shadow: 0 0 8px rgba(255,82,82,.55); }
        .divider { height: 1px; background: var(--border); margin: 2px 0; }
    </style>
</head>
<body>
    <div class="app">
        <header>
            <h1>Printer Climate</h1>
            <div id="status">Loading…</div>
        </header>

        <div class="top">
            <section class="card temp-readout">
                <div class="card-head">
                    <span class="card-title">Temperature</span>
                </div>
                <div class="stat-stack">
                    <div class="stat">
                        <div class="stat-val"><span id="t1">--</span><span class="u">&deg;C</span></div>
                        <div class="stat-sub"><i id="dotT1"></i>Sensor 1</div>
                    </div>
                    <div class="stat">
                        <div class="stat-val"><span id="t2">--</span><span class="u">&deg;C</span></div>
                        <div class="stat-sub"><i id="dotT2"></i>Sensor 2</div>
                    </div>
                </div>
            </section>

            <section class="card controls">
                <div class="card-head">
                    <span class="card-title">Control</span>
                    <span class="heat" id="heat"><span class="dot"></span><span id="heatTxt">Off</span></span>
                </div>
                <div class="row">
                    <span class="k">Mode</span>
                    <button id="modeAuto" class="mode-btn" onclick="setMode('auto')">Auto</button>
                    <button id="modeManual" class="mode-btn" onclick="setMode('manual')">Manual</button>
                </div>
                <div class="row" id="autoControl">
                    <span class="k">Target</span>
                    <input id="target" type="number" step="0.1" placeholder="--">
                    <span class="suffix">&deg;C</span>
                    <button id="setBtn" onclick="setTarget()">Set</button>
                </div>
                <div class="row" id="manualControl">
                    <span class="k">Output</span>
                    <button id="manOn" onclick="setManual('on')">ON</button>
                    <button id="manOff" onclick="setManual('off')">OFF</button>
                </div>
                <div class="row" id="pwmControl">
                    <span class="k">Timing</span>
                    <input id="pulse" type="number" step="0.1" min="0" onchange="setPwm()">
                    <span class="suffix">s on, every</span>
                    <input id="period" type="number" step="0.1" min="0.1" onchange="setPwm()">
                    <span class="suffix">s</span>
                </div>
            </section>
        </div>

        <section class="card">
            <div class="card-head">
                <span class="card-title">Temperature</span>
                <div class="legend">
                    <span class="lg"><i id="lgT1"></i>Sensor 1</span>
                    <span class="lg"><i id="lgT2"></i>Sensor 2</span>
                </div>
            </div>
            <canvas id="tempChart" width="600" height="150"></canvas>
        </section>

        <section class="card">
            <div class="card-head">
                <span class="card-title">Humidity</span>
                <div class="legend">
                    <span class="lg"><i id="lgH1"></i>Sensor 1</span>
                    <span class="lg"><i id="lgH2"></i>Sensor 2</span>
                </div>
            </div>
            <div class="stats">
                <div class="stat">
                    <div class="stat-val"><span id="h1">--</span><span class="u">%</span></div>
                    <div class="stat-sub"><i id="dotH1"></i>Sensor 1</div>
                </div>
                <div class="stat">
                    <div class="stat-val"><span id="h2">--</span><span class="u">%</span></div>
                    <div class="stat-sub"><i id="dotH2"></i>Sensor 2</div>
                </div>
            </div>
            <canvas id="humChart" width="600" height="150"></canvas>
        </section>
    </div>

    <script>
        const SAMPLE_INTERVAL_S = 2;

        // Data colors are the only accent in the greyscale UI.
        // Two shades per metric: lighter = Sensor 1, stronger = Sensor 2.
        const C = {
            t1: '#ff8787', t2: '#e03131',   // temperature: soft / strong red
            h1: '#74c0fc', h2: '#1c7ed6'    // humidity:    soft / strong blue
        };

        const tc = document.getElementById('tempChart');
        const hc = document.getElementById('humChart');
        let lastData = null;

        // Heater relay state, refreshed each poll and advanced locally between polls.
        let relayOn = false;        // actual pin state reported by the device
        let transitionAt = null;    // timestamp of the next on<->off edge (null = steady)
        let pulseMs = 0, periodMs = 0;

        // Paint legend swatches + stat dots from the single color source.
        function paint(id, c) { const e = document.getElementById(id); if (e) e.style.background = c; }
        paint('lgT1', C.t1); paint('lgT2', C.t2); paint('dotT1', C.t1); paint('dotT2', C.t2);
        paint('lgH1', C.h1); paint('lgH2', C.h2); paint('dotH1', C.h1); paint('dotH2', C.h2);

        function fmtAgo(totalSec) {
            if (totalSec >= 60) {
                const m = Math.floor(totalSec / 60), s = totalSec % 60;
                return '-' + m + 'm' + (s ? s + 's' : '');
            }
            return '-' + totalSec + 's';
        }

        // Draw one or more series sharing a Y axis. Each series: {data:[...], color}.
        // null / non-finite values create gaps (handles a missing/disconnected sensor).
        function drawChart(canvas, series) {
            const ctx = canvas.getContext('2d');
            const rect = canvas.getBoundingClientRect();
            const dpr = window.devicePixelRatio || 1;
            canvas.width = rect.width * dpr;
            canvas.height = rect.height * dpr;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const w = rect.width, h = rect.height;
            ctx.clearRect(0, 0, w, h);

            // Shared scale across all series for honest overlay comparison.
            let mn = Infinity, mx = -Infinity, maxLen = 0;
            for (const s of series) {
                if (!s.data) continue;
                maxLen = Math.max(maxLen, s.data.length);
                for (const v of s.data) {
                    if (v != null && isFinite(v)) { if (v < mn) mn = v; if (v > mx) mx = v; }
                }
            }
            if (maxLen < 2 || mn === Infinity) {
                ctx.fillStyle = '#5a5a5a';
                ctx.font = '12px -apple-system, sans-serif';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText('Collecting data…', w / 2, h / 2);
                return;
            }
            const range = mx - mn;
            if (range < 1) { mn -= 0.5; mx += 0.5; }
            else { mn -= range * 0.1; mx += range * 0.1; }

            const pad = { left: 40, right: 10, top: 10, bottom: 20 };
            const plotW = w - pad.left - pad.right;
            const plotH = h - pad.top - pad.bottom;

            // Gridlines + Y labels
            ctx.font = '10px -apple-system, sans-serif';
            ctx.fillStyle = '#7a7a7a';
            ctx.textAlign = 'right';
            ctx.textBaseline = 'middle';
            ctx.strokeStyle = 'rgba(255,255,255,0.05)';
            ctx.lineWidth = 1;
            for (let i = 0; i <= 4; i++) {
                const v = mn + (mx - mn) * (i / 4);
                const y = pad.top + plotH - (i / 4) * plotH;
                ctx.fillText(v.toFixed(1), pad.left - 6, y);
                ctx.beginPath();
                ctx.moveTo(pad.left, y);
                ctx.lineTo(pad.left + plotW, y);
                ctx.stroke();
            }
            // Baseline
            ctx.strokeStyle = 'rgba(255,255,255,0.12)';
            ctx.beginPath();
            ctx.moveTo(pad.left, pad.top + plotH);
            ctx.lineTo(pad.left + plotW, pad.top + plotH);
            ctx.stroke();

            // Data lines
            for (const s of series) {
                if (!s.data || s.data.length < 2) continue;
                const n = s.data.length;
                ctx.strokeStyle = s.color;
                ctx.lineWidth = 1.8;
                ctx.lineJoin = 'round';
                ctx.lineCap = 'round';
                ctx.beginPath();
                let pen = false;
                for (let i = 0; i < n; i++) {
                    const v = s.data[i];
                    if (v == null || !isFinite(v)) { pen = false; continue; }
                    const x = pad.left + (i / (n - 1)) * plotW;
                    const y = pad.top + plotH - ((v - mn) / (mx - mn)) * plotH;
                    if (!pen) { ctx.moveTo(x, y); pen = true; }
                    else ctx.lineTo(x, y);
                }
                ctx.stroke();
            }

            // X labels (relative time)
            ctx.fillStyle = '#7a7a7a';
            ctx.textAlign = 'left';
            ctx.textBaseline = 'top';
            const totalSec = (maxLen - 1) * SAMPLE_INTERVAL_S;
            ctx.fillText(fmtAgo(totalSec), pad.left, pad.top + plotH + 5);
            ctx.textAlign = 'right';
            ctx.fillText('now', pad.left + plotW, pad.top + plotH + 5);
        }

        function setNum(id, v) {
            document.getElementById(id).textContent = (v == null || !isFinite(v)) ? '--' : v.toFixed(1);
        }

        async function setTarget() {
            const el = document.getElementById('target');
            try { await fetch('/set?target=' + encodeURIComponent(el.value)); update(); }
            catch (e) { document.getElementById('status').textContent = 'Failed to set target'; }
        }
        async function setMode(m) {
            try { await fetch('/mode?value=' + m); update(); }
            catch (e) { document.getElementById('status').textContent = 'Failed to set mode'; }
        }
        async function setManual(s) {
            try { await fetch('/manual?value=' + s); update(); }
            catch (e) { document.getElementById('status').textContent = 'Failed to set heater'; }
        }
        async function setPwm() {
            const period = document.getElementById('period').value;
            const pulse = document.getElementById('pulse').value;
            try {
                await fetch('/pwm?period=' + encodeURIComponent(period) + '&pulse=' + encodeURIComponent(pulse));
                update();
            } catch (e) { document.getElementById('status').textContent = 'Failed to set timing'; }
        }

        function applyMode(mode) {
            const isManual = (mode === 'manual');
            document.getElementById('modeAuto').classList.toggle('active', !isManual);
            document.getElementById('modeManual').classList.toggle('active', isManual);
            document.getElementById('target').disabled = isManual;
            document.getElementById('setBtn').disabled = isManual;
            document.getElementById('manOn').disabled = !isManual;
            document.getElementById('manOff').disabled = !isManual;
        }

        // Reflect the real relay state (dot) and a live countdown to its next edge.
        // Called ~5x/s; when the countdown crosses zero between polls it flips the
        // state locally using pulse/period, and the next poll snaps it back to truth.
        function renderHeat() {
            const heat = document.getElementById('heat');
            const ht = document.getElementById('heatTxt');
            if (transitionAt === null) {            // steady: no upcoming edge
                heat.classList.toggle('on', relayOn);
                ht.textContent = relayOn ? 'On' : 'Off';
                return;
            }
            let remaining = transitionAt - Date.now();
            if (remaining <= 0 && periodMs > 0) {   // edge passed since last poll -> advance locally
                relayOn = !relayOn;
                const span = relayOn ? pulseMs : (periodMs - pulseMs);
                transitionAt = Date.now() + span;
                remaining = span;
            }
            const secs = Math.max(0, remaining / 1000).toFixed(1) + 's';
            heat.classList.toggle('on', relayOn);
            ht.textContent = relayOn ? ('On · off in ' + secs) : ('Off · on in ' + secs);
        }

        async function update() {
            try {
                const r = await fetch('/data');
                const d = await r.json();
                lastData = d;

                setNum('t1', d.t1); setNum('t2', d.t2);
                setNum('h1', d.h1); setNum('h2', d.h2);

                document.getElementById('status').textContent =
                    new Date().toLocaleTimeString() + '  ·  ' +
                    (d.n1 || 0) + '/' + (d.n2 || 0) + ' samples';

                // Ingest relay truth from the device; renderHeat() draws + counts down.
                relayOn = !!d.relay;
                pulseMs = (d.pulse || 0) * 1000;
                periodMs = (d.period || 0) * 1000;
                transitionAt = (d.next && d.next > 0) ? (Date.now() + d.next) : null;
                renderHeat();

                applyMode(d.mode);

                const tgt = document.getElementById('target');
                if (document.activeElement !== tgt) tgt.value = (d.target == null) ? '' : d.target;
                const pu = document.getElementById('pulse');
                if (document.activeElement !== pu && d.pulse != null) pu.value = d.pulse;
                const pe = document.getElementById('period');
                if (document.activeElement !== pe && d.period != null) pe.value = d.period;

                drawChart(tc, [{ data: d.tHist1, color: C.t1 }, { data: d.tHist2, color: C.t2 }]);
                drawChart(hc, [{ data: d.hHist1, color: C.h1 }, { data: d.hHist2, color: C.h2 }]);
            } catch (e) {
                document.getElementById('status').textContent = 'Connection error';
            }
        }

        // Keep charts crisp on rotate/resize without waiting for the next poll.
        window.addEventListener('resize', function () {
            if (!lastData) return;
            drawChart(tc, [{ data: lastData.tHist1, color: C.t1 }, { data: lastData.tHist2, color: C.t2 }]);
            drawChart(hc, [{ data: lastData.hHist1, color: C.h1 }, { data: lastData.hHist2, color: C.h2 }]);
        });

        update();
        setInterval(update, 2000);
        setInterval(renderHeat, 200);   // smooth duty-cycle countdown between data polls
    </script>
</body>
</html>
)rawliteral";

// ---- Sensor helpers ------------------------------------------------------
void pushSample(SensorState& s, float t, float h) {
  s.tempSamples[s.sampleIndex] = t;
  s.humSamples[s.sampleIndex] = h;
  s.sampleIndex = (s.sampleIndex + 1) % SAMPLE_COUNT;
  if (s.sampleCount < SAMPLE_COUNT) s.sampleCount++;
}

float avgTemp(SensorState& s) {
  if (s.sampleCount == 0) return NAN;
  float sum = 0;
  for (int i = 0; i < s.sampleCount; i++) sum += s.tempSamples[i];
  return sum / s.sampleCount;
}

float avgHum(SensorState& s) {
  if (s.sampleCount == 0) return NAN;
  float sum = 0;
  for (int i = 0; i < s.sampleCount; i++) sum += s.humSamples[i];
  return sum / s.sampleCount;
}

// Append the current smoothed averages to the plot history. Called every read
// cycle for both sensors so the two histories stay the same length / aligned.
// A sensor with no valid samples yet stores NaN, which the chart renders as a gap.
void pushHistory(SensorState& s) {
  s.tempHistory[s.historyIndex] = avgTemp(s);
  s.humHistory[s.historyIndex] = avgHum(s);
  s.historyIndex = (s.historyIndex + 1) % HISTORY_SIZE;
  if (s.historyCount < HISTORY_SIZE) s.historyCount++;
}

// Attempt one read; only commit to the moving average if both fields are valid.
void readOne(SensorState& s) {
  float h = s.dht->readHumidity();
  float t = s.dht->readTemperature(false);  // true = Fahrenheit
  if (!isnan(h) && !isnan(t)) pushSample(s, t, h);
}

// The thermostat needs a single temperature. Default: sensor 1.
// To control on the average of both instead, replace with something like:
//   float a = avgTemp(s1), b = avgTemp(s2);
//   if (isnan(a)) return b; if (isnan(b)) return a; return (a + b) / 2;
float controlTemp() {
  return avgTemp(s2);
}

void readSensor() {
  unsigned long now = millis();
  if (now - lastReadMs < READ_INTERVAL_MS && (s1.sampleCount > 0 || s2.sampleCount > 0)) return;
  lastReadMs = now;

  readOne(s1);
  readOne(s2);

  // One aligned timeline tick for both sensors.
  pushHistory(s1);
  pushHistory(s2);

  // Re-evaluate heater output on fresh data.
  updateControl();
}

// The single source of truth for the physical output: given the logical `heating`
// decision and the pulse/period chop, report whether the relay is energized RIGHT NOW
// and how long until its next edge.
//   on     -> is the pin currently HIGH?
//   nextMs -> ms until the next ON<->OFF transition; 0 means "steady" (no upcoming edge:
//             always-on, always-off, or control disabled).
// pwmUpdate() drives the pin from this, and /data reports it so the UI can show the real
// relay state plus a live countdown rather than the bare pulse/period numbers.
//   heating == false        -> off, steady
//   pwmPulseMs == 0          -> off, steady (zero on-time even if thermostat wants heat)
//   pwmPulseMs >= pwmPeriodMs-> on,  steady (pulse covers the whole cycle)
//   otherwise                -> HIGH for the first pwmPulseMs of each pwmPeriodMs window.
// Free-running phase off millis(); deliberately not aligned to anything.
void relayStatus(bool& on, unsigned long& nextMs) {
  nextMs = 0;
  if (!heating || pwmPulseMs == 0) {
    on = false;
  } else if (pwmPulseMs >= pwmPeriodMs) {
    on = true;
  } else {
    unsigned long phase = millis() % pwmPeriodMs;   // pwmPeriodMs floored >= 100, never 0
    if (phase < pwmPulseMs) { on = true;  nextMs = pwmPulseMs  - phase; }  // until it cuts off
    else                    { on = false; nextMs = pwmPeriodMs - phase; }  // until the next pulse
  }
}

// Drive the physical pin from the current relay state. Only place that touches
// HEATER_PIN. Called continuously from loop() and right after each control decision.
void pwmUpdate() {
  bool on;
  unsigned long nextMs;
  relayStatus(on, nextMs);
  digitalWrite(HEATER_PIN, on ? HIGH : LOW);
}

// Decide whether the heater SHOULD be on (sets `heating`); pwmUpdate() then chops it.
//   MANUAL: heating follows manualOn directly; thermostat logic is bypassed.
//   AUTO:   bang-bang control with a hysteresis deadband, acting on controlTemp().
//           ON below (target - HYSTERESIS), OFF above (target + HYSTERESIS), hold in between.
void updateControl() {
  if (heaterMode == MODE_MANUAL) {
    heating = manualOn;
  } else {
    // AUTO mode
    float t = controlTemp();
    if (isnan(targetTemp) || isnan(t)) {
      heating = false;
    } else if (t < targetTemp - HYSTERESIS) {
      heating = true;
    } else if (t > targetTemp + HYSTERESIS) {
      heating = false;
    }
    // else: within deadband -> hold current state
  }
  pwmUpdate();  // reflect the decision on the pin right away
}

// Build a JSON array of one sensor's history (oldest first). NaN -> null so the
// front-end can render gaps instead of breaking the auto-scaling.
String historyJson(SensorState& s, bool temp) {
  float* buf = temp ? s.tempHistory : s.humHistory;
  String out;
  out.reserve(s.historyCount * 8 + 2);   // limit heap fragmentation on long histories
  out = "[";
  int start = (s.historyCount < HISTORY_SIZE) ? 0 : s.historyIndex;
  for (int i = 0; i < s.historyCount; i++) {
    int idx = (start + i) % HISTORY_SIZE;
    if (i > 0) out += ",";
    float v = buf[idx];
    out += isnan(v) ? "null" : String(v, 1);
  }
  out += "]";
  return out;
}

void setup() {
  Serial.begin(115200);

  dht1.begin();
  dht2.begin();
  s1.dht = &dht1;
  s2.dht = &dht2;

  pinMode(HEATER_PIN, OUTPUT);
  digitalWrite(HEATER_PIN, LOW);  // start with heater off

  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("");
  Serial.println("WiFi connected");
  Serial.print("IP address: ");
  Serial.println(WiFi.localIP());

  server.on("/", HTTP_GET, []() {
    server.send_P(200, "text/html", index_html);
  });

  server.on("/data", HTTP_GET, []() {
    float t1 = avgTemp(s1), h1 = avgHum(s1);
    float t2 = avgTemp(s2), h2 = avgHum(s2);

    bool relayOn;
    unsigned long nextMs;
    relayStatus(relayOn, nextMs);  // actual pin state + ms to next edge

    String json;
    json.reserve(256);
    json = "{\"t1\":";
    json += isnan(t1) ? "null" : String(t1, 1);
    json += ",\"h1\":";
    json += isnan(h1) ? "null" : String(h1, 1);
    json += ",\"n1\":";
    json += s1.sampleCount;
    json += ",\"t2\":";
    json += isnan(t2) ? "null" : String(t2, 1);
    json += ",\"h2\":";
    json += isnan(h2) ? "null" : String(h2, 1);
    json += ",\"n2\":";
    json += s2.sampleCount;
    json += ",\"target\":";
    json += isnan(targetTemp) ? "null" : String(targetTemp, 1);
    json += ",\"heating\":";
    json += heating ? "true" : "false";
    json += ",\"relay\":";
    json += relayOn ? "true" : "false";
    json += ",\"next\":";
    json += nextMs;                 // ms until next relay edge; 0 = steady
    json += ",\"period\":";
    json += String(pwmPeriodMs / 1000.0, 1);
    json += ",\"pulse\":";
    json += String(pwmPulseMs / 1000.0, 1);
    json += ",\"mode\":\"";
    json += (heaterMode == MODE_MANUAL) ? "manual" : "auto";
    json += "\",\"manualOn\":";
    json += manualOn ? "true" : "false";
    json += ",\"tHist1\":";
    json += historyJson(s1, true);
    json += ",\"hHist1\":";
    json += historyJson(s1, false);
    json += ",\"tHist2\":";
    json += historyJson(s2, true);
    json += ",\"hHist2\":";
    json += historyJson(s2, false);
    json += "}";
    server.send(200, "application/json", json);
  });

  // Set (or clear) the target setpoint. /set?target=22.5  or  /set?target= to disable.
  server.on("/set", HTTP_GET, []() {
    if (server.hasArg("target")) {
      String v = server.arg("target");
      if (v.length() == 0) {
        targetTemp = NAN;          // empty -> disable control
      } else {
        targetTemp = v.toFloat();
      }
      updateControl();             // apply immediately
    }
    String json = "{\"target\":";
    json += isnan(targetTemp) ? "null" : String(targetTemp, 1);
    json += ",\"heating\":";
    json += heating ? "true" : "false";
    json += "}";
    server.send(200, "application/json", json);
  });

  // Switch control mode. /mode?value=auto  or  /mode?value=manual
  server.on("/mode", HTTP_GET, []() {
    if (server.hasArg("value")) {
      String v = server.arg("value");
      if (v == "manual") {
        // Carry the current output into manual so switching doesn't jolt the relay.
        manualOn = heating;
        heaterMode = MODE_MANUAL;
      } else if (v == "auto") {
        heaterMode = MODE_AUTO;
      }
      updateControl();             // apply immediately
    }
    String json = "{\"mode\":\"";
    json += (heaterMode == MODE_MANUAL) ? "manual" : "auto";
    json += "\",\"heating\":";
    json += heating ? "true" : "false";
    json += "}";
    server.send(200, "application/json", json);
  });

  // Manual on/off. /manual?value=on  or  /manual?value=off
  // Forces MANUAL mode so the request is honored even if currently in AUTO.
  server.on("/manual", HTTP_GET, []() {
    if (server.hasArg("value")) {
      String v = server.arg("value");
      heaterMode = MODE_MANUAL;
      if (v == "on")       manualOn = true;
      else if (v == "off") manualOn = false;
      updateControl();             // apply immediately
    }
    String json = "{\"mode\":\"";
    json += (heaterMode == MODE_MANUAL) ? "manual" : "auto";
    json += "\",\"manualOn\":";
    json += manualOn ? "true" : "false";
    json += ",\"heating\":";
    json += heating ? "true" : "false";
    json += "}";
    server.send(200, "application/json", json);
  });

  // Set the dimmer timing, applied in both modes. Values are in seconds.
  //   /pwm?period=5&pulse=2   -> on 2 s out of every 5 s window
  // Either arg may be sent alone. period is floored to PWM_PERIOD_MIN_MS to keep the
  // modulo safe; both are capped at PWM_TIME_MAX_MS.
  server.on("/pwm", HTTP_GET, []() {
    if (server.hasArg("period")) {
      unsigned long ms = (unsigned long)(server.arg("period").toFloat() * 1000.0);
      if (ms < PWM_PERIOD_MIN_MS) ms = PWM_PERIOD_MIN_MS;
      if (ms > PWM_TIME_MAX_MS)   ms = PWM_TIME_MAX_MS;
      pwmPeriodMs = ms;
    }
    if (server.hasArg("pulse")) {
      float p = server.arg("pulse").toFloat();
      if (p < 0) p = 0;
      unsigned long ms = (unsigned long)(p * 1000.0);
      if (ms > PWM_TIME_MAX_MS) ms = PWM_TIME_MAX_MS;
      pwmPulseMs = ms;
    }
    pwmUpdate();                   // apply immediately
    String json = "{\"period\":";
    json += String(pwmPeriodMs / 1000.0, 1);
    json += ",\"pulse\":";
    json += String(pwmPulseMs / 1000.0, 1);
    json += ",\"heating\":";
    json += heating ? "true" : "false";
    json += "}";
    server.send(200, "application/json", json);
  });

  server.begin();
}

void loop() {
  server.handleClient();
  readSensor();   // autonomous: runs regardless of whether a client is polling
  pwmUpdate();    // chop the output to the pulse/period timing every iteration
}