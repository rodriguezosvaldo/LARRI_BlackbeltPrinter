# Piece models for the twin

Put the STL of a piece here to show it instead of the G-code toolpath.

- **Format:** binary STL (what Cura exports), in mm.
- **Name:** the same as the Cura mesh (`Short Side Column.stl`, shown as "Model" in the twin and
  in `job.build.objects` on the Duet) or as the G-code file (`<gcode name>.stl`). Case doesn't matter.
- **Orientation:** by default the STL's Y axis is the belt direction and Z is up. If that is not
  the case, add `<same name>.json` next to it, e.g. `{"belt": "x", "up": "z", "flip": false}`.
  "Flip STL" in the viewer swaps which end is printed first (remembered per browser).

The STL is aligned with the G-code outer walls when they are loaded, and cut by the gantry
plane at the current belt Z.
