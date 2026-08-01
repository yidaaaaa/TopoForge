# Deterministic tiling, connectors, and print-local manufacturing contract

Phase 5 starts from a completed, checksum-verified TopoForge build bundle. Tiling is a manufacturing-grid contract, not a slippy-map replacement: it partitions the already normalized and printer-sampled processed DEM without downloading, resampling, mirroring, sharpening, or changing elevations.

## Coordinate and identity rules

- `+X = East`, `+Y = North`, `+Z = Up`.
- Raster sample row `0` is the north edge; tile row `0` is northmost.
- Tile column `0` is westmost.
- Core windows are half-open cell windows and partition every processed cell exactly once.
- Adjacent core sample windows share their boundary sample row/column, so no edge sample is missing.
- `sampling_window` adds a configured `overlap_cells` halo and clips it to the processed sample grid.
- Physical tile bounds are computed from integer grid fractions in millimetres; they never become the primary identity.
- IDs are fixed-width `tile-r####-c####`; keys namespace them under a deterministic layout digest derived from schema, source shape, model/tile sizes, overlap, axes, and row/column origins.

## CLI

Plan a layout from a verified bundle:

```bash
uv run topoforge tile-plan \
  outputs/example-bundle \
  --max-tile-size-mm 120 120 \
  --overlap-cells 1 \
  --output outputs/example-tile-layout.json
```

Extract the planned sampling windows into a new directory:

```bash
uv run topoforge tile-extract \
  outputs/example-bundle \
  --layout outputs/example-tile-layout.json \
  --output outputs/example-tile-set

uv run topoforge tile-mesh \
  outputs/example-tile-set \
  --source-bundle outputs/example-bundle \
  --output outputs/example-tile-mesh-set

uv run topoforge tile-connect \
  outputs/example-tile-mesh-set \
  --tile-set outputs/example-tile-set \
  --source-bundle outputs/example-bundle \
  --output outputs/example-print-tile-set

uv run topoforge tile-slice \
  outputs/example-print-tile-set \
  --mesh-set outputs/example-tile-mesh-set \
  --tile-set outputs/example-tile-set \
  --source-bundle outputs/example-bundle \
  --slicer bambu-studio \
  --machine-profile MACHINE.json \
  --process-profile PROCESS.json \
  --filament-profile FILAMENT.json \
  --output outputs/example-tile-slice-set
```

All five commands refuse overwrite. Each stage strictly verifies its complete source chain, writes into a sibling staging directory, reopens and remeasures every new role, and atomically publishes only after the stage passes. `tile-connect` consumes immutable global-frame mesh evidence; `tile-slice` consumes only the new print-local 3MF role.

## Published tile-set roles

```text
example-tile-set/
  tile-layout.json
  coverage_map.json
  seam_report.json
  assembly_manifest.json
  tiles/
    tile-r0000-c0000/
      processed_dem.tif
      original_nodata_mask.tif
      tile_provenance.json
      tile_validation.json
      tile_manifest.json
    ...
```

`tile_provenance.json` retains:

- layout, tile, row, column, core-cell, core-sample, and sampling-window identities;
- physical `+X East/+Y North` bounds;
- CRS and window-adjusted transform;
- source bundle manifest SHA-256;
- retained raw source DEM SHA-256 and processed DEM SHA-256;
- source grid shape, overlap count, source bounds, dataset semantics, and orientation evidence.

`tile_validation.json` measures the sampling/core shapes, finite elevation status, binary original NoData mask, NoData count/fraction, sample/core extrema, CRS, transform, and one required-check result. `tile_manifest.json` binds the four tile artifacts and the exact validation object to SHA-256 values.

`coverage_map.json` is a canonical north-to-south list of west-to-east tile IDs. `assembly_manifest.json` binds the layout, coverage-map, and seam-report hashes, source identities, tile grid, overlap, relative paths, per-tile manifest hashes, artifact hashes, windows, and physical bounds. Paths are relative and validated against traversal.

## Numerical seam verification

`seam_report.json` uses schema `topoforge-tile-seam-report-v1`. Adjacent pairs are enumerated exactly once in stable row-major order by checking each tile's east neighbor and then south neighbor. Each pair compares both the one-sample shared core boundary and the complete intersection of the two overlap sampling windows.

The report records per-seam and aggregate sample counts, maximum/mean absolute elevation differences, mismatch counts, original NoData-mask mismatch counts, CRS equality, and transform-coordinate alignment measured at the overlap corners in the source metric CRS. Extracted windows are direct source slices, so the default elevation tolerance is exactly `0.0 m`; transform alignment must be at most `1e-9 m`. A seam passes only when every compared elevation is within tolerance, both masks match exactly, CRS values match, and the transform threshold passes.

New tile sets always publish the canonical report and bind its SHA-256 from the assembly manifest. `verify_tile_set()` checks that hash, strictly reopens the report, remeasures all tile rasters using the recorded elevation tolerance, and rejects any difference or failed required check. Legacy v1 assembly manifests that omit both optional seam fields remain readable and are reported as `not-reported`; that compatibility does not add a seam claim to old evidence.

## Published mesh-set roles

```text
example-tile-mesh-set/
  tile-layout.json
  tile-coverage.png
  tile-mesh-assembly-validation.json
  tile-mesh-assembly-manifest.json
  tiles/
    tile-r0000-c0000/
      model.global.stl
      model.global.3mf
      preview.global.glb
      tile_mesh_validation.json
      tile_mesh_manifest.json
    ...
```

Only each tile's `core_sample_window` becomes geometry; overlap halos remain source/seam evidence for later connector work. Source elevations are mapped through the exact `ScalingResult` stored in source provenance. The core heightfield is flipped once so source row 0 remains north, built as a closed flat-bottom solid, and translated by `physical_bounds_mm` into the common model origin. Per-tile validation strictly reopens STL, GLB, and 3MF; compares global bounds, peak coordinates, triangle counts, orientation metadata, watertightness, winding, manifoldness, volume, bottom, and dimensions; and binds all roles to the source tile manifest and DEM hashes.

The root assembly validation compares reopened STL boundary samples for every east/south adjacency, checks the complete global bounds and non-overlapping footprint partition, and compares the summed tile volume with the original source STL. `tile-coverage.png` maps row 0 north and column 0 west, includes north/east markers, and is bound by the root mesh manifest. These global-frame meshes are assembly evidence; print-local placement and per-tile slicing are a subsequent gate.

## Connector and print-local roles

`connector-plan.json` fixes connector IDs, seam ownership, polarity, insertion direction, XY polygons, Z ranges, and a printer-derived policy. West tiles own male dovetails on east-west seams; north tiles own male dovetails on north-south seams. Female cavities open through the bottom and assemble downward along `+Z`. `connector_tolerance_mm` is total lateral clearance, split equally per side. The verified range, minimum wall, connector height, remaining roof, neck/head/depth, edge margin, and maximum spacing are derived from nozzle diameter, layer height, minimum feature, base thickness, and the profile tolerance.

Each print tile publishes connector-bearing global STL/3MF/GLB and print-local STL/3MF/GLB. The local transform translates the actual connector bounds to `(0,0,0)` without rotation; the inverse transform is recorded exactly. Validation reopens all six formats, verifies reversible transforms, bounds, peaks, topology, bed-contact planarity, thin walls, build-volume fit, unchanged terrain-top vertices, male presence, female cavity removal, and assembled collision volume. `connector-map.png` labels every tile and depicts male/female placement with north orientation.

`tile-slice-manifest.json` binds the print assembly, connector plan, slicer executable, copied settings/filament hashes, and every G-code/report. Per-tile reports preserve the literal command, stdout/stderr, exit code, G-code hash/size, reopened layer/time/material metrics, and explicit out-of-bed, empty-layer, floating-region, and support checks. Bambu Studio results additionally carry the complete P2S parameter gate. The release script `scripts/verify_phase5_bambu_tile_projects.py` creates the separate Bambu project 3MF role, verifies its archive and embedded G-code MD5, and performs a second normative slice without external profiles.

## Strict verification

`verify_tile_set()` reopens and cross-checks:

1. canonical `tile-layout.json`, `coverage_map.json`, and `assembly_manifest.json`;
2. layout digest, grid, source-bundle model footprint, tile order, origins, and root hashes;
3. every relative path and per-tile manifest/artifact SHA-256;
4. canonical tile provenance and validation JSON;
5. every tile DEM/mask band, CRS, shape, transform, finite values, binary mask, extrema, and NoData measurements;
6. raw source, processed DEM, and source build-manifest hashes;
7. when the source bundle is supplied, exact array equality between every extracted raster and its declared source `sampling_window`;
8. the optional assembly-bound seam-report checksum, canonical bytes, layout/source identity, and fresh numerical remeasurement.

The extractor runs this verifier before publication. The CLI runs it again against the final published directory. Failures remove staging output; an existing requested destination is preserved.

`verify_tile_mesh_set()` additionally rechecks the source tile-set seam gate, source build/STL/scaling identities, every mesh-set relative path and checksum, canonical per-tile/root reports, reopened STL/GLB/3MF measurements, 3MF metadata, mesh-boundary samples, global bounds, footprint area, volume sum, and coverage PNG. Generation invokes the same verifier before atomic publication.

`verify_print_tile_set()` regenerates the connector plan from the saved printer profile and base, reopens every global/local STL/3MF/GLB, compares source terrain-top vertices, remeasures male/cavity/collision volumes, checks bed contact, walls, printer fit, transforms, metadata, map/preview, and all hashes. `verify_tile_slice_set()` reparses every G-code and recomputes all aggregate metrics and P2S parameter gates.

## Determinism

Layout and JSON bytes use sorted keys, compact separators, explicit UTF-8, and one terminal newline. GeoTIFF windows use identical source values, CRS, transforms, dtype, and deterministic writer settings. Identical source bundles, layouts, and code produce byte-identical tile-set roles.

The retained Gongga `resource-v3` seam evidence uses a `100 x 100 mm` maximum tile footprint and one overlap cell:

- layout: `layout-694b1e78d24ba9f5920e`;
- tile grid: `2 x 2` / four tiles;
- model footprint: `180 x 175.35911560058594 mm`;
- each overlapped sampling grid: `221 x 227` samples;
- each core sample grid: `220 x 226` samples;
- seam count: `4`;
- shared core / overlap sample counts: `892 / 2688`;
- maximum core / overlap elevation difference: `0.0 / 0.0 m`;
- core / overlap elevation mismatches: `0 / 0`;
- core / overlap mask mismatches: `0 / 0`;
- maximum transform alignment error: `0.0 m`;
- all CRS match and seam status: `true / passed`;
- primary/repeat file count: `24` each;
- primary/repeat byte comparison: `24/24` identical;
- retained source SHA-256: `00664a26192dea531606e60978f902bccbd3d93499c10c2ba89f9d37f4d7bbbc`.

Evidence: `artifacts/verification/topoforge-0.3.1-gongga-tile-seams-100mm-v2.json` and its checksum/determinism companions.

The derived Gongga mesh set contains four global-frame solids at 198,876 triangles each. Four reopened STL mesh seams have `0.0 mm` planar and Z error with zero mismatches; global bounds match, footprint overlap is `0.0 mm2`, and summed volume differs from the source STL by `0.0007486393442377448 mm3` within a `6.391751232061993 mm3` tolerance. The `1200 x 1258` coverage PNG was visually checked, and all 24 primary/repeat files are byte-identical. Evidence: `artifacts/verification/topoforge-0.3.1-gongga-tile-meshes-100mm-v1.json`.

The completed Gongga connector set has eight bottom dovetails over four seams, `0.2 mm` total lateral / `0.2 mm` vertical clearance, `0.0 mm` terrain-top deviation, and `0.0 mm3` assembled collision. Bed contact, remaining walls, printer volume, global bounds, and every fit probe pass. Primary/repeat comparison is 37/37 byte-identical. Official Bambu Studio `02.07.01.62` sliced all four local tiles with the complete P2S parameter gate, maximum 224 layers, total `224.53 g`, and no out-of-bed, empty-layer, floating-region, or support result. Four Bambu project 3MF files passed archive/MD5 and no-external-profile reopen/reslice. Evidence: `artifacts/verification/topoforge-0.4.0-gongga-phase5-verification.json`.

## Next contracts

Phase 5 layout, extraction, seams, connectors, print-local transforms, actual per-tile slicing, and Bambu project roles are complete and frozen at TopoForge 0.4.0. Phase 6 now focuses on an easier resumable local software workflow. Physical calibration is retained but skipped for now. API/Web is retained as a deferred later phase after local completion. Manufacturing tiles remain distinct from future Web XYZ map tiles.
