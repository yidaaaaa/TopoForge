# Authoritative Plan

Updated: 2026-07-31

## Roadmap

- [x] Phase 0 — repository, toolchain, official-source research, architecture, fixtures. Completed 2026-07-31.
- [x] Milestone 01 — full-raster local GeoTIFF/synthetic manufacturing core, deterministic 3MF/provenance, validation, and real slicer evidence. Completed 2026-07-31.
- [ ] Phase 1 closure — explicit local AOI bbox/polygon clipping and remaining GIS edge contracts.
- [ ] Phase 2 closure — revalidate deterministic export/provenance after Phase 1 AOI closure.
- [ ] Phase 3 — global/high-resolution providers, AOI/geocoding, cache, selection/fallback.
- [ ] Phase 4 — printer-aware sampling, build-volume/resource estimation, vertical-scaling refinements.
- [ ] Phase 5 — tiling, assembly manifest/map, labels, and verified connectors.
- [ ] Phase 6 — worker-backed FastAPI and React/MapLibre/Three.js Web application.
- [ ] Phase 7 — provenance-aware GPX/road/river/contour/label/coast overlays.
- [ ] Phase 8 — Docker, CI, complete benchmarks, reference regions, and release preparation.

## Completed Milestone 01 gate

- [x] Python 3.12 `uv` project and lock.
- [x] Recovery documents, Apache-2.0 license, governance, dependency/data notices.
- [x] Official provider, 3MF, and slicer research.
- [x] Deterministic analytic GeoTIFF catalog.
- [x] Full-raster metric/north-up normalization, rotated coverage crop, and cell budget.
- [x] Original NoData mask and bounded nearest-valid-cell interior-hole interpolation.
- [x] Natural, fit-height, auto-perceptual, and custom vertical policies.
- [x] Minimum, sea-level, custom, and low-percentile baseline mapping preserved through mesh construction.
- [x] Robust-range vertical policy plus hard final model-height and printer build-volume gate.
- [x] Closed rectangular terrain mesh with flat base and millimetre contract.
- [x] STL, GLB, official lib3mf 3MF with stable UUIDs and strict reread.
- [x] Geometry JSON/HTML, provenance, resolved YAML, checksum-verified manifest, PNG preview.
- [x] CLI build/synthetic/inspect/validate/slice/preview/providers/cache/doctor.
- [x] YAML config with explicit CLI option overrides.
- [x] OrcaSlicer and PrusaSlicer adapters with real execution evidence.
- [x] Reproducibility, property, unit, GIS, geometry, CLI, provider-registry, integration, and slicer tests.
- [x] Final gates: uv sync, Ruff check/format, Pyright, 71-test suite.

## Current milestone: Milestone 02 AOI and no-key global terrain

Dependencies: AOI normalization/local crop -> Copernicus AWS catalog/cache -> windowed COG fetch -> provider selection/fallback -> real Gongga/Amazon builds -> provenance and slicer evidence.

- [x] Verify current official sources, access, licenses, attribution, and no-key route.
- [ ] Implement bbox and center-radius `AreaOfInterest` normalization.
- [ ] Apply explicit AOI window/polygon clipping to local rasters.
- [ ] Implement antimeridian/high-latitude/cross-zone projection decisions and tests.
- [ ] Implement Nominatim-compatible geocoding with candidate handling and usage policy.
- [ ] Implement content-addressed cache, timeout/retry/rate-limit boundaries.
- [ ] Implement configurable `copernicus-aws` GLO-30/GLO-90 COG provider.
- [ ] Preserve Copernicus quality/editing masks when assets expose them.
- [ ] Implement explainable auto-selection and recorded fallback attempts.
- [ ] Execute a real Gongga build from fetched DSM data.
- [ ] Execute a real Amazon low-relief build with DSM semantics visible.
- [ ] Slice at least one global-provider 3MF and publish milestone-02 evidence.

## Milestone 02 acceptance

- Local and no-key global commands accept normalized bbox/center AOIs and emit complete bundles.
- Place resolution returns candidates for ambiguity rather than silently selecting.
- Provider provenance contains concrete release, tile URLs, ETag/Last-Modified, SHA-256, license notices, DSM semantics, horizontal resolution, EGM2008 vertical reference, NoData, and fallback history.
- Offline provider tests pass by default; live tests are marked integration.
- Real Gongga and Amazon builds plus at least one actual global-provider slice are preserved as evidence.

## Cancelled or replaced routes

- The initial hand-written Core-only 3MF writer was replaced by official `lib3mf==2.5.0`; deterministic UUID and strict-reader tests cover the production path.
- OrcaSlicer 2.4.2 on this Ubuntu 22.04 host was replaced operationally by OrcaSlicer 2.3.0/PrusaSlicer fallback after verified GLIBC/GLIBCXX incompatibility. The adapter still prefers a working Orca installation.
- The ambiguous `copernicus` provider descriptor was split into the no-key `copernicus-aws` route and authenticated `copernicus-cdse` route.
