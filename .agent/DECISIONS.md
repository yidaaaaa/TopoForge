# Architecture Decisions

## ADR-001 — Python-first core and thin adapters

- **Date:** 2026-07-31
- **Context:** CLI, API, and Web must share geospatial/manufacturing behavior.
- **Decision:** Implement one typed Python package; CLI/API/worker layers call the same build service.
- **Alternatives:** Separate implementations; an early Rust core.
- **Impact:** Lower duplication and faster correctness work; Rust remains an evidence-driven optimization.

## ADR-002 — Trimesh plus explicit regular-grid construction

- **Date:** 2026-07-31
- **Context:** Phase 1 needs auditable closed rectangular terrain meshes.
- **Decision:** Construct top, walls, and base explicitly with NumPy and use Trimesh for serialization and independent geometric measurements. Do not rely on repair to create missing topology.
- **Alternatives:** Blender GUI, OpenSCAD, implicit surfaces.
- **Impact:** Deterministic topology, headless operation, and precise winding tests.

## ADR-003 — Rasterio/PyProj geospatial boundary

- **Date:** 2026-07-31
- **Context:** GeoTIFF CRS, transforms, NoData, reprojection, and output metadata must remain explicit.
- **Decision:** Use Rasterio/GDAL and PyProj; meshes are generated only after conversion to a local metric grid.
- **Alternatives:** Hand-written TIFF/CRS parsing; xarray/rioxarray as mandatory core dependencies.
- **Impact:** Mature metadata handling with a smaller Phase 1 surface.

## ADR-004 — Unknown vertical datum remains unknown

- **Date:** 2026-07-31
- **Context:** Many GeoTIFFs omit vertical reference metadata.
- **Decision:** Preserve an explicit `unknown` datum instead of guessing; surface it in validation/provenance.
- **Alternatives:** Infer ellipsoidal/orthometric datum from horizontal CRS or region.
- **Impact:** Honest semantics; later providers can supply verified transformations.

## ADR-005 — Official lib3mf is the manufacturing 3MF writer

- **Date:** 2026-07-31
- **Context:** 3MF requires explicit units, metadata, deterministic identifiers, strict reread, and future multi-object support.
- **Decision:** Pin `lib3mf==2.5.0`, derive UUIDv5 identifiers from canonical geometry/metadata, strict-write/read with zero warnings, harden the OPC ZIP, and independently compare XML topology/dimensions.
- **Alternatives:** Hand-written Core XML package; Trimesh exporter.
- **Impact:** Reference implementation and extension path at the cost of a native wheel dependency; Linux ARM64 packaging remains future work.

## ADR-006 — Slicer adapter preference and truth boundary

- **Date:** 2026-07-31
- **Context:** Slicer availability and CLI flags differ by release/host.
- **Decision:** Prefer a working OrcaSlicer adapter and fall back to PrusaSlicer. Execute argument arrays without a shell, publish G-code atomically, parse the G-code itself, and embed literal version/command/output/exit/metrics into the build.
- **Alternatives:** Trimesh reopen as a proxy; one hard-coded slicer path.
- **Impact:** Real manufacturing evidence remains separate from geometry validation and degrades explicitly by availability.

## ADR-007 — Copernicus AWS is the planned no-key global route

- **Date:** 2026-07-31
- **Context:** Phase 3 needs a legal, current, no-private-key global land source.
- **Decision:** Implement configurable Copernicus DEM AWS 2021 COG mirrors, GLO-30 first and GLO-90 fallback, with DSM semantics and exact Copernicus notices.
- **Alternatives:** CDSE authenticated access, NASADEM Earthdata, OpenTopography personal key, legacy terrain PNGs.
- **Impact:** No account for the default path; mirror release/completeness must remain explicit and ocean gaps require a separate policy/provider.

## ADR-008 — Crop reprojection-only gaps, preserve source NoData

- **Date:** 2026-07-31
- **Context:** Normalizing a rotated raster to a north-up grid creates corner cells outside the source footprint.
- **Decision:** Reproject a separate source-coverage mask, crop to its largest all-covered rectangle, then apply the normal source-NoData policy inside that crop.
- **Alternatives:** Interpolate corners; reject every rotated raster; zero-fill outside coverage.
- **Impact:** Rectangular Phase 1 models stay evidence-backed without confusing geometric reprojection gaps with source observations.
