# Offline Local Workflow

TopoForge 0.10.3 CLI can configure, run, inspect, back up, restore, resume, and add checksum-bound local overlays without starting the optional Web server. A new global AOI still needs either network access or every required provider request already present in the content-addressed cache.

## Prepare the locked environment

On a connected machine, populate the `uv` cache and retain the repository plus `.venv` or the cache directory:

```bash
uv sync
uv run topoforge doctor
```

With the required wheels already cached, the same lock can be installed without network access:

```bash
uv sync --offline
uv run topoforge doctor
```

TopoForge constrains NumPy to `<2.5` because Rasterio 1.5.0 reads generated masked arrays through an API deprecated in NumPy 2.5. NumPy 2.4.6 removes those warnings; the retained comparison proves the core STL, 3MF, GLB, PNG, processed DEM, and NoData-mask hashes are unchanged.

## Create and run a local job

```bash
uv run topoforge wizard \
  --output outputs/local-terrain \
  --source local \
  --dem /ABSOLUTE/PATH/terrain.tif \
  --no-slice \
  --no-run \
  --yes

uv run topoforge storage outputs/local-terrain
uv run topoforge resume outputs/local-terrain
uv run topoforge browse outputs/local-terrain --no-open
```

`storage` uses configured resource ceilings before the first run and measured grid/triangle counts after completion. It reports the current workspace bytes, estimated peak and increment, free disk headroom, cleanup candidates, and backup input bytes. Compression and G-code size vary, so the estimate is conservative rather than a guaranteed reservation.

The saved `workflow-launch.yaml` is the only command reconstruction needed. `resume` strictly reopens every current stage manifest and checksum before reuse. `browse` regenerates a dependency-free `workflow-report.html` with workspace-contained relative links.

## Reuse a global job offline

A completed global workspace contains its acquired metric raster, acquisition manifest, provider trace, aligned quality masks, and every manufacturing stage. Reopening or browsing it does not require the network.

For a new global run without network access, all catalog, source COG, and quality-mask requests must already be cache hits. The provider cache location is operational state and is not copied into the workflow backup because it can be much larger than one job. A cache miss remains a real acquisition failure; TopoForge does not substitute synthetic terrain or silently expand the AOI.

## Review and apply cleanup

```bash
uv run topoforge cleanup outputs/local-terrain
```

The review JSON lists only children of `stages/` that are not referenced by the current `workflow-manifest.json`, their measured sizes, and an exact apply command. Current stage identities, launch/status/manifest records, source evidence, reports, and provider caches are preserved.

Run the emitted command only after checking the paths:

```bash
uv run topoforge cleanup outputs/local-terrain \
  --apply \
  --confirm-workflow-id local-EXACT_WORKFLOW_ID
```

The workflow id is checked again immediately before deletion, then the completed workspace is strictly reopened. A wrong or stale confirmation leaves every candidate in place.

## Back up and restore

```bash
uv run topoforge backup outputs/local-terrain \
  --output outputs/backups/local-terrain.zip

uv run topoforge restore outputs/backups/local-terrain.zip \
  --output outputs/local-terrain-restored

uv run topoforge browse outputs/local-terrain-restored --no-open
```

The backup command first verifies the completed workflow. It stores every regular workspace file plus referenced local DEM, source-acquisition manifest, and slicer setting/filament files that live outside the workspace. Symlinks are rejected. ZIP timestamps and modes are fixed, entries are sorted, and the embedded manifest records source path, role, size, and SHA-256 for every file. The archive is fully reread before success is reported.

Restore rejects an existing destination, verifies paths/CRCs/sizes/SHA-256 values, extracts into a sibling staging directory, remaps saved launch paths, strictly reopens the workflow, and only then atomically publishes the destination. External files are restored below `backup-external/`. A relocated workflow remains immediately browsable; resuming it may create new content-addressed stages because absolute source/output paths are part of the saved evidence.

## Boundaries

- Several TopoForge terrain models have been physically printed on a Bambu Lab P2S with very good operator-reported results; quantitative connector calibration remains pending and does not block local software use.
- `self_intersection_status` remains `not_fully_checked`; Phase 6 did not promote a backend with unsuitable accuracy, licensing, or resource behavior.
- The optional Phase 11 WebUI packages its assets locally and binds only to loopback. Local DEM XYZ tiles and assembly views remain same-origin and deterministic. CLI workflows still call the Python core directly and do not need the Web service. Uncached global AOIs and the optional OpenStreetMap layer remain the only network-dependent paths.
