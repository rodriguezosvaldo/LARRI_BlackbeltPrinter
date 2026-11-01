// HX711 load cells: reading every READ_INTERVAL_MS + on-board calibration (button / web)
#pragma once
#include <Arduino.h>

void cellsBegin();
void cellsLoop();  // call from loop(): button + non-blocking read timer

bool cellsOk();               // last read succeeded
uint32_t cellsLastSeenUnix();  // unix time of the last good reading (0 = none)

String cellsDataJson();  // GET /data: {"seq","ok","cell1","cell2"}
String calibJson();      // GET /calibration
void calibStart();
void calibCancel();
