# Deterministic tiling contract

Phase 5 begins with `topoforge-tile-layout-v1`. The layout is a manufacturing-grid contract, not a slippy-map replacement: it partitions the processed DEM sample grid that already passed CRS, NoData, sampling, orientation, and printer preflight.

## Coordinate and identity rules

- `+X = East`, `+Y = North`, `+Z = Up`.
- Raster sample row `0` is the north edge; tile row `0` is northmost.
- Tile column `0` is westmost.
- Core windows are half-open cell windows and partition every processed cell exactly once.
- Adjacent core sample windows share their boundary sample row/column, so a seam has no missing edge sample.
- `sampling_window` adds a configured `overlap_cells` halo and clips it to the processed sample grid.
- Physical tile bounds are computed from integer grid fractions in millimetres; they never become the primary identity.
- IDs are fixed-width `tile-r####-c####`; keys namespace them under a deterministic layout digest derived from schema, source shape, model/tile sizes, overlap, and axis origins.

## CLI

```bash
uv run topoforge tile-plan \
  outputs/example-bundle \
  --max-tile-size-mm 120 120 \
  --overlap-cells 1 \
  --output artifacts/tiling/example-tile-layout.json
```

The command strictly reopens the completed bundle, derives the processed grid shape and model footprint from `processed_dem.tif` and `validation.json`, writes canonical sorted JSON atomically, and strictly reopens the layout. Existing output files are never overwritten.

## Next contracts

The layout is intentionally separate from per-tile extraction. The next implementation will crop the `sampling_window` from the processed DEM and original NoData mask while retaining the core window, CRS, transform, source coverage, and source hash. An assembly manifest will bind tile IDs, per-tile checksums, seam comparisons, coverage-map coordinates, and final mesh roles before connectors or API jobs are introduced.
