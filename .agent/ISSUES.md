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
- **Status:** Open
- **Reproduction:** `topoforge providers` shows global entries as `implemented: false`; `topoforge build` currently requires `--dem` or a local config.
- **Expected behavior:** bbox/center/place inputs can select, fetch, cache, and build real global data.
- **Actual behavior:** Official-source research and provider contract exist; fetch/selection/cache/AOI code is pending Phase 3.
- **Owner:** Primary agent
- **Resolution:** Planned Copernicus AWS no-key provider followed by USGS/OpenTopography options.

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
- **Actual behavior:** 33 warnings about masked-array shape assignment; 71 tests pass.
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
- **Status:** Open
- **Reproduction:** `topoforge build` accepts a full local DEM but has no bbox/polygon window option.
- **Expected behavior:** Phase 1 supports a user AOI crop before reprojection and mesh construction.
- **Actual behavior:** The engine processes the full raster and only crops reprojection-only rotated coverage gaps.
- **Owner:** Primary agent
- **Resolution:** Milestone 02 begins with AOI normalization and local bbox/polygon clipping before global-provider integration.
