# Windows acceptance boundary

TopoForge does not currently claim native Windows support. Hosted Windows Server CI is
contract and packaging evidence only. It is not Windows 10 or Windows 11 client evidence,
and `TopoForge-Web.cmd --check --no-open` is not proof that Uvicorn served the application,
a Web job completed through HTTP, or the default browser opened.

The 0.10.3 Windows ZIPs built during Phase 12 are rehearsal artifacts. They share the
published Linux release version while containing later source changes, and therefore cannot
close a 0.11.0 or 0.11.1 gate. A release gate must use the exact final 0.11.x archive and
source commit that appear in both clean-client reports.

## Hosted evidence

Hosted Server execution must be explicit and remains non-release evidence:

```powershell
uv run python scripts/verify_windows_portable.py `
  --archive dist/windows-primary/topoforge-VERSION-windows-x64-portable.zip `
  --expected-version VERSION `
  --execute `
  --hosted-server `
  --browser-mode skip `
  --work-root artifacts/windows-server-acceptance `
  --report artifacts/logs/windows-server-portable.json
```

The resulting `execution.windows_target.target_verified` is `false`. Browser mode `skip`
records that no browser evidence was collected; it must never be promoted to a clean-VM
browser result.

## Clean Windows client evidence

Run the identical final archive and 40-hex source commit separately on Windows 10 22H2 x64
and Windows 11 x64. Use a fresh work root for every run. The verifier starts the candidate's
actual `TopoForge-Web.cmd`, waits for `/api/v1/health`, loads `/`, submits a synthetic job by
HTTP, polls it to completion, downloads `model_3mf`, verifies its JobRecord SHA-256, strictly
reopens it, stops the server tree, and proves the loopback port closed. Browser mode `require`
also requires the operating system's default-browser launch API to report success; the
launcher's controlled `--no-open` server argument is recorded separately and is not browser
evidence.

Each clean run writes two reports. `--report` is the private, path-bearing machine report and
must stay in the access-controlled workflow artifact. `--public-report` is a schema-bounded,
redacted projection suitable for tracking only after the release verifier proves it is a
faithful projection of that exact private report. Neither report may be produced from the
release tag itself as a substitute for the independent clean runner.

```powershell
$commit = "0123456789abcdef0123456789abcdef01234567"

uv run python scripts/verify_windows_portable.py `
  --archive dist/windows-primary/topoforge-VERSION-windows-x64-portable.zip `
  --expected-version VERSION `
  --execute `
  --expected-target win10-22h2 `
  --expected-source-commit $commit `
  --browser-mode require `
  --work-root "C:\TopoForge Evidence\Win10 22H2 地形" `
  --report "$env:RUNNER_TEMP\topoforge-win10-private.json" `
  --public-report artifacts/logs/windows-10-portable.public.json

uv run python scripts/verify_windows_portable.py `
  --archive dist/windows-primary/topoforge-VERSION-windows-x64-portable.zip `
  --expected-version VERSION `
  --execute `
  --expected-target win11 `
  --expected-source-commit $commit `
  --browser-mode require `
  --work-root "C:\TopoForge Evidence\Win11 地形" `
  --report "$env:RUNNER_TEMP\topoforge-win11-private.json" `
  --public-report artifacts/logs/windows-11-portable.public.json
```

The target gate reads `ProductName`, `DisplayVersion`, `CurrentBuildNumber`, `UBR`, and
`InstallationType` from the Windows registry. It also calls `IsWow64Process2` and requires a
native AMD64 host and process; x86/WOW and x64 emulation on ARM64 are rejected. Windows
10 requires Client, 22H2, and build
19045. Windows 11 requires Client and build 22000 or newer. `ProductName` remains recorded, but
a literal `Windows 11` value is not required because that compatibility registry value can
remain `Windows 10` on Windows 11.
Windows Server is rejected for either target. Declaring `--expected-target` itself makes native
Windows x64 mandatory; omitting `--require-windows` cannot turn a target run into
contract-only evidence.

Each report binds the archive SHA-256/byte count, clean source commit, runtime-config hash,
build-constraints hash, project-wheel hash, and builder/portable/system/Bambu/helper verifier
hashes. Nested reports reopen that binding and the parent verifier cross-checks their target
registry values. Any source
working-tree change (including untracked files), commit mismatch, archive change, config
change, verifier change, nested target
change, or artifact checksum change fails acceptance.

Publication also downloads the canonical Linux x86_64/Python 3.12 core report uploaded by
the same successful `ci` run. It requires exact Linux/Win10/Win11 manufacturing dimensions,
volume, triangles, topology, orientation, artifact roles/determinism, and strict reopen
results. The version-specific rollback script must pre-exist in the candidate, and a tracked
verification report must prove installed and source rollback preserve retained evidence. Raw
machine reports remain outside the wheel and sdist.

On native Windows, system acceptance also launches an isolated worker that calls
`enable_current_process_containment` before spawning a child. One scenario lets the worker
exit normally and requires Job Object kill-on-close to remove the child; another cancels the
leader through the production termination adapter and again requires the child to disappear.
The process identities, probe hash, candidate-binding hash, source commit, and system-verifier
hash are recorded. A skipped non-Windows probe is explicitly not Job Object evidence.

## Official Bambu Studio evidence

Do not call an executable official merely because it is named `bambu-studio.exe` or exists
under Program Files. Before executing the binary, the verifier requires Authenticode status
`Valid`, exactly one operator-frozen publisher subject, and exactly one certificate
thumbprint. Obtain these values from the official installer on the evidence system; do not
guess or add an unmeasured publisher allowlist to the repository.

Add the frozen identity to each clean-target command:

```powershell
  --verify-bambu `
  --expected-publisher-subject "EXACT SIGNER SUBJECT FROM ACCEPTED INSTALLER" `
  --expected-certificate-thumbprint "EXACT40HEXTHUMBPRINTFROMINSTALLER" `
  --expected-profile-content-identity-sha256 $profileContentIdentitySha `
  --expected-machine-profile-sha256 $machineProfileSha `
  --expected-process-profile-sha256 $processProfileSha `
  --expected-filament-profile-sha256 $filamentProfileSha
```

All six identity inputs are mandatory for a clean Bambu target. Obtain the path-independent
profile content identity and the three resolved-profile hashes from a preliminary non-target
observation of the same signed official installation, freeze them in the separately approved
Phase 12B identity policy, and then rerun both clean targets. The authoritative report fields
are `bambu_studio.profiles_root_binding.profile_content_identity_sha256` and
`resolved_profiles.{machine,process,filament}.sha256`.

The clean verifier requires the profile root to resolve exactly to
`resources/profiles/BBL` beside the authenticated executable. It reopens every dependency
source record, checks its relative path, size, and SHA-256, records a canonical source-record
identity, and rechecks the whole binding after slicing. Normally omit
`--bambu-profiles-root`. An explicit CLI or environment override is accepted only with all
four frozen content hashes and still cannot replace the executable-sibling requirement for clean
release evidence.

Authenticode and the frozen profile identity do not replace the existing official P2S
parameter, slice, project export, no-external-profile reopen, and reslice gates. Windows 10
and Windows 11 must pass with the same archive, source commit, signed executable identity,
profile content identity, and resolved machine/process/filament hashes before any Windows Bambu
automation claim is made.
