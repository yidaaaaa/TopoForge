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
