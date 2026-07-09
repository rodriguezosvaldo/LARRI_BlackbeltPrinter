;load E0

M291 P"load E0?, press OK" R"Loading filament" S3

M302 P0			; Prohibit cold extrusion
T0			; Active tool
G10 P0 R245 S245
M116 P0
G28

G1 Y100 F1000
M400


M83			; Relative extruder moves

G1 E3 F600		; push filament at slow speed
G1 E100 F900		; retract at medium speed
G1 E1070 F1500		; Pre-load at high speed
G1 E170 F200		; Pre-load at slow speed

G4 S2			; Wait a bit

M291 P"Filament loaded E0" S1

M568 P0 R0 S0	; cool down extruder