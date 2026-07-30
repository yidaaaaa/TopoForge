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
