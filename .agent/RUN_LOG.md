# Run Log

## 2026-07-31

### Repository audit

Command: `find`, `git status`, `git log`, Python/tool version probes.

Result: workspace contained an empty `TopoForge/` directory; it was not a Git repository. Python 3.12.3, Git 2.34.1, NumPy 2.3.2, Trimesh 4.12.2, Pillow 11.3.0, SciPy 1.18.0, and Matplotlib 3.10.5 were present. `uv`, Rasterio, PyProj, Pydantic, Typer, Pytest, Hypothesis, FastAPI, OrcaSlicer, and PrusaSlicer were not detected.

### Baseline

Command: `git init -b main && git commit --allow-empty -m 'chore: capture empty repository baseline'`

Result: exit 0; baseline commit `10112b0a827bd27db6054d3ecf01a47d62b4aed5`.

### Environment and dependency lock

Command: `python3 -m pip install 'uv>=0.8,<0.9' && uv sync`

Result: exit 0; uv 0.8.24, Python 3.12 environment created. Final lock resolves 71 packages and audits 62 installed packages. `lib3mf==2.5.0` is pinned.

### Core implementation tests

Commands included focused unit, geometry, GIS, property, reproducibility, 3MF, and slicer suites while implementing.

Results: rotated-raster regression initially exposed 21.88% reprojection-only corner NoData; fixed with a separately reprojected coverage mask and largest-covered-rectangle crop. Initial 3MF CLI inspection exposed slots-dataclass `__dict__` misuse; fixed with `dataclasses.asdict`. Both have regression coverage.

### Final quality gates

Command: `uv sync`

Result: exit 0; resolved 71 packages, audited 62.

Command: `uv run ruff check .`

Result: exit 0; `All checks passed!`

Command: `uv run ruff format --check .`

Result: exit 0; `75 files already formatted`.

Command: `uv run pyright`

Result: exit 0; `0 errors, 0 warnings, 0 informations`.

Command: `uv run pytest`

Result: exit 0; `64 passed, 31 warnings in 5.37s`. Warnings are the tracked Rasterio/NumPy deprecation issue TF-006.

### Final synthetic build

Command: `uv run topoforge build --dem examples/synthetic/gaussian-hill.tif --size-mm 180 0 --base-mm 3 --max-height-mm 42 --vertical-scale fit-height --printer-profile bambu-p2s-0.4 --dataset-type dtm ... --output outputs/milestone-01-synthetic`

Result: exit 0; 180 x 144 x 43.261032 mm, 20,476 triangles, watertight/manifold/winding consistent, one component, 0 degenerate/duplicate faces, 0.0 mm bottom error, 3.0 mm minimum base.

### Strict 3MF and actual slice

Command: `uv run topoforge inspect outputs/milestone-01-synthetic/model.3mf`

Result: exit 0; millimetres, one object/build item, 10,240 vertices, 20,476 triangles, dimensions 180 x 144 x 43.261032 mm, lib3mf 2.5.0, zero strict warnings.

Command: `uv run topoforge slice outputs/milestone-01-synthetic/model.3mf --output artifacts/slicer/milestone-01-final.gcode`

Result: exit 0; PrusaSlicer 2.4.0, 5,094,910-byte G-code, 144 layers, estimated 7h 10m 51s, 41,017.85 mm / 98.66 cm3 filament, no support/out-of-bed/empty-layer/floating warning.


### Independent QA correction and final Milestone 01 gate

Reproduction: sea-level/custom baseline samples were mapped correctly by the scaling layer but then renormalized by the mesh constructor; the original robust fit-height fixture also reached 43.261032 mm against a 42 mm command limit.

Resolution: changed the mesh contract to preserve absolute manufacturing Z, added a hard extrema height gate, required height/base/triangle checks, verified manifest SHA-256 on reopen, completed YAML/CLI override semantics, filled local provenance fields, and added regressions.

Command: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`

Result: exit 0; Ruff passed, 78 files formatted, Pyright reported 0 errors/0 warnings, and Pytest reported `71 passed, 33 warnings in 5.42s`.

Command: corrected 42 mm synthetic build, strict 3MF inspect, `topoforge preview`, and `topoforge slice`.

Result: exit 0; 180 x 144 x 42.0 mm, 20,476 triangles, hard height/base/triangle gates true, watertight/manifold/winding true, strict lib3mf warnings 0. PrusaSlicer 2.4.0 produced 5,030,602 bytes, 140 layers, estimated 7h 11m 41s, and 40,552.39 mm / 97.54 cm3 filament with no support/out-of-bed/empty-layer/floating warning.

Command: `topoforge COMMAND --help` for build/synthetic/inspect/validate/slice/preview/providers/cache/doctor.

Result: all nine command help invocations exited 0.


### Milestone 01 patch replay and rollback

Source commit: `c8a1ef38711d76111d8c8922db709e6ef2fe958a`.

Command: generate `artifacts/patches/milestone-01.patch` from baseline to source commit, then run `git apply --check`, `git apply --whitespace=error-all`, and `git diff --check` in `/tmp/topoforge-milestone01-apply.U3a2mO` at the empty baseline.

Result: exit 0; patch SHA-256 `a7e9be54c80ab7d9d9b6feb139f63af7b41c44ceca7db5a1fac649b126ba32ff`, 633,418 bytes, no whitespace warnings. Replay suite: 71 passed, 33 warnings. Replay build: 180 x 144 x 42.0 mm, strict lib3mf warnings 0, PrusaSlicer exit 0, 5,030,602-byte G-code, 140 layers, and checksum-reopened 12-role bundle.

Command: run `scripts/rollback-milestone-01.sh --confirm-rollback` in `/tmp/topoforge-milestone01-rollback.tk9j5e` checked out at the source commit.

Result: exit 0; HEAD `10112b0a827bd27db6054d3ecf01a47d62b4aed5`, working tree clean.

### Default P2S and official Bambu Studio release hardening

Command: resolve official Bambu Lab P2S 0.4 machine, 0.20mm Standard process, and Bambu PLA Basic presets by recursively flattening inherits/include fragments.

Result: official Bambu Studio leaf presets alone were shown to fall back to a generic 200 x 200 mm platform. The flattened profiles resolve 113 machine, 197 process, and 140 filament keys and hard-check a 256 x 256 x 256 mm P2S platform.

Command: official Bambu Studio 02.07.01.62 normative export/slice of outputs/gongga-copernicus-glo30/model.3mf, followed by reopening outputs/gongga-copernicus-glo30-bambu-p2s/model.bambu-p2s.3mf without external profiles and slicing again.

Result: both invocations exited 0 with return_code 0, error_string Success, and empty warning_message. The Bambu project SHA-256 is 898610a7b3094ed51d5ff9bb8e1f5701eee93ecd666c1e1d9e0cf61556f8d27d; the independent G-code SHA-256 is 27b43eb59965ae93c0f0867ca3c63b38d19fc13f97a5aeba3486366b8a19654d. Reopened model dimensions are 180 x 175.359 x 45 mm with 477,396 triangles, 24,550.9707 s estimate, and 211.008 g filament. Embedded settings and G-code MD5 checks pass.

Command: uv run ruff format . && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest

Result: 85 files unchanged by formatting, Ruff passed, Pyright reported 0 errors/0 warnings, and Pytest reported 80 passed with 33 tracked upstream warnings.

Regression: the first full run reported 78 passed and one failure because PrusaSlicer 2.4.0 writes zero sentinels for optional filament density and maximum volumetric speed.

Resolution: non-positive values for positive optional G-code settings now parse as unset. Focused real-Prusa/parser tests report 5 passed; the full suite reports 80 passed.

### P2S source patch replay and rollback

Command: create a detached worktree at HEAD a9f5f5d; run git apply --check, git apply --whitespace=error-all, git diff --check, Ruff check/format, Pyright with the project Python, and the full Pytest suite against the patched worktree.

Result: forward check/apply exited 0; Ruff passed; Pyright reported 0 errors/0 warnings; Pytest reported 80 passed and 33 tracked warnings.

Command: bash scripts/rollback-p2s-bambu-validation.sh --confirm-rollback --patch /root/autodl-tmp/bambu/TopoForge/artifacts/patches/p2s-bambu-validation.patch

Result: exit 0; reverse check/apply passed, HEAD remained a9f5f5d, and git status --porcelain was empty.

### Printer-aware sampling, orientation, AOI, and real fidelity rebuild

Command: source DEM SHA-256 and local processing probe using `print-aware`, `max_grid_cells=600000`, `max_estimated_memory_mb=768`.

Result: source SHA-256 `00664a26192dea531606e60978f902bccbd3d93499c10c2ba89f9d37f4d7bbbc`; source 1008 x 1181 at 28.8508136 m; processed 439 x 451 at 68.9588821 m; 0.4001816 mm physical spacing; 791,952 triangles; 60.4214 MiB estimate; 5.0415039 m peak loss; 24.5162760 m peak shift; fidelity thresholds passed.

Command: `uv run topoforge build --config downloads/gongga-copernicus-glo30/build-config.fidelity-v2.yaml`.

Result: exit 0; new output `/root/autodl-tmp/bambu/TopoForge/outputs/gongga-copernicus-glo30-fidelity-v2`; 180 x 175.359116 x 45 mm; watertight/manifold/winding/positive volume/flat bottom true; +X East/+Y North/+Z Up; strict 3MF warning count 0.

Command: strict `topoforge preview/inspect` for bundle, STL, GLB, 3MF plus JSON/YAML/GeoTIFF/PNG reload.

Result: exit 0; STL/GLB 791,952 triangles and 639,175.1232 mm3; 3MF 395,978 vertices, 791,952 triangles, correct orientation metadata, peak 115.6 x 91.282832 x 45 mm, zero strict warnings.

Command: `uv run topoforge slice outputs/gongga-copernicus-glo30-fidelity-v2/model.3mf --slicer prusa --output artifacts/slicer/gongga-fidelity-v2-prusa.gcode`.

Result: exit 0; PrusaSlicer 2.4.0; 30,325,645-byte G-code; 149 layers; 15h14m17s estimate; 90,237.44 mm / 217.05 cm3 filament; support false; no floating region, empty layer, or out-of-bed warning. This is diagnostic evidence; the official P2S release gate is not claimed.

Command: repeated real build to `outputs/gongga-copernicus-glo30-fidelity-v2-repeat` and SHA-256 comparison.

Result: processed DEM, NoData mask, STL, 3MF, GLB, and PNG were byte-identical. Record: `artifacts/verification/gongga-fidelity-v2-determinism.txt`.

Command: full quality gates.

Result before fixture correction: Ruff and Pyright passed; Pytest 93 passed/5 failed because the legacy scaling fixture did not populate new required `RasterResult` evidence fields. The fixture was completed without changing assertions; focused scaling tests then reported 5 passed. Final post-documentation full gate follows.

Command: final `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest` after fixture, CLI, documentation, and state updates.

Result: exit 0; Ruff `All checks passed!`; format `92 files already formatted`; Pyright `0 errors, 0 warnings, 0 informations`; Pytest `100 passed, 40 warnings in 6.41s`. Warnings are the tracked Rasterio/NumPy deprecation issue.


### Fidelity/AOI patch replay and rollback

Command: construct an isolated copy of the pre-fidelity/P2S-hardened source tree, run `git apply --check`, apply `artifacts/patches/fidelity-aoi.patch` with `--whitespace=error-all`, compare the resulting regular-file tree with the captured post-change tree, execute `scripts/rollback-fidelity-aoi.sh --confirm-rollback`, and compare the reversed regular-file tree with the captured baseline.

Result: forward check/apply exited 0, the applied file tree matched the post-change tree, the rollback script exited 0, and the reversed file tree matched the baseline. Empty untracked directory shells are excluded because Git patches do not represent directories. The final patch hash, byte count, commands, and literal statuses are stored in `artifacts/verification/fidelity-aoi-patch-rollback-summary.txt`.


### Content-addressed cache, bounded HTTP, and Copernicus AWS provider

Command: focused offline cache/transport/provider tests covering canonical request identity, content-SHA objects, atomic reopen, same-content corruption recovery, timeout propagation, bounded retries/backoff, minimum request spacing, download limits, authoritative catalog parsing, GLO-30/GLO-90 whole-product fallback, antimeridian cells, source-footprint edge crop, and provider acquisition through the full local build engine.

Result: all focused tests passed; Pyright reported 0 errors/0 warnings. `topoforge providers` now reports `copernicus-aws` implemented, `topoforge cache status` emits object/request/temp counts, and `fetch-dem`/`build-global` accept bbox or center-radius AOIs.

Command: live `build-global` for Amazon bbox `[-60.02, -3.13, -60.00, -3.11]` using public Copernicus AWS GLO-30, 30 s timeout, four attempts, 0.25 s request interval, 250,000-cell/256 MiB build budgets.

Result v1: catalog and 44,747,048-byte COG downloaded and verified; the local NoData gate stopped on a 74-pixel east-edge reprojection gap. Source, manifest, cache, and failure log were retained. No zero fill or terrain invention was applied.

Result v2 after coverage fix: catalog and COG were verified cache hits. The independent source-footprint mask selected 74 x 74 from the original 74 x 75 grid and documented 74 discarded reprojection-only cells. The source and processed grids remain 74 x 74 at 29.7638 m with zero peak loss/shift. The 80 x 80.6587 x 9.7146 mm / 21,900-triangle bundle is watertight, manifold, winding-consistent, positive-volume, flat-bottom, and strict-3MF clean.

Command: `uv run topoforge slice outputs/amazon-copernicus-aws-v2/model.3mf --slicer prusa --output artifacts/slicer/amazon-copernicus-aws-v2-prusa.gcode`.

Result: exit 0; 2,085,508-byte G-code, 32 layers, 2h10m52s estimate, 9,085.44 mm / 21.85 cm3 filament, support false, and no floating/empty/out-of-bed warning. This is diagnostic evidence.

Command: final `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest`, Amazon bundle preview, strict 3MF inspect, and cache status after code/documentation updates.

Result: exit 0; Ruff `All checks passed!`; format `98 files already formatted`; Pyright `0 errors, 0 warnings, 0 informations`; Pytest `113 passed, 45 warnings in 7.34s`; Amazon bundle required checks true; strict lib3mf warnings 0; cache 2 request entries/2 content objects/45,857,948 bytes/0 temporary files.


### Copernicus EDM/FLM/HEM/WBM preservation and 0.2.0

Command: query the public GLO-30 and GLO-90 S3 ListObjectsV2 prefixes for the already cached Amazon tile and inspect its official XML metadata.

Result: both products expose EDM (editing), FLM (ancillary filling), HEM (per-pixel height-error standard deviation), and WBM (modified water) GeoTIFFs under `AUXFILES`. Implementation now caches the exact prefix listing and listed mask objects instead of assuming filenames.

Command: focused `uv run pytest -q tests/providers/test_copernicus_aws.py`, Ruff, and Pyright while adding present, absent, source-grid mismatch, cache hit/corruption recovery, GLO-30/GLO-90, and provider-to-build bundle tests.

Result: 11 provider tests passed with 5 tracked warnings; Ruff passed after formatting; Pyright reported 0 errors/0 warnings. Present masks require exact DEM source CRS/transform/shape, use nearest-neighbour reprojection and the same DEM target crop, retain raw values, and become `source_quality_{edm,flm,hem,wbm}` build-manifest roles.

Command: live `fetch-dem` for Amazon bbox `[-60.02, -3.13, -60.00, -3.11]` into a new evidence directory using the existing provider cache.

Result: tile catalog and 44,747,048-byte DEM were verified cache hits; the new tile-prefix listing and four official ancillary sources were fetched once. Cache became 9 request entries/9 content objects/152,952,754 bytes/0 temporary files. All four AOI masks reopen on the exact 74 x 74 DEM grid.

Command: bump `pyproject.toml`/`uv.lock` to 0.2.0, update the HTTP User-Agent, rebuild `outputs/amazon-copernicus-aws-quality-v2` from cached source evidence, strict-read the bundle, and run `uv run topoforge slice ... --slicer prusa`.

Result: `topoforge doctor` reports 0.2.0. The 18-role post-slice bundle passes all required geometry checks, strict lib3mf warnings are 0, and PrusaSlicer exits 0 with 32 layers, 2,085,508-byte G-code, support false, and no floating/empty/out-of-bed warning. EDM/FLM/HEM/WBM are separate checksummed artifacts. DEM/STL/3MF/GLB/PNG remain byte-identical to the pre-mask Amazon v2 bundle. Verification: `artifacts/verification/amazon-copernicus-aws-quality-v2-verification.json`.

Command: final `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest`, and `git diff --check` after version, documentation, quality-mask integration, and real verification updates.

Result: exit 0; Ruff `All checks passed!`; format `99 files already formatted`; Pyright `0 errors, 0 warnings, 0 informations`; Pytest `118 passed, 45 warnings in 7.75s`; diff whitespace check passed. Log: `artifacts/logs/topoforge-0.2.0-quality-gates.log`.

Command: `git commit -m "feat: release global AOI terrain pipeline v0.2.0"`.

Result: commit `d70e62497d97fdeafefad75f821874fc0637ff80` created with 97 audited files; version is 0.2.0.

Command: from a no-hardlink clone of the final evidence HEAD, run `scripts/rollback-topoforge-0.2.0.sh --confirm-rollback`. The script requires a clean worktree and resets tracked source to baseline `a9f5f5da77ba231f23128fe76e21c6f93890b7ef`.

Result: exit 0; rollback HEAD and tree exactly matched the baseline (`89de6c49d88002a169b80c2bb82c5e3555a0c2d7`) and the isolated worktree was clean. Record: `artifacts/verification/topoforge-0.2.0-git-rollback.txt`.

Command: generate `artifacts/patches/topoforge-0.2.0-source.patch` from the release baseline while excluding agent state, binary previews, and prior verification/patch archives; in the baseline clone run forward check/apply, `git diff --check`, reverse check/apply.

Result: all five statuses were 0 and the clone was clean after reverse. Exact final patch SHA-256/bytes are stored in `artifacts/verification/topoforge-0.2.0-source-patch-verification.txt`.


### Explainable provider selection, candidate geocoding, and 0.3.0

Command: implement `ProviderSelectionPolicy`, registry evaluation/ranking, fetch attempt aggregation, retained-destination evidence stop, acquisition-manifest trace writing, exact build-provenance passthrough, and `--provider auto`/explicit CLI integration.

Result: focused selection/Copernicus/CLI tests passed (`28 passed`). Exact DTM/DSM semantics outrank nominal resolution; fallback is opt-in; incomplete coverage, missing credentials, unimplemented providers, licence/datum/download constraints, deterministic ties, successful fallback, all-failed aggregation, and retained evidence are covered.

Command: implement cached Nominatim-compatible JSONv2 search, explicit candidate selection, resolved-place AOI normalization, `topoforge geocode`, and global `--place/--place-candidate-id` integration.

Result: public endpoint policy enforces a one-second interval, canonical cached requests, no autocomplete, bounded candidates, and OSM attribution. Zero/unique/ambiguous states are distinct; ambiguity returns candidates and blocks acquisition until an id is supplied. Offline geocoding/AOI/CLI tests passed.

Command: full pre-release quality gate: `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest`, and `git diff --check`.

Result: exit 0; Ruff passed; format `104 files already formatted`; Pyright `0 errors, 0 warnings, 0 informations`; Pytest `135 passed, 45 warnings in 8.14s`; diff whitespace check passed. Log: `artifacts/logs/topoforge-provider-selection-geocoding-pre-release.log`.

Command: replay Amazon bbox `[-60.02, -3.13, -60.00, -3.11]` through `fetch-dem --provider auto --terrain-mode dsm` into a new evidence directory using the existing cache.

Result: exit 0; all catalog/DEM/listing/EDM/FLM/HEM/WBM assets were cache hits; no source was redownloaded; `copernicus-aws` was selected and every planned provider rejection was recorded. The 74 x 74 raster SHA-256 is `d9b0b6b827f7e26565cdd0d835f4afb83eb08352a2ccf6ed56030240149c6029`. Verification: `artifacts/verification/amazon-provider-selection-v1.json`.

Command: bump `pyproject.toml`/`uv.lock` and HTTP User-Agent to 0.3.0 with `uv lock --offline`; run `topoforge doctor`.

Result: package rebuilt locally, doctor reports TopoForge `0.3.0`, Python 3.12.3, GDAL 3.12.1, PROJ 9.7.1, and PrusaSlicer available.


Command: final 0.3.0 gate after docs/version updates.

Result: exit 0; Ruff `All checks passed!`; format `104 files already formatted`; Pyright `0 errors, 0 warnings, 0 informations`; Pytest `135 passed, 45 warnings in 7.90s`; `git diff --check` passed. Log: `artifacts/logs/topoforge-0.3.0-quality-gates.log`.

Command: `git commit -m "feat: add provider selection and candidate geocoding v0.3.0"` and create tag `v0.3.0`.

Result: release implementation commit `b44b518f852cbfb6569c2024567121a633e72635` created.

Command: in no-hardlink isolated clones, execute `scripts/rollback-topoforge-0.3.0.sh --confirm-rollback` and forward/check/apply/reverse-check/reverse `artifacts/patches/topoforge-0.3.0-source.patch`.

Result: rollback exit 0 and tree exactly matched baseline `434eb7e663d251ad6fcef70ab8d7daf8afe6856a`; patch verification statuses were all 0 and the clone was clean after reverse. Patch SHA-256 `b4914d0f69900e9bb75b2ab4f29f8ee19a98fc8dd25803566edfa07cb9fd3de6`, 105,051 bytes.

### Manufacturing resource preflight and 0.3.1 real evidence

Command: implement `ResourceBudgetMode`, exact triangle ceilings, deterministic adapt/strict sampling, early printer-dimension validation, reusable `preflight_local_terrain`, `topoforge preflight`, a typed `manufacturing_preflight.json` bundle role, and cross-report verification.

Result: focused phase-4 suites pass. The report includes printer fit/headroom, source/processed grids, cells, exact triangles, memory, physical spacing, requested/resolved vertical exaggeration, hard pass booleans, warnings, decisions, and suggested actions.

Command: reuse `downloads/gongga-copernicus-glo30/gongga-copernicus-glo30-crop.tif` to build `outputs/gongga-copernicus-glo30-resource-v3` and `-repeat` with `--max-estimated-triangles 900000 --resource-budget-mode adapt`.

Result: both builds exited 0 without a source download. Source SHA-256 is `00664a26192dea531606e60978f902bccbd3d93499c10c2ba89f9d37f4d7bbbc`. The output is 439 x 451 at 68.958882 m, 791,952 triangles, estimated 60.421448 MiB, and 180 x 175.359116 x 45 mm. DEM, NoData mask, preflight JSON, STL, 3MF, GLB, and PNG match byte-for-byte across repeats.

Command: bundle preview, STL validation, strict 3MF inspect, independent GLB reopen, and `uv run topoforge slice outputs/gongga-copernicus-glo30-resource-v3/model.3mf --slicer prusa --output artifacts/slicer/gongga-resource-v3-prusa.gcode`.

Result: reopened STL/GLB are watertight, winding-consistent, positive-volume, and 791,952 triangles; strict lib3mf warning count is 0. PrusaSlicer 2.4.0 exited 0 with 149 layers, 30,325,346 bytes, 15h14m13s estimate, support false, and no floating/empty/out-of-bed warning. G-code SHA-256 is `34387b06170e718340368f3ea2bd22ee4c26839257f2c5fb56085032f281ea3e`.

Command: first full 0.3.1 quality gate.

Result: Pyright, 141 tests, and diff whitespace passed, but Ruff found three new line-length errors in explanatory strings. The strings were split without changing their literal values.

Command: corrected final `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest`, and `git diff --check`.

Result: exit 0; Ruff `All checks passed!`; format `106 files already formatted`; Pyright `0 errors, 0 warnings, 0 informations`; Pytest `141 passed, 50 warnings in 9.28s`; diff whitespace passed. Final log: `artifacts/logs/topoforge-0.3.1-quality-gates.log`.

### 0.3.1 implementation commit, source patch, and rollback

Command: `git commit -m "feat: add manufacturing resource preflight v0.3.1"`.

Result: implementation commit `c9755601f121ec9d30ea76c01db53afaa016771d` from baseline `1284a1754e00976572150447853d910dcdd12b12`.

Command: generate `artifacts/patches/topoforge-0.3.1-source.patch`; in an isolated no-hardlink clone run forward check/apply, `git diff --check`, reverse check/apply; run `scripts/rollback-topoforge-0.3.1.sh --confirm-rollback` in a separate release clone.

Result: all patch statuses are 0 and the patch clone is clean after reverse. Patch SHA-256 `2c1e4b2c092c0c0700df34662d8245b9eb77328ff2144186bb4a7bfc6d6e1949`, 59,219 bytes. Rollback exits 0 at baseline `1284a1754e00976572150447853d910dcdd12b12`, exact tree `1b776f03ef87b61377c33c3edba3817fc952eb47`, clean worktree.

Command: inspect the first evidence commit with `git show --check`.

Result: Git treated standard unified-diff context markers inside the committed `.patch` artifact as trailing whitespace. Added `.gitattributes` with `artifacts/patches/*.patch -whitespace`; source files and the verified patch bytes are unchanged.

Command: amend the final evidence commit with scoped `.patch` whitespace attributes and create annotated tag `v0.3.1`.

Result: `git show --check` is clean, the working tree is clean, and `v0.3.1` points to the final rollback/evidence commit.

### Phase 5 deterministic tile layout foundation

Command: implement `topoforge-tile-layout-v1` with stable layout digests, fixed-width `tile-r####-c####` IDs, row 0 north/column 0 west mapping, deterministic cell partitioning, physical +X East/+Y North bounds, seam-sharing core samples, clipped overlap halos, canonical JSON, strict reopen, and bundle-backed `topoforge tile-plan`.

Result: focused layout and CLI tests report 9 passed. A 24 x 30 synthetic bundle plans a deterministic two-column layout, writes a canonical JSON artifact, and strictly reopens it. The core cell windows cover every source cell exactly once; adjacent tiles share seam samples and interior overlap windows expand by the configured halo.

Command: full gate after Phase 5 layout work: `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest`, and `git diff --check`.

Result: Ruff passed; format `111 files already formatted`; Pyright `0 errors, 0 warnings, 0 informations`; Pytest `150 passed, 55 warnings in 10.43s`; diff whitespace passed. Warnings remain the tracked Rasterio/NumPy masked-array deprecations and non-exhaustive self-intersection classification.

### Phase 5 per-tile extraction, assembly manifest, and real Gongga evidence

Command: implement `topoforge-tile-artifact-v1`, `topoforge-assembly-manifest-v1`, `topoforge-tile-coverage-v1`, `extract_tile_set()`, reusable `verify_tile_set()`, and `topoforge tile-extract`.

Result: extraction verifies every source build-manifest checksum, writes exact `sampling_window` DEM/NoData GeoTIFFs with preserved CRS/window transforms, retains raw-source/processed/source-manifest hashes and dataset/orientation evidence, publishes canonical per-tile provenance/validation/manifests plus coverage/assembly JSON, rejects overwrite/path traversal, and removes staging output on failure. Verification recomputes measurements and compares tile arrays to exact source windows.

Command: focused `./.venv/bin/pytest -q tests/cli/test_tiling_cli.py tests/integration/test_tile_extraction.py` plus tamper regressions.

Result: 7 focused tests pass (1 CLI and 6 extraction). They cover exact windows/masks/CRS, source and processed hashes, assembly/coverage roles, byte-identical repeats, overwrite rejection, layout mismatch cleanup, any source-role checksum corruption, published tile tampering, and layout/model-size mismatch rejection.

Command: full `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest`, and `git diff --check` with output captured in `artifacts/logs/phase5-tile-extraction-final-quality-gates.log`.

Result: exit 0; Ruff `All checks passed!`; format `113 files already formatted`; Pyright `0 errors, 0 warnings, 0 informations`; Pytest `156 passed, 221 warnings in 16.44s`; diff whitespace passed. Warnings remain the visible TF-006 Rasterio/NumPy deprecations.

Command: reuse `outputs/gongga-copernicus-glo30-resource-v3` without acquisition, run `tile-plan --max-tile-size-mm 100 100 --overlap-cells 1`, then run `tile-extract` into `outputs/gongga-copernicus-glo30-resource-v3-tiles-100mm-v1` and a separate `-repeat` directory.

Result: all commands exit 0. Layout `layout-694b1e78d24ba9f5920e` contains a 2 x 2 north/west grid. Each tile sampling grid is 221 x 227 and each core sample grid is 220 x 226. Both final directories strictly reopen against the source bundle; all 23 relative files are byte-identical. The retained source DEM was rehashed in place as `00664a26192dea531606e60978f902bccbd3d93499c10c2ba89f9d37f4d7bbbc`; no download occurred.

Command: generate and run `sha256sum -c artifacts/verification/topoforge-0.3.1-gongga-tiles-100mm-v1-checksums.sha256`.

Result: exit 0; retained source, external layout, verification/determinism records, root manifests/maps, and all 20 per-tile roles report `OK`. Verification: `artifacts/verification/topoforge-0.3.1-gongga-tiles-100mm-v1.json`.

### Phase 5 deterministic numerical tile seams

Command: implement `topoforge-tile-seam-report-v1`, integrate it into `extract_tile_set()` and `verify_tile_set()`, bind its path/SHA-256 from `assembly_manifest.json`, expose the path and status through `topoforge tile-extract`, and add elevation/mask/transform/legacy/determinism regressions.

Result: focused CLI/extraction coverage reports 10 passed. Every east and south adjacency is measured once over the shared core line and complete overlap rectangle. Default elevation tolerance is 0.0 m; CRS, original NoData mask, and metric transform alignment are required. New tile sets strictly remeasure the canonical report, while legacy assembly manifests without both seam fields reopen as `not-reported`.

Command: final post-documentation quality gate `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest`, `git diff --check`, and Gongga v2 `sha256sum -c`.

Result: exit 0; Ruff `All checks passed!`; format `114 files already formatted`; Pyright `0 errors, 0 warnings, 0 informations`; Pytest `159 passed, 464 warnings in 19.52s`; diff whitespace and every Gongga v2 checksum passed. Warnings remain visible under TF-006. Log: `artifacts/logs/phase5-tile-seams-final-quality-gates.log`.

Command: reuse the retained Gongga `resource-v3` bundle and existing layout to extract `outputs/gongga-copernicus-glo30-resource-v3-tiles-100mm-v2` and `-repeat`, strictly reopen both, compare every relative file, and run `sha256sum -c artifacts/verification/topoforge-0.3.1-gongga-tile-seams-100mm-v2-checksums.sha256`.

Result: no source download occurred. Layout `layout-694b1e78d24ba9f5920e` has four seams, 892 shared-core samples, and 2,688 overlap samples. Core/overlap maximum elevation differences, elevation mismatches, mask mismatches, and maximum transform alignment error are all zero; all CRS match and status is `passed`. Primary/repeat roles are 24/24 byte-identical, and every checksum reports `OK`. Verification: `artifacts/verification/topoforge-0.3.1-gongga-tile-seams-100mm-v2.json`.

### Phase 5 global-frame tile meshes and assembly

Command: implement `topoforge-tile-mesh-artifact-v1`, `topoforge-tile-mesh-assembly-v1`, `topoforge-tile-mesh-assembly-validation-v1`, `generate_tile_mesh_set()`, `verify_tile_mesh_set()`, 3MF global bounds inspection, deterministic coverage rendering, and `topoforge tile-mesh`.

Result: geometry uses only core samples, the exact source `ScalingResult`, one north-row flip, and translation into shared +X East/+Y North/+Z Up bounds. Every tile publishes STL, strict 3MF, GLB, validation, and manifest roles. Root verification reopens every role, compares mesh boundary samples, global bounds, footprint partition, summed volume versus source STL, and coverage PNG. Focused 3MF/integration/CLI tests report 5 passed.

Command: reuse `outputs/gongga-copernicus-glo30-resource-v3-tiles-100mm-v2` and the retained `resource-v3` bundle to generate `outputs/gongga-copernicus-glo30-resource-v3-tile-meshes-100mm-v1` and `-repeat`.

Result: both commands exit 0 without acquisition. Four tiles each contain 198,876 triangles. Four mesh seams have 0.0 mm planar error, 0.0 mm maximum Z gap, and zero mismatches. Footprint overlap is 0.0 mm2; global bounds match; tile volume sum is 639,175.1224575599 mm3 versus source 639,175.1232061993 mm3, a 0.0007486393442377448 mm3 difference within a 6.391751232061993 mm3 tolerance. The 1200 x 1258 coverage image was visually checked; 24/24 primary/repeat files are byte-identical.

Command: final `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest`, `git diff --check`, and `sha256sum -c artifacts/verification/topoforge-0.3.1-gongga-tile-meshes-100mm-v1-checksums.sha256`.

Result: exit 0; Ruff `All checks passed!`; format `116 files already formatted`; Pyright `0 errors, 0 warnings, 0 informations`; Pytest `161 passed, 774 warnings in 22.57s`; diff whitespace and every retained source/tile/mesh/evidence checksum passed. Warnings remain visible under TF-006. Log: `artifacts/logs/phase5-tile-meshes-final-quality-gates.log`.

### Phase 5 connectors, print-local placement, and per-tile release evidence

Command: implement `topoforge-connector-plan-v1`, connector-bearing global/print-local tile artifacts, `generate_print_tile_set()`, `verify_print_tile_set()`, deterministic connector map/assembly preview, and `topoforge tile-connect`.

Result: focused Phase 5 gate reports 25 passed with Ruff/format/Pyright clean. West/north male and east/south female bottom dovetails derive clearance/walls/roof/features from the saved printer profile. Tests cover polarity, terrain-top preservation, bed contact, topology, printer fit, real fit/collision booleans, reversible transforms, strict formats, tamper, failure cleanup, and byte determinism.

Command: reuse the retained Gongga `resource-v3` mesh/tile evidence to generate `outputs/gongga-copernicus-glo30-resource-v3-tile-print-100mm-v1` and `-repeat`; strictly reopen both and visually inspect `connector-map.png`.

Result: no acquisition occurred; source SHA-256 remains `00664a26192dea531606e60978f902bccbd3d93499c10c2ba89f9d37f4d7bbbc`. Eight connectors span four seams. Total lateral clearance is 0.2 mm (0.1 mm/side), vertical clearance 0.2 mm, minimum wall 0.8 mm, remaining roof 1.2 mm. Maximum terrain-top deviation and collision are 0.0; all bed/wall/build-volume/global-bound checks pass; 37/37 primary/repeat files are byte-identical.

Command: implement source-bound `slice_print_tile_set()`, `verify_tile_slice_set()`, and `topoforge tile-slice`; run PrusaSlicer diagnostic and official Bambu Studio 02.07.01.62 on all four print-local 3MF files with retained complete P2S profiles.

Result: Prusa diagnostic exits 0 for all four. Official v2 evidence has four exit codes 0, 62,032,141 total G-code bytes, 26,261 estimated seconds, 224.53 g, maximum 224 layers, no out-of-bed/empty/floating/support result, and every P2S machine/process/material parameter check passes.

Command: run `scripts/verify_phase5_bambu_tile_projects.py` to export four separate Bambu project 3MF roles, verify ZIP and embedded G-code MD5, then reopen/reslice each without external profiles.

Result: all four project exports and all four reopen runs return 0. Embedded G-code matches primary bytes, dimensions and triangle counts match strict source 3MF, and all eight official parameter gates pass.

Command: generate `artifacts/verification/topoforge-0.4.0-gongga-phase5-verification.json`, three complete checksum lists, and connector determinism record; run all three `sha256sum -c` commands.

Result: 37 connector/print-local, 12 official-slice, and 44 Bambu-project files all report `OK`. Phase 5 evidence is complete; outputs remain ignored and tracked summaries contain no large model/G-code bytes.

Command: final 0.4.0 gate: `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest`, `git diff --check`, and all three Phase 5 `sha256sum -c` lists; output captured in `artifacts/logs/topoforge-0.4.0-phase5-final-quality-gates.log`.

Result: exit 0; Ruff `All checks passed!`; format `121 files already formatted`; Pyright `0 errors, 0 warnings, 0 informations`; Pytest `166 passed, 1552 warnings in 36.35s`; Git whitespace passed; all 93 retained connector/slice/project entries report `OK`. Warnings remain visible under TF-006.

Command: commit Phase 5 implementation as `d8342ddc069bf070981c8484e24ddf509d08eec0`; generate `artifacts/patches/topoforge-0.4.0-source.patch` from baseline `d82ce3af6c69fab109733be9532170bb3d925d6f`; verify forward/reverse patch application and `scripts/rollback-topoforge-0.4.0.sh --confirm-rollback` in independent no-hardlink clones.

Result: patch SHA-256 `16513093a5cd635a3ae3c09f46b5e988b3f3f3ad85989fbe7b3ed7c81aa723bc`, 243,854 bytes. Forward check/apply/diff check, reverse check/apply-clean, and isolated rollback all pass; record: `artifacts/verification/topoforge-0.4.0-patch-rollback.txt`.

### Local-first roadmap and deferred API

Decision: retain API/Web as deferred Phase 9 because current operation is single-user/local. Phase 6 now covers physical connector calibration, resumable one-command orchestration, local configuration/report UX, self-intersection and dependency-warning hardening, recovery, cleanup, backup, and offline documentation. Overlay and local release phases precede API/Web.

Result: `.agent/PLANS.md`, `STATE.md`, ADR-025, TF-025, AGENTS.md, README, and architecture/tiling documentation consistently preserve the API plan while making connector coupons and physical measurements the next exact work.

### Physical validation deferred

Decision: skip all tasks that require a physical printer or manual measurements for now. Keep TF-025 and coupon/preset work as saved non-blocking tasks; retain the 0.2 mm connector wording as software-validated only.

Result: Phase 6 now starts with resumable one-command local orchestration, followed by local configuration/artifact UX, core hardening, recovery, cleanup, backup, and offline documentation. API/Web remains deferred separately.

### Phase 6 resumable local workflow increment

Command: implement `topoforge.workflow` and `topoforge run` over the existing build/layout/extract/mesh/connect/slice core functions; add content-addressed stage identities, canonical request/manifest/status/failure records, strict reuse, and optional project evidence.

Result: the end-to-end synthetic test executes six stages, reruns them as six strict reuses, records a synthetic slicer failure with all upstream stages ready, resumes only the slice stage, and then reuses all seven ready stages with byte-identical canonical workflow manifest. Project evidence is rejected without slicing or with a diagnostic slicer.

Command: migrate `scripts/verify_phase5_bambu_tile_projects.py` logic into `topoforge.validation.bambu_projects`, keep the script as a compatibility wrapper, then run its `--verify-only` path against the retained four-tile Gongga evidence.

Result: status `verified`; tile count 4; all projects reopened true; all release gates passed true; required checks passed true. No DEM download, slicing, project regeneration, or physical print occurred.

Command: final increment gate `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest`, and `git diff --check`.

Result: Ruff `All checks passed!`; format `125 files already formatted`; Pyright `0 errors, 0 warnings, 0 informations`; Pytest `168 passed, 2115 warnings in 43.37s`; whitespace check passed. The visible warnings remain the TF-006 Rasterio/NumPy compatibility work, not hidden or suppressed.

### Phase 6 global acquisition workflow increment

Command: implement `GlobalAcquisitionConfig`/`GlobalSourceEvidence`, strict `acquire_global_source()`/`verify_global_source()`, `WorkflowStage.ACQUIRE`, canonical `acquire.json`, acquisition-manifest SHA-256 binding in the shared source stage, provider-derived `BuildConfig` enrichment, global workflow ids, and `topoforge run` bbox/center-radius/provider/cache/transport options with slicing timeout kept separate.

Result: local DEM behavior remains on the existing source/build chain. Global requests identity normalized AOI and provider policy while excluding operational cache/retry location, strictly reopen projected single-band raster/NoData/provider/dataset/selection/mask evidence, reject raster/manifest/mask tampering, retain acquisition failure status, resume without a second provider fetch, and produce byte-identical repeat workflow manifests. New offline tests use real metric GeoTIFF and aligned quality-mask files.

Command: `uv run pytest tests/providers/test_selection.py tests/providers/test_copernicus_aws.py tests/integration/test_local_workflow.py tests/integration/test_global_workflow.py tests/cli -q`.

Result: `38 passed, 874 warnings in 16.89s`.

Command: production Copernicus AWS cache replay for Amazon bbox `[-60.02, -3.13, -60.00, -3.11]` through `run_local_workflow`, using retained `cache/providers` and an opener that raises on any network request; then repeat and formally reopen with `uv run topoforge run --config outputs/amazon-copernicus-aws-quality-v2/build_config.resolved.yaml --output outputs/amazon-global-workflow-phase6-cache-replay-v1 --bbox -60.02 -3.13 -60.0 -3.11 --provider copernicus-aws --terrain-mode best-available --cache-dir cache/providers --no-slice`.

Result: first run completed acquire/source/build/layout/extract/mesh/connect; all 7 provider requests were cache hits; network opener calls were 0. Retained and replayed 74 x 74 rasters have identical CRS/transform/finite mask/elevation values and 0.0 m maximum difference. Build/extract/mesh/connect strict reopen passed. Repeat and CLI runs reused all seven stages; workflow manifest remained byte-identical. Evidence: `artifacts/verification/topoforge-0.4.0-amazon-global-workflow-phase6-cache-replay.json`.

Command: final `uv run ruff format --check .`, `uv run ruff check .`, `uv run pyright`, and `uv run pytest`.

Result: format `127 files already formatted`; Ruff `All checks passed!`; Pyright `0 errors, 0 warnings, 0 informations`; Pytest `175 passed, 2278 warnings in 47.48s`; `git diff --check` passed; all 21 retained/replayed workflow checksum roles passed `sha256sum -c`. Warnings remain visible under TF-006.
