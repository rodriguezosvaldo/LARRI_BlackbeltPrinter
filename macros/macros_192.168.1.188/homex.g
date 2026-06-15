; homex.g
; called to home the X axis

;
G91               ; relative positioning
G1 H2 Y5 F800     ; lift Y relative to current position
G1 H1 X-495 F1500 ; move quickly to X axis endstop and stop there (first pass)
G1 H2 X5 F3000    ; go back a few mm
G1 H1 X-495 F250  ; move slowly to X axis endstop once more (second pass)
G1 H2 Y-5 F250    ; lower Y again
G90               ; absolute positioning
M400              ; wait for movement to end