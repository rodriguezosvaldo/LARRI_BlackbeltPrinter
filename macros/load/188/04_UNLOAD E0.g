
;Unload E0

M291 P"Unload E0? press OK" R"Unloading filament" S3

G28
T0			; Active tool
G1 Y100 F1000
M400
G10 P0 R245 S245
M116 P0
M83			; Relative extruder moves

G1 E3 F600		; push filament at slow speed
G1 E-20 F120	; retract at low speed
G1 E-100 F900	; retract at medium speed
G1 E-1420 F1500		; Pre-load at high speed

G4 S2			; Wait a bit
M291 P"Filament Unloaded E0"

M568 P0 S0 R160