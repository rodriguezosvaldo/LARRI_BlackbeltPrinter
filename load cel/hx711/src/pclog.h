// Logs sent to the PC running log_server.py (LOG_SERVER_URL in secrets.h): job log lines,
// printer replies and load-cell readings (CSV). Everything is queued in RAM and sent from the
// network task; if the PC is down it retries later (oldest CSV rows dropped past PCLOG_MAX_ROWS).
#pragma once
#include <Arduino.h>

bool pclogEnabled();
void pclogBegin();

// key = unique id of the print (base + start time); the PC picks the file name from base
void pclogJobLine(const String& key, const String& base, const String& line);
void pclogReply(const String& line);
void pclogCells(uint32_t t, long c1, long c2);

void pclogProcess();  // network task: send what's pending

// Reloads the last RATE_WINDOW of readings from the PC into the spool history.
// Returns the number of rows loaded, or -1 if the PC could not be reached.
int pclogLoadHistory();
