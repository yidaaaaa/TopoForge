# TopoForge Agent Guide

## Goal and boundaries

TopoForge turns real elevation rasters into dimensionally controlled, printable terrain artifacts. The core engine is CLI-first and is the sole source of business logic for future API and Web surfaces. Geospatial calculations use metres in an explicit projected CRS; manufacturing meshes use millimetres.

The validated core includes local and no-key global AOI acquisition, printer-aware sampling, orientation, resource preflight, deterministic manufacturing exports, provenance, and slicer evidence. Phase 5 deterministic tiling, seams, connectors, print-local placement, per-tile slicing, and Bambu project roles are complete in 0.4.0. The current milestone is Phase 6 single-user local completion and physical calibration. API/Web is retained as deferred Phase 9 after local workflow, overlays, and release hardening.

## Architecture

- `src/topoforge/models`: typed domain and report models.
- `src/topoforge/raster`: raster loading, reprojection, clipping, NoData resolution, and deterministic fixtures.
- `src/topoforge/scaling`: horizontal scale, baseline, and vertical exaggeration policies.
- `src/topoforge/mesh`: closed terrain geometry only; no file-format concerns.
- `src/topoforge/exporters`: STL/GLB/3MF serialization.
- `src/topoforge/validation`: geometry and slicer verification.
- `src/topoforge/rendering`: deterministic preview rendering.
- `src/topoforge/providers`: provider protocol and provider implementations.
- `src/topoforge/cli`: thin Typer adapters calling the engine.
- `tests`: unit, property, geometry, provider, slicer, integration, and golden evidence.

## Code and test rules

- Python 3.12, full annotations on public APIs, Pydantic at external boundaries.
- Name units in variables and fields (`width_mm`, `resolution_m`).
- Public APIs have docstrings; errors must identify a corrective action.
- Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, and `uv run pytest` before a milestone commit.
- Geometry claims require measured assertions: watertightness, winding, volume, flat bottom, dimensions, degeneracy, duplicates, and connected components.
- Online tests are opt-in integration tests; default tests must work offline.
- The default manufacturing target is Bambu Lab P2S with a 0.4 mm nozzle. Production P2S 3MF evidence requires official Bambu Studio normative slicing, resolved official preset hashes, hard parameter assertions, and project reopen/reslice verification.

## Data and license rules

- Code is Apache-2.0. Dataset terms remain separate and are recorded in provenance and `DATA_LICENSES.md`.
- Preserve provider name/version/type, CRS and vertical datum status, resolution, URLs, checksums, acquisition period, license, attribution, NoData, and interpolation fractions.
- Unknown vertical datum stays `unknown`. Never infer it from geographic location.
- Interpolation is restricted to small gaps, preserves the original mask, and records the changed fraction.
- Never fabricate terrain detail, disguise resolution, or bundle restricted datasets.

## Prohibited behavior

- No random terrain substituted for missing real data.
- No angle-space mesh construction in EPSG:4326.
- No silent CRS, datum, unit, NoData, or provider fallback.
- No unverified printable/manifold/slicer claims.
- No P2S production release based only on a slicer exit code, preset filename, OrcaSlicer, or PrusaSlicer; the official Bambu Studio parameter gate must pass.
- No large DEM, mesh, cache, download, or G-code files in Git.
- No duplicated core algorithms in CLI/API/Web.

## Directory ownership and collaboration

- A subagent owns only its assigned directories/files; coordinate before touching shared config/state files.
- Every subagent first reads this file plus `.agent/{PLANS,STATE,DECISIONS,ISSUES}.md`, then checks `git status` and `git log --oneline -10`.
- Reports must list changed files, commands, literal results, and unresolved issues. The primary agent independently reruns evidence before integration.
- Keep one authoritative plan in `.agent/PLANS.md`.

## Recovery protocol

1. Read `AGENTS.md` and `.agent/{PLANS,STATE,DECISIONS,ISSUES}.md`.
2. Run `git status --short --branch` and `git log --oneline -10`.
3. Execute the `Next exact command` from `.agent/STATE.md` unless repository evidence supersedes it.
4. Record significant commands in `.agent/RUN_LOG.md`; full logs belong in `artifacts/logs/`.

## Definition of Done

A milestone is done only when source, tests, generated artifacts, validation reports, preview, and (where required) actual slicer output exist; all required quality commands pass; state documents match the repository; generated outputs are ignored; a milestone report exists; and the milestone is committed with a reversible diff. For the default P2S target, completion also requires separate interoperable and Bambu-project 3MF roles plus official reopen/reslice evidence.
