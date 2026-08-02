# Local release and offline installation

TopoForge 0.9.0 is the Phase 10 local-use release. It packages the CLI, worker-backed loopback FastAPI adapter, and checksum-bound Chinese/English React, MapLibre, and Three.js application. It does not add a public deployment, authentication layer, database service, or remote multi-user contract.

## Verified platform boundary

The Phase 10 installation evidence uses CPython 3.12.3 on Linux x86_64 with
glibc 2.35, GDAL 3.12.1, PROJ 9.7.1, and official `lib3mf==2.5.0`.

The TopoForge wheel is pure Python, but lib3mf, Rasterio, SciPy, Shapely, and related
dependencies are platform-specific. An offline wheelhouse must match the offline
workstation's OS, CPU architecture, Python minor version, and compatible libc. Linux
ARM64 and other unverified targets require separate dependency-wheel and release tests.

## Build and verify a release

```bash
export SOURCE_DATE_EPOCH=1580601600
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest

uv build --no-sources --out-dir dist/primary
uv build --no-sources --out-dir dist/repeat
uv run python scripts/verify_release.py \
  --primary-dir dist/primary \
  --repeat-dir dist/repeat \
  --version 0.9.0 \
  --install \
  --report artifacts/logs/phase10-release-verification.json
```

The verifier requires byte-identical sdist and wheel archives, bounded archive contents,
SPDX metadata, all three license/notice files, the console entry point, and an installed
CLI import outside the repository. The installed CLI verifies the packaged Web assets and bilingual manifest, creates a synthetic raster, builds STL/3MF/GLB and reports, and strict-reads the 3MF with zero warnings.

## Connected installation

```bash
python3.12 -m venv ~/.venvs/topoforge-0.9.0
~/.venvs/topoforge-0.9.0/bin/python -m pip install \
  /PATH/TO/topoforge-0.9.0-py3-none-any.whl
~/.venvs/topoforge-0.9.0/bin/topoforge doctor
~/.venvs/topoforge-0.9.0/bin/topoforge web --check --workspace-root /tmp/topoforge-workspaces --input-root . --no-open
```

## Prepare an offline wheelhouse

On a connected machine matching the offline target:

```bash
python3.12 -m venv /tmp/topoforge-wheelhouse-tools
/tmp/topoforge-wheelhouse-tools/bin/python -m pip install --upgrade pip
mkdir -p topoforge-0.9.0-wheelhouse
/tmp/topoforge-wheelhouse-tools/bin/python -m pip download \
  --dest topoforge-0.9.0-wheelhouse \
  /PATH/TO/topoforge-0.9.0-py3-none-any.whl
sha256sum topoforge-0.9.0-wheelhouse/* > topoforge-0.9.0-wheelhouse/SHA256SUMS
sha256sum -c topoforge-0.9.0-wheelhouse/SHA256SUMS
```

On the offline workstation:

```bash
cd /PATH/TO/topoforge-0.9.0-wheelhouse
sha256sum -c SHA256SUMS
python3.12 -m venv ~/.venvs/topoforge-0.9.0
~/.venvs/topoforge-0.9.0/bin/python -m pip install \
  --no-index --find-links . topoforge==0.9.0
~/.venvs/topoforge-0.9.0/bin/topoforge doctor
~/.venvs/topoforge-0.9.0/bin/topoforge web --check --workspace-root /tmp/topoforge-workspaces --input-root . --no-open
```

A TopoForge wheel by itself is not an offline installation bundle. The wheelhouse must
contain every resolved platform dependency, especially lib3mf and the geospatial wheels.

## Upgrade without changing retained evidence

1. Run `topoforge backup WORKSPACE --output BACKUP.zip` for each active workflow.
2. Keep the previous environment and wheelhouse until the new version passes
   `topoforge doctor` and a local smoke build.
3. Install 0.9.0 into a new environment rather than mutating the old one.
4. Resume or browse existing workspaces. Stage reuse remains checksum-bound.
5. Generate new outputs in new directories; existing DEMs and bundles remain immutable.

## Rollback

Installed CLI rollback keeps the 0.9.0 environment intact and switches back to 0.8.1:

```bash
~/.venvs/topoforge-0.8.1/bin/topoforge doctor
ln -sfn ~/.venvs/topoforge-0.8.1/bin/topoforge ~/.local/bin/topoforge
```

For a source checkout after the Phase 10 release commit and tag:

```bash
scripts/rollback-topoforge-0.9.0.sh --confirm-rollback
```

Restore a workflow only from a checksum-verified backup:

```bash
topoforge restore BACKUP.zip --output NEW_WORKSPACE
topoforge browse NEW_WORKSPACE --no-open
```

No rollback command deletes retained DEMs, output bundles, caches, or the newer
environment.
