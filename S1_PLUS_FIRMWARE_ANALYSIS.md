# Análisis del firmware de fábrica del InfinityFlow S1 Plus

`IF_S1_Plus_firmware.bin` es el firmware de fábrica del
[InfinityFlow S1 Plus](https://infinityflow3d.com/products/s1-plus-automatic-filament-loader),
un cargador automático de filamento ("automatic filament reloader") de dos canales que
InfinityFlow vende como accesorio para varias marcas de impresoras. Este documento resume
lo que se pudo determinar sobre su arquitectura y protocolos **para servir de referencia**
al escribir un firmware propio que vincule el S1 Plus con el firmware de la BlackBelt
(Duet 3 / RepRapFirmware, ver [alarm.py](alarm.py)).

**Método de análisis**: no se desensambló el binario. Se extrajeron ~5000 strings ASCII
imprimibles (`re.findall(rb'[\x20-\x7e]{5,}', data)` en Python) y se interpretaron a la luz
de las convenciones estándar de ESP-IDF/FreeRTOS/NimBLE y de los protocolos locales
conocidos de Klipper/Moonraker, Bambu Lab y PrusaLink. Es decir: es un mapa razonablemente
fiable de *qué componentes, tareas, endpoints y tópicos existen*, pero no una spec exacta
de la lógica interna (orden de estados, timings, cálculos) — eso requeriría desensamblado.

## Plataforma

- Imagen de aplicación **ESP-IDF v5.4.1** para **ESP32-S3**.
- Nombre de proyecto interno: `IF-03-S+_Code`. Compilado 2025-10-09 (`Oct 9 2025 11:29:04`).
- Componentes usados: `esp_wifi`, NimBLE (`bt/host/nimble`), `esp_http_client`,
  `esp-mqtt`/`esp_tls` (mqtts), `lwip`, `esp_ota_ops` (OTA), `nvs_flash`, `adc_oneshot`,
  RMT (`led_strip` para NeoPixel), `mbedtls`.

## Hardware y tareas FreeRTOS

Nombres de tarea encontrados literalmente en el binario:

| Tarea | Rol probable |
|---|---|
| `Provisioning Task` | Flujo de alta inicial: BLE + WiFi + certificado |
| `WifiConnectTask` | Conexión/reconexión WiFi |
| `SendStateChangeTask` | Publica cambios de estado a la nube (`/s1plus/state`) |
| `SendStepCountTask` | Publica conteo de pasos de motor (`/s1plus/step`) |
| `Calibration Task` | Calibración de sensores Hall (`minHall_%s`/`maxHall_%s`) |
| `Button Task` / `button_hold_timer` | Botón físico, con detección de pulsación larga |
| `Activation Task` | Activación/registro del dispositivo |
| `Load Task` | Secuencia de carga de filamento |
| `Hall Sensor Task` | Lectura continua de sensores Hall (uno por canal) |
| `MotorPollingTask1` / `MotorPollingTask2` | Un task por canal de motor (dos canales, A/B) |
| `BLE Handler Task` / `BLE_stop_task` | Servidor BLE (`S1-Server`), con timeout (`BLE Timeout`) |
| `LED Pulse Task` | Animación de los LEDs NeoPixel de estado |

**Dos canales independientes**: cada uno con su propio motor
(`MotorPollingTask1`/`2`, `motor_%s_meters`), su propio sensor Hall con calibración
individual (`minHall_%s`/`maxHall_%s`), y su propio LED NeoPixel
(`neopixel_a_handle`/`neopixel_b_handle`, driver `led_strip` sobre RMT). Esto encaja con
que el S1 Plus alimenta un solo hotend desde dos spools y conmuta entre ellos sin
desperdicio cuando uno se agota ("zero filament waste" en la página de producto).

**Máquina de estados por motor/canal**:

```
MOTOR_STATE_ENTRY -> MOTOR_STATE_RUNNING -> MOTOR_STATE_RUNOUT
                                          -> MOTOR_STATE_WAITING_TTO_CLEAR
                                          -> MOTOR_STATE_ERROR
```

(`TTO` probablemente "Time-To-Out"/algún timeout de espera; no se pudo confirmar sin
desensamblar). Estados de alto nivel del dispositivo reportados a la nube: `Loaded`,
`Sleep`, `Active`.

## Provisioning y seguridad (BLE + mTLS)

- El dispositivo expone un servidor BLE GATT (`S1-Server`) usado por la app móvil para la
  configuración inicial de WiFi.
- Comandos BLE identificados: `GET_MAC`, `GET_CSR`, `SET_CERT`.
- El propio dispositivo genera un par de claves y un CSR (`device_csr`, con
  `CN=device,O=InfinityFlow`), que la nube firma y devuelve como certificado de cliente
  (`device_cert`/`device_key`, formato PEM `-----BEGIN/END CERTIFICATE-----`). Esto se usa
  para autenticación mTLS contra el broker MQTT de InfinityFlow.

## Nube InfinityFlow ("FlowQ")

- MQTT sobre TLS: `mqtts://broker.infinityflow3d.com:8883`.
- Tópicos identificados: `/s1plus/step`, `/s1plus/state`, `/s1plus/logs`,
  `/s1plus/online/status`, `/s1plus/ota/result`, `/s1plus/%s/%s` (genérico), y variantes
  `/hub/%s/%s`, `/hub/online/status`, `/hub/status/report`, `/hub/upload/report` — sugiere
  que el mismo firmware (o una variante) puede operar como "hub" que agrega el estado de
  varios loaders/impresoras hacia una única conexión a la nube.
- OTA: descarga por HTTPS desde una `ota_url` recibida por MQTT/config, valida (`starting`,
  `already_up_to_date`, `invalid_json`, `no_url`, `memory_error`), y reporta el resultado a
  `/s1plus/ota/result` (con soporte de rollback, `rolledback`, vía `esp_ota_ops`).
- La página de producto confirma que esta nube se comercializa como **FlowQ** ("3D printer
  automation software", panel web).

## Integración local con la impresora — tres protocolos, ninguno RepRapFirmware

El firmware habla directamente con la impresora en la LAN local usando tres protocolos
distintos según la marca, coherente con los modelos listados en la página de producto
(Bambu A1/A1 mini/P1S, Creality Ender 3 V3 KE/SE, Elegoo Neptune 4):

1. **Klipper / Moonraker** (Creality Ender 3 V3 KE/SE, Elegoo Neptune 4 corren Klipper):
   - `GET http://%s:7125/printer/%s` — polling de estado.
   - Body de query de objetos: `{"path": "objects/query", "request": {"objects":
     {"heater_bed": ["temperature","target"], "extruder": ["temperature","target"],
     "toolhead": ["position","homed_axes"], "print_stats":
     ["state","filename","total_duration","print_duration"]}}}`.
   - `POST http://%s:7125/server/files/upload` — subida de gcode, `multipart/form-data`
     con boundary `----MyBoundaryInfinityFlowApp`,
     `Content-Disposition: form-data; name="file"; filename="%s"`.

2. **Bambu Lab** (A1, A1 mini, P1S):
   - FTP local con credenciales fijas conocidas del ecosistema Bambu:
     `USER bblp` / `PASS %s` (el "access code" del printer), `TYPE I`, y comandos
     `STOR %s` / `SIZE %s` / `DELE %s` para gestionar archivos.
   - MQTT local de Bambu: tópicos `device/%s/report` (telemetría) y `device/%s/request`
     (comandos) — el protocolo local ya documentado por la comunidad para X1/P1/A1.

3. **Genérico tipo PrusaLink** (posiblemente para otras impresoras no listadas
   explícitamente, o Prusa):
   - `http://%s/api/v1/%s`, `http://%s/api/v1/files/usb/%s`.
   - Autenticación por header `X-Api-Key` / campo `api_key`.

Además de estos tres, hay una capa de comandos internos uniforme (probablemente entre el
firmware y la app/nube, no hacia la impresora) con forma
`{"command": %d, "path": "<ruta>"}`, con rutas vistas: `status`, `files/gcodes/%s`,
`files/usb/%s`, `files/metascan?filename=%s`.

## Lo que le falta para hablar con Duet/RepRapFirmware

El S1 Plus de fábrica **no tiene ningún soporte para RepRapFirmware/Duet** — ni HTTP
`rr_model`, ni nada equivalente. Para el firmware nuevo, el patrón más cercano a reutilizar
es el de Klipper/Moonraker (polling HTTP periódico + subida de archivos), sustituyendo:

| Klipper/Moonraker (S1 Plus de fábrica) | Duet/RepRapFirmware (a implementar) |
|---|---|
| `GET http://%s:7125/printer/%s` objects/query | `GET {printer_ip}/rr_model?key=...&flags=d99nfo` (ver [alarm.py:156-193](alarm.py#L156-L193)) |
| `print_stats.state` (`printing`, `paused`, ...) | `state.status`, con vocabulario ya definido en `PRINTING_STATES`/`STOPPED_STATES` ([alarm.py:63-64](alarm.py#L63-L64)) |
| `toolhead.position` | `job.layer`, `job.rawExtrusion` (ver `get_layer`/`get_raw_extrusion`) |
| `POST /server/files/upload` (multipart) | Subida de gcode vía `rr_upload` en RepRapFirmware (no usado hoy por `alarm.py`, pero documentado en el Object Model de Duet) |

`alarm.py` ya resuelve exactamente el mismo problema (polling del object model del Duet)
para el caso de monitoreo/alertas, así que sus funciones `get_raw_extrusion`,
`get_state_status`, `get_layer`, `get_seqs_reply` son la referencia de protocolo más
directa disponible en este repo — mismo host, misma API `rr_model`, mismo `requests.get`.

## Limitaciones de este análisis

- Basado solo en strings extraídos del binario; no se desensambló el código, por lo que no
  se conocen con certeza: el orden exacto de los estados del motor, los timings/umbrales de
  calibración Hall, el formato binario exacto de los mensajes MQTT/BLE, ni si existen
  funciones adicionales sin strings asociados (tablas de saltos, tareas triviales, etc.).
- Es un mapa útil para diseñar la integración (qué protocolos, qué tareas, qué tópicos
  existen), no una especificación completa lista para clonar 1:1.
