# Local Web application

TopoForge 0.10.1 includes a single-user local Web application. It is an adapter over the
same `WorkflowLaunchConfig` and `execute_workflow_launch()` path used by the CLI. Raster,
sampling, mesh, tiling, overlay, slicing, validation, and artifact logic remains in the
Python core.

## Start and verify

```bash
topoforge web --check --workspace-root topoforge-workspaces --input-root . --no-open

topoforge web \
  --host 127.0.0.1 \
  --port 8765 \
  --state-dir ~/.topoforge/web \
  --workspace-root topoforge-workspaces \
  --input-root .
```

The default URL is `http://127.0.0.1:8765/`. `--check` reopens the bundled asset manifest,
verifies every production asset SHA-256 and byte size, and reports the Chinese/English
language and React/MapLibre/Three.js framework contracts without starting a listener.

## First local build

1. Start the loopback service with a dedicated state directory, workspace root, and one or more explicit input roots.
2. Select `Local DEM`, browse to an existing GeoTIFF, and choose a unique workspace name.
3. Keep `Print aware` and `Adapt` for the first build. The defaults are 180 mm width, automatic depth, 45 mm maximum height, 3 mm base, 180 mm tile limits, one overlap cell, and software slicing disabled.
4. Select `Start build` and follow the durable job in the results panel. Processed terrain map layers, the 3D model, assembly, metrics, and downloads appear after the job reaches `Completed`. Before completion, a local-DEM map intentionally shows only the offline geographic reference background.
5. Open `Map` for terrain/elevation/hillshade, `3D model` for the whole GLB, and `Assembly` for physical tile layout and per-tile 3D. Download `model.3mf` or `model.stl` from the artifact list.

The online basemap toggle changes geographic context only; it does not change or download the terrain source. Bbox and center-radius sources use the provider/cache workflow and may require network access when the requested data is not already cached. File browsing remains limited to the configured `--input-root` values.

Tile planning treats model dimensions within 0.001 mm of an integer tile-limit boundary as that boundary count. This prevents automatic-aspect floating-point drift from creating a visually surprising extra row or column while preserving the exact model dimensions and still partitioning material overages.

## Interface

The language switch in the header changes the complete interface between `zh-CN` and
English. Both versions expose the same controls and results:

- local GeoTIFF, bbox, or center-radius sources;
- MapLibre AOI drawing and normalization, with bundled Natural Earth country outlines and a graticule by default;
- deterministic local terrain, elevation, and hillshade XYZ tiles derived from the completed processed DEM;
- geographic manufacturing tile footprints with map selection synchronized to assembly;
- optional OpenStreetMap raster tiles when the operator enables the online basemap;
- model dimensions, sampling mode, mesh spacing, and adapt/strict resource budgets;
- deterministic tile size, overlap, overlay YAML, slicing, and Bambu project settings;
- persistent jobs, progress events, cancellation, structured failures, and corrective text;
- measured workflow metrics and checksum-bound artifact downloads;
- measured local-project storage, reclaimable old stages, deterministic backup creation and download, exact-identity cleanup, and atomic restore as a newly registered completed job;
- 2D physical assembly with tile labels, connectors, and a North marker;
- Three.js whole-model and per-tile assembly viewing with visibility, explosion, selection, and `+X East`, `+Y North`, and `+Z Up` labels.

The WebUI does not provide a separate terrain implementation. A submitted form is
validated into the existing workflow launch model and executed by an isolated Python
child process.

## Local boundaries

- Only `localhost`, `127.0.0.0/8`, or `::1` bind addresses are accepted.
- Trusted host middleware rejects non-loopback host headers.
- File browsing and YAML loading are limited to repeated explicit `--input-root` values.
- Web-created workspaces must be strict children of `--workspace-root`.
- Durable request, job, event, stdout, stderr, and worker-result records live below
  `--state-dir`.
- Artifact downloads are resolved from completed workflow records, checked for workspace
  containment, and rehashed before serving.
- Backup archives live below the adapter state directory, are strictly reopened before
  listing or download, and expose their verified SHA-256 in response headers.
- Restores reject path escapes and existing destinations, extract through the core atomic
  restore contract, and are registered only after strict workspace reopen succeeds.
- Assembly metadata is anchored to the published workflow manifest and validated CONNECT-stage
  manifest SHA, then cross-checks assembly validation, tile manifests, and per-tile GLBs.
- Date-line processed rasters use split Web Mercator coverage and circular longitude centers.
  Partial latitude clipping is reported; rasters fully outside Web Mercator are rejected.
- Static assets are served only after the package manifest passes SHA-256 and size checks.
- The content security policy permits same-origin application traffic and the explicit
  OpenStreetMap tile origin. OpenStreetMap is the sole external browser origin.

This is a loopback application for the local operator. It has no authentication, public
deployment, database service, or remote multi-user contract.

## Job recovery

Job records survive a Web process restart. On startup the manager reconciles retained
PIDs, worker result files, and the checksum-bound workflow status. A running child process
continues independently if the HTTP process stops; the restarted manager reconnects to
its durable state. A failed job preserves request JSON, events, stdout, stderr, structured
error details, and the underlying workflow failure record.

Cancellation sends a process-group termination signal and records `cancelling` followed by
`cancelled`. It does not delete source data, completed content-addressed stages, workspaces,
or earlier evidence.

## Offline operation

The application shell, bundled offline reference map, local DEM processing, deterministic local map-tile cache, per-tile assembly, cached provider
replay, job state, previews, and artifacts work without browser network access. A global
AOI still requires either provider network access or a complete retained provider cache.
Enabling the OpenStreetMap switch explicitly requests public map tiles and does not alter
the terrain source or manufacturing result.

## Frontend development and checks

```bash
npm --prefix web ci
npm --prefix web run typecheck
npm --prefix web run test
npm --prefix web run build
uv run topoforge web --check
npm --prefix web run test:ui
```

The Vite production build writes directly to `src/topoforge/web/static/`, then
`web/scripts/write-manifest.mjs` writes the strict asset manifest. Playwright starts a
loopback server on an isolated test port. Its desktop check creates a real completed
workflow, measures a cleanup candidate, creates and downloads a verified backup, accepts
the exact cleanup confirmation, restores a registered completed copy, exercises all three
DEM styles and the OSM request under CSP, rejects browser errors, verifies whole-model and
per-tile 3D framing, and checks Chinese/English switching. Its mobile check verifies the
primary controls and rejects horizontal overflow.

Generated `web/node_modules/`, `web/test-results/`, and `web/playwright-report/` directories
are excluded from source archives. The sdist retains frontend source and lock files; the
wheel retains only the compiled, checksum-bound application inside `topoforge.web`.

## Rollback

Stop the 0.10.1 listener, start the retained 0.10.0 CLI environment, and keep existing
workspaces and state directories unchanged:

```bash
~/.venvs/topoforge-0.10.0/bin/topoforge doctor
ln -sfn ~/.venvs/topoforge-0.10.0/bin/topoforge ~/.local/bin/topoforge
```

For a source checkout exactly at the 0.10.1 release tag, run `scripts/rollback-topoforge-0.10.1.sh --confirm-rollback`; it creates a separate detached 0.10.0 worktree and leaves retained state untouched.
