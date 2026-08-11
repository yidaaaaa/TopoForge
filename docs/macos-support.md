# Phase 13 macOS support matrix

Frozen: 2026-08-10

## Current truth

TopoForge does **not** currently claim macOS support. Linux x86_64 remains the only verified
platform and Windows Phase 12 is unfinished. Native hosted macOS core CI has passed on both frozen
arm64 runner labels as historical source-tree feasibility evidence, but clean-system,
application-package, Gatekeeper, signing/notarization,
first-launch, and Bambu Studio gates have not passed. The machine-readable contract is
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

- TopoForge remains CPython `>=3.12,<3.13`; Python.org provides a macOS universal2 installer for
  [Python 3.12.10](https://www.python.org/downloads/release/python-31210/). The final embedded arm64
  runtime is still unselected and must be checksum- and architecture-bound before packaging.
- `uv.lock` SHA-256 at freeze is
  `4db256ba2e4ffd8127d63b90afa00bb68224658e6a0dff39466151631e24c7e0`.
- With `MACOSX_DEPLOYMENT_TARGET=15.0`, wheel-only `uv` dry runs resolve 53 runtime packages and 67
  packages including development groups for `aarch64-apple-darwin`. The same runtime-only dry run
  also resolves for `x86_64-apple-darwin`, but that does not change the Intel disposition.
- The strongest current arm64 wheel floor among the locked direct native dependencies is macOS
  14.0 (`pyproj` and `rasterio`); the declared application floor remains 15.0 because only macOS 15
  and 26 are release candidates.

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

Hosted CI now passes the locked source environment, doctor, deterministic synthetic STL/3MF/GLB
generation and reopen, source-tree Web checks, worker recovery, and the full Python regression
suite on both runner labels. For each literal clean-system target, one release candidate must still
pass application-data and temporary-path behavior, paths with spaces and non-ASCII text,
backup/restore, native `TopoForge.app`, signed/notarized distribution, quarantine, normal
Gatekeeper first launch, and packaged worker/browser behavior. Phase 13B then adds the complete
official Bambu workflow. The common Phase 12 Darwin worker-identity fix must also be exercised
after integration: `/bin/ps -ww -o command=` must return the untruncated command line used by
PID-reuse, recovery, and cancellation protections.

The source-tree hosted suite must also be rerun after the audited Phase 12 foundation is integrated,
with commit, input-file, and report hashes. Until every matching report exists, README and
release metadata must continue to say macOS is unverified.
