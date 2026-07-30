# Architecture

## Boundary and units

TopoForge has one Python core used by CLI and future API/worker/Web adapters. Geospatial coordinates and elevation values use metres with an explicit CRS. Manufacturing vertices use millimetres. Interfaces select configuration; they do not duplicate raster, scaling, mesh, export, or validation logic.

## Phase 1/2 build pipeline

```text
BuildConfig (Pydantic)
  -> local raster open and metadata validation
  -> north-up metric CRS selection/reprojection when required
  -> source-coverage crop for rotated reprojection corners
  -> cell-budget average resampling
  -> conservative interior-hole NoData fill + preserved binary mask
  -> processed_dem.tif
  -> aspect-preserving horizontal scale
  -> natural / fit-height / auto-perceptual / custom vertical scale
  -> explicit regular-grid top + perimeter walls + flat bottom
  -> in-memory geometry invariants
  -> STL + lib3mf 3MF + GLB
  -> reopened STL validation + strict lib3mf reread + OPC/XML hardening
  -> preview.png + provenance/validation/config/manifest
  -> optional external slicer adapter and embedded slice evidence
```

Builds use a sibling staging directory. Every required file is written and reopened before the stage is atomically renamed to the requested new output directory. A non-empty destination is preserved and rejected.

## Package responsibilities

- `models` and `config`: external validation, units, semantics, printer profiles, resolved YAML.
- `raster`: analytic fixtures, local GeoTIFF ingestion, CRS normalization, resource guard, NoData policy.
- `scaling`: physical horizontal scale, baseline, robust relief, vertical exaggeration.
- `mesh`: deterministic topology; it does not repair an incomplete solid.
- `exporters`: format-specific serialization only. lib3mf stable UUIDv5 values make 3MF deterministic.
- `validation`: measured geometry plus independent OrcaSlicer/PrusaSlicer adapters.
- `rendering`: hillshade/color derived only from measured elevation samples.
- `provenance`: stable JSON and dependency-free HTML reports.
- `engine`: atomic orchestration and bundle verification.
- `cli`: Typer argument parsing and JSON presentation.
- `providers`: explainable provider contracts and capability registry.

## Geometry topology

For an `R x C` grid, TopoForge creates matching top and bottom vertex grids. Each interior cell contributes two top and two bottom triangles. A counter-clockwise perimeter contributes two outward-facing wall triangles per boundary edge. All faces share indexed vertices before export, the bottom is `z=0`, and the minimum top sample is exactly `base_thickness_mm`.

## Validation truth

Manufacturing claims come from the reopened STL, not only the source NumPy arrays. Checks include finite vertices/normals, watertightness, winding, edge manifoldness, positive volume, one component, degenerates, duplicates, dimensions, flat bottom, base thickness, and triangle count. Exhaustive self-intersection is explicitly `not_fully_checked` in the current backend.

The 3MF path adds official lib3mf strict read with zero warnings, unit/object/build/topology checks, independent XML dimensions, safe ZIP paths, supported compression, no encryption, and no external relationships. An actual slicer invocation remains a separate gate.

## Extension sequence

Phase 3 adds cached global providers and AOI/geocoding resolution without changing the build engine contract. Phase 4 makes sampling printer-aware. Tiling, API, and Web remain consumers of the same core.
