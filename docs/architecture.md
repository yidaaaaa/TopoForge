# Architecture

## Boundary and units

TopoForge has one Python core used by CLI and future API/worker/Web adapters. Geospatial coordinates and elevation values use metres with an explicit CRS. Manufacturing vertices use millimetres. Interfaces select configuration; they do not duplicate raster, scaling, mesh, export, or validation logic.

## Phase 1/2 build pipeline

```text
BuildConfig (Pydantic)
  -> local raster open and metadata validation
  -> bbox/center-radius WGS84 AOI normalization and explicit source-pixel clipping
  -> north-up metric CRS selection/reprojection after clipping
  -> source-coverage crop for rotated reprojection corners
  -> deterministic print-aware/source-preserving/custom sampling
  -> cell/triangle/memory budget enforcement with average resampling
  -> conservative interior-hole NoData fill + preserved binary mask
  -> processed_dem.tif
  -> aspect-preserving horizontal scale
  -> natural / fit-height / auto-perceptual / custom vertical scale
  -> row flip from raster-north row 0 to manufacturing +Y North
  -> explicit regular-grid top + perimeter walls + flat bottom
  -> in-memory geometry invariants
  -> STL + lib3mf 3MF + GLB
  -> reopened STL validation + strict lib3mf reread + OPC/XML hardening
  -> preview.png + provenance/validation/config/manifest
  -> official Bambu Studio P2S release gate + optional diagnostic slicers
  -> separate interoperable model.3mf and embedded-settings model.bambu-p2s.3mf
```

Builds use a sibling staging directory. Every required file is written and reopened before the stage is atomically renamed to the requested new output directory. A non-empty destination is preserved and rejected.

## Package responsibilities

- `models` and `config`: external validation, units, semantics, printer profiles, resolved YAML.
- `raster`: AOI normalization/clipping, analytic fixtures, local GeoTIFF ingestion, CRS normalization, printer-aware sampling/resource guards, and NoData policy.
- `scaling`: physical horizontal scale, baseline, robust relief, vertical exaggeration.
- `mesh`: deterministic topology; it does not repair an incomplete solid.
- `exporters`: format-specific serialization only. lib3mf stable UUIDv5 values make 3MF deterministic.
- `validation`: measured geometry, official Bambu Studio/P2S release checks, and optional OrcaSlicer/PrusaSlicer diagnostics.
- `rendering`: hillshade/color derived only from measured elevation samples.
- `provenance`: stable JSON and dependency-free HTML reports.
- `engine`: atomic orchestration and bundle verification.
- `cli`: Typer argument parsing and JSON presentation.
- `providers`: normalized-AOI provider contracts, explainable deterministic selection/fetch fallback, Copernicus AWS catalog/tile/ancillary-mask planning, content-addressed objects/request indexes, bounded HTTP transport, source-footprint reprojection, and capability registry.
- `geocoding`: cached Nominatim-compatible candidate search and explicit ambiguity resolution; selected candidates become ordinary recorded AOIs before provider selection.

## Geometry topology

For an `R x C` grid, TopoForge creates matching top and bottom vertex grids. Each interior cell contributes two top and two bottom triangles. A counter-clockwise perimeter contributes two outward-facing wall triangles per boundary edge. All faces share indexed vertices before export, the bottom is `z=0`, and the minimum top sample is exactly `base_thickness_mm`.

## Validation truth

Manufacturing claims come from the reopened STL, not only the source NumPy arrays. Checks include finite vertices/normals, watertightness, winding, edge manifoldness, positive volume, one component, degenerates, duplicates, dimensions, flat bottom, base thickness, and triangle count. Exhaustive self-intersection is explicitly `not_fully_checked` in the current backend.

The interoperable 3MF path adds official lib3mf strict read with zero warnings, unit/object/build/topology checks, independent XML dimensions, safe ZIP paths, supported compression, no encryption, and no external relationships. For the default P2S target, official Bambu Studio normative slicing and resolved-parameter assertions form a separate mandatory release gate. The Bambu project 3MF remains a second artifact because its vendor project parts are outside the Core-only lib3mf contract.

## Extension sequence

The local/resolved-place AOI, printer-aware sampling, content-addressed cache, configurable no-key Copernicus AWS GLO-30/GLO-90 provider, explainable provider-selection/fetch-fallback engine, Nominatim-compatible candidate handling, and quality-mask preservation are complete. Network acquisitions enter the existing local pipeline as a metric AOI raster plus `source_acquisition.json`; no download-first parallel geometry path exists. Next steps are additional production provider implementations, remaining manufacturing-resource UX, tiling, API, and Web.
