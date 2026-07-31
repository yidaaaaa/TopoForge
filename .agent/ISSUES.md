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
- **Status:** Resolved for layout planning; extraction/assembly remain open roadmap work
- **Reproduction:** There was no canonical partition for processed sample grids, no fixed row/column origin, and no machine-readable overlap window for later tile workers.
- **Expected behavior:** Identical grid/model/tile-size/overlap inputs produce byte-identical layout JSON, stable IDs, complete non-overlapping core cell coverage, shared seam samples, and clipped outer halos.
- **Resolution:** Added `topoforge.tiling` layout models, deterministic layout digest and tile IDs, north/west mapping, physical bounds, neighbor metadata, overlap windows, strict canonical write/reopen, bundle-backed `topoforge tile-plan`, and unit/CLI regressions.
