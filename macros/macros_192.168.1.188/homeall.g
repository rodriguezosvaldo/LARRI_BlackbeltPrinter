; homeall.g
; called to home all axes


;
G91                    ; relative positioning

G1 H1 X-495 U495 F1500 ; move quickly to X & U axis endstop and stop there (first pass)
G1 H1 Y-505 F1200 ; move quickly to Y axis endstop and stop there (first pass)
G1 H2 X5 Y5 U-5 F3000    ; go back a few mm
G1 H1 X-495 Y-505 U495 F250  ; move slowly to X & Y & U axis endstop once more (second pass)
G90                        ; absolute positioning
M400                                 ; wait for movement to end
