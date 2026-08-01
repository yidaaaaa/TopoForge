# Local overlays

TopoForge 0.6.0 adds local, provenance-aware overlays to an already verified terrain build. The terrain DEM and source manufacturing artifacts remain immutable. Overlay solids are separate raised/embed meshes mapped onto the existing terrain triangles.

## Supported sources

| Kind | Input | Geometry |
| --- | --- | --- |
| `gpx` | GPX 1.1 track/route in WGS84 | buffered line |
| `road` | GeoJSON line/multiline with explicit CRS | buffered line |
| `river` | GeoJSON line/multiline with explicit CRS | buffered line |
| `coast` | GeoJSON line/multiline with explicit CRS | buffered line |
| `label` | GeoJSON point/multipoint with label property | deterministic bitmap boxes |
| `contour` | generated from the processed DEM | threshold-cell-boundary lines |

GeoJSON parsing rejects unsupported geometry, non-finite coordinates, missing CRS configuration, empty results, and excessive feature counts. GPX parsing rejects external entities and malformed tracks. Source files, license, attribution, version, acquisition period, URLs, CRS, size, and SHA-256 enter provenance.

Generated contours use only measured DEM values. They do not interpolate extra peaks, sharpen terrain, or adjust elevation to a reference summit.

## Configuration

```yaml
sources:
  - source_id: route
    kind: gpx
    format: gpx
    path: data/route.gpx
    dataset_name: Local route
    dataset_version: 1
    license: User supplied
    attribution: Local survey
    style:
      color: "#d1495b"
      line_width_mm: 0.8
      raised_height_mm: 0.4
      embed_depth_mm: 0.2

  - source_id: contours
    kind: contour
    format: generated-contours
    dataset_name: DEM derived contours
    license: Inherits terrain source terms
    attribution: Derived by TopoForge
    contour_interval_m: 20
    style:
      color: "#7f5539"
      line_width_mm: 0.6

allow_original_nodata: false
clip_to_model: true
max_features: 20000
max_triangles: 2000000
preview_width_px: 1200
```

Run:

```bash
uv run topoforge overlay outputs/terrain-build \
  --config overlays.yaml \
  --output outputs/terrain-overlays
```

`topoforge run` and `topoforge wizard` also accept `--overlay-config`. The workflow creates a content-addressed `15-overlay` stage after the build and before layout/extraction. A repeated run reuses it only after strict verification.

## Geometry and orientation

All vector coordinates transform explicitly into the processed metric CRS. Model XY uses the same sample-centre mapping as the terrain:

- `+X = East`
- `+Y = North`
- `+Z = Up`
- source raster row 0 maps to `y=model_depth_mm`

Every overlay point is evaluated against the same fixed diagonal used by the terrain mesh. The maximum surface mapping error is measured per layer. Raised solids extend above the surface; embed depth intersects the terrain to prevent floating parts. The source terrain mesh and elevations are never rewritten.

Original NoData overlap is rejected by default. Explicit opt-in records the overlap area; it does not invent elevation values.

## Manufacturing outputs

A completed overlay bundle contains:

- `layers/SOURCE_ID.stl` for each independent overlay source
- `model-with-overlays.3mf`
- `preview-with-overlays.glb`
- `overlay-preview.png`
- `overlay-plan.geojson`
- `overlay_config.resolved.yaml`
- `provenance.json`
- `validation.json` and `validation.html`
- `overlay_manifest.json`

The 3MF keeps named mesh resources for terrain and every overlay. All meshes have one explicit Core base-material assignment. One components object references every mesh with an identity transform, and one top-level build item references the assembly. This preserves relative placement while retaining independent names and geometry hashes.

Strict verification checks:

- source terrain hashes before and after
- local source hashes
- watertight, winding-consistent, positive-volume layer meshes
- layer bounds, feature counts, triangle budgets, and printer minimum features
- original NoData policy
- exact 3MF mesh/component/material/build-item counts and zero lib3mf warnings
- GLB geometry count, PNG decoding, plan GeoJSON, and every manifest checksum
- deterministic bytes across output directories

## Slicing evidence

The retained Amazon Phase 7 release uses synthetic local verification vectors for road, river, coast, label, and GPX. They are not real mapped features. Contours derive from the retained real Copernicus DEM. The official Bambu Studio P2S slice exits 0 with 49 layers, 23.74 g, no floating region, no empty layer, no out-of-bed result, and no support material.

Bambu Studio emits `T65535` from the official P2S machine end G-code as an AMS unload sentinel. It also emits internal `ZFiller` polygon diagnostics on accepted single-terrain baselines. Phase 7 preserves those literal outputs and classifies them in `artifacts/verification/topoforge-0.6.0-phase7-overlays-verification.json`; it does not suppress logs or modify the official preset.
