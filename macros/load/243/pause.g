; pause.g
; called when a print from SD card is paused
;
M83                   ; relative extrusion
G1 E-2 F3600          ; retract 2mm
G90                   ; absolute movement
G1 S2 X0 F4000   ; park both heads U260
