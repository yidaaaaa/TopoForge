# Milestone 01 — Validated Local Terrain Manufacturing Pipeline

Date: 2026-07-31
Baseline commit: `10112b0a827bd27db6054d3ecf01a47d62b4aed5`
Scope: Phase 0 plus the verified full-raster local subset of Phases 1 and 2

## Implemented

- Python 3.12 package managed by `uv`, with a deterministic lock file.
- Typed Pydantic configuration and domain models for terrain, printers, raster results,
  datasets, builds, and validation.
- Deterministic analytic DEM catalog covering flat, slope, pyramid, Gaussian hill/valley,
  saddle, step, cone, coastline, and NoData cases.
- Local GeoTIFF ingestion with CRS checks, metric north-up reprojection, rotated-raster
  coverage cropping, cell-budget downsampling, conservative interior NoData interpolation,
  and preservation of the original NoData mask.
- Aspect-preserving horizontal scaling plus `natural`, `fit-height`, `auto-perceptual`, and
  `custom` vertical scale policies, with preserved datum offsets and a hard extrema height gate.
- Explicit watertight rectangular terrain construction with top surface, four walls, and a
  flat bottom in millimetres.
- Deterministic STL, GLB, and official `lib3mf==2.5.0` 3MF export.
- Strict 3MF reread plus independent OPC/XML package checks.
- Reopened geometry validation, JSON/HTML reports, complete local provenance fields, resolved
  YAML, checksum-verified manifest, and deterministic PNG preview.
- Atomic staged builds: a failed build does not publish a success-looking output directory.
- Typer commands: `build`, `synthetic`, `inspect`, `validate`, `slice`, `preview`,
  `providers`, `cache`, and `doctor`, including tested YAML/explicit-CLI override semantics.
- Headless OrcaSlicer and PrusaSlicer adapters with real executions and automatic fallback.

## Changed files

- Core package: `src/topoforge/`
- Tests: `tests/geometry/`, `tests/integration/`, `tests/property/`, `tests/slicer/`,
  `tests/unit/`, `tests/cli/`, `tests/providers/`
- Printer profiles: `printer_profiles/`
- Synthetic fixture and configuration: `examples/synthetic/`
- Project/recovery state: `AGENTS.md`, `.agent/`
- Architecture, provider, provenance, manufacturing, and license documentation: `docs/`,
  `README.md`, `DATA_LICENSES.md`, `THIRD_PARTY_NOTICES.md`
- Governance and release metadata: `LICENSE`, `CITATION.cff`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `SECURITY.md`

## Commands run

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest

uv run topoforge synthetic \
  --output examples/synthetic/gaussian-hill.tif \
  --terrain gaussian-hill --rows 64 --columns 80 --pixel-size-m 10

uv run topoforge build \
  --dem examples/synthetic/gaussian-hill.tif \
  --size-mm 180 0 --base-mm 3 --max-height-mm 42 \
  --vertical-scale fit-height \
  --printer-profile bambu-p2s-0.4 \
  --dataset-type dtm \
  --dataset-name "TopoForge analytic Gaussian mountain" \
  --dataset-version "milestone-01-fixture-v1" \
  --acquisition-period "deterministic analytic fixture" \
  --vertical-datum synthetic-local-zero \
  --data-license "Apache-2.0 synthetic fixture" \
  --attribution "TopoForge deterministic analytic fixture" \
  --output outputs/milestone-01-synthetic

uv run topoforge inspect outputs/milestone-01-synthetic/model.3mf
uv run topoforge slice \
  outputs/milestone-01-synthetic/model.3mf \
  --output artifacts/slicer/milestone-01-final.gcode
```

## Test results

- `uv sync`: exit `0`; 71 packages resolved, 62 packages audited.
- Ruff lint: exit `0`; `All checks passed!`
- Ruff format check: exit `0`; 78 files formatted.
- Pyright: exit `0`; 0 errors, 0 warnings, 0 informations.
- Pytest: exit `0`; 71 passed, 33 tracked upstream deprecation warnings.
- Actual final-3MF slice: exit `0` with PrusaSlicer 2.4.0.
- OrcaSlicer 2.3.0 also completed a real independent slice during adapter validation.

## Generated artifacts

Canonical local bundle (ignored manufacturing output):

`/root/autodl-tmp/bambu/TopoForge/outputs/milestone-01-synthetic`

The bundle contains:

```text
model.stl
model.3mf
preview.glb
processed_dem.tif
original_nodata_mask.tif
provenance.json
validation.json
validation.html
build_config.resolved.yaml
build_manifest.json
preview.png
slicer_validation.json
```

Committed compact evidence:

- `artifacts/previews/milestone-01-synthetic.png`
- `artifacts/reports/milestone-01-validation.json`
- `artifacts/reports/milestone-01-provenance.json`
- `artifacts/reports/milestone-01-3mf-slice.json`
- `artifacts/reports/milestone-01-slicer-validation.json`

Verified geometry and manufacturing measurements:

- Dimensions: `180.0 x 144.0 x 42.0 mm`
- Vertices / triangles: `10,240 / 20,476`
- Watertight / manifold / winding consistent: `true / true / true`
- Connected components: `1`
- Degenerate / duplicate faces: `0 / 0`
- Bottom planarity error: `0.0 mm`
- Minimum base thickness: `3.0 mm`
- Strict lib3mf warnings: `0`
- G-code: `5,030,602` bytes, `140` layers
- Estimated print time: `7h 11m 41s`
- Filament: `40,552.39 mm / 97.54 cm3`
- Support, out-of-bed, empty-layer, and floating-region warnings: none

## Known issues

- TF-003: OrcaSlicer 2.4.2 targets a newer Linux runtime than this host; verified OrcaSlicer
  2.3.0 and PrusaSlicer 2.4.0 provide working headless paths.
- TF-005: exhaustive robust self-intersection classification is recorded as
  `not_fully_checked`; all other required topology and slicer checks pass.
- TF-006: Rasterio 1.5.0 with NumPy 2.5.1 emits 33 masked-array deprecation warnings;
  regression behavior remains green.

## Deferred work

- Explicit bbox/polygon AOI clipping for local inputs; current verified builds process the full raster.
- No-key global DEM provider, center/place resolution, geocoding, caching, and fallback.
- Real Gongga and Amazon builds.
- Printer-aware refinement/resource estimation beyond the current cell and triangle budgets.
- Tiling/connectors, API/worker, Web UI, overlays, Docker, and release automation.

## Next milestone

Milestone 02 begins with AOI normalization and explicit local clipping, then connects the
configurable Copernicus AWS GLO-30/GLO-90 COG provider, content-addressed caching, explainable
selection/fallback, and actual Gongga/Amazon builds with provenance and slicing evidence.
