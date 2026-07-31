# Authoritative Plan

Updated: 2026-08-01

## Roadmap

- [x] Phase 0 — repository, toolchain, official-source research, architecture, fixtures. Completed 2026-07-31.
- [x] Milestone 01 — full-raster local GeoTIFF/synthetic manufacturing core, deterministic 3MF/provenance, validation, and real slicer evidence. Completed 2026-07-31.
- [x] Phase 1 closure — explicit local AOI bbox/center-radius clipping and GIS edge contracts. Completed 2026-07-31.
- [x] Phase 2 closure — deterministic export/provenance revalidated after Phase 1 AOI closure. Completed 2026-07-31.
- [ ] Phase 3 — global/high-resolution providers, AOI/geocoding, cache, selection/fallback.
- [ ] Phase 4 — printer-aware sampling foundation is complete; remaining build-volume/resource UX and vertical-scaling refinements continue.
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

## Completed cross-cutting P2S release hardening

- [x] Make bambu-p2s-0.4 the default build profile while retaining an explicit generic FDM profile.
- [x] Make official Bambu Studio the production slice validator; keep OrcaSlicer/PrusaSlicer diagnostic-only.
- [x] Flatten official P2S machine/process/filament inheritance and include fragments.
- [x] Hard-check the 256 mm platform, nozzle, preset IDs, layer/shell/infill/support/brim/temperature settings, and slice warnings.
- [x] Preserve dual 3MF roles: interoperable lib3mf source plus Bambu project with embedded settings.
- [x] Reopen the Bambu project without external profiles and complete a second normative slice.
- [x] Publish the real Copernicus GLO-30 Gongga P2S reference bundle and verification record.
- [x] Add unit, CLI, adapter, parser, and manufacturing-gate regressions; final suite is 80 passed.

## Completed fidelity/orientation hardening

- [x] Add deterministic `print-aware`, `source-preserving`, and `custom` sampling modes.
- [x] Enforce cell/triangle/memory budgets and report source versus processed resolution.
- [x] Report raw/processed extrema, peak loss, peak shift, thresholds, decisions, and warnings.
- [x] Standardize manufacturing coordinates as +X East, +Y North, +Z Up.
- [x] Cross-check asymmetric corners and peak positions in STL, GLB, and strict-read 3MF.
- [x] Rebuild the existing real DSM without another download at 439 x 451 / 68.9589 m.
- [x] Actual PrusaSlicer diagnostic: exit 0, 149 layers, no floating/empty/out-of-bed/support warning.
- [x] Repeat real build: DEM/STL/3MF/GLB/PNG roles byte-identical.

## Completed milestone: Milestone 02 AOI and no-key global terrain

Dependencies: AOI normalization/local crop -> Copernicus AWS catalog/cache -> windowed COG fetch -> provider selection/fallback -> candidate geocoding -> real Gongga/Amazon builds -> provenance and slicer evidence.

- [x] Verify current official sources, access, licenses, attribution, and no-key route.
- [x] Implement bbox and center-radius `AreaOfInterest` normalization.
- [x] Apply explicit AOI pixel-window clipping to local rasters with coverage/NoData records.
- [x] Implement antimeridian/high-latitude/cross-zone projection decisions and tests.
- [x] Implement Nominatim-compatible geocoding with candidate handling and usage policy.
- [x] Implement content-addressed cache, timeout/retry/rate-limit boundaries.
- [x] Implement configurable `copernicus-aws` GLO-30/GLO-90 COG provider.
- [x] Preserve Copernicus quality/editing masks when assets expose them.
- [x] Implement explainable auto-selection and recorded fallback attempts.
- [x] Execute a real Gongga build from fetched DSM data (reference bundle complete; integrated provider automation remains pending).
- [x] Execute a real Amazon low-relief build with DSM semantics visible; v2 provider/cache bundle and diagnostic slice completed 2026-07-31.
- [x] Slice at least one global-provider 3MF and publish milestone-02 evidence (official Bambu Studio P2S bundle).

## Milestone 02 acceptance — passed in 0.3.0

- Local and no-key global commands accept normalized bbox/center AOIs and emit complete bundles.
- Place resolution returns candidates for ambiguity rather than silently selecting.
- Provider provenance contains concrete release, tile URLs, ETag/Last-Modified, SHA-256, license notices, DSM semantics, horizontal resolution, EGM2008 vertical reference, NoData, and fallback history.
- Offline provider tests pass by default; live tests are marked integration.
- Real Gongga and Amazon builds plus at least one actual global-provider slice are preserved as evidence.

## Cancelled or replaced routes

- The initial hand-written Core-only 3MF writer was replaced by official `lib3mf==2.5.0`; deterministic UUID and strict-reader tests cover the production path.
- OrcaSlicer 2.4.2 on this Ubuntu 22.04 host was replaced operationally by OrcaSlicer 2.3.0/PrusaSlicer fallback after verified GLIBC/GLIBCXX incompatibility. The adapter still prefers a working Orca installation.
- The ambiguous `copernicus` provider descriptor was split into the no-key `copernicus-aws` route and authenticated `copernicus-cdse` route.
- Direct loading of Bambu leaf preset JSON was replaced by deterministic inherits/include flattening after official Bambu Studio silently fell back to a generic 200 x 200 mm platform.
- Replacing the interoperable Core model.3mf with a Bambu vendor project was rejected; both 3MF roles are preserved because lib3mf does not reopen the vendor project parts as the Core source package.
