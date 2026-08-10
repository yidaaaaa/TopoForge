# Local release and offline installation

TopoForge 0.10.3 is a local-use manufacturing and usability patch over 0.10.2. It preserves the CLI, loopback FastAPI adapter, bilingual React/MapLibre/Three.js application, manufacturing core, durable batch lifecycle, and public-tree privacy gate. Generated terrain, downloads, local Web state, contributor-only state, physical test records, and machine-local verification evidence remain excluded from the release tree. It does not add a public deployment, authentication layer, database service, or remote multi-user contract.

## 0.10.3 patch contents

- A deterministic compact calibration generator produces six male/female connector pairs at 0.10, 0.15, 0.20, 0.25, 0.30, and 0.40 mm total clearance, with Core and official Bambu Studio project 3MF roles.
- Labels are recessed 0.4 mm into both coupon halves and remain separate from connector geometry, eliminating the raised-label seating interference observed in the first physical coupon.
- All six revised pairs were reported to insert and completely seat on a Bambu Lab P2S. This is bounded physical evidence, not a universal clearance recommendation; the WebUI keeps all six values operator-selectable.
- The selected clearance is persisted through `printer_profile.connector_tolerance_mm` and used by the existing validated connector pipeline.
- Selecting the active job again or using the detail close control now clears selection across polling, so the detail layer no longer obscures the task list.
- Completed jobs distinguish the interoperable `Generic 3MF (geometry only)` from the separately verified `Bambu Studio project 3MF (recommended for printing)` and safely backfill old completed records from strict project manifests.

## Verified platform boundary

The Phase 11 installation evidence uses CPython 3.12.3 on Linux x86_64 with
glibc 2.35, GDAL 3.12.1, PROJ 9.7.1, and official `lib3mf==2.5.0`.

The TopoForge wheel is pure Python, but lib3mf, Rasterio, SciPy, Shapely, and related
dependencies are platform-specific. An offline wheelhouse must match the offline
workstation's OS, CPU architecture, Python minor version, and compatible libc. Linux
ARM64 and other unverified targets require separate dependency-wheel and release tests.

## Phase 12 development boundary

The community package metadata accepts CPython 3.11 through 3.14. The universal
`uv.lock` resolves each supported minor explicitly, and CI runs the public-tree audit,
deterministic core/Web acceptance, and full Python regression suite on 3.11, 3.12, 3.13,
and 3.14. Python 3.12 remains the reference release-verifier interpreter; it is not the
only accepted community interpreter.

The Windows x64 portable candidate embeds CPython 3.12.13 from the immutable
python-build-standalone 20260807 artifact declared in
`packaging/windows-x64-runtime.json`. The builder accepts only the pinned byte count
and SHA-256, installs locked `win_amd64` wheels without source builds, removes
host-specific generated scripts, bundles dependency/runtime licenses and provenance,
and emits a fixed-timestamp ZIP with a complete file-hash manifest. The verifier
rejects traversal, links, Windows case collisions, unsupported compression, expansion
overruns, metadata drift, wheel/install divergence, and any content not covered by the
manifest.

Hosted Windows CI builds the candidate twice and executes it natively, but does not
replace clean Windows 10 22H2 x64 and Windows 11 x64 VM evidence. The candidate is not
published by `.github/workflows/release.yml` until both clean-system gates close.

### Phase 12B official Bambu Studio acceptance

Official Bambu Studio integration remains a separate, unpublished candidate. TopoForge
can discover `bambu-studio.exe` in the standard 64-bit Program Files installation or
the user's Local AppData Programs directory. It then locates the sibling
`resources/profiles/BBL` tree, strictly resolves the P2S machine, 0.20 mm Standard
process, and Bambu PLA Basic inheritance graphs, and stores a content-addressed bundle
with exact selected-source, resolved-profile, and executable hashes. The acceptance
verifier additionally records the measured Bambu Studio version. Both the CLI and Web
entry points retain manual executable and profile-root overrides.

The hosted `windows-bambu-contracts` job intentionally has no official Bambu Studio
binary. It tests only discovery, profile, path, and verifier contracts and is not
release evidence. On each clean Windows target, run the official tool gate separately
with a new work root:

```powershell
uv run python scripts/verify_windows_bambu.py `
  --work-root artifacts/windows-bambu-acceptance `
  --require-windows `
  --report artifacts/logs/windows-bambu-acceptance.json
```

If standard discovery is unavailable, add
`--bambu-studio-executable "C:\Program Files\Bambu Studio\bambu-studio.exe"` and
`--bambu-profiles-root "C:\Program Files\Bambu Studio\resources\profiles\BBL"`.
The verifier creates a synthetic terrain below a path containing spaces and non-ASCII
characters, runs normative P2S slicing, exports the Bambu project, reopens it without
external profiles, reslices it, checks every manufacturing parameter gate, and binds
the OS build, Python, TopoForge, executable, profiles, workflow, and artifacts by
SHA-256.

To prove the packaged application rather than the source environment, add the opt-in
gate to native portable verification. `--work-root` is mandatory so the evidence is
retained:

```powershell
uv run python scripts/verify_windows_portable.py `
  --archive dist/windows-primary/topoforge-0.10.3-windows-x64-portable.zip `
  --expected-version 0.10.3 `
  --execute `
  --verify-bambu `
  --work-root artifacts/windows-portable-bambu-acceptance `
  --report artifacts/logs/windows-portable-bambu-verification.json
```

Run both commands for the identical release candidate on clean Windows 10 22H2 x64
and Windows 11 x64 hosts and retain separate reports. Neither hosted CI, the successful
Linux official-Bambu regression, nor one Windows version may substitute for the other;
do not publish a Windows or Bambu support claim until both reports pass.

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
  --version 0.10.3 \
  --install \
  --report artifacts/logs/topoforge-0.10.3-release-verification.json
```

The verifier requires byte-identical sdist and wheel archives, bounded archive contents,
SPDX metadata, all three license/notice files, the console entry point, and an installed
CLI import outside the repository. The installed CLI verifies the packaged Web assets and bilingual manifest, creates a synthetic raster, builds STL/3MF/GLB and reports, and strict-reads the 3MF with zero warnings.

## GitHub Release publication

The .github/workflows/release.yml workflow runs on main, version tags, and manual
dispatch. A main push selects the newest reachable tag without a GitHub Release, which
bootstraps historical tags such as v0.9.0. A tag push publishes that exact tag. The
workflow checks out the target tag, requires v<project.version> identity, builds the Web
assets, creates two fixed-epoch archive sets, runs isolated installation verification,
writes and rechecks SHA256SUMS, and uploads the wheel, sdist, verification JSON, and
checksums. Existing Release pages are detected and skipped without replacing assets.

## Connected installation

```bash
python3.12 -m venv ~/.venvs/topoforge-0.10.3
~/.venvs/topoforge-0.10.3/bin/python -m pip install \
  /PATH/TO/topoforge-0.10.3-py3-none-any.whl
~/.venvs/topoforge-0.10.3/bin/topoforge doctor
~/.venvs/topoforge-0.10.3/bin/topoforge web --check --workspace-root /tmp/topoforge-workspaces --input-root . --no-open
```

## Prepare an offline wheelhouse

On a connected machine matching the offline target:

```bash
python3.12 -m venv /tmp/topoforge-wheelhouse-tools
/tmp/topoforge-wheelhouse-tools/bin/python -m pip install --upgrade pip
mkdir -p topoforge-0.10.3-wheelhouse
/tmp/topoforge-wheelhouse-tools/bin/python -m pip download \
  --dest topoforge-0.10.3-wheelhouse \
  /PATH/TO/topoforge-0.10.3-py3-none-any.whl
sha256sum topoforge-0.10.3-wheelhouse/* > topoforge-0.10.3-wheelhouse/SHA256SUMS
sha256sum -c topoforge-0.10.3-wheelhouse/SHA256SUMS
```

On the offline workstation:

```bash
cd /PATH/TO/topoforge-0.10.3-wheelhouse
sha256sum -c SHA256SUMS
python3.12 -m venv ~/.venvs/topoforge-0.10.3
~/.venvs/topoforge-0.10.3/bin/python -m pip install \
  --no-index --find-links . topoforge==0.10.3
~/.venvs/topoforge-0.10.3/bin/topoforge doctor
~/.venvs/topoforge-0.10.3/bin/topoforge web --check --workspace-root /tmp/topoforge-workspaces --input-root . --no-open
```

A TopoForge wheel by itself is not an offline installation bundle. The wheelhouse must
contain every resolved platform dependency, especially lib3mf and the geospatial wheels.

## Upgrade without changing retained evidence

1. Run `topoforge backup WORKSPACE --output BACKUP.zip` for each active workflow.
2. Keep the previous environment and wheelhouse until the new version passes
   `topoforge doctor` and a local smoke build.
3. Install 0.10.3 into a new environment rather than mutating the old one.
4. Resume or browse existing workspaces. Stage reuse remains checksum-bound.
5. Generate new outputs in new directories; existing DEMs and bundles remain immutable.

## Rollback

Installed CLI rollback keeps the 0.10.3 environment intact and switches back to 0.10.2:

```bash
~/.venvs/topoforge-0.10.2/bin/topoforge doctor
ln -sfn ~/.venvs/topoforge-0.10.2/bin/topoforge ~/.local/bin/topoforge
```

For a source checkout exactly at the 0.10.3 release tag:

```bash
scripts/rollback-topoforge-0.10.3.sh --confirm-rollback
```

Restore a workflow only from a checksum-verified backup:

```bash
topoforge restore BACKUP.zip --output NEW_WORKSPACE
topoforge browse NEW_WORKSPACE --no-open
```

No rollback command deletes retained DEMs, output bundles, caches, or the newer
environment.
