# G-code Cheat Sheet — Duet 3 / RepRapFirmware (BlackBelt)

Quick reference for **configuration** and **day-to-day** commands used on BB printers (Duet 3 MB 6HC + 3HC expansion).  
Firmware dialect: **RepRapFirmware (RRF)** — not identical to Marlin.

---

## General & Network

| Code | Purpose | Example / Notes |
|------|---------|-----------------|
| **M550** | Set machine name (hostname) | `M550 P"BB_UofL Sn OMD3D-BB-230"` |
| **M552** | Configure network interface | `M552 P0.0.0.0 S1` — DHCP, enable networking |
| **M586** | Enable/disable network protocols | `M586 P0 S1` — P0=HTTP, P1=FTP, P2=Telnet, P3=HTTPS, P4=FTPS |
| **M575** | Serial port settings (PanelDue, etc.) | `M575 P1 S0 B57600` — P=port, S=mode, B=baud |
| **M98** | Run macro file | `M98 P"JobQueue/config.g"` |
| **M118** | Send message to host / log | `M118 P2 S"Printer ready"` |
| **M409** | Query object model (JSON) | `M409 K"heat.heaters[0]"` — used by DWC and monitoring scripts |

---

## Timing & Motion Mode (also used in macros)

| Code | Purpose | Example / Notes |
|------|---------|-----------------|
| **G4** | Dwell (pause) | `G4 S2` — wait 2 seconds; `G4 P500` — wait 500 ms |
| **G90** | Absolute positioning | Default for most prints |
| **G91** | Relative positioning | Used in power-fail retract macro |
| **G92** | Set current position without moving | `G92 Z0` — set Z origin |
| **G1** | Linear move | `G1 X100 Y50 F6000` — F in mm/min on RRF |
| **G0** | Rapid move (same as G1 on RRF) | `G0 X0 Y0` |
| **M83** | Extruder relative mode | `M83` then `G1 E-5` — retract 5 mm |
| **M82** | Extruder absolute mode | Standard slicer output |

---

## Steppers & Kinematics

| Code | Purpose | Example / Notes |
|------|---------|-----------------|
| **M569** | Driver direction | `M569 P0.0 S0` — P=driver (board.driver), S=0 reverse / 1 forward |
| **M584** | Map drives to axes | `M584 X0.0 Y0.1:0.2 Z0.3 E1.0:1.1` — dual Y motors use `:` |
| **M350** | Microstepping | `M350 X16 Y16 Z16 E16:16 I1` — I1 enables interpolation (TMC) |
| **M92** | Steps per mm | `M92 X100 Y88.89 Z317.41 E676.34:676.34` |
| **M566** | Jerk (instantaneous speed change) | `M566 X600 Y600 Z600 E120:120` — units: mm/min |
| **M203** | Max feed rate | `M203 X15000 Y15000 Z3000 E3600:3600` — mm/min |
| **M201** | Max acceleration | `M201 X1000 Y1000 Z500 E350:350` — mm/s² |
| **M906** | Motor current (mA) + idle % | `M906 X1400 Y1400 Z1000 E720:720 I50` — I=idle current % |
| **M84** | Disable motors / idle timeout | `M84 S180` — disable after 180 s idle; `M84` — disable now |
| **M913** | Reduce motor current (e.g. low VIN) | `M913 X0 Y0 U0` — in power-fail macro |
| **M915** | Stall detection (TMC) | `M915 X S3 R1 F0 H200` — optional, not in BB config |

---

## Axis Limits & Homing

| Code | Purpose | Example / Notes |
|------|---------|-----------------|
| **M208** | Axis min/max limits | `M208 X500 Y485 Z99999999` — maxima; `M208 X0 Y0 Z0 S1` — minima (S1 flag) |
| **M574** | Endstop configuration | `M574 X1 P"io1.in" S1` — X=axis, P=pin, S=1 active high |
| **G28** | Home axes | `G28` — home all; `G28 X Y` — home X and Y only |
| **G30** | Single-point Z probe | `G30 X100 Y100` |
| **G32** | Run bed compensation macro | Depends on `bed.g` macro |

**M574 Z types (RRF):** `Z0` = no endstop (BB belt Z); `Z1` = min endstop; `Z2` = max endstop; `Z3` = motor stall; `Z4` = Z probe.

---

## Temperature Sensors

| Code | Purpose | Example / Notes |
|------|---------|-----------------|
| **M308** | Configure temperature sensor | See examples below |

**Common M308 parameters:**

| Param | Meaning |
|-------|---------|
| `S` | Sensor number (0–255) |
| `P` | Pin name (`"temp0"`, `"1.temp0"` for expansion board) |
| `Y` | Sensor type (`"thermistor"`, `"pt1000"`, `"max31855"`, etc.) |
| `A` | Display name |
| `T`, `B`, `C` | Thermistor Steinhart–Hart coefficients |
| `R` | Series resistor (Ω), e.g. `R4700` for E3D thermistor |

**BB examples:**
```
M308 S0 P"temp0" Y"thermistor" A"Bed0" T100000 B4725 C7.06e-8
M308 S4 P"1.temp0" Y"thermistor" A"E0" T4606017 B5848 C5.548428e-8 R4700
```

---

## Heaters & Beds

| Code | Purpose | Example / Notes |
|------|---------|-----------------|
| **M950** | Create heater, fan, or GPIO | `M950 H0 C"out1" T0` — heater 0 on out1, sensor T0 |
| **M143** | Heater fault limits (monitor) | `M143 H0 P0 T0 C0 S200 A0` — shut down if sensor T0 exceeds action |
| **M307** | Heater model (PID / tuning) | `M307 H0 R0.417 K0.945 D3.16 E1.35 S1.00 B0` |
| **M140** | Define heated bed | `M140 P0 H0 S0 R0` — bed 0 uses heater H0 |
| **M301** | Legacy PID (RRF 2.x) | Replaced by **M307** in RRF 3 |

**Runtime temperature (not config, but essential):**

| Code | Purpose |
|------|---------|
| **M104** | Set hotend temp (no wait) — `M104 S200` |
| **M109** | Set hotend temp and wait — `M109 S200` |
| **M140** | Set bed temp (no wait) — `M140 S60` |
| **M190** | Set bed temp and wait — `M190 S60` |
| **M116** | Wait for temps (all heaters or specified) — `M116 P0` |

**M307 tuning:** Run `M303 H0 S80` to auto-tune heater H0, then save with `M500`.

---

## Fans

| Code | Purpose | Example / Notes |
|------|---------|-----------------|
| **M950** | Assign fan to pin | `M950 F0 C"1.out3"` — fan 0 on expansion board out3 |
| **M106** | Configure or set fan speed | See below |

**M106 parameters (configuration in `config.g`):**

| Param | Meaning |
|-------|---------|
| `P` | Fan number |
| `C` | Name (for DWC) |
| `S` | Default speed 0–1 (or 0–255 on some firmware) |
| `H` | Heater to trigger thermostatic mode |
| `T` | Thermostatic threshold (°C) |
| `B` | Blip time (s) at startup |
| `L`, `X` | Min/max PWM when variable-speed |

**BB examples:**
```
M106 P0 C"HF-E0" S0 B0.1 H3 T45    ; Hotend fan — thermostatic on heater H3 at 45°C
M106 P2 C"PF-E0" S0 L0 X1 B1       ; Part cooling fan
M106 P1 C"SF" S0 L0 X1 B1          ; System fan
```

**During print:** `M106 P2 S0.5` — part fan at 50%.

---

## Tools (Multi-extruder / IDEX)

| Code | Purpose | Example / Notes |
|------|---------|-----------------|
| **M563** | Define tool | `M563 P0 S"T0" D0:1 H4 F2` — drives, heaters, fans |
| **M568** | Tool active/standby temps | `M568 P0 R0 S0` — R=standby, S=active (°C) |
| **M567** | Mixing ratios | `M567 P0 E1:1` — equal mix on dual extruder drives |
| **G10** | Tool offsets & temps | `G10 P0 X0 Y0 Z0 R0 S0` — X/Y/Z offset; R/S = standby/active temp |
| **T** | Select tool | `T0` — select tool 0; `T-1` — deselect all |

**Dual-head BB (commented in config):** U axis, tool T1, copy tool T2 with `M563 P2 ... X0:3`.

---

## Filament Monitoring

| Code | Purpose | Example / Notes |
|------|---------|-----------------|
| **M591** | Configure filament sensor | `M591 D0 P7 C"1.io1.in" S1 R10:300 E10 L2.95` |
| **M591 Dn** | Report sensor parameters | `M591 D0` — display settings for drive 0 |

**M591 parameters:**

| Param | Meaning |
|-------|---------|
| `D` | Extruder drive number |
| `P` | Sensor type (3=simple switch, 7=motion encoder, etc.) |
| `C` | Pin name |
| `S` | Enabled (1) / disabled (0) |
| `R` | Allowed movement % range (min:max) |
| `E` | Minimum extrusion length (mm) before checking |
| `L` | Filament diameter for motion sensor (mm) |

---

## Power Management & Safety

| Code | Purpose | Example / Notes |
|------|---------|-----------------|
| **M911** | Low VIN threshold + power-fail macro | `M911 S21.0 R23.5 P"M913 X0 Y0 U0 G91 M83 G1 E-5 F1000"` |
| **M912** | Configure stepper driver over-temp warnings | `M912 P0 S8` — optional |
| **M112** | Emergency stop (software) | Halts all motion and heaters |
| **M0** | Unconditional stop | `M0` — pause with message |
| **M1** | Optional stop (if enabled in firmware) | Same as M0 if configured |

---

## Configuration Save / Load

| Code | Purpose |
|------|---------|
| **M500** | Save parameters to `config-override.g` |
| **M501** | Load `config-override.g` |
| **M502** | Reset settings to firmware defaults |
| **M503** | Report current settings |

After editing `config.g` on SD card, reboot or run `M98 P"config.g"` (if supported) to reload.

---

## Global Variables (RRF 3)

Not G-code per se, but used in BB `config.g`:

```
global mytemp = 0
global bedtemp = 0
```

Set: `set global.mytemp = 200`  
Read in macros: `{global.mytemp}`

---

## Codes Referenced in BB Config (summary)

| Section | Codes |
|---------|-------|
| General | M550, M575 |
| Network | M552, M586 |
| Startup delay | G4 |
| Drivers | M569 |
| Motion | M584, M350, M92, M566, M203, M201, M906, M84 |
| Limits | M208 |
| Endstops | M574 |
| Sensors | M308 |
| Heaters | M950, M143, M307, M140 |
| Fans | M950, M106 |
| Tools | M563, M568, M567, G10 |
| Filament | M591 |
| Power fail | M911 (uses M913, G91, M83, G1) |
| Macros | M98 |

---

## Useful Extras (not in config.g but good to know)

| Code | Purpose |
|------|---------|
| **M114** | Report current position |
| **M115** | Firmware identification |
| **M122** | Diagnostic info (drives, MCU temp, VIN) |
| **M122 P200** | Clear driver warnings |
| **M122 P1000** | Extended diagnostics |
| **M408** | Legacy status report (use M409 on RRF 3) |
| **M558** | Z probe type and pin | `M558 P5 C"io7.in" H5 F120` |
| **G31** | Z probe trigger height | `G31 P500 X0 Y0 Z2.5` |
| **M557** | Mesh grid definition | `M557 X10:290 Y10:290 P5:5` |
| **M558 / G29 / G32** | Bed leveling workflow (depends on macros) |
| **M117** | Display message on PanelDue | `M117 Printing...` |
| **M220** | Set speed factor % | `M220 S50` — 50% speed |
| **M221** | Set extrusion factor % | `M221 S95` — 95% flow |
| **M292** | Pause / resume print (DWC macro) | Depends on `pause.g` / `resume.g` |
| **M600** | Filament change | Requires `filament-change.g` macro |
| **M955** | Configure accelerometer (input shaping) | Duet 3 toolboard feature |
| **M956** | Run accelerometer data collection | Used for input shaping tuning |

---

## BlackBelt-Specific Notes

1. **Belt Z axis** — `M574 Z0` means no physical Z endstop; Z is effectively infinite (`M208 Z99999999`).
2. **Dual Y motors** — `M584 Y0.1:0.2` maps two drivers to one Y axis.
3. **Expansion board prefix** — pins on 3HC board use `1.` prefix: `1.out0`, `1.temp0`, `1.io1.in`.
4. **Driver addressing** — `P0.0` = mainboard driver 0; `P1.0` = expansion board driver 0.
5. **IDEX / dual head** — U axis (`M584 ... U0.4`), extra tools T1/T2, and extra heaters/fans are in commented blocks in `config.g`.
6. **HTTP API** — send G-code via `GET http://<printer-ip>/rr_gcode?gcode=M122` (used by DWC and monitoring tools).

---

## Reference Links

- [RepRapFirmware G-code wiki](https://github.com/Duet3D/RepRapFirmware/wiki/G-Codes)
- [Object Model documentation](https://github.com/Duet3D/RepRapFirmware/wiki/Object-Model-Documentation)
- [Duet 3 MB 6HC wiring](https://docs.duet3d.com/Duet3D_hardware/Duet_3_family/Duet_3_Mainboard_6HC)
