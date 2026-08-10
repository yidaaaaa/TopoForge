# Phase 13 macOS support matrix

Frozen: 2026-08-10

## Current truth

TopoForge does **not** currently claim macOS support. Linux x86_64 remains the only verified
platform, Windows Phase 12 is unfinished, and the native macOS CI, clean-system, application,
Gatekeeper, signing/notarization, recovery, and Bambu Studio gates have not passed. The
machine-readable contract is [`macos-support-matrix.json`](macos-support-matrix.json).

This document freezes Phase 13 release candidates; it does not widen README or package support
metadata.

## Frozen 0.12.x candidates

| Target | Phase 13A | Phase 13B | Clean-system capacity | Public status |
| --- | --- | --- | --- | --- |
| macOS Sequoia 15.7.9, Apple Silicon arm64 | planned, unverified | planned, unverified | not provisioned | unsupported today |
| macOS Tahoe 26.6.1, Apple Silicon arm64 | planned, unverified | planned, unverified | not provisioned | unsupported today |

The exact patch versions are the current Apple security releases at the freeze date. Later patch
versions do not inherit support automatically; the matrix and matching evidence must be updated.
[Apple's security release list](https://support.apple.com/100100) recorded Sequoia 15.7.9 and
Tahoe 26.6.1 on 2026-08-06.

The application deployment target is macOS 15.0. The locked CPython 3.12 dependency set contains
arm64 wheels compatible with that target. This is dependency-resolution evidence only, not proof
that native imports, GDAL/PROJ data lookup, lib3mf, Web workers, cancellation/recovery, or packaged
launch behavior work.

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

Phase 13A covers Generic Core STL/3MF/GLB plus manual import. Phase 13B remains a separate gate for
automatic `.app` discovery, official profile provenance, normative slicing, project export, and
reopen/reslice on both advertised targets.

## Gates before any support claim

For each target, one release candidate must still pass locked installation, doctor, deterministic
synthetic STL/3MF/GLB generation, strict 3MF reopen, measured orientation/topology, packaged Web
checks, worker start/cancel/forced termination/recovery, paths with spaces and non-ASCII text,
backup/restore, native `TopoForge.app`, signed/notarized distribution, quarantine, and normal
Gatekeeper first launch. Phase 13B then adds the complete official Bambu workflow.

Until every matching report exists, README and release metadata must continue to say macOS is
unverified.
