# Current State

- **Current milestone:** Milestone 02 — AOI normalization/local clipping followed by the no-key global provider.
- **Current branch/worktree:** `main`, `/root/autodl-tmp/bambu/TopoForge`.
- **Last completed task:** Corrected baseline preservation and hard height enforcement, rebuilt the canonical 42.0 mm model, strict-read its 3MF, actually sliced it, and passed all code gates.
- **Current implementation status:** Full-raster local GeoTIFF/synthetic builds emit and reopen STL, 3MF, GLB, processed DEM, original NoData mask, provenance, validation JSON/HTML, resolved YAML, checksum-verified manifest, and PNG. Explicit local AOI cropping, network provider fetching, and geocoding remain next.
- **Known failing tests:** None. Latest full suite: 71 passed.
- **Current blockers:** No blocking external dependency. OrcaSlicer 2.4.2 targets a newer system runtime; verified OrcaSlicer 2.3.0 and PrusaSlicer 2.4.0 paths are available.
- **Known warnings:** 33 upstream Rasterio/NumPy masked-array deprecation warnings under NumPy 2.5.1; behavior tests pass.
- **Next exact action:** Implement normalized bbox/center AOIs and explicit local raster clipping before connecting the configurable Copernicus AWS GLO-30/GLO-90 provider.
- **Next exact command:** `uv run topoforge providers && uv run pytest tests/providers tests/integration -q`
- **Important paths:** `src/topoforge/engine/build.py`, `src/topoforge/raster/processing.py`, `src/topoforge/scaling/policy.py`, `src/topoforge/exporters/three_mf.py`, `src/topoforge/validation/slicers/`, `docs/data-sources.md`, `outputs/milestone-01-synthetic/`, `artifacts/reports/milestone-01.md`.
