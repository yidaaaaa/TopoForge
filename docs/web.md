# Local Web application

TopoForge 0.8.1 includes a single-user local Web application. It is an adapter over the
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

## Interface

The language switch in the header changes the complete interface between `zh-CN` and
English. Both versions expose the same controls and results:

- local GeoTIFF, bbox, or center-radius sources;
- MapLibre AOI drawing and normalization, with bundled Natural Earth country outlines and a graticule by default;
- optional OpenStreetMap raster tiles when the operator enables the online basemap;
- model dimensions, sampling mode, mesh spacing, and adapt/strict resource budgets;
- deterministic tile size, overlap, overlay YAML, slicing, and Bambu project settings;
- persistent jobs, progress events, cancellation, structured failures, and corrective text;
- measured workflow metrics and checksum-bound artifact downloads;
- Three.js GLB viewing with `+X East`, `+Y North`, and `+Z Up` labels.

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

The application shell, bundled offline reference map, local DEM processing, cached provider
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
loopback server when one is not already available. Its desktop check requires a spatially varied offline map, exercises the OSM request under CSP, rejects browser errors, verifies complete 3D framing across aspect ratios, and checks Chinese/English switching. Its
mobile check verifies the primary controls and rejects horizontal overflow.

Generated `web/node_modules/`, `web/test-results/`, and `web/playwright-report/` directories
are excluded from source archives. The sdist retains frontend source and lock files; the
wheel retains only the compiled, checksum-bound application inside `topoforge.web`.

## Rollback

Stop the 0.8.1 listener, start the retained 0.8.0 CLI environment, and keep existing
workspaces and state directories unchanged:

```bash
~/.venvs/topoforge-0.8.0/bin/topoforge doctor
ln -sfn ~/.venvs/topoforge-0.8.0/bin/topoforge ~/.local/bin/topoforge
```

For a source checkout exactly at the 0.8.1 release tag, run `scripts/rollback-topoforge-0.8.1.sh --confirm-rollback`.
