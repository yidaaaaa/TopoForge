# Architecture

## Boundary and units

TopoForge has one Python core used by the CLI, isolated workers, loopback API, and Web application. Geospatial coordinates and elevation values use metres with an explicit CRS. Manufacturing vertices use millimetres. Interfaces select configuration; they do not duplicate raster, scaling, mesh, export, or validation logic.

## Phase 1/2 build pipeline

```text
BuildConfig (Pydantic)
  -> local raster open and metadata validation
  -> bbox/center-radius WGS84 AOI normalization and explicit source-pixel clipping
  -> north-up metric CRS selection/reprojection after clipping
  -> source-coverage crop for rotated reprojection corners
  -> deterministic print-aware/source-preserving/custom sampling
  -> adapt/strict cell/triangle/memory budget enforcement with average resampling
  -> conservative interior-hole NoData fill + preserved binary mask
  -> processed_dem.tif
  -> aspect-preserving horizontal scale
  -> natural / fit-height / auto-perceptual / custom vertical scale
  -> manufacturing preflight: printer fit/headroom, resource utilization, height policy
  -> row flip from raster-north row 0 to manufacturing +Y North
  -> explicit regular-grid top + perimeter walls + flat bottom
  -> in-memory geometry invariants
  -> STL + lib3mf 3MF + GLB
  -> reopened STL validation + strict lib3mf reread + OPC/XML hardening
  -> preview.png + manufacturing_preflight/provenance/validation/config/manifest
  -> official Bambu Studio P2S release gate + optional diagnostic slicers
  -> separate interoperable model.3mf and embedded-settings model.bambu-p2s.3mf
```

Phase 5 tiling starts from a completed bundle and the processed sample grid:

```text
processed_dem.tif + original_nodata_mask.tif + validation dimensions
  -> topoforge tile-plan
  -> deterministic tile-layout-v1 JSON
  -> topoforge tile-extract
  -> exact sampling-window DEM/mask GeoTIFFs
  -> canonical per-tile provenance/validation/manifests
  -> checksummed assembly manifest + north/west coverage map
  -> strict source-window equality and repeat-byte verification
  -> assembly-bound numerical seam report + fresh remeasurement
  -> topoforge tile-mesh
  -> global-frame per-tile STL/3MF/GLB + strict reopen
  -> mesh-boundary/volume/footprint assembly + coverage PNG
  -> topoforge tile-connect
  -> printer-derived dovetail plan + connector booleans below the terrain surface
  -> global assembly and reversible print-local STL/3MF/GLB
  -> fit/collision/wall/bed/build-volume/top-surface validation + connector map
  -> topoforge tile-slice
  -> actual per-tile G-code + official P2S parameter gate
  -> separate Bambu project 3MF + no-external-profile reopen/reslice evidence
```

Builds use a sibling staging directory. Every required file is written and reopened before the stage is atomically renamed to the requested new output directory. A non-empty destination is preserved and rejected.

Phase 6 adds a content-addressed single-workstation local/global orchestration layer without duplicating the core algorithms:

```text
topoforge wizard/run/resume + saved WorkflowLaunchConfig
  -> optional acquire/<request SHA-256> -> normalized AOI + provider/cache selection
  -> canonical acquire.json -> raster/provider-manifest/quality-mask SHA-256 binding
  -> source/<content SHA-256> -> local or acquired DEM identity record
  -> build/<stage SHA-256> -> build_local_terrain + strict bundle reopen
  -> layout/<stage SHA-256> -> deterministic layout plan
  -> extract/<stage SHA-256> -> exact raster tiles + seam verification
  -> mesh/<stage SHA-256> -> global-frame assembly verification
  -> connect/<stage SHA-256> -> connector/print-local verification
  -> slice/<stage SHA-256> -> optional actual G-code/release verification
  -> project/<stage SHA-256> -> optional Bambu export + isolated reopen/reslice
  -> canonical workflow manifest + atomic status + retained failure record
  -> measured workflow-summary.json + dependency-free workflow-report.html
  -> workflow-storage.json from configured ceilings or completed measurements
  -> reviewed cleanup of manifest-unreferenced stage identities only
  -> deterministic SHA-256 backup ZIP + atomic verified restore
```

Each stage identity binds its upstream manifest SHA-256 values and effective content settings. For global acquisition, AOI and provider-selection policy determine the stage identity while cache location, timeout, attempts, and rate-limit timing remain operational controls. Reuse strictly reopens the metric single-band raster, normalized AOI, provider/dataset trace, NoData count, source-acquisition manifest hash, and every aligned quality mask through canonical `acquire.json`; file existence alone is never sufficient. Changed settings choose another content-addressed path, failures receive retained status records, and reviewed evidence is never silently overwritten. The execution-specific CLI summary reports newly completed versus reused stages without making canonical artifacts depend on invocation history.

Phase 7 adds an optional content-addressed overlay stage after the immutable terrain build:

```text
verified build bundle + local OverlayConfig
  -> strict GPX or GeoJSON parse, or DEM-derived threshold contours
  -> explicit source CRS to processed metric CRS transformation
  -> clip to the exact model footprint and reject empty results
  -> map XY to the fixed-diagonal terrain triangles without changing elevations
  -> reject original NoData overlap unless explicitly allowed
  -> printer minimum-feature and triangle-budget gates
  -> independent watertight raised/embed overlay meshes
  -> per-layer STL + plan GeoJSON
  -> named terrain/overlay mesh resources
  -> one single-material components object + one top-level 3MF build item
  -> colored GLB + north-marked PNG
  -> provenance/validation HTML/JSON + strict checksum manifest
```

The overlay stage identity includes the source build manifest, complete overlay settings, and every local source SHA-256. Workflow backup/restore includes referenced external overlay files. Strict reuse reopens all STL, 3MF, GLB, PNG, JSON, YAML, source hashes, terrain hashes, assembly counts, and material assignments.

Phase 9 adds a local Web adapter over the same saved launch and workflow execution path:

```text
Chinese/English React form + MapLibre AOI + Three.js GLB viewer
  -> typed FastAPI request and AOI/job validation
  -> workspace/input-root containment
  -> durable request.json + job.json + monotonic events.jsonl
  -> bounded LocalJobManager queue
  -> isolated `python -m topoforge.web.worker` child process
  -> existing execute_workflow_launch() and content-addressed core stages
  -> strict workflow workspace reopen
  -> checksum-bound artifact inventory and downloads
  -> persistent completed/failed/cancelled state and recovery after server restart
```

The listener accepts only loopback addresses and trusted local host headers. Static
production assets are bundled below `topoforge.web`, verified against a SHA-256/size
manifest before application creation, and served with a restrictive content security
policy. The default map is offline; enabling the OpenStreetMap layer affects only visual
context. Browser choices do not modify terrain semantics, source resolution, orientation,
or manufacturing geometry.

## Package responsibilities

- `models` and `config`: external validation, units, semantics, printer profiles, resolved YAML.
- `raster`: AOI normalization/clipping, analytic fixtures, local GeoTIFF ingestion, CRS normalization, printer-aware sampling/resource guards, and NoData policy.
- `scaling`: physical horizontal scale, baseline, robust relief, vertical exaggeration.
- `mesh`: deterministic topology; it does not repair an incomplete solid.
- `exporters`: format-specific serialization only. lib3mf stable UUIDv5 values make 3MF deterministic.
- `validation`: measured geometry, official Bambu Studio/P2S release checks, and optional OrcaSlicer/PrusaSlicer diagnostics.
- `rendering`: hillshade/color derived only from measured elevation samples.
- `provenance`: stable JSON and dependency-free HTML reports.
- `engine`: atomic build orchestration, reusable local preflight, and bundle verification. `preflight_local_terrain()` runs the production raster/scaling path in a temporary directory without publishing a build.
- `workflow`: normalized global acquisition plus local source identity, content-addressed stage manifests, strict reuse, saved launch/resume settings, measured summaries, a workspace-contained static artifact browser, disk estimation, reviewed cleanup, verified backup/restore, status/failure records, and direct composition of existing core functions.
- `cli`: Typer argument parsing, reviewed wizard prompts, JSON presentation, optional desktop-browser opening, and no business-logic duplication.
- `providers`: normalized-AOI provider contracts, explainable deterministic selection/fetch fallback, Copernicus AWS catalog/tile/ancillary-mask planning, content-addressed objects/request indexes, bounded HTTP transport, source-footprint reprojection, and capability registry.
- `geocoding`: cached Nominatim-compatible candidate search and explicit ambiguity resolution; selected candidates become ordinary recorded AOIs before provider selection.
- `tiling`: versioned deterministic layout/extraction, numerical and mesh seam measurement, global-frame assembly, printer-derived connectors, reversible print-local placement, actual per-tile slicer evidence, deterministic maps/previews, complete SHA-256 binding, canonical JSON, and atomic publication. Overlap halos remain evidence rather than duplicate solids; terrain tops remain unchanged by base-only connector booleans.
- `web`: typed loopback API, strict local path boundaries, persistent isolated job workers, cancellation/recovery, checksum-verified artifact serving, packaged React assets, and no manufacturing-algorithm duplication.
- `overlays`: strict local GPX/GeoJSON and DEM-contour sources, CRS transformation, exact terrain-triangle surface mapping, NoData/minimum-feature/resource gates, deterministic label geometry, independent watertight meshes, components-assembly 3MF, colored preview artifacts, provenance, validation, and strict reuse.

## Geometry topology

For an `R x C` grid, TopoForge creates matching top and bottom vertex grids. Each interior cell contributes two top and two bottom triangles. A counter-clockwise perimeter contributes two outward-facing wall triangles per boundary edge. All faces share indexed vertices before export, the bottom is `z=0`, and the minimum top sample is exactly `base_thickness_mm`.

## Validation truth

Manufacturing claims come from the reopened STL, not only the source NumPy arrays. Checks include finite vertices/normals, watertightness, winding, edge manifoldness, positive volume, one component, degenerates, duplicates, dimensions, flat bottom, base thickness, and triangle count. Exhaustive self-intersection is explicitly `not_fully_checked` in the production backend. Phase 6 evaluated Manifold3D, libigl wheels, MeshLib, PyMeshLab/VCGLib, and Open3D: the behaviorally reliable candidates either imposed non-commercial/GPL distribution constraints and roughly 260-294 MB installs, while the MIT Open3D predicate failed the adjacent-contact and connected-intersection fixtures. No backend failure or unavailable check is reported as a pass.

The interoperable 3MF path adds official lib3mf strict read with zero warnings, unit/object/build/topology checks, independent XML dimensions, safe ZIP paths, supported compression, no encryption, and no external relationships. For the default P2S target, official Bambu Studio normative slicing and resolved-parameter assertions form a separate mandatory release gate. The Bambu project 3MF remains a second artifact because its vendor project parts are outside the Core-only lib3mf contract.

## Extension sequence

The local/resolved-place AOI, provider/cache path, printer-aware manufacturing core, Phase 5 manufacturing tiling, Phase 6 single-workstation workflow, Phase 7 local overlays, Phase 8 release hardening, and Phase 9 worker-backed bilingual local Web application and 0.8.1 visual stabilization are complete. Physical connector calibration remains deferred and non-blocking. Future work may add optional map-tile/assembly visualization or remote deployment contracts, but those must continue to call the same Python core.
