# Authoritative Plan

Updated: 2026-08-01

## Roadmap

- [x] Phase 0 — repository, toolchain, official-source research, architecture, fixtures. Completed 2026-07-31.
- [x] Milestone 01 — full-raster local GeoTIFF/synthetic manufacturing core, deterministic 3MF/provenance, validation, and real slicer evidence. Completed 2026-07-31.
- [x] Phase 1 closure — explicit local AOI bbox/center-radius clipping and GIS edge contracts. Completed 2026-07-31.
- [x] Phase 2 closure — deterministic export/provenance revalidated after Phase 1 AOI closure. Completed 2026-07-31.
- [x] Phase 3 — global/high-resolution providers, AOI/geocoding, cache, selection/fallback. Completed in 0.3.0.
- [x] Phase 4 — build-volume/resource UX, adapt/strict budgets, vertical-scaling preflight, real rebuild, and slice evidence. Completed in 0.3.1.
- [x] Phase 5 — tiling, assembly manifest/map, labels, verified connectors, print-local files, and per-tile slicing. Completed in 0.4.0.
- [ ] Phase 6 — local software completion: resumable one-command workflow, local UX, recovery, and core hardening.
- [ ] Phase 7 — local provenance-aware GPX/road/river/contour/label/coast overlays.
- [ ] Phase 8 — local release hardening: packaging, CI, benchmarks, reference regions, and offline documentation.
- [ ] Phase 9 (deferred) — worker-backed FastAPI and React/MapLibre/Three.js Web application, only after local-use completion.

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

## Completed Phase 4 — manufacturing resource preflight (0.3.1)

- [x] Add explicit `max_estimated_triangles` and `resource_budget_mode: adapt | strict`.
- [x] Make cells, exact triangles, and estimated memory participate in one deterministic effective sampling budget.
- [x] Make `adapt` record the limiting setting and deterministic downsampling; make `strict` reject with requested cells/triangles/memory and corrective action.
- [x] Reject impossible explicit printer dimensions and non-positive terrain height budgets before raster work.
- [x] Add reusable `topoforge preflight` with printer fit/headroom, resource utilization, physical spacing, vertical-policy adjustment, warnings, and suggested actions.
- [x] Publish `manufacturing_preflight.json`, embed the identical object in validation/provenance, and require/cross-check it during bundle reopen.
- [x] Reuse the retained Gongga DEM without a download; create new `resource-v3` and repeat bundles.
- [x] Verify seven deterministic core roles byte-identical, strict 3MF warnings zero, STL/GLB geometry consistent, and PrusaSlicer exit 0 with no floating/empty/out-of-bed/support warning.
- [x] Final gates: Ruff, format, Pyright, 141 tests, and diff whitespace all pass.

## Completed Phase 5 — deterministic manufacturing tiling (0.4.0)

1. [x] Define deterministic layout IDs, north/west row/column mapping, and overlap windows.
2. [x] Extract exact per-tile DEM/NoData windows with source-bound manifests.
3. [x] Publish and remeasure numerical raster seams.
4. [x] Generate shared global-frame tile STL/3MF/GLB and verify mesh assembly.
5. [x] Derive stable west/north-male and east/south-female connector identities/polarity.
6. [x] Derive clearance, wall, roof, neck/head/depth/height, edge, and spacing thresholds from the printer profile.
7. [x] Generate base-only dovetails without changing the terrain top; verify presence, cavities, collision, topology, bed contact, thin walls, and printer fit.
8. [x] Publish reversible print-local STL/3MF/GLB and connector labels/map.
9. [x] Actually slice every Gongga tile with PrusaSlicer diagnostics and official Bambu Studio P2S parameter gates.
10. [x] Export separate Bambu project 3MF roles and perform no-external-profile reopen/reslice.
11. [x] Prove connector/print-local determinism (37/37), strict checksums, tamper detection, and atomic cleanup.
12. [x] Freeze the tile/assembly/connector/slice contracts before Phase 6.

Real evidence: eight connectors over four seams, `0.2 mm` total lateral / `0.2 mm` vertical clearance, `0.0 mm` terrain-top deviation, and `0.0 mm3` collision. Official Bambu Studio `02.07.01.62` sliced all four print-local tiles with exit 0, complete P2S parameter checks, maximum 224 layers, `224.53 g`, and no out-of-bed/empty/floating/support result. Four project 3MF files passed archive/MD5 and no-external-profile reopen/reslice. Verification: `artifacts/verification/topoforge-0.4.0-gongga-phase5-verification.json`.

## Current Phase 6 — local software completion

1. [ ] Add one resumable local command for acquisition/build/tiling/connectors/slicing/project evidence, reusing completed stages by manifest SHA-256.
2. [ ] Add a local configuration wizard and concise run summary so ordinary solo use does not require assembling long CLI commands.
3. [ ] Improve local artifact browsing: open reports, previews, connector maps, and output directories without a server. A lightweight desktop/TUI shell may call the Python core directly.
4. [ ] Evaluate and validate an exhaustive self-intersection backend for TF-005.
5. [ ] Resolve or constrain the Rasterio/NumPy compatibility warning tracked by TF-006 using benchmark and regression evidence.
6. [ ] Add local failure recovery, disk-space estimates, cleanup commands, backup/export, and offline end-to-end documentation.
7. [ ] Freeze the local software workflow before beginning overlay work.

## Deferred non-blocking physical validation

- [ ] Generate connector calibration coupons and a bounded tolerance matrix when physical testing is desired.
- [ ] Print coupons/tiles and record insertion force, play, dimensional error, material, humidity, orientation, and failure mode.
- [ ] Promote material/nozzle/layer connector presets only after measured evidence.

These items are intentionally skipped for now and do not block Phases 6-8 or the later saved API/Web phase. The current `0.2 mm` connector remains software-validated, not physically calibrated.

## Saved deferred Phase 9 — API and Web

The API plan is retained, not cancelled. After Phases 6-8 complete local use, define typed job/progress/error/artifact/cache/provenance schemas; implement bounded isolated workers and FastAPI submission/status/cancellation/download endpoints; stabilize those contracts; then build React/MapLibre/Three.js. Until then no API service, database, queue, authentication, or Web deployment work is required for single-user local operation.

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
