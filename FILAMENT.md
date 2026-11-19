# Filament and spools per part

## Filament

3DXTECH filament (material GUID `886450a6-4a75-4d06-841f-57610f7fe91a` in the G-code), 1.75 mm.

| Property | Value |
|---|---|
| Diameter | 1.75 mm |
| Density | 1.11 g/cc |
| Tensile strength, break | 48 MPa |
| Tensile modulus | 5355 MPa |
| Tensile elongation, break | 3 % |
| Flexural strength | 78 MPa |
| Flexural modulus | 5210 MPa |
| Glass transition temp | 105 °C |
| Deflection temp | 97 °C |
| Surface resistance | >10⁹ Ohm/sq |

Slicer settings used in the G-code: nozzle 265 °C, bed 110 °C, nozzle 0.9 mm, layer 0.5 mm,
infill 10 %, material flow 75 %, gantry angle 35°.

### Spool (4 kg reel, large)

| Property | Value |
|---|---|
| Filament | 4 kg net |
| Empty reel | 1000 g |
| Reel size | 300 mm x 100 mm |
| Reel rated for | 110 °C |

## Spools per part

| Part | Filament (m) | Spools | Filament weight |
|---|---|---|---|
| Top Brace Squared | 3 544 | 2.37 | ≈9.5 kg |
| Bottom Brace | 3 577 | 2.39 | ≈9.6 kg |
| Short Side Column | 3 535 | 2.36 | ≈9.4 kg |
| Column 6-15 | 4 056 | 2.71 | ≈10.8 kg |
| Roof Slant Mirror | 3 143 | 2.10 | ≈8.4 kg |

Every part needs **3 spools** loaded.

## How the table is calculated

1. **Filament length per part.** We use the length measured by the job log (`FILAMENT CONSUMED`
   on a complete print) when there is one. Otherwise we use the slicer's `;Filament used` line
   in the G-code header.
   - Bottom Brace: 3 577 m, P2, 2026-11-04 → 11-10 (G-code: 3 572 m).
   - Short Side Column: 3 535 m, P1, 2026-11-09 → 11-15 (G-code: 3 524 m).
   - Column 6-15: 4 056 m, P2, 2026-11-10 → 11-17.
   - Top Brace Squared (3 544 m) and Roof Slant Mirror (3 143 m): G-code only. The Top Brace
     logs started mid-print, so they only cover part of the job.

   Where both exist, the log and the G-code differ by only 5–11 m (priming and purging), so the
   G-code value is reliable.
2. **Weight per metre.** The cross-section of 1.75 mm filament is π × (1.75 / 2)² = 2.405 mm².
   At a density of 1.11 g/cc that gives 2.405 mm² × 1.11 g/cm³ = **2.67 g per metre**.
3. **Length per spool.** 4 000 g / 2.67 g/m = **≈1 498 m** per spool. The empty reel (1 000 g)
   is not filament and is left out.
4. **Per part.** Weight = metres × 2.67 g/m. Spools = weight / 4 000 g (= metres / 1 498).
   Round up to get the number of spools to load.

**Load-cell cross-check.** The empty reel's 1 000 g gives the load cells' scale: ≈205 counts/g
(reading without a reel vs with an empty reel). On `p1_cell1` and `p2_cell1`, full spool minus
empty reel comes to 4.04–4.11 kg, which confirms 4 kg net. The print-rate calibration
(`load cel/calibration_results.txt`) gives ~1 565 m per spool, ≈4 % more than the 1 498 m from
the datasheet. Actual filament diameter tolerance (≈1.73 mm instead of 1.75 mm) explains the
difference. The table uses the datasheet value, which is the more conservative one: with
1 565 m per spool every part needs ≈0.1 spool less. Either way, every part needs 3 spools.
