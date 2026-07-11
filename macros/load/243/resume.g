; resume.g
; called before a print from SD card is resumed
;
G1 R1 X0 Y0 Z0 F250  ; move to the resume location
M83                  ; relative extrusion
G1 E2 F3600          ; undo the retraction