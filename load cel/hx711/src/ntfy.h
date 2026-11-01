// ntfy push notifications. notify() only queues the message (never blocks the caller);
// ntfyProcess() sends it from the network task.
#pragma once
#include <Arduino.h>

void ntfyBegin();
void notify(const String& title, const String& message, const char* priority = "default",
            const char* tags = "");
void ntfyProcess();
