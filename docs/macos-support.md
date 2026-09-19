# Phase 13 macOS support matrix

Frozen: 2026-08-10

## Current truth

TopoForge does **not** currently claim macOS support. Native hosted macOS core CI has passed, and
packaged-app CI has run on both frozen arm64 runner labels, but the first hosted unsigned candidate was disproved
by a real launch from a user account without a system Python Framework: its embedded `_ssl` still
loaded `/Library/Frameworks/Python.framework/Versions/3.12/lib/libssl.3.dylib`. The hosted runner's
preinstalled framework masked that non-self-contained dependency. The next hosted candidate closed
that Mach-O defect but was also disproved by a real land-AOI fetch: neither launcher selected the
locked `certifi` trust store, so embedded `urllib` could not validate the Copernicus AWS certificate.
Both candidates are invalid even though their hosted jobs passed. Clean-system, Gatekeeper,
signing/notarization, first-launch, and
Bambu Studio gates remain open. The machine-readable contract is
[`macos-support-matrix.json`](macos-support-matrix.json).

This document freezes Phase 13 release candidates; it does not widen README or package support
metadata.

## Frozen 0.12.x candidates

| Target | Phase 13A | Phase 13B | Clean-system capacity | Public status |
| --- | --- | --- | --- | --- |
| macOS Sequoia 15.7.9, Apple Silicon arm64 | historical source-tree pass; rerun required; overall unverified | planned, unverified | not provisioned | unsupported today |
| macOS Tahoe 26.6.1, Apple Silicon arm64 | historical source-tree pass; rerun required; overall unverified | planned, unverified | not provisioned | unsupported today |

The exact patch versions are the current Apple security releases at the freeze date. Later patch
versions do not inherit support automatically; the matrix and matching evidence must be updated.
[Apple's security release list](https://support.apple.com/100100) recorded Sequoia 15.7.9 and
Tahoe 26.6.1 on 2026-08-06.

The application deployment target is macOS 15.0. The locked CPython 3.12 dependency set contains
arm64 wheels compatible with that target. Resolution alone was only feasibility evidence. The
retained hosted run described below now proves native imports, doctor, source-tree Web behavior,
deterministic model generation, and worker recovery on the two hosted runner labels; it does not
prove a clean installation or packaged application.

## Explicitly excluded

- Intel x86_64 is unsupported for 0.12.x. Its locked wheels and current hosted runners establish
  feasibility only. There is no clean Intel acceptance system, package, Gatekeeper, recovery, or
  official Bambu automation evidence, so TopoForge will build an arm64-specific application rather
  than a universal application.
- macOS Sonoma 14.8.9 arm64 is outside the 0.12.x matrix. The GitHub macOS 14 runner is already in
  its deprecation period and is scheduled to become unsupported on 2026-11-02, while no separate
  clean-system capacity is available.
- macOS 27 beta is a preview OS and is unsupported. It cannot stand in for either stable target.

These exclusions are release decisions, not assertions that the source cannot run there.

## Runtime and dependency evidence

- TopoForge remains CPython `>=3.11,<3.15`; the Phase 13A candidate pins Python.org's official
  [Python 3.12.10](https://www.python.org/downloads/release/python-31210/) universal2 installer at
  `https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg`, exactly
  `45,720,356` bytes and SHA-256
  `8373e58da4ea146b3eb1c1f9834f19a319440b6b679b06050b1f9ee3237aa8e4`. The measured
  upstream primary Mach-O contains x86_64 (minimum 10.13) and arm64 (minimum 11.0); native
  builds must thin every applicable Mach-O to arm64 and retain the app deployment target of
  15.0. This identity is fixed build input, not evidence that a packaged candidate has run.
- `uv.lock` SHA-256 at freeze is
  `4db256ba2e4ffd8127d63b90afa00bb68224658e6a0dff39466151631e24c7e0`.
- With `MACOSX_DEPLOYMENT_TARGET=15.0`, wheel-only `uv` dry runs resolve 53 runtime packages and 67
  packages including development groups for `aarch64-apple-darwin`. The same runtime-only dry run
  also resolves for `x86_64-apple-darwin`, but that does not change the Intel disposition.
- The strongest current arm64 wheel floor among the locked direct native dependencies is macOS
  14.0 (`pyproj` and `rasterio`); the declared application floor remains 15.0 because only macOS 15
  and 26 are release candidates.

## Phase 13A unsigned application candidate

The local Phase 13A implementation builds only on native, non-Rosetta arm64 macOS 15 or 26. It
packages the pinned CPython runtime, the exact `uv.lock` production dependency set, TopoForge
Core/CLI, production Web assets, deterministic `Info.plist`, a CLI launcher, and a loopback-only
application launcher into `TopoForge.app`. It does not require a user-installed Python, `uv`,
Node, source checkout, or development environment. Durable defaults remain under
`~/Library/Application Support/TopoForge`.

The manifest and acceptance reports bind source commit, runtime, `uv.lock`, `pyproject.toml`,
build constraints, all package verifier hashes, the exact app payload, final archive, native OS,
and CPU architecture. Static validation enforces bounded exact archive closure, macOS
case/Unicode path uniqueness, internal symlink closure, ordinary-file/hard-link rules, deterministic
ZIP metadata, `Info.plist`, real arm64 Mach-O slices and deployment floors, every dynamic load
command and dylib identity, locked dependency inventory, and production Web assets. Before
archiving, every non-Apple dependency is resolved inside the bundle and rewritten to a canonical
`@loader_path` reference; build-machine `LC_RPATH` values are removed. Final verification allows
external images only under `/System/Library/` and `/usr/lib/` and never consults matching host
paths. Native acceptance clears all `DYLD_*` search fallbacks, explicitly exercises `ssl`, `_ssl`,
`hashlib`, `_hashlib`, `http.client`, and `urllib.request`, records dyld-loaded images, and rejects
any non-system image outside the extracted app. The replacement contract copies the CA collection
from the exact locked `certifi` dependency to an ordinary app resource, binds both copies to the
manifest, overrides all supported TLS CA environment inputs for CLI, Web, and inherited workers,
and completes a verified HTTPS request to the authoritative Copernicus catalog while hostile host
CA variables point outside the app. It also starts from a path containing spaces and
non-ASCII text, executes doctor and `web --check`, rejects external host arguments, runs a
synthetic worker job plus a real small-land Copernicus Web provider job, strictly reopens
STL/3MF/GLB, restarts and cancels a recovered worker, and backs up and restores the completed
project.

The workflow definition builds the same source twice, requires byte-identical archives, retains
one checksum-bound unsigned candidate, and sends that exact archive SHA to macOS 15 and macOS 26
hosted-package acceptance jobs. Its results are `hosted-package` evidence only: they cannot
establish clean-system state, signing, notarization, quarantine/Gatekeeper first launch, public
support, or Phase 13B Bambu Studio automation.

### Invalidated hosted candidates

[macOS workflow run 31670153746](https://github.com/yidaaaaa/TopoForge/actions/runs/31670153746)
completed its hosted gates for source
`ce1b5dabc9f5a69dcf2c64536076fc88827112fb`. It produced archive SHA-256
`7036ea340734d284b9b406b43dbb9547ba6e28186fe16aee4433a5e4ff0c6e78` and app-payload SHA-256
`580fd9c2911117958ca15658814db33769fb8577d329b1c413f23782bdb6fa13`. A subsequent real user
launch without `/Library/Frameworks/Python.framework` failed while importing `_ssl`. Complete
load-command enumeration found 14 absolute Python Framework load edges across the framework
executables, OpenSSL, curses/panel, and Tk extensions, plus build-host RPATHs. This result
invalidates the archive for all uses; it must not be renamed, promoted, or treated as usable
package evidence. Historical artifacts remain retained according to GitHub policy so the failure
record is not erased.

[macOS workflow run 31731949652](https://github.com/yidaaaaa/TopoForge/actions/runs/31731949652)
then completed hosted gates for source
`a5366a8ea2c47e204fe8c933852f3e146b3e69d7`, producing archive SHA-256
`d745e5d22a2e7e3862a263f2763efdc3232d8643bcdd9887c7e05c69b5b5c4bc` and app-payload SHA-256
`bb24493c3cf8863beffbbfe2ba7f144137c14dacfb32cbeb3f63770fc71b4eb5`. It closed the Mach-O
dependency defect, but a real user land-AOI acquisition still failed with the generic provider
selection error. Diagnostic run
[31738905128](https://github.com/yidaaaaa/TopoForge/actions/runs/31738905128) reproduced the raw
candidate-Python failure on both macOS 15 and 26 with host CA paths made unavailable:
`ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED` (`unable to get local issuer
certificate`), wrapped by `urllib.error.URLError`. The archive contained locked `certifi` CA bytes,
but neither launcher selected them. This second archive is also unusable and retained only as
historical failure evidence.

## CI and clean-system capacity

[GitHub's runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
provides native M1 arm64 `macos-15` and `macos-26` labels. These labels are CI implementation
details, not the support contract, and runner images can use patch versions different from the
clean-system targets. GitHub also states that nested virtualization is unavailable on arm64 macOS
runners, so hosted CI cannot create the required clean-system or Gatekeeper evidence.

[Native macOS core CI run 31419016599](https://github.com/yidaaaaa/TopoForge/actions/runs/31419016599)
completed successfully at commit `5df03c40536363d63678f0b23b69b228ee008e6a` on both `macos-15`
and `macos-26`. It verified native arm64 identity, the locked all-groups environment, all direct
native dependency imports, doctor, deterministic STL/3MF/GLB generation and reopen checks, the
source-tree Web asset and configuration check, Ruff, formatting, Pyright, all 273 Python tests, and
recovered-worker cancellation. The retained artifacts are `macos-macos-15-runtime-evidence` and
`macos-macos-26-runtime-evidence`.

This historical run is feasibility evidence only and does not close any Phase 13A item. It used
shared foundation `da6999101ee28b1309798e66900edc6b53052d48`, before the current Phase 12 audit
fixes. It must be rerun after the audited Phase 12 fixes are integrated. The uploaded JSON
files had a 30-day retention window and no retained SHA-256 identities, so they are not durable
release evidence.
Hosted runner labels and their image patch versions remain implementation evidence, not public
support targets.

Phase 13 therefore still requires clean Apple Silicon installations at Sequoia 15.7.9 and Tahoe
26.6.1. No such capacity, Developer ID signing identity, notarization credentials, quarantine
download path, or normal first-launch record is currently provisioned.

## Official Bambu Studio boundary

Bambu Lab publishes one current macOS DMG for
[Bambu Studio 02.07.01.62](https://github.com/bambulab/BambuStudio/releases/tag/v02.07.01.62):

- asset: `Bambu_Studio_mac-v02.07.01.62-20260616174358.dmg`
- size: `283034195` bytes
- GitHub release digest:
  `1e54c25aefc5249d56b63711cf773bed56f14430aafcc34340cd4894aef15896`

The vendor's [download page](https://bambulab.com/download/studio) lists macOS 10.15 or later, and
the official [CLI documentation](https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage)
describes the batch options TopoForge uses. Availability is not Phase 13B evidence. The DMG has not
been installed or invoked on either target, profiles have not been resolved, and no normative
slice, project export, standalone reopen, or reslice has passed on macOS.

`02.07.01.62` is only the frozen expected identity. Project evidence must derive the version from
the exact executable probe and independently parse both the primary and reopened G-code; all three
values must match the source slice manifest. The doctor command reports `.app` discovery path, probe
version, and availability but keeps automation support explicitly `unverified`.

Phase 13A covers Generic Core STL/3MF/GLB plus manual import. Phase 13B remains a separate gate for
automatic `.app` discovery, official profile provenance, normative slicing, project export, and
reopen/reslice on both advertised targets.

## Gates before any support claim

Historical hosted source-core CI passed the locked source environment, doctor, deterministic
synthetic STL/3MF/GLB generation and reopen, source-tree Web checks, worker recovery, and the then
current Python regression suite on both runner labels. A packaged-app workflow also passed, but
its archive was invalidated by the clean-user dynamic-library failure described above.
For each literal clean-system target, the same checksum-bound release candidate must still
pass application-data and temporary-path behavior, paths with spaces and non-ASCII text,
backup/restore, native `TopoForge.app`, signed/notarized distribution, quarantine, normal
Gatekeeper first launch, and packaged worker/browser behavior. Phase 13B then adds the complete
official Bambu workflow. The common Phase 12 Darwin worker-identity fix must also be exercised
after integration: `/bin/ps -ww -o command=` must return the untruncated command line used by
PID-reuse, recovery, and cancellation protections.

The source-tree hosted suite must also be rerun after the audited Phase 12 foundation is integrated,
with commit, input-file, and report hashes. Until every matching report exists, README and
release metadata must continue to say macOS is unverified.
