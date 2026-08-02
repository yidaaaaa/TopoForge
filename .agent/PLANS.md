# Authoritative Plan

Updated: 2026-08-02

## Roadmap

- [x] Phase 0 — repository, toolchain, official-source research, architecture, fixtures. Completed 2026-07-31.
- [x] Milestone 01 — full-raster local GeoTIFF/synthetic manufacturing core, deterministic 3MF/provenance, validation, and real slicer evidence. Completed 2026-07-31.
- [x] Phase 1 closure — explicit local AOI bbox/center-radius clipping and GIS edge contracts. Completed 2026-07-31.
- [x] Phase 2 closure — deterministic export/provenance revalidated after Phase 1 AOI closure. Completed 2026-07-31.
- [x] Phase 3 — global/high-resolution providers, AOI/geocoding, cache, selection/fallback. Completed in 0.3.0.
- [x] Phase 4 — build-volume/resource UX, adapt/strict budgets, vertical-scaling preflight, real rebuild, and slice evidence. Completed in 0.3.1.
- [x] Phase 5 — tiling, assembly manifest/map, labels, verified connectors, print-local files, and per-tile slicing. Completed in 0.4.0.
- [x] Phase 6 — local software completion: resumable one-command workflow, local UX, recovery, and core hardening. Completed in 0.5.0 on 2026-08-02.
- [x] Phase 7 — local provenance-aware GPX/road/river/contour/label/coast overlays. Completed in 0.6.0 on 2026-08-02.
- [x] Phase 8 — local release hardening: packaging, CI, benchmarks, reference regions, and offline documentation. Completed in 0.7.0 on 2026-08-02.
- [x] Phase 9 — worker-backed loopback FastAPI and bilingual React/MapLibre/Three.js local Web application. Completed in 0.8.0 on 2026-08-02.
- [x] Phase 9.1 — WebUI map and 3D presentation stabilization. Completed in 0.8.1 on 2026-08-02.

## Completed Phase 9 — bilingual local Web application

1. [x] Define typed job, progress, cancellation, error, and artifact contracts.
2. [x] Execute workflows in isolated recoverable child processes without duplicating core logic.
3. [x] Add a loopback-only FastAPI adapter with constrained input/workspace paths.
4. [x] Add `topoforge web` health checks and one-command local launch.
5. [x] Build a Chinese/English React interface for local DEM and bbox/center-radius AOIs.
6. [x] Add MapLibre AOI interaction and Three.js GLB inspection.
7. [x] Expose sampling, dimensions, tiling, slicing, progress, cancellation, and artifacts.
8. [x] Verify desktop/mobile layout, maps, WebGL canvas, API jobs, packaging, and offline launch.
9. [x] Publish Phase 9 release evidence, checksums, rollback, commit, and tag.

Final evidence: 204 Python tests, 6 Vitest tests, 2 applicable Playwright scenarios, 8 checksum-verified production Web assets, one real HTTP workflow with six ready stages and 22 artifacts, byte-reproducible sdist/wheel archives, and isolated installed `topoforge web --check`. Release verification: `artifacts/verification/topoforge-0.8.0-phase9-release-verification-final.json`.

## Completed Phase 9.1 — WebUI stabilization

1. [x] Replace the single-color offline map with bundled Natural Earth country geometry and a deterministic graticule.
2. [x] Remove duplicate AOI source registration and style-not-loaded browser errors.
3. [x] Permit the explicit OSM tile origin in both image and Fetch CSP directives.
4. [x] Frame Z-up GLB models from their bounding sphere and current canvas aspect ratio.
5. [x] Scale and position the grid and East/North arrows from model bounds.
6. [x] Reject page/console errors and require spatially varied map/3D pixels plus an intercepted OSM request.
7. [x] Reuse the retained HTTP smoke input; verify six stages, 22 artifacts, strict 3MF, and unchanged manufacturing hash.
8. [x] Publish reproducible 0.8.1 archives, isolated installed smoke evidence, screenshots, rollback, commit, and tag.

Final gates: Ruff clean; 160 files formatted; Pyright 0 errors; Pytest 204 passed in 81.24 s; Vitest 9 passed; Playwright 2 applicable passed / 2 project-inapplicable skipped; fixed-epoch sdist/wheel byte-identical; installed 0.8.1 wheel passed doctor, Web asset checks, build, and strict 3MF inspection. Manufacturing model SHA-256 remains `6a237cfd4adfaf0f9eb44ed8c2ebb6b584a8ac2603d47d6c5c35527ba6064ce4`.

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

## Completed Phase 6 — local software completion (0.5.0)

1. [x] Add `topoforge run` for local source identity, build, tiling, connectors, optional slicing, and optional Bambu project evidence. Stages are content-addressed by complete settings/upstream SHA-256 values, strictly reopened before reuse, atomically status-tracked, and resumable after retained failure records.
2. [x] Connect the existing no-key global provider/cache acquisition path to the same source-stage contract. `topoforge run` accepts bbox or center-radius, publishes canonical `acquire.json`, strictly binds provider/manifest/raster/mask evidence, records acquisition failure/recovery, and reuses verified stages without a second fetch.
3. [x] Add `topoforge wizard`, saved `workflow-launch.yaml`, and `topoforge resume` so local DEM/bbox/center-radius, manufacturing, slicing, and project choices are reviewed once and resumed without reconstructing a long command. Every run publishes a concise measured `workflow-summary.json`.
4. [x] Add `topoforge browse` and dependency-free `workflow-report.html` with strictly workspace-bound links to validation, provenance, previews, connector maps, models, slice/project roles, and stage directories. Browser opening is optional and no server is required.
5. [x] Evaluate exhaustive self-intersection backends for TF-005. No candidate passed behavior, licensing, and resource acceptance together; production remains honestly `not_fully_checked` with retained evidence.
6. [x] Resolve TF-006 by constraining NumPy to `<2.5`; NumPy 2.4.6 removes the Rasterio warnings while six core artifact roles remain byte-identical.
7. [x] Add measured disk-space estimates, workflow-id-confirmed cleanup, deterministic checksum-bound backup/restore, and offline end-to-end documentation.
8. [x] Freeze the local software workflow at TopoForge 0.5.0 before beginning overlay work.

Real global-workflow evidence: `artifacts/verification/topoforge-0.4.0-amazon-global-workflow-phase6-cache-replay.json`. The production Copernicus AWS provider replayed the retained Amazon bbox with 7/7 cache hits and a fail-on-network opener that recorded zero network attempts. The retained and replayed 74 x 74 elevation arrays match exactly; all seven stages strictly reopen and the repeat/CLI runs reuse all seven stages with a byte-identical workflow manifest.

Phase 6 closure evidence: `artifacts/verification/topoforge-0.5.0-phase6-local-software-verification.json`. The retained Amazon workflow reused all seven stages without acquisition or terrain rebuild, reported sufficient disk headroom and zero cleanup candidates, produced two byte-identical 3,320,977-byte backups, restored 57/57 stage files byte-identically, and passed strict static browsing. TF-005 and TF-006 evidence are retained beside it. Final suite: 179 passed with no warnings.

## Completed Phase 7 — local provenance-aware overlays (0.6.0)

1. [x] Define strict typed local GPX, GeoJSON, and generated-contour source/config/provenance contracts.
2. [x] Implement road, river, coast, GPX, label, and DEM-derived contour layers without changing terrain elevations.
3. [x] Transform explicit source CRS values into the processed metric CRS, including antimeridian and 85-degree high-latitude AEQD cases.
4. [x] Map overlay points to the exact fixed-diagonal terrain triangles in +X East/+Y North/+Z Up coordinates.
5. [x] Reject original NoData intersections by default and record explicit opt-in overlap.
6. [x] Enforce printer minimum features, feature count, triangle budget, model bounds, watertightness, winding, and positive volume.
7. [x] Publish per-layer STL, plan GeoJSON, colored GLB, north-marked PNG, provenance, validation JSON/HTML, resolved YAML, and checksums.
8. [x] Export seven named mesh resources through one identity-transform components assembly, one top-level build item, and one explicit Core base-material group.
9. [x] Integrate `topoforge overlay`, optional workflow/wizard overlay config, content-addressed reuse, static browsing, storage estimates, and backup/restore of external sources.
10. [x] Prove strict reread, tamper rejection, CLI behavior, source terrain hash preservation, format orientation, and byte determinism.
11. [x] Reuse the retained Amazon build without download/rebuild; generate primary/repeat 0.6.0 bundles and actual official Bambu Studio P2S slicing.
12. [x] Freeze Phase 7 with 186 passing tests, release documentation, verification JSON, SHA256SUMS, commit, and tag.

Real evidence: `outputs/amazon-phase7-overlays-0.6.0-v1` and `artifacts/verification/topoforge-0.6.0-phase7-overlays-verification.json`. Six sources produce 76 features and 14,320 overlay triangles. The combined 3MF has seven named mesh objects, seven components, one components object, one top-level build item, one base-material group, seven material assignments, 36,220 triangles, and zero lib3mf warnings. All 14 manifest artifact roles repeat byte-for-byte and source terrain hashes are unchanged.

Official Bambu Studio `02.07.01.62` exits 0 with 49 layers, 23.74 g, and 1h 3m 3s. Floating, empty-layer, out-of-bed, and support checks are false. The literal official P2S `T65535` AMS unload sentinel and internal `ZFiller` diagnostics are retained and classified against accepted Gongga build/reopen baselines; no logs or presets are hidden or modified.

## Completed Phase 8 — local release hardening (0.7.0)

1. [x] Define bounded Hatch sdist/wheel contents and exclude private agent state, generated caches, historical artifacts, downloads, outputs, and large manufacturing evidence.
2. [x] Publish SPDX `Apache-2.0` metadata and include code, dataset, and third-party notice files in the wheel.
3. [x] Remove the unused API dependency extra while preserving the complete API/Web design as deferred Phase 9.
4. [x] Prove byte-reproducible sdist and wheel builds under a fixed `SOURCE_DATE_EPOCH`.
5. [x] Install the wheel with all dependencies into a fresh Python 3.12 environment outside the checkout and run doctor, synthetic DEM, complete build, and strict 3MF inspection.
6. [x] Add pinned GitHub Actions quality, release, reference-region, and benchmark jobs.
7. [x] Add three deterministic full-build benchmark cases with exact topology, resource ceilings, and two-run core artifact hash equality.
8. [x] Add seven AOI reference definitions and offline reread of four retained real terrain sources without network access or rebuild.
9. [x] Document connected/offline installation, same-platform wheelhouses, upgrade, workflow backup/restore, and non-destructive rollback.
10. [x] Revalidate the retained Phase 7 official slice and 20-file checksum list without rerunning terrain acquisition or slicing.
11. [x] Freeze Phase 8 with 192 passing tests, final verification JSON, checksum manifest, commit, and tag.

Final package evidence: sdist SHA-256 `067f63e50ada269970ca6aace2d64c635e09b5eab76269b6083319e98e00a900`; wheel SHA-256 `ca56d0bb8824fd3a441bab1fc8cff187ff33d69fbdf36918c2be95d4d3d141d1`. Installed smoke used Python 3.12.3, imported only from the temporary venv, built a 40 x 32 x 20 mm model, and strict-read 3MF with zero warnings. Benchmark maximums were 15.692 s and 956.504 MiB for 256 x 320 / 327,676 triangles. Verification: `artifacts/verification/topoforge-0.7.0-phase8-release-verification.json`.

## Deferred non-blocking physical validation

- [ ] Generate connector calibration coupons and a bounded tolerance matrix when physical testing is desired.
- [ ] Print coupons/tiles and record insertion force, play, dimensional error, material, humidity, orientation, and failure mode.
- [ ] Promote material/nozzle/layer connector presets only after measured evidence.

These items are intentionally skipped for now and do not block Phases 6-9. The current `0.2 mm` connector remains software-validated, not physically calibrated.

## Completed Phase 9 — API and Web

The local API and Web plan is complete and visually stabilized in 0.8.1: typed durable jobs, bounded isolated workers, loopback FastAPI routes, strict path and artifact checks, saved configuration loading, Chinese/English React controls, MapLibre AOI interaction, Three.js GLB inspection, desktop/mobile browser evidence, packaged assets, CI, release verification, and rollback. Public deployment, authentication, and a remote multi-user contract remain intentionally outside this local release.

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
