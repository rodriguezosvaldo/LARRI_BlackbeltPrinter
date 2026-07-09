# def check_analog_sensors(model_dynamic_values: dict, printer_ip: str, ntfy: str) -> None:
#     # sensors.analog[n].state: openCircuit, shortCircuit, timeout, hardwareError, etc
#     prev = _prev(printer_ip)
#     sensors = _safe(model_dynamic_values, "sensors", "analog", default=[]) or []
#     for i, s in enumerate(sensors):
#         if not isinstance(s, dict):
#             continue
#         st = s.get("state")
#         last = prev["analog"].get(i)
#         key = f"analog_fault_{i}"
#         if st and st != "ok":
#             stop_notifications(
#                 printer_ip,
#                 key,
#                 "Temperature Sensor Fault",
#                 f"Analog sensor #{i} ({s.get('name', '?')}) state: {st}",
#                 priority="urgent",
#                 tags="thermometer,warning",
#                 ntfy=ntfy,
#             )
#         else:
#             clear_problem_notification(printer_ip, key)
#         prev["analog"][i] = st

# def check_heaters(model_dynamic_values: dict, printer_ip: str, ntfy: str) -> None:
#     # heat.heaters[n].state: fault or offline
#     prev = _prev(printer_ip)
#     heaters = _safe(model_dynamic_values, "heat", "heaters", default=[]) or []
#     for i, h in enumerate(heaters):
#         if not isinstance(h, dict):
#             continue
#         st = h.get("state")
#         last = prev["heaters"].get(i)
#         key = f"heater_fault_{i}"
#         if st in HEATER_FAULT_STATES:
#             cur = h.get("current")
#             act = h.get("active")
#             stop_notifications(
#                 printer_ip,
#                 key,
#                 "Heater Fault",
#                 f"Heater #{i} state={st} current={cur} setpoint={act}",
#                 priority="urgent",
#                 tags="fire,rotating_light",
#                 ntfy=ntfy,
#             )
#         else:
#             clear_problem_notification(printer_ip, key)
#         prev["heaters"][i] = st

# def check_board(model_dynamic_values: dict, printer_ip: str, ntfy: str) -> None:
#     # boards[0].vIn.current and boards[0].mcuTemp.current
#     prev = _prev(printer_ip)
#     boards = _safe(model_dynamic_values, "boards", default=[]) or []
#     for i, b in enumerate(boards):
#         if not isinstance(b, dict):
#             continue
#         vin = _safe(b, "vIn", "current")
#         key_vin = f"low_vin_{i}"
#         if isinstance(vin, (int, float)):
#             if vin < VIN_MIN:
#                 stop_notifications(
#                     printer_ip,
#                     key_vin,
#                     "Low VIN",
#                     f"Board #{i} VIN={vin:.2f}V (< {VIN_MIN}V)",
#                     priority="urgent",
#                     tags="electric_plug,warning",
#                     ntfy=ntfy,
#                 )
#             else:
#                 clear_problem_notification(printer_ip, key_vin)
#         mcu = _safe(b, "mcuTemp", "current")
#         key_mcu = f"mcu_overheat_{i}"
#         if isinstance(mcu, (int, float)):
#             if mcu > MCU_TEMP_MAX:
#                 stop_notifications(
#                     printer_ip,
#                     key_mcu,
#                     "MCU Overheat",
#                     f"Board #{i} MCU={mcu:.1f}C (> {MCU_TEMP_MAX}C)",
#                     priority="urgent",
#                     tags="fire,warning",
#                     ntfy=ntfy,
#                 )
#             else:
#                 clear_problem_notification(printer_ip, key_mcu)