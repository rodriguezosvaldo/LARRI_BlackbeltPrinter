#include "ntfy.h"

#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>

#include "config.h"
#include "util.h"

namespace {

struct Note {
  String title, message, priority, tags;
};

QueueHandle_t queue;
const int QUEUE_LEN = 20;

bool send(const Note& n) {
  String url = NTFY_URL;
  bool https = url.startsWith("https");
  WiFiClientSecure secure;
  WiFiClient plain;
  if (https) secure.setInsecure();  // ntfy.sh over TLS without pinning a CA certificate
  HTTPClient http;
  http.setConnectTimeout(5000);
  http.setTimeout(10000);
  bool begun = https ? http.begin(secure, url) : http.begin(plain, url);
  if (!begun) {
    Serial.println("[notify error] bad ntfy URL");
    return false;
  }
  http.addHeader("Title", n.title);
  http.addHeader("Priority", n.priority);
  if (n.tags.length()) http.addHeader("Tags", n.tags);
  int code = http.POST(n.message);
  http.end();
  if (code < 200 || code >= 300) {
    Serial.printf("[notify error] HTTP %d (%s)\n", code, http.errorToString(code).c_str());
    return false;
  }
  return true;
}

}  // namespace

void ntfyBegin() { queue = xQueueCreate(QUEUE_LEN, sizeof(Note*)); }

void notify(const String& title, const String& message, const char* priority, const char* tags) {
  Note* n = new Note{title, message, priority, tags};
  if (timeValid()) n->message += "\n\n" + fmtDt(time(nullptr), "\n");
  Serial.printf("[ALARM] %s: %s\n", n->title.c_str(), n->message.c_str());
  if (xQueueSend(queue, &n, 0) != pdTRUE) {
    Serial.println("[notify error] queue full, dropped");
    delete n;
  }
}

void ntfyProcess() {
  // Wait for WiFi instead of dropping: messages queued while offline go out on reconnect
  Note* n;
  while (WiFi.isConnected() && xQueueReceive(queue, &n, 0) == pdTRUE) {
    send(*n);  // one attempt, like alarm.py
    delete n;
  }
}
