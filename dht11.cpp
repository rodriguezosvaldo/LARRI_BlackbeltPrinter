#include <WiFi.h>
#include <WebServer.h>
#include <DHT.h>
 
// Network credentials
const char* ssid = "thecage";
const char* password = "12345678";
 
// DHT11 setup
#define DHTPIN 33
#define DHTTYPE DHT11
DHT dht(DHTPIN, DHTTYPE);
 
WebServer server(80);
 
// Moving average buffers
const int SAMPLE_COUNT = 30;
float tempSamples[SAMPLE_COUNT];
float humSamples[SAMPLE_COUNT];
int sampleIndex = 0;
int sampleCount = 0;
 
// History buffer for plotting (ring buffer)
const int HISTORY_SIZE = 1200;
float tempHistory[HISTORY_SIZE];
float humHistory[HISTORY_SIZE];
int historyIndex = 0;
int historyCount = 0;
 
unsigned long lastReadMs = 0;
const unsigned long READ_INTERVAL_MS = 2000;
 
const char index_html[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta charset="utf-8">
    <title>DHT11 Sensor</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 20px;
            text-align: center;
            background-color: #f0f0f0;
        }
        .panel {
            display: inline-block;
            background-color: white;
            padding: 30px 40px;
            border-radius: 10px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.2);
            max-width: 600px;
        }
        .readings {
            display: flex;
            justify-content: center;
            gap: 40px;
            margin: 20px 0;
        }
        .reading {
            text-align: center;
        }
        .label {
            font-size: 16px;
            color: #666;
        }
        .value {
            font-size: 42px;
            font-weight: bold;
            color: #333;
        }
        .unit {
            font-size: 20px;
            color: #666;
        }
        .chart-wrap {
            margin: 20px 0 10px;
        }
        .chart-title {
            font-size: 13px;
            color: #666;
            text-align: left;
            margin: 10px 0 4px;
        }
        canvas {
            width: 100%;
            height: 140px;
            display: block;
        }
        #status {
            font-size: 12px;
            color: #999;
            margin-top: 10px;
        }
    </style>
</head>
<body>
    <div class="panel">
        <h1>DHT11 Sensor</h1>
        <div class="readings">
            <div class="reading">
                <div class="label">Temperature</div>
                <div><span id="temp" class="value">--</span><span class="unit"> &deg;F</span></div>
            </div>
            <div class="reading">
                <div class="label">Humidity</div>
                <div><span id="hum" class="value">--</span><span class="unit"> %</span></div>
            </div>
        </div>
        <div class="chart-wrap">
            <div class="chart-title">Temperature (&deg;F)</div>
            <canvas id="tempChart" width="540" height="140"></canvas>
            <div class="chart-title">Humidity (%)</div>
            <canvas id="humChart" width="540" height="140"></canvas>
        </div>
        <div id="status">Loading...</div>
    </div>
    <script>
        const SAMPLE_INTERVAL_S = 2;
 
        function drawChart(canvas, data, color) {
            const ctx = canvas.getContext('2d');
            // Match canvas pixel size to displayed size for crisp rendering
            const rect = canvas.getBoundingClientRect();
            const dpr = window.devicePixelRatio || 1;
            canvas.width = rect.width * dpr;
            canvas.height = rect.height * dpr;
            ctx.scale(dpr, dpr);
            const w = rect.width;
            const h = rect.height;
            ctx.clearRect(0, 0, w, h);
 
            if (!data || data.length < 2) {
                ctx.fillStyle = '#999';
                ctx.font = '12px Arial';
                ctx.textAlign = 'center';
                ctx.fillText('Collecting data...', w / 2, h / 2);
                return;
            }
 
            const pad = { left: 42, right: 8, top: 8, bottom: 22 };
            const plotW = w - pad.left - pad.right;
            const plotH = h - pad.top - pad.bottom;
 
            let mn = Math.min.apply(null, data);
            let mx = Math.max.apply(null, data);
            const range = mx - mn;
            if (range < 1) { mn -= 0.5; mx += 0.5; }
            else { mn -= range * 0.1; mx += range * 0.1; }
 
            // Gridlines + Y labels
            ctx.fillStyle = '#666';
            ctx.font = '11px Arial';
            ctx.textAlign = 'right';
            ctx.textBaseline = 'middle';
            ctx.strokeStyle = '#eee';
            ctx.lineWidth = 1;
            for (let i = 0; i <= 4; i++) {
                const v = mn + (mx - mn) * (i / 4);
                const y = pad.top + plotH - (i / 4) * plotH;
                ctx.fillText(v.toFixed(1), pad.left - 5, y);
                ctx.beginPath();
                ctx.moveTo(pad.left, y);
                ctx.lineTo(pad.left + plotW, y);
                ctx.stroke();
            }
 
            // Axes
            ctx.strokeStyle = '#bbb';
            ctx.beginPath();
            ctx.moveTo(pad.left, pad.top);
            ctx.lineTo(pad.left, pad.top + plotH);
            ctx.lineTo(pad.left + plotW, pad.top + plotH);
            ctx.stroke();
 
            // Data line
            ctx.strokeStyle = color;
            ctx.lineWidth = 2;
            ctx.beginPath();
            for (let i = 0; i < data.length; i++) {
                const x = pad.left + (i / (data.length - 1)) * plotW;
                const y = pad.top + plotH - ((data[i] - mn) / (mx - mn)) * plotH;
                if (i === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            }
            ctx.stroke();
 
            // X labels (relative time)
            ctx.fillStyle = '#666';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            const totalSec = (data.length - 1) * SAMPLE_INTERVAL_S;
            const leftLabel = totalSec >= 60
                ? '-' + Math.floor(totalSec / 60) + 'm' + (totalSec % 60 ? (totalSec % 60) + 's' : '')
                : '-' + totalSec + 's';
            ctx.fillText(leftLabel, pad.left, pad.top + plotH + 5);
            ctx.fillText('now', pad.left + plotW, pad.top + plotH + 5);
        }
 
        async function update() {
            try {
                const r = await fetch('/data');
                const d = await r.json();
                if (isFinite(d.temp)) {
                    document.getElementById('temp').textContent = d.temp.toFixed(1);
                    document.getElementById('hum').textContent = d.humidity.toFixed(1);
                    document.getElementById('status').textContent =
                        'Avg of ' + d.samples + ' samples \u2014 ' + new Date().toLocaleTimeString();
                } else {
                    document.getElementById('status').textContent = 'Sensor read failed';
                }
                drawChart(document.getElementById('tempChart'), d.tempHistory, '#e74c3c');
                drawChart(document.getElementById('humChart'), d.humHistory, '#3498db');
            } catch (e) {
                document.getElementById('status').textContent = 'Connection error';
            }
        }
        update();
        setInterval(update, 2000);
    </script>
</body>
</html>
)rawliteral";
 
void readSensor() {
  unsigned long now = millis();
  if (now - lastReadMs < READ_INTERVAL_MS && sampleCount > 0) return;
  lastReadMs = now;
 
  float h = dht.readHumidity();
  float t = dht.readTemperature(true);  // true = Fahrenheit
 
  if (!isnan(h) && !isnan(t)) {
    // Update moving average buffers
    tempSamples[sampleIndex] = t;
    humSamples[sampleIndex] = h;
    sampleIndex = (sampleIndex + 1) % SAMPLE_COUNT;
    if (sampleCount < SAMPLE_COUNT) sampleCount++;
 
    // Append current moving averages to history
    float avgT = averageTemp();
    float avgH = averageHumidity();
    tempHistory[historyIndex] = avgT;
    humHistory[historyIndex] = avgH;
    historyIndex = (historyIndex + 1) % HISTORY_SIZE;
    if (historyCount < HISTORY_SIZE) historyCount++;
  }
}
 
float averageTemp() {
  if (sampleCount == 0) return NAN;
  float sum = 0;
  for (int i = 0; i < sampleCount; i++) sum += tempSamples[i];
  return sum / sampleCount;
}
 
float averageHumidity() {
  if (sampleCount == 0) return NAN;
  float sum = 0;
  for (int i = 0; i < sampleCount; i++) sum += humSamples[i];
  return sum / sampleCount;
}
 
// Build a JSON array of history values in chronological order (oldest first)
String historyJson(float* buf) {
  String out = "[";
  // Start from the oldest entry. If buffer hasn't wrapped yet, that's index 0.
  // If it has wrapped, oldest is at historyIndex (next slot to overwrite).
  int start = (historyCount < HISTORY_SIZE) ? 0 : historyIndex;
  for (int i = 0; i < historyCount; i++) {
    int idx = (start + i) % HISTORY_SIZE;
    if (i > 0) out += ",";
    out += String(buf[idx], 1);
  }
  out += "]";
  return out;
}
 
void setup() {
  Serial.begin(115200);
 
  dht.begin();
 
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
    readSensor();
    float t = averageTemp();
    float h = averageHumidity();
    String json = "{\"temp\":";
    json += isnan(t) ? "null" : String(t, 1);
    json += ",\"humidity\":";
    json += isnan(h) ? "null" : String(h, 1);
    json += ",\"samples\":";
    json += sampleCount;
    json += ",\"tempHistory\":";
    json += historyJson(tempHistory);
    json += ",\"humHistory\":";
    json += historyJson(humHistory);
    json += "}";
    server.send(200, "application/json", json);
  });
 
  server.begin();
}
 
void loop() {
  server.handleClient();
}