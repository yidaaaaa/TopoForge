# Deterministic tiling, extraction, and numerical seam contract

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
```

Both commands refuse overwrite. `tile-plan` strictly verifies the source build, derives the processed grid shape and model footprint, writes canonical sorted JSON atomically, and recomputes the layout identity on reopen. `tile-extract` verifies every source build-manifest checksum, stages all files, strictly verifies the complete staged tile set, then atomically publishes the directory.

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

## Next contracts

Numerical raster seam consistency is complete. This pass does not prove that independently exported tile solids assemble without gaps, duplicate walls, orientation changes, or Z offsets. The next Phase 5 gate generates per-tile meshes in one shared global manufacturing frame, verifies multi-tile mesh assembly, and adds a coverage image. Connector geometry plus printer-profile tolerance tests follow. Worker API implementation follows stable extraction/assembly/seam/connector contracts; Web/MapLibre/Three.js follows the stable API contract.
