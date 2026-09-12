# Local Web application

TopoForge 0.10.3 includes a single-user local Web application. It is an adapter over the
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
3. Keep `Print aware` and `Adapt` for the first build. The defaults are 180 mm width, automatic depth, 45 mm maximum height, 3 mm base, 180 mm tile limits, 0.20 mm connector total clearance, one overlap cell, and software slicing disabled. The connector menu also offers 0.10, 0.15, 0.25, 0.30, and 0.40 mm so the operator can use results from the target printer and material instead of relying on a universal tolerance.
4. Select `Start build` and follow the durable job in the results panel. Processed terrain map layers, the 3D model, assembly, metrics, and downloads appear after the job reaches `Completed`. Before completion, a local-DEM map intentionally shows only the offline geographic reference background.
5. Open `Map` for terrain/elevation/hillshade, `3D model` for the whole GLB, and `Assembly` for physical tile layout and per-tile 3D. When Bambu project evidence is enabled, download `Bambu Studio project 3MF (recommended for printing)`. `Generic 3MF (geometry only)` is the interoperable Core geometry and may prompt Bambu Studio to import settings because it is not a Bambu project. Use STL only when the target tool requires it.
6. Click the selected job again or use the close icon in the job detail header to clear the selection. The map, assembly, and detail overlays remain cleared across the one-second job refresh loop until another job is selected.

The online basemap toggle changes geographic context only; it does not change or download the terrain source. Bbox and center-radius sources use the provider/cache workflow and may require network access when the requested data is not already cached. File browsing remains limited to the configured `--input-root` values.

Tile planning treats model dimensions within 0.001 mm of an integer tile-limit boundary as that boundary count. This prevents automatic-aspect floating-point drift from creating a visually surprising extra row or column while preserving the exact model dimensions and still partitioning material overages.

## Interface

The language switch in the header changes the complete interface between `zh-CN` and
English. Both versions expose the same controls and results:

- local GeoTIFF, bbox, or center-radius sources;
- MapLibre AOI drawing and normalization, with bundled Natural Earth land outlines and a graticule by default;
- deterministic local terrain, elevation, and hillshade XYZ tiles derived from the completed processed DEM;
- geographic manufacturing tile footprints with map selection synchronized to assembly;
- optional OpenStreetMap Shortbread vector tiles when the operator enables the online basemap;
- model dimensions, sampling mode, mesh spacing, and adapt/strict resource budgets;
- deterministic tile size, user-selected connector total clearance, overlap, overlay YAML, slicing, and Bambu project settings;
- persistent jobs, progress events, cancellation, explicit job deselection, bilingual workspace/id search, status filters, newest/oldest/name/status sorting, terminal-job selection, measured batch preflight, structured failures, and corrective text;
- measured workflow metrics and checksum-bound artifact downloads;
- measured local-project storage, reclaimable old stages, deterministic backup creation and download, exact-identity cleanup, and atomic restore as a newly registered completed job;
- record-only removal, recoverable workspace quarantine, optional verified backup before quarantine, seven-day trash retention, complete restore, and explicitly confirmed permanent purge;
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
  containment, and rehashed before serving. Bambu project manifests additionally publish each
  checksum-matched tile project and validation report as explicit download roles.
- Backup archives live below the adapter state directory, are strictly reopened before
  listing or download, and expose their verified SHA-256 in response headers.
- Recoverable job records live below `--state-dir/trash`; quarantined workspaces live
  below `--workspace-root/.topoforge-trash` so directory publication stays on the
  workspace filesystem.
- In-progress batch intent lives below `--state-dir/trash-transactions`. Restore and
  permanent-purge audit records live below `--state-dir/deletion-audit`.
- Restores reject path escapes and existing destinations, extract through the core atomic
  restore contract, and are registered only after strict workspace reopen succeeds.
- Assembly metadata is anchored to the published workflow manifest and validated CONNECT-stage
  manifest SHA, then cross-checks assembly validation, tile manifests, and per-tile GLBs.
- Date-line processed rasters use split Web Mercator coverage and circular longitude centers.
  Partial latitude clipping is reported; rasters fully outside Web Mercator are rejected.
- Static assets are served only after the package manifest passes SHA-256 and size checks.
- The content security policy permits same-origin application traffic and the explicit
  local reference-tile endpoint. The browser uses only same-origin requests; the
  runtime fetches fixed-host OSM vector tiles using its configured network/proxy.

This is a loopback application for the local operator. It has no authentication, public
deployment, database service, or remote multi-user contract.

## Job recovery

Job records survive a Web process restart. On startup the manager reconciles retained
PIDs, worker result files, and the checksum-bound workflow status. A running child process
continues independently if the HTTP process stops; the restarted manager reconnects to
its durable state. A failed job preserves request JSON, events, stdout, stderr, structured
error details, and the underlying workflow failure record.

Batch removal writes a strict transaction before moving any job or workspace. On startup,
a transaction without a prepared `trash.json` is rolled back to the original job and
workspace paths. A transaction with the exact prepared record is completed and strictly
reopened as a recoverable trash batch before its intent record is removed.

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

Stop the 0.10.3 listener, start the retained 0.10.2 CLI environment, and keep existing
workspaces and state directories unchanged:

```bash
~/.venvs/topoforge-0.10.2/bin/topoforge doctor
ln -sfn ~/.venvs/topoforge-0.10.2/bin/topoforge ~/.local/bin/topoforge
```

For a source checkout exactly at the 0.10.3 release tag, run `scripts/rollback-topoforge-0.10.3.sh --confirm-rollback`; it creates a separate detached 0.10.2 worktree and leaves retained state untouched.


### Local reference-map trial

The online reference uses OSM Shortbread vector tiles for natural features, roads,
local names, country names and selected POIs. Taiwan, Hong Kong and Macao labels
use the regional-name style rather than the country-name style. Raw Shortbread
boundary lines remain disabled because they omit the countries involved in a dispute.

Boundary lines come from a bundled Natural Earth v5.1.2 layer (1:10 million
reference scale). China-related lines use its `FCLASS_CN` worldview: replacement
claim geometries become visible, and superseded lines are removed. Other regions
retain their original boundary classifications. Hong Kong/Macao map-unit lines use
a lighter internal-boundary style, and Taiwan uses a regional label (台湾省 in Chinese).

The maritime context uses the nine individual strokes in the publisher's China
supplement. Each is rendered as a continuous stroke; the gaps come from the source
geometry. The legacy Taiwan-east arc has no CN override and is omitted: it is not
used as a substitute for an additional claim stroke. This pinned source is the
nine-stroke version; no tenth stroke has been added.
A Doklam segment tagged with Bhutan on both sides is also explicitly associated with
China, following the matching release's CN worldview polygon. Unclassified claim-only
lines and historical reference/overlay/lease limits are omitted.

Source URLs, hashes, per-feature classifications and review IDs are recorded in
`web/src/data/reference-boundaries.provenance.json`. Download the four pinned source
files into one directory and run
`node web/scripts/build-reference-boundaries.mjs <directory>` to reproduce the layer.
Original coordinates are unchanged. This small-scale reference layer works offline;
country/regional names require the corresponding viewed tiles in cache. DEM acquisition
and manufacturing coordinates are independent of this display layer.

Text uses local system fonts (Chinese coverage depends on installed fonts). Online tiles
are requested only when enabled, through the same-origin local tile relay. The
runtime uses its existing proxy/TLS settings, with a fixed upstream host, bounded
coordinates, a 20-second socket timeout and an 8 MiB response limit. Browser
responses honor seven-day freshness; failed responses are not cached. Viewed tiles
are also stored under `state_dir/reference-map/shortbread-v1.sqlite3`, bounded to
128 MiB of payloads and 4096 tiles with least-recently-used eviction (database metadata
and transient SQLite journals add small storage overhead). Only visible requested tiles
are saved; no prefetch or bulk download is performed. The `Use local cache only` switch
reads this persistent cache without upstream requests, including expired entries. Areas
and zoom levels not already cached show a missing-cache message. The selected mode is
remembered across reloads together with the last map position and zoom; cache-only responses bypass browser caching so disk misses
remain visible. Online mode refreshes entries after seven days. Cache storage errors
are reported instead of silently losing offline coverage. This switch controls reference
map requests; model acquisition still needs a local DEM or separately cached elevation data.
The cache resides on the machine running TopoForge, including the remote host when using
a forwarded preview. Do not bulk-download
OSMF tiles or use this service for offline packs. Keep the displayed OSM attribution.
Service availability is best-effort; see https://operations.osmfoundation.org/policies/vector/.

### Local standard-map original

When `state_dir/reference-map/local-standard-map.json` and
`local-standard-map.jpg` are present, the map panel offers **标准地图原图 /
Standard map original**. This opens a separate local image viewer with pan,
zoom, fit-to-window, native-size viewing, and original-file download. Display uses only the locally cached pixel tiles visible
in the viewport, avoiding whole-image decoding in the browser. Its interface
follows the application's saved language. No remote images, fonts, or tiles are
needed for this view.

The JPEG remains unchanged. The metadata uses schema
`topoforge-local-standard-map-v1` and records `title`, `source_sha256`,
`width_px`, `height_px`, optional HTTP(S) `source_url`, and `provenance`.
Prepare the local display pyramid after installing the pair:

```bash
uv run python scripts/prepare_standard_map.py --state-dir /path/to/web-state
```

The source digest identifies a separate `standard-map-tiles` directory. Every PNG
tile has a recorded digest; highest-resolution tiles preserve the source's decoded
RGB pixels (using the embedded colour profile when present). Smaller display levels
are sampled only for viewing. The original JPEG download remains byte-for-byte
unchanged. Tile requests never decode the full JPEG. Both fixed source files and
their display cache must remain inside the reference-map directory. The runtime
validates the digest, JPEG format, and dimensions; limits are 24 MiB for the image,
64 KiB for metadata, and 80 million pixels. A missing pair disables the entry;
a partial, invalid, or changed pair produces an explicit error. Refresh the app
after installing or replacing a pair.

`GET /api/v1/reference/standard-map` returns the verified source metadata and a
local image URL, or JSON `null` when no source is installed. The image URL includes
the source digest so browser caching cannot mix different originals. The source
files belong to runtime state and are excluded from code and release assets.

This is an image reference, with no conversion from page pixels to geographic
coordinates. Select print areas on the existing interactive map. Its Natural Earth
boundary catalog and OSM layers remain unchanged. The trial conversion of the
user-supplied GS(2022)4309 EPS was not activated: the file lacks CRS metadata and
independent registration checks showed material positional errors, especially in
the separately scaled South China Sea inset.
