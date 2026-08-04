# Printing and slicer validation

Manufacturing coordinates are millimetres. STL relies on this coordinate contract; 3MF explicitly stores millimetre units. The chosen elevation baseline maps to the configured base reference above a flat `z=0` bottom. Minimum-baseline builds place the lowest terrain sample exactly there; other datum modes preserve their absolute offset and must still pass the printer-profile minimum-material gate.

## Printer-aware mesh sampling

`sampling_mode` is one of:

- `print-aware` (default): selects the finest relevant printer limit from `preferred_mesh_sampling_mm`, nozzle diameter, and minimum feature size, but never upsamples beyond source data.
- `source-preserving`: requests every normalized source sample, then applies the explicit cell and memory budgets with a recorded warning if either budget limits the request.
- `custom`: uses `mesh_sampling_mm` directly, or uses `max_grid_cells` as the explicit control when spacing is omitted.

The decision is deterministic. Reports separate source and processed resolution/grid shape, physical model spacing, downsampling factor, exact triangle estimate, memory estimate, peak elevation loss, peak horizontal shift, thresholds, reasons, and warnings. Average resampling may reduce a raster peak; that measured loss is retained instead of reconstructing or sharpening terrain that the source did not contain.

## Axis and north convention

Manufacturing coordinates are `+X = East`, `+Y = North`, and `+Z = Up`. A north-up raster stores its north edge in row 0, so the heightfield is flipped once before mesh construction. This is a coordinate-direction correction only. It does not mirror east/west, change elevations, reverse winding, or alter the geographic terrain. The preview includes a north arrow, 3MF metadata records the axes/source bounds/transform, and reopened STL, GLB, and 3MF peak coordinates must agree.

## Default release target

TopoForge defaults to **Bambu Lab P2S with a 0.4 mm nozzle**. A production 3MF is release-ready only after official Bambu Studio completes normative checking and slicing with resolved official presets. OrcaSlicer and PrusaSlicer may provide independent diagnostic evidence but do not satisfy the P2S release gate.

The default verified preset set is:

| Role | Required value |
| --- | --- |
| Machine | `Bambu Lab P2S 0.4 nozzle` |
| Build volume | `256 x 256 x 256 mm` |
| Process | `0.20mm Standard @BBL P2S` |
| Filament | `Bambu PLA Basic @BBL P2S` |
| Plate | `Textured PEI Plate` |
| Layer / first layer | `0.20 / 0.20 mm` |
| Walls / top / bottom | `2 / 5 / 3` |
| Sparse infill | `15% grid` |
| Support | disabled |
| Brim | `auto_brim`, width `5 mm` |
| Nozzle / bed | `220 / 55 °C` |

Bambu's leaf preset JSON files contain inheritance and include references. Passing the leaf files directly can report success while silently producing a `200 x 200 mm` generic bed and generic start G-code. `scripts/resolve_bambu_profiles.py` therefore flattens parent profiles and included P2S firmware templates before validation. The gate compares resolved settings and G-code comments, not preset names alone.

## Verified Gongga reference

The delivered reference uses real Copernicus DEM GLO-30 terrain for Mount Gongga and passed official Bambu Studio `02.07.01.62` software validation:

- Project: `outputs/gongga-copernicus-glo30-bambu-p2s/model.bambu-p2s.3mf`
- Project SHA-256: `27aa850ebe666c14cc061dfcba97a42d4729a0bd9a37fd0a4d1888961386a55f`
- Primary G-code SHA-256: `2b6373a306f4d82adc2bb5bde2243b9528cb4e7cb5f305a9cf0d016466e050ae`
- Reopen G-code SHA-256: `2bf706403e4e82f299246af745e418c98d3a43ae105ca25a3e566cda4cf64d7c`
- First result: `return_code=0`, `Success.`, `224` layers, `210.9887 g`, `24492.496 s`
- Independent reopen: no external settings or filament files; `return_code=0`, `Success.`, empty structured warning
- Full record: `outputs/gongga-copernicus-glo30-bambu-p2s/bambu_studio_validation.json`

The 3MF ZIP test, embedded G-code MD5, embedded-versus-external G-code bytes, embedded resolved settings, P2S firmware scripts, build-volume placement, and both structured slicing results are checked. This specific evidence remains a software validation record.

## Real-world print status

The project operator has physically printed several TopoForge terrain models on a Bambu Lab P2S and reported very good results for all of them. This is qualitative end-to-end printability evidence.

Exact print settings, model identities, photos, and dimensional measurements for those terrain prints were not published, and the report is not vendor certification.

A compact six-pair connector coupon was subsequently printed on the same P2S. The first version showed that all `.10` through `.40` pairs inserted and felt good, but raised labels on both top surfaces contacted before complete seating. The corrected v3 coupon recesses each label by `0.4 mm` without changing the male/female connector dimensions and passes official Bambu Studio project export plus standalone reopen/reslice. The operator then printed all six v3 pairs and reported complete seating with very good results, confirming that the label-interference defect is resolved. No production tolerance is promoted yet because per-clearance insertion force, play, dimensional error, print parameters, and a preferred clearance were not reported.

## Artifact roles

- `model.3mf`: deterministic interoperable geometry, strict-read with lib3mf.
- `model.bambu-p2s.3mf`: Bambu Studio project with embedded P2S settings and sliced plate data.
- `bambu_studio_validation.json`: official binary identity, commands, profile hashes, parameter assertions, slicing results, reopen/reslice evidence, and release decision.
- `resolved_print_settings.json`: the complete settings embedded in the Bambu project.

Bambu project 3MF files contain vendor project parts that lib3mf does not treat as the Core-only source package. Keep both 3MF roles rather than replacing the interoperable source.

## Operational PLA note

Bambu Studio may emit the official `bed_temperature_too_high_than_filament` enclosure ventilation notice for PLA on a 55 °C Textured PEI Plate. Open the door or remove the top cover during the print as directed by the machine workflow. This notice is recorded separately from structural slicing success.

The Milestone 01 PrusaSlicer result remains historical diagnostic evidence. It is not P2S release certification.
