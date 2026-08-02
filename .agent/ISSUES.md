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

## TF-005 — Exhaustive self-intersection backend evaluation

- **Severity:** Medium
- **Status:** Resolved by evaluated non-adoption in 0.5.0
- **Reproduction:** Read any current `validation.json`.
- **Expected behavior:** Robust exhaustive result where a verified backend exists.
- **Actual behavior:** Report is honestly `not_fully_checked`; all other topology and slicer gates pass.
- **Owner:** Primary agent
- **Resolution:** Evaluated Manifold3D, libigl 2.6.2, MeshLib 3.1.3.297, PyMeshLab/VCGLib 2025.7.post1, and Open3D CPU 0.19.0 against clean, separate, adjacent, overlapping, connected-cross, duplicate, degenerate, Amazon, and Gongga meshes. MeshLib and PyMeshLab passed behavior but imposed non-commercial/GPL distribution constraints and roughly 260-294 MB installs; Open3D produced adjacent-contact false positives and missed the connected-cross fixture; the other candidates lacked the required predicate. Production remains literally `not_fully_checked`; no backend failure is promoted to passed. Evidence: `artifacts/verification/topoforge-0.5.0-phase6-self-intersection-backend-evaluation.json`.

## TF-006 — Rasterio/NumPy masked-array deprecation warnings

- **Severity:** Low
- **Status:** Resolved in 0.5.0
- **Reproduction:** Run the full suite with Rasterio 1.5.0 and NumPy 2.5.1.
- **Expected behavior:** No upstream deprecation warnings.
- **Actual behavior:** NumPy 2.5.1 produced 10 warnings in the focused five-test reproduction and thousands across the full suite.
- **Owner:** Primary agent
- **Resolution:** Constrained NumPy to `>=2.1,<2.5` and locked 2.4.6 with Rasterio 1.5.0. The focused tests now pass with zero warnings; STL, 3MF, GLB, PNG, processed DEM, and NoData-mask SHA-256 values are byte-identical before/after, and build time changed from 2.694 s to 2.645 s. Evidence: `artifacts/verification/topoforge-0.5.0-phase6-numpy-rasterio-compatibility.json`.


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

## TF-029 — Local workflow lacked bounded maintenance and portable recovery

- **Severity:** Medium
- **Status:** Resolved in 0.5.0
- **Reproduction:** Before Phase 6 closure, users could not estimate workflow disk use, identify only unreferenced stage identities, back up external local source/config files, or strictly restore a completed workspace.
- **Expected behavior:** Maintenance is measurable, review-first, workspace-contained, checksum-bound, and usable without an API or server.
- **Resolution:** Added `topoforge storage`, `cleanup`, `backup`, and `restore`; configured-ceiling/completed-measurement estimates; exact workflow-id deletion confirmation; deterministic safe-path ZIPs with per-file SHA-256; external source/config inclusion; atomic restore and launch remapping; tamper/determinism/offline-resume tests; and `docs/offline-workflow.md`. The real Amazon backup repeats byte-for-byte and restores 57/57 stage files exactly.

## TF-030 — Multi-object overlay 3MF sliced as floating independent parts

- **Severity:** High
- **Status:** Resolved in 0.6.0
- **Reproduction:** Slice the first Phase 7 smoke 3MF in Bambu Studio; terrain and six overlay meshes are seven top-level build items.
- **Expected behavior:** Named overlay resources keep exact relative placement and slice as one manufacturing assembly.
- **Resolution:** Added one stable-UUID components object, seven stable component instances, one assembly build item, one Core base-material group, seven material assignments, strict XML/lib3mf count checks, and regression coverage. The final official slice reports no floating region.

## TF-031 — Overlay format gate did not enforce the new assembly shape

- **Severity:** High
- **Status:** Resolved in 0.6.0
- **Reproduction:** After changing to one build item, the old report still expected one build item per mesh, while `required_checks_passed` did not include `format_reopen_checks_passed`.
- **Expected behavior:** A report cannot pass when its own strict format contract fails.
- **Resolution:** Added typed build-item/components/material counts, updated strict bundle reopen, made the complete format result mandatory, added provenance assembly detail, and expanded integration assertions.

## TF-032 — Bambu P2S logs label the official AMS unload sentinel invalid

- **Severity:** Low
- **Status:** Accepted upstream diagnostic with retained evidence
- **Reproduction:** Slice the final Phase 7 3MF or retained accepted Gongga build/project through Bambu Studio 02.07.01.62.
- **Expected behavior:** Manufacturing failures and official preset sentinels are distinguishable without altering the preset.
- **Actual behavior:** Bambu logs `Invalid T command (T65535)`; the literal command is present in official `machine_end_gcode` as the AMS unload sequence. `ZFiller` internal diagnostics also appear in accepted single-terrain baselines.
- **Resolution:** The release verifier binds the preset, G-code, raw logs, accepted baseline logs, result JSON, and all required slice gates. Diagnostics remain visible; no filtering or preset modification is applied.
## TF-033 — Default sdist leaked private and generated repository state

- **Severity:** High
- **Status:** Resolved in 0.7.0
- **Reproduction:** Run `uv build --no-sources` before Phase 8 and list the sdist; it contains `.agent`, 166 Hypothesis cache files, 92 historical artifact files, and other non-release state.
- **Expected behavior:** Source releases contain an intentional source/test/documentation set and no private agent state, caches, downloads, outputs, or large evidence.
- **Resolution:** Added a Hatch sdist allowlist, cache exclusions, archive path/link validation, required content checks, double-build equality, tests, and CI release verification. Final sdist contains 167 bounded members and zero forbidden members.

## TF-034 — A repository build did not prove an installable standalone CLI

- **Severity:** High
- **Status:** Resolved in 0.7.0
- **Reproduction:** Build the pre-Phase 8 wheel and run only `uv run topoforge`; imports can still resolve through the editable checkout and package metadata/licence contents are not independently checked.
- **Expected behavior:** A fresh environment installs the wheel and dependencies, imports outside the checkout, runs the console entry point, builds a real artifact bundle, and strict-reads 3MF.
- **Resolution:** Added `scripts/verify_release.py`. The final test installs 51 packages in a fresh Python 3.12 venv, reports no repository import leakage, completes doctor/synthetic/build/inspect with exit 0, and reports zero strict 3MF warnings.

## TF-035 — A TopoForge wheel alone is not a complete offline install set

- **Severity:** Medium
- **Status:** Accepted platform packaging constraint; documented
- **Reproduction:** Install only `topoforge-0.7.0-py3-none-any.whl` with `uv pip install --offline` in an empty environment; lib3mf and other platform wheels are unavailable.
- **Expected behavior:** Offline instructions distinguish the project wheel from a complete same-platform dependency wheelhouse.
- **Resolution:** `docs/release.md` provides connected wheelhouse creation, SHA-256 verification, `--no-index --find-links` installation, platform matching, upgrade, and rollback. The verified platform is Linux x86_64 / CPython 3.12; other targets require separate release evidence.

## TF-036 — Local use lacked a complete bilingual graphical workflow surface

- **Severity:** Medium
- **Status:** Resolved in 0.8.0
- **Reproduction:** Before Phase 9, use `topoforge wizard`, `run`, and `browse`; configuration and reports work locally, but there is no live AOI map, persistent job queue, cancellation UI, GLB canvas, or unified artifact panel.
- **Expected behavior:** A one-command, loopback-only Chinese/English application validates the same workflow contract, runs isolated recoverable jobs, exposes measured progress/errors/artifacts, and does not duplicate or weaken the manufacturing core.
- **Resolution:** Added typed durable Web contracts, isolated subprocess workers, constrained FastAPI routes, strict configuration/static/artifact checks, React controls, MapLibre AOI interaction, Three.js orientation-aware preview, responsive desktop/mobile layouts, browser pixel/overlap tests, real HTTP completion/download evidence, packaged assets, CI, reproducible archives, installed smoke, and rollback.

## TF-037 — Offline map was blank and online tiles were blocked

- **Severity:** High
- **Status:** Resolved in 0.8.1
- **Reproduction:** Open 0.8.0 with the default basemap or enable OpenStreetMap. Offline mode renders only `#dce5e2`; online tile Fetch requests violate `connect-src 'self'`; initialization may report `Source "aoi" already exists`.
- **Expected behavior:** Offline use has recognizable geographic context, optional OSM requests are consistent with CSP, and MapLibre initializes/switches styles without page errors.
- **Resolution:** Added bundled Natural Earth countries, borders, and a graticule; made AOI a style-owned source with one `style.load` restore path; allowed the explicit OSM origin in CSP; and added browser error, palette, and intercepted-request assertions.

## TF-038 — Three.js camera cropped valid Z-up GLB models

- **Severity:** High
- **Status:** Resolved in 0.8.1
- **Reproduction:** Open the retained 40 x 32 x 20 mm HTTP smoke GLB in the 0.8.0 narrow center pane. The fixed extent-offset camera crops most of the solid and fixed 18-unit guides hide the direction arrows.
- **Expected behavior:** Reopened manufacturing bounds fit the current canvas while +X East, +Y North, +Z Up remain unchanged and visible.
- **Resolution:** Added bounding-sphere/FOV/aspect camera framing with unit coverage tests, responsive reframing, bound-derived grid/arrow size and placement, deterministic tone mapping, final real-GLB screenshots, and browser error/palette checks. The strict 3MF hash remains byte-identical to 0.8.0.

## TF-039 — Completed jobs lacked local processed-terrain maps and assembly interaction

- **Severity:** High
- **Status:** Resolved in 0.9.0
- **Reproduction:** Open 0.8.1 after a tiled workflow completes. The map shows reference geography and AOI but not the processed DEM, manufacturing footprints, connector layout, or per-tile assembly.
- **Expected behavior:** Offline local use displays checksum-bound terrain/elevation/hillshade tiles, synchronized manufacturing footprints, a North-marked 2D layout, and an inspectable per-tile 3D assembly without changing terrain data.
- **Resolution:** Added deterministic local XYZ generation/cache, map manifest/routes, footprint selection, 2D/3D assembly, bilingual controls, real 2 x 2 HTTP evidence, strict format rereads, screenshots, and browser tests.

## TF-040 — Visualization trusted a mutable assembly root outside the workflow SHA chain

- **Severity:** High
- **Status:** Resolved in 0.9.0 before release
- **Reproduction:** Canonically edit `print-tile-assembly-manifest.json` after job completion, changing `east_axis` or a tile bound without changing the workflow manifest. The pre-fix `/assembly` and `/map/manifest` returned 200 and `required_checks_passed=true`.
- **Expected behavior:** Visualization rejects any assembly metadata or role that is no longer bound to the published completed workflow evidence.
- **Resolution:** Bind JobRecord `workflow_manifest` SHA to the canonical workflow, require the validated CONNECT stage, require exact output/manifest paths and stage manifest SHA, verify assembly validation and tile-manifest hashes, use the measured validation gate, and retain the tamper regression. Both map and assembly now return structured 422 errors on the reproduced mutation.

## TF-041 — Date-line/polar map coverage and exploded assembly framing had untested edge failures

- **Severity:** High
- **Status:** Resolved in 0.9.0 before release
- **Reproduction:** A date-line raster centered near -179.95 degrees produced center longitude 0.05 and near-global Mercator bounds; an 86-degree raster produced south greater than north; a loaded assembly retained its old camera after resize/explosion/reset.
- **Expected behavior:** Date-line coverage remains local, Web Mercator latitude limits are explicit, and every visible exploded tile remains inside the 3D frame after responsive changes and reset.
- **Resolution:** Add circular longitude centers, two Mercator coverage segments, explicit partial-clipping metadata, full-outside rejection, unwrapped raster source bounds, deterministic visible/exploded assembly bounds, resize/state/reset reframing, a four-column narrow-aspect corner projection test, and real Playwright maximum-explosion/resize/reset checks.

## TF-042 — GitHub publication credentials were unavailable on this host

- **Severity:** Release operations
- **Status:** Resolved before Phase 11
- **Reproduction:** The HTTPS dry-run for main and v0.9.0 exited 128 because no username/token helper was available.
- **Expected behavior:** Publish the verified main commit and annotated tag without embedding credentials.
- **Resolution:** Changed origin to git@github.com:yidaaaaa/TopoForge.git, confirmed SSH authentication as yidaaaaa, and pushed main plus v0.9.0. The public repository and tag now resolve through both Git and the GitHub API.

## TF-043 — Public v0.9.0 CI used the wrong release version and no Release page existed

- **Severity:** High release operations
- **Status:** Resolved in 0.10.0 and verified remotely
- **Reproduction:** GitHub Actions run 30749563043 checks out v0.9.0 source but invokes verify_release.py with --version 0.8.0, so the release job fails. The public Releases API returns an empty list after the tag push.
- **Expected behavior:** CI verifies the checked-out package version, and tagged versions publish verified downloadable assets.
- **Resolution:** CI now uses uv version --short. The generic Release workflow builds and verifies the target tag, bootstraps the newest non-current tag from main, publishes four checksum-bound assets, and skips existing Release pages. Runs 30752031951, 30752306723, and 30752306731 succeed; both v0.9.0 and v0.10.0 Release assets pass downloaded checksum verification.

## TF-044 — Completed Web jobs lacked project backup, cleanup, and restore controls

- **Severity:** Medium local usability
- **Status:** Resolved in 0.10.0
- **Reproduction:** Open a completed 0.9.0 job. The WebUI shows manufacturing artifacts but no storage estimate, cleanup candidates, backup archive, or restore action even though the CLI maintenance core already exists.
- **Expected behavior:** A local operator can manage completed projects in Chinese or English without losing checksum/path/atomicity guarantees.
- **Resolution:** Added typed maintenance routes and UI over the existing workflow core, explicit cleanup confirmation, strict backup download identity, atomic restored-job registration, 24 focused backend/release tests, 18 Vitest tests, real Playwright lifecycle coverage, and retained HTTP evidence.

## TF-045 — Concurrent main/tag Release runs raced and the bootstrap selector used system Python 3.10

- **Severity:** High release operations
- **Status:** Resolved on main after v0.10.0 publication
- **Reproduction:** Push main and v0.10.0 close together. Per-ref concurrency allows both jobs to observe no Release; main can publish the current tag while the tag job fails on an existing Release. The first bootstrap correction then fails before setup-python because Ubuntu 22.04 system Python lacks tomllib.
- **Expected behavior:** Main publishes only the prior missing tag, tag publication is serialized, and version parsing uses the declared Python 3.12 toolchain.
- **Resolution:** Use one release-publication concurrency group, skip the current project tag during main bootstrap selection, stop after the newest non-current tag, and run setup-python before tomllib. Final bootstrap run 30752306731 succeeds and publishes v0.9.0; final main CI 30752306723 succeeds. The earlier failed runs remain visible as the reproduced evidence and have no unresolved effect.


## TF-046 — Auto-aspect floating drift created an unnecessary tile row

- **Severity:** Medium
- **Status:** Resolved on main after 0.10.0
- **Reproduction:** Build the retained 439 x 439 Great Trango GLO-30 DEM at 180 mm width with automatic depth and a 180 mm maximum tile depth. The measured model depth is 180.00015258789062 mm, so exact `ceil()` planning produced a 2 x 1 layout and five connectors for a 0.0001526 mm overflow.
- **Expected behavior:** Numerical drift below the manufacturing geometry tolerance does not create an extra tile, while dimensions that materially exceed the limit still partition.
- **Resolution:** Added a 0.001 mm tile-boundary tolerance, integer-multiple and over-tolerance regressions, a planner-v2 cache identity, and strict legacy-layout reopen. The same real source now produces 1 x 1, one tile, zero connectors, unchanged terrain/model metrics, and exit code 0.

## TF-047 — Persistent Playwright backup state made restore selection ambiguous

- **Severity:** Low
- **Status:** Resolved on main after 0.10.0
- **Reproduction:** Run the Phase 11 Playwright lifecycle scenario repeatedly without deleting `/tmp/topoforge-playwright-state`; multiple backup rows share the same workflow ID and the global restore-button selector becomes ambiguous or selects an older restore target.
- **Expected behavior:** Browser acceptance remains valid with retained historical jobs and backups, matching the supported local lifecycle UI.
- **Resolution:** Capture the exact backup POST response, assert its backup ID is listed, and scope restore to that checksum-bound backup row. The retained-state Playwright rerun reports 2 passed and 2 project-inapplicable skipped.

## TF-048 — Correct terrain geometry looked inverted or ambiguous in the Web 3D viewer

- **Severity:** High local usability
- **Status:** Resolved on main after 0.10.0
- **Reproduction:** Open the retained 180 x 180.0001526 x 45 mm Great Trango GLB. The previous low southeast-oblique view, south-biased lighting, default gray material, and tiny direction arrows make the central valleys and surrounding ridges look like a distorted basin even though strict format checks and peak mapping pass.
- **Expected behavior:** The whole solid is immediately legible, North and East are spatially clear, relief does not exhibit lighting-induced inversion, reset restores a known view, and browser tests wait for the actual GLB rather than the placeholder.
- **Resolution:** Added an elevated due-south frame, northwest key light, deterministic display-only elevation colors, world-space N/E labels, a reset icon, loaded-model state, orientation/color unit tests, and stronger Playwright assertions. The real GLB loads in 3.093 s with palette 98 and zero console errors; STL, 3MF, and GLB hashes remain unchanged.

## TF-049 — High-DPI canvases overflowed the 3D viewport on first open

- **Severity:** High local usability
- **Status:** Resolved on main after 0.10.0
- **Reproduction:** Open the corrected model viewer on a desktop using approximately 150% display scaling. A 665 x 656 center container receives a 997 x 984 canvas because Three.js pixel ratio affects the drawing buffer while setSize(..., false) leaves CSS size unset. The model appears enlarged, shifted toward the lower-right, and clipped even though clicking reset may partially mask the problem.
- **Expected behavior:** Canvas CSS dimensions match the visible container at every device pixel ratio, the high-resolution buffer remains proportional to DPR, the first real GLB frame is centered without manual input, and the assembly view follows the same contract.
- **Resolution:** Enable CSS size updates in both Three.js renderers, settle initial framing across animation frames and a 250 ms layout window, run desktop Playwright at 1.5 DPR, assert client/container/buffer ratios, and require normalized colorful terrain center within 0.35 to 0.65 on both axes. The retained model finishes at center (0.4895, 0.4593) with zero browser errors and unchanged STL/3MF/GLB hashes.

## TF-050 — Expanding slicing controls scrolled the desktop application out of view

- **Severity:** High local usability
- **Status:** Resolved locally after 0.10.0
- **Reproduction:** At a 1365 x 758 CSS viewport and 1.5 device pixel ratio, scroll the left control panel to `Run software slicing` and enable it. The added slicer selector increases root document height, the browser changes window scrollY from 0 to 185 while panel scrollTop remains 287, the header moves to top -185, and a blank white region appears below the fixed-height application.
- **Expected behavior:** Dynamic controls expand only inside the independently scrollable configuration panel; the desktop header and center workspace remain fixed in the viewport, while the mobile layout retains natural document scrolling.
- **Resolution:** Fix the desktop body to the viewport and clip the root at widths above 940 px, retain the existing mobile overrides, and add a 1365 x 758 / 1.5 DPR Playwright regression that expands slicing, verifies the Bambu Studio selector, requires window scrollY/header/app top to remain 0, and preserves the panel scroll position. The live 8772 instance passes the same measurement.

## TF-051 — WebUI did not expose the existing vertical scaling policies

- **Severity:** Medium local usability
- **Status:** Resolved locally after 0.10.0
- **Reproduction:** Open the local Web configuration. Maximum model height is available, but there is no control for the core vertical_scale_mode or vertical_exaggeration fields that are already supported by BuildConfig and the CLI.
- **Expected behavior:** A local user can choose automatic perceptual scaling, natural scale, fit-to-height, or a custom exaggeration without bypassing the core maximum-height and validation contracts.
- **Resolution:** Added typed Chinese/English mode selection, a conditional custom coefficient input, default/custom request serialization tests, language-switch tests, dynamic-layout Playwright coverage, a checksum-bound production build, and a live 8772 measurement at custom 2.5 with scrollY/header/app top all 0.

## TF-052 — Web Bambu jobs could not resolve the retained Studio executable or P2S profiles

- **Severity:** High local manufacturing workflow
- **Status:** Resolved locally after 0.10.0
- **Reproduction:** Start the loopback Web service without a Bambu-specific runtime option, select `Run software slicing` and `Generate Bambu project evidence`, then submit. The isolated worker reaches the slice stage and fails with `Bambu Studio probe did not resolve an executable path` even though the AppImage exists under `downloads/tools/bambu-studio`; after resolving only the binary, the request still lacks the three flattened official P2S profiles required by the production gate.
- **Expected behavior:** The Web service either rejects the request before queueing with exact corrective startup options or injects one fully validated executable/profile configuration into the saved launch and worker. Direct API submission must not bypass this gate, and successful configuration must produce real official slice and project/reopen evidence.
- **Resolution:** Added four typed `topoforge web` options and startup file checks, all-or-none profile validation, pre-queue slicer probing and profile injection for validate/submit, worker `TOPOFORGE_BAMBU_STUDIO` propagation, API/check reporting, and focused regressions. The corrected real job `3778ca822657498eb3a4acca6ce89e97` completed all nine stages with exit code 0; official slice reports 598 layers with no empty/floating/out-of-bed/support result, and the Bambu project passes export, archive, embedded-G-code, no-external-profile reopen, and reslice gates.

## TF-053 - Cancelled and failed Web jobs could not be removed

- **Severity:** Medium local usability and storage management
- **Status:** Resolved locally after 0.10.0
- **Reproduction:** Cancel or fail a Web job. The durable record remains in the right task list indefinitely, and the only trash-labelled action for completed projects cleans unreferenced stages rather than removing the task or project.
- **Expected behavior:** Terminal jobs have explicit record-only and project-file removal actions, while active jobs, shared workspaces, unsafe paths, and backups remain protected.
- **Resolution:** Added typed DELETE models/route/manager behavior, exact confirmation, terminal-state and path gates, shared-workspace protection, measured deletion results, bilingual controls, stale-request cancellation, manager/API/Vitest/Playwright coverage, checksum-bound Web assets, and a live 8772 restart. Playwright deletes only a test-created restored copy and proves the job is gone while its backup remains downloadable.
