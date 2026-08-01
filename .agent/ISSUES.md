# Actual Issues

## TF-001 — `uv` absent at initial audit

- **Severity:** Medium
- **Status:** Resolved
- **Reproduction:** Initial `uv --version` returned command not found.
- **Expected behavior:** Locked Python environment is reproducible.
- **Actual behavior:** `uv 0.8.24` was installed; `uv sync` now exits 0.
- **Owner:** Primary agent
- **Resolution:** Installed `uv`, generated `uv.lock`, and verified a clean sync.

## TF-002 — No headless slicer at initial audit

- **Severity:** High
- **Status:** Resolved
- **Reproduction:** Initial PATH probe found no OrcaSlicer/PrusaSlicer.
- **Expected behavior:** At least one actual manufacturing slicer runs headlessly.
- **Actual behavior:** PrusaSlicer 2.4.0 and OrcaSlicer 2.3.0 completed real slices; adapters and tests landed.
- **Owner:** slicer-validation agent
- **Resolution:** Installed PrusaSlicer, verified official Orca packages, implemented typed fallback adapters, and sliced the final terrain 3MF.

## TF-003 — Latest OrcaSlicer binary incompatible with host runtime

- **Severity:** Medium
- **Status:** Mitigated
- **Reproduction:** Run official OrcaSlicer 2.4.2 Ubuntu 24.04 AppImage in extraction mode on Ubuntu 22.04.
- **Expected behavior:** CLI help/slice starts.
- **Actual behavior:** Missing `GLIBC_2.38` and `GLIBCXX_3.4.32` symbols.
- **Owner:** slicer-validation agent
- **Resolution:** Checksum and failure recorded; official generic Orca 2.3.0 and installed PrusaSlicer work, and discovery falls back automatically.

## TF-004 — Global provider and geocoding path not implemented

- **Severity:** High
- **Status:** Resolved
- **Reproduction:** Earlier global commands directly instantiated Copernicus AWS and accepted no resolved place candidate.
- **Expected behavior:** bbox/center/place inputs can select, fetch, cache, and build real global data.
- **Actual behavior:** `fetch-dem`/`build-global` default to explainable `--provider auto`; full registry evaluation/fetch history is recorded, cached Nominatim-compatible searches return explicit candidates, ambiguous names require an id, and resolved places enter the normal AOI/build pipeline. Copernicus AWS remains the current production network implementation; additional providers are separate roadmap work.
- **Owner:** Primary agent
- **Resolution:** Completed provider selection/fallback and candidate geocoding in TopoForge 0.3.0 with offline regressions and a real all-cache-hit Amazon auto-selection replay.

## TF-005 — Exhaustive self-intersection backend pending

- **Severity:** Medium
- **Status:** Open
- **Reproduction:** Read any current `validation.json`.
- **Expected behavior:** Robust exhaustive result where a verified backend exists.
- **Actual behavior:** Report is honestly `not_fully_checked`; all other topology and slicer gates pass.
- **Owner:** Primary agent
- **Resolution:** Evaluate a robust backend against representative meshes; retain literal classification until validated.

## TF-006 — Rasterio/NumPy masked-array deprecation warnings

- **Severity:** Low
- **Status:** Open
- **Reproduction:** Run the full suite with Rasterio 1.5.0 and NumPy 2.5.1.
- **Expected behavior:** No upstream deprecation warnings.
- **Actual behavior:** 50 warnings about masked-array shape assignment and related reads; 141 tests pass.
- **Owner:** Primary agent
- **Resolution:** Track upstream compatibility or constrain NumPy after benchmark/compatibility evidence; warnings are not hidden.


## TF-007 — Baseline offset and hard height contract

- **Severity:** High
- **Status:** Resolved
- **Reproduction:** Use sea-level baseline with positive elevations, or build the original 42 mm fit-height fixture.
- **Expected behavior:** The selected datum survives mesh construction and final height stays at or below the configured hard limit.
- **Actual behavior:** The mesh previously renormalized top Z and the canonical extrema reached 43.261032 mm; the corrected mesh preserves absolute manufacturing Z and the rebuilt artifact is exactly 42.0 mm.
- **Owner:** Primary agent
- **Resolution:** Changed the mesh contract to absolute top-surface Z, added hard extrema/build-height enforcement, made height/base/triangle gates required, and added regressions.

## TF-008 — YAML defaults overwrote or ignored CLI intent

- **Severity:** High
- **Status:** Resolved
- **Reproduction:** Run `topoforge build --config CONFIG --size-mm ...` or omit `--output` while YAML defines one.
- **Expected behavior:** YAML values remain unless the corresponding CLI option is explicit; every explicit option overrides YAML.
- **Actual behavior:** The default output replaced YAML while non-path CLI values were ignored.
- **Owner:** Primary agent
- **Resolution:** Added Click parameter-source detection, complete override mapping, explicit-null depth handling, and two CLI runner regressions.

## TF-009 — Explicit local AOI crop not implemented

- **Severity:** High
- **Status:** Resolved
- **Reproduction:** `topoforge build` accepts a full local DEM but has no bbox/polygon window option.
- **Expected behavior:** Phase 1 supports a user AOI crop before reprojection and mesh construction.
- **Actual behavior:** The engine processes the full raster and only crops reprojection-only rotated coverage gaps.
- **Owner:** Primary agent
- **Resolution:** Added validated bbox/center-radius normalization, WGS84 antimeridian geometry, UTM/AEQD selection, explicit source-pixel crop, full/partial/empty/NoData handling, provenance, CLI inputs, and offline tests before provider integration.

## TF-010 — Bambu leaf presets silently produced a generic platform

- **Severity:** High
- **Status:** Resolved
- **Reproduction:** Pass the official P2S leaf machine/process JSON directly to Bambu Studio CLI and inspect the resolved platform/G-code.
- **Expected behavior:** P2S 0.4 resolves to a 256 x 256 x 256 mm machine and P2S firmware/process settings.
- **Actual behavior:** The command returned success but unresolved inheritance/include fragments produced a generic 200 x 200 mm platform.
- **Owner:** P2S validation workstream
- **Resolution:** Added deterministic preset flattening and a release gate that asserts actual platform, machine, process, filament, temperatures, shells, infill, support, and brim values.

## TF-011 — Bambu project 3MF is not the interoperable Core source role

- **Severity:** Medium
- **Status:** Resolved
- **Reproduction:** Open the Bambu-exported project 3MF through the existing lib3mf Core-only inspection path.
- **Expected behavior:** The portable geometry source remains strict-reader compatible.
- **Actual behavior:** Bambu vendor project parts trigger Could not create OPC Part in the Core-only path.
- **Owner:** P2S validation workstream
- **Resolution:** Preserve model.3mf as the interoperable lib3mf source and publish model.bambu-p2s.3mf as a separate embedded-settings project artifact.

## TF-012 — Prusa zero-value settings broke the shared G-code parser

- **Severity:** Medium
- **Status:** Resolved
- **Reproduction:** Run the real PrusaSlicer integration test after adding typed resolved settings; Prusa emits filament_density = 0 and filament_max_volumetric_speed = 0 sentinels.
- **Expected behavior:** Optional unset settings do not invalidate an otherwise successful diagnostic slice.
- **Actual behavior:** Pydantic rejected the zero sentinels for positive optional fields.
- **Owner:** Primary agent
- **Resolution:** Parse non-positive values for positive optional settings as unset and add a focused regression; the real Prusa test and full 80-test suite pass.

## TF-013 — Fixed cell budget obscured terrain resolution loss

- **Severity:** High
- **Status:** Resolved
- **Reproduction:** Build the real DSM with `max_grid_cells: 120000`; the 1008 x 1181 source becomes 341 x 350 at 88.8177 m while dataset provenance appears to describe that as dataset resolution.
- **Resolution:** Added explicit sampling modes and separate source/processed fields. The print-aware real rebuild is 439 x 451 at 68.9589 m, with exact physical spacing, triangle/memory estimates, 5.0415 m peak loss, and 24.5163 m peak shift.

## TF-014 — Raster north edge mapped to y=0

- **Severity:** High
- **Status:** Resolved
- **Reproduction:** Map a four-corner asymmetric north-up DEM directly into the previous heightfield constructor; row 0 appears at y=0 and +Y points south.
- **Resolution:** Flip rows once before mesh construction, record +X East/+Y North/+Z Up, add a preview north arrow, preserve positive winding/volume, and verify all four corners plus peak coordinates in STL/GLB/3MF.

## TF-015 — Published summit elevation differs from source DSM peak

- **Severity:** Medium
- **Status:** Accepted data limitation
- **Evidence:** The source DSM maximum is 7437.5244 m while the retained published nominal context is 7556.0 m, a 118.4756 m difference.
- **Resolution:** Record dataset type `dsm`, vertical datum `EGM2008`, the comparison, and `terrain_adjustment_applied=false`. No synthetic spike, sharpening, or peak-height correction is performed.


## TF-016 — Provider AOI at COG tile edge retained a reprojection-only gap

- **Severity:** High
- **Status:** Resolved
- **Reproduction:** Acquire Amazon bbox `[-60.02, -3.13, -60.00, -3.11]`; the east boundary is exactly the source tile boundary and the first metric acquisition contained one 74-pixel NoData column.
- **Expected behavior:** Reprojection-only cells outside source footprints are distinguished from source NoData and excluded without interpolation or silent AOI expansion.
- **Actual behavior:** The v1 local build correctly stopped at the NoData edge-gap gate.
- **Resolution:** Reproject a separate all-source footprint mask, select the largest all-covered rectangle, update the transform, report the original/selected shapes and pixel window, and preserve genuine source NoData. The v2 real build and slice pass.


## TF-017 — Copernicus ancillary quality masks were not preserved

- **Severity:** Medium
- **Status:** Resolved
- **Reproduction:** Acquire any Copernicus AWS tile before 0.2.0; only the DEM COG and tile catalog enter cache/provenance.
- **Expected behavior:** Preserve every exposed official EDM, FLM, HEM, and WBM source with exact identity and an AOI-aligned bundle role.
- **Resolution:** Cache the exact S3 tile-prefix listing, discover rather than guess ancillary keys, store each source by content SHA-256, require exact source-grid alignment with its DEM, apply nearest-neighbour reprojection and the identical DEM crop, and publish complete roles without remapping values or modifying elevations. Offline present/absent/misaligned/cache-corrupt/GLO-90 tests and a real Amazon build/slice pass.
## TF-018 — Resource limits adapted sampling without a standalone manufacturing preflight

- **Severity:** Medium
- **Status:** Resolved
- **Reproduction:** Request source-preserving sampling above the configured cell/memory budget before 0.3.1; the build adapts but there is no separate printer-fit/resource report or explicit strict rejection mode.
- **Expected behavior:** Users can inspect resolved dimensions, headroom, cells, exact triangles, memory, physical spacing, and vertical adjustment before mesh export and choose deterministic adaptation or rejection.
- **Resolution:** Added typed adapt/strict budgets, exact triangle limits, early build-volume checks, reusable CLI/core preflight, cross-checked bundle evidence, focused tests, and a retained-DEM Gongga rebuild/repeat/slice.
## TF-019 — Phase 5 lacked a stable tile identity and overlap contract

- **Severity:** High
- **Status:** Resolved in 0.4.0
- **Reproduction:** There was no canonical partition for processed sample grids, no fixed row/column origin, and no machine-readable overlap window for later tile workers.
- **Expected behavior:** Identical grid/model/tile-size/overlap inputs produce byte-identical layout JSON, stable IDs, complete non-overlapping core cell coverage, shared seam samples, and clipped outer halos.
- **Resolution:** Added `topoforge.tiling` layout models, deterministic layout digest and tile IDs, north/west mapping, physical bounds, neighbor metadata, overlap windows, strict canonical write/reopen, bundle-backed `topoforge tile-plan`, and unit/CLI regressions.

## TF-020 — Per-tile outputs lacked strict source and assembly binding

- **Severity:** High
- **Status:** Resolved for raster extraction
- **Reproduction:** Before this increment, `tile-plan` produced windows but no per-tile files, root assembly identity, complete source-manifest validation, or published tamper detector.
- **Expected behavior:** A new tile-set directory contains exact DEM/NoData windows, complete per-tile provenance/validation/manifests, a coverage map and assembly manifest, and strict source-window/hash verification with no partial publication.
- **Resolution:** Added `extract_tile_set()`, `verify_tile_set()`, and `topoforge tile-extract`; full source role verification, safe relative paths, canonical JSON, strict GeoTIFF reopen, report remeasurement, exact source-window equality, source model-size binding, staging cleanup, overwrite rejection, tamper tests, and real Gongga repeat-byte evidence all pass. Mesh assembly is resolved by TF-022; connector and print-local completion is resolved by TF-023/TF-024.

## TF-021 — Extracted tile sets lacked published numerical seam evidence

- **Severity:** High
- **Status:** Resolved for raster continuity
- **Reproduction:** Reopen a pre-seam Phase 5 tile set; exact source windows are present, but no report enumerates adjacencies or measures shared-core/overlap elevation, mask, CRS, and transform consistency.
- **Expected behavior:** Every new tile set contains deterministic, checksummed, strictly remeasured evidence for all raster seams, with explicit thresholds and tamper regressions.
- **Resolution:** Added `measure_tile_seams()` and canonical `seam_report.json`, assembly-manifest path/hash binding, strict remeasurement, stable east/south adjacency enumeration, exact core/overlap elevation and mask checks, metric transform alignment, legacy manifest compatibility, failure/tamper tests, and retained Gongga v2 repeat-byte evidence. Connector-free mesh continuity is resolved by TF-022; connector fit and slicing are resolved by TF-023/TF-024.

## TF-022 — Raster seam success did not prove printable tile-solid assembly

- **Severity:** High
- **Status:** Resolved in 0.4.0
- **Reproduction:** Reopen a seam-passing raster tile set before this increment; no per-tile STL/3MF/GLB or evidence proves shared X/Y/Z coordinates, closed geometry, boundary-height equality, complete footprint, or volume preservation.
- **Expected behavior:** A separate immutable mesh set binds exact source scaling and tile identities, strictly reopens every format, measures every mesh boundary and the whole assembly, publishes a coverage image, detects tampering, and repeats byte-for-byte.
- **Resolution:** Added `generate_tile_mesh_set()`, `verify_tile_mesh_set()`, and `topoforge tile-mesh`; global-frame core-only solids; strict STL/3MF/GLB bounds/peak/topology checks; assembly seam, footprint, bounds, and volume validation; deterministic coverage PNG; atomic publication/overwrite rejection; CLI, tamper, asymmetric orientation, and determinism regressions; and retained Gongga primary/repeat evidence. Connector geometry and print-local slicing are resolved by TF-023/TF-024.

## TF-023 — Connector-free tiles lacked mechanical fit and print-local placement

- **Severity:** High
- **Status:** Resolved in 0.4.0
- **Reproduction:** Reopen the Phase 5 global mesh set before 0.4.0; seams pass, but tiles have no polarity/clearance contract and global offsets are unsuitable for individual slicing.
- **Resolution:** Added deterministic printer-derived bottom dovetails, stable ownership/polarity/placement, base-only manifold booleans, global and reversible print-local roles, strict format/top/bed/wall/build-volume/fit/collision validation, connector map labels, tamper regressions, and 37/37 real repeat-byte evidence.

## TF-024 — Per-tile print readiness lacked actual P2S and project-reopen evidence

- **Severity:** High
- **Status:** Resolved in 0.4.0
- **Reproduction:** Global-frame mesh evidence cannot establish that four local tiles fit the P2S bed, slice without floating/empty layers or support, use the intended parameters, or reopen as self-contained Bambu projects.
- **Resolution:** Added source-bound `tile-slice`, exact profile copies/hashes, G-code reparse and aggregate metrics, complete official P2S parameter gates, support/out-of-bed/empty/floating rejection, and a project export/archive/MD5/no-external-profile reopen script. Four real Gongga tiles pass all gates.

## TF-025 — Connector fit lacks physical calibration evidence

- **Severity:** Medium
- **Status:** Deferred by operator; non-blocking
- **Reproduction:** Review the 0.4.0 connector record: geometry, clearance envelopes, P2S slicing, and project reopen pass, but no printed coupon or assembled tile has measured insertion force, play, shrinkage, or dimensional error.
- **Expected behavior:** Material/nozzle/layer-specific connector presets are backed by printed tolerance coupons and recorded measurements.
- **Next action:** When hardware validation resumes, generate deterministic coupons across a bounded clearance matrix, publish a worksheet, obtain measurements, and only then promote physically calibrated presets.

## TF-026 — Multi-command local workflow lacked resumability and strict stage reuse

- **Severity:** High
- **Status:** Resolved; global source front-end completed by TF-027
- **Reproduction:** Run build, tile-plan, tile-extract, tile-mesh, tile-connect, tile-slice, and the project script manually; an interruption has no single status or safe automatic resume contract.
- **Resolution:** Added typed content-addressed workflow stages, canonical request/manifest/status/failure schemas, strict source/config/upstream identity checks, verifier-backed reuse, a single `topoforge run` command, optional software slicing and Bambu project evidence, and end-to-end interruption/recovery/determinism coverage. TF-027 attaches normalized provider acquisition to the same contract.

## TF-027 — Resumable workflow could not start from a global AOI

- **Severity:** High
- **Status:** Resolved
- **Reproduction:** Before this increment, `topoforge run` required `BuildConfig.dem_path`; bbox/center-radius users had to invoke separate acquisition/build commands and could not reuse provider evidence as a strict workflow stage.
- **Expected behavior:** The one-command local workflow accepts a normalized no-key global AOI, preserves provider/cache/source evidence, records acquisition failures, resumes without a second fetch, and rejects changed raster/manifest/mask evidence.
- **Resolution:** Added typed `GlobalAcquisitionConfig`/`GlobalSourceEvidence`, production provider-selection reuse, `WorkflowStage.ACQUIRE`, canonical `acquire.json`, source-stage acquisition-manifest hash binding, global workflow ids, dataset-aware `BuildConfig` enrichment, separate acquisition transport CLI limits, offline real-GeoTIFF tests, failure recovery, and a production Amazon cache-only replay with 7/7 hits and zero network attempts.

## TF-028 — Local workflow lacked a saved configuration and artifact browser

- **Severity:** Medium
- **Status:** Resolved
- **Reproduction:** Before this increment, complete `topoforge run` arguments had to be reconstructed and users manually searched content-addressed stage directories for reports, previews, maps, and manufacturing roles.
- **Expected behavior:** One reviewed local/global launch can be saved, resumed with a short command, summarized with measured results, and browsed without deploying a service.
- **Resolution:** Added strict `WorkflowLaunchConfig` YAML round-trip, `topoforge wizard`, `resume`, and `browse`; automatic `workflow-summary.json` and dependency-free `workflow-report.html`; workspace containment and stage-manifest hash checks; preview/connector-map links; local/global wizard tests; resume/reuse/tamper tests; and a retained Amazon all-stage-reuse UX verification record with seven checksum-verified roles.
