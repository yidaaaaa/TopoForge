# Architecture Decisions

## ADR-001 — Python-first core and thin adapters

- **Date:** 2026-07-31
- **Context:** CLI, API, and Web must share geospatial/manufacturing behavior.
- **Decision:** Implement one typed Python package; CLI/API/worker layers call the same build service.
- **Alternatives:** Separate implementations; an early Rust core.
- **Impact:** Lower duplication and faster correctness work; Rust remains an evidence-driven optimization.

## ADR-002 — Trimesh plus explicit regular-grid construction

- **Date:** 2026-07-31
- **Context:** Phase 1 needs auditable closed rectangular terrain meshes.
- **Decision:** Construct top, walls, and base explicitly with NumPy and use Trimesh for serialization and independent geometric measurements. Do not rely on repair to create missing topology.
- **Alternatives:** Blender GUI, OpenSCAD, implicit surfaces.
- **Impact:** Deterministic topology, headless operation, and precise winding tests.

## ADR-003 — Rasterio/PyProj geospatial boundary

- **Date:** 2026-07-31
- **Context:** GeoTIFF CRS, transforms, NoData, reprojection, and output metadata must remain explicit.
- **Decision:** Use Rasterio/GDAL and PyProj; meshes are generated only after conversion to a local metric grid.
- **Alternatives:** Hand-written TIFF/CRS parsing; xarray/rioxarray as mandatory core dependencies.
- **Impact:** Mature metadata handling with a smaller Phase 1 surface.

## ADR-004 — Unknown vertical datum remains unknown

- **Date:** 2026-07-31
- **Context:** Many GeoTIFFs omit vertical reference metadata.
- **Decision:** Preserve an explicit `unknown` datum instead of guessing; surface it in validation/provenance.
- **Alternatives:** Infer ellipsoidal/orthometric datum from horizontal CRS or region.
- **Impact:** Honest semantics; later providers can supply verified transformations.

## ADR-005 — Official lib3mf is the manufacturing 3MF writer

- **Date:** 2026-07-31
- **Context:** 3MF requires explicit units, metadata, deterministic identifiers, strict reread, and future multi-object support.
- **Decision:** Pin `lib3mf==2.5.0`, derive UUIDv5 identifiers from canonical geometry/metadata, strict-write/read with zero warnings, harden the OPC ZIP, and independently compare XML topology/dimensions.
- **Alternatives:** Hand-written Core XML package; Trimesh exporter.
- **Impact:** Reference implementation and extension path at the cost of a native wheel dependency; Linux ARM64 packaging remains future work.

## ADR-006 — Slicer adapter preference and truth boundary

- **Status:** Superseded for the default production release by ADR-009; retained for diagnostic fallback.
- **Date:** 2026-07-31
- **Context:** Slicer availability and CLI flags differ by release/host.
- **Decision:** Prefer a working OrcaSlicer adapter and fall back to PrusaSlicer. Execute argument arrays without a shell, publish G-code atomically, parse the G-code itself, and embed literal version/command/output/exit/metrics into the build.
- **Alternatives:** Trimesh reopen as a proxy; one hard-coded slicer path.
- **Impact:** Real manufacturing evidence remains separate from geometry validation and degrades explicitly by availability.

## ADR-007 — Copernicus AWS is the planned no-key global route

- **Date:** 2026-07-31
- **Context:** Phase 3 needs a legal, current, no-private-key global land source.
- **Decision:** Implement configurable Copernicus DEM AWS 2021 COG mirrors, GLO-30 first and GLO-90 fallback, with DSM semantics and exact Copernicus notices.
- **Alternatives:** CDSE authenticated access, NASADEM Earthdata, OpenTopography personal key, legacy terrain PNGs.
- **Impact:** No account for the default path; mirror release/completeness must remain explicit and ocean gaps require a separate policy/provider.

## ADR-008 — Crop reprojection-only gaps, preserve source NoData

- **Date:** 2026-07-31
- **Context:** Normalizing a rotated raster to a north-up grid creates corner cells outside the source footprint.
- **Decision:** Reproject a separate source-coverage mask, crop to its largest all-covered rectangle, then apply the normal source-NoData policy inside that crop.
- **Alternatives:** Interpolate corners; reject every rotated raster; zero-fill outside coverage.
- **Impact:** Rectangular Phase 1 models stay evidence-backed without confusing geometric reprojection gaps with source observations.

## ADR-009 — Official Bambu Studio and dual 3MF roles define the default P2S release boundary

- **Date:** 2026-07-31
- **Context:** The default printer must be Bambu Lab P2S 0.4. Official leaf preset JSON files can return success while omitting inherited/include fragments and falling back to a generic 200 x 200 mm platform. Bambu project 3MF files also contain vendor parts outside the Core-only lib3mf source contract.
- **Decision:** Default builds to bambu-p2s-0.4. Flatten official machine/process/filament presets in parent/include/child order, run official Bambu Studio with normative checks, assert resolved machine and print parameters from generated G-code, export a separate model.bambu-p2s.3mf, then reopen that project without external profiles and slice it again. Keep interoperable model.3mf unchanged as the strict lib3mf geometry source. OrcaSlicer and PrusaSlicer remain diagnostic.
- **Alternatives:** Keep generic FDM as default; trust leaf preset names or exit code alone; use OrcaSlicer/PrusaSlicer as release evidence; replace the Core 3MF with the Bambu project.
- **Impact:** P2S releases have reproducible official-tool and parameter evidence, while portability and strict Core 3MF validation remain intact. Builds store two intentional 3MF artifacts and a larger Bambu validation record.

## ADR-010 — Printer-aware sampling is explicit and resource-bounded

- **Date:** 2026-07-31
- **Context:** A fixed cell budget reduced a nominal ~30 m DSM to ~89 m without enough explanation, while unconditional source preservation can create meshes finer than the printer can reproduce.
- **Decision:** Provide `print-aware`, `source-preserving`, and `custom` modes. Resolve print-aware spacing from preferred mesh sampling, nozzle diameter, minimum feature size, source/model scale, max cells, exact triangle count, and estimated memory. Never upsample beyond source data.
- **Impact:** The real reference resolves to 0.40018 mm physical spacing and 68.9589 m processed resolution. Source and processed claims are separate, resource limits are deterministic, and source-preserving limitations emit explicit warnings.

## ADR-011 — Manufacturing axes are +X East, +Y North, +Z Up

- **Date:** 2026-07-31
- **Context:** North-up raster row 0 previously mapped to y=0, making +Y point south in viewers.
- **Decision:** Flip the processed heightfield once before mesh construction. Record the transform/source bounds in provenance and 3MF metadata; verify asymmetric corners and peak coordinates after STL, GLB, and 3MF serialization.
- **Impact:** North is the maximum-y edge without east/west mirroring, winding reversal, negative volume, or terrain alteration.

## ADR-012 — AOIs normalize in WGS84 before source-pixel clipping

- **Date:** 2026-07-31
- **Context:** Provider and local builds need one explicit bbox/center-radius contract, including dateline, high-latitude, cross-zone, partial-overlap, and empty-result behavior.
- **Decision:** Normalize user input to recorded WGS84 bounds/geometry. Use UTM only for a single mid-latitude zone and local AEQD otherwise. Clip local rasters to an explicit pixel window before metric reprojection; record full/partial coverage and reject empty/NoData-only results.
- **Impact:** Local AOI behavior is complete and testable. Global providers must consume this contract rather than implementing a parallel download-first AOI path.


## ADR-013 — Provider bytes are content-addressed and network requests are bounded

- **Date:** 2026-07-31
- **Context:** A no-key provider needs reproducible source identities, corruption detection, operational limits, and offline tests before global downloads enter the manufacturing engine.
- **Decision:** Index canonical provider/dataset/version/URL requests separately from immutable SHA-256 objects; atomically publish and reopen both. Enforce timeout, bounded attempts, exponential backoff, minimum request spacing, and byte limits. Preserve ETag, Last-Modified, byte count, attempts, cache state, and content hash.
- **Impact:** Cache hits are verified rather than trusted by filename, corrupt content is refetched, duplicate content is deduplicated, and network behavior has testable upper bounds.

## ADR-014 — Copernicus AWS plans one complete product through normalized AOIs

- **Date:** 2026-07-31
- **Context:** Filename formulas do not establish coverage, partial GLO-30/GLO-90 blending obscures dataset identity, and projected AOI rectangles can contain cells outside source footprints.
- **Decision:** Treat `tileList.txt` as coverage authority; use GLO-30 only when every required one-degree cell exists, otherwise use complete GLO-90 or report incomplete coverage. Reproject source-footprint masks independently, crop only reprojection gaps, preserve source NoData, and enter the resulting metric raster plus `source_acquisition.json` through the local build engine.
- **Impact:** `build-global` shares local AOI/sampling/orientation/validation behavior, source product identity remains explicit, and the real Amazon tile-edge case is documented rather than filled.


## ADR-015 — Copernicus quality rasters remain source evidence, not elevation edits

- **Date:** 2026-08-01
- **Context:** The public Copernicus AWS tile prefixes expose EDM, FLM, HEM, and WBM rasters, but filenames alone do not prove availability and categorical/error values must not be converted into fabricated terrain.
- **Decision:** Cache and parse the exact per-tile S3 ListObjectsV2 response. Download only listed ancillary objects through the normal content-addressed transport, require each source raster to match its DEM tile CRS/transform/shape, retain raw values, use nearest-neighbour reprojection, and crop with the exact DEM target window. Publish a composite only when every selected tile exposes the role; otherwise record `absent` or `incomplete`. Copy complete aligned rasters into build bundles as checksummed source-quality roles.
- **Impact:** Quality/editing/filling/water/error evidence becomes reproducible and self-contained while terrain elevations and geometry remain unchanged. The final Amazon DEM/STL/3MF/GLB/PNG are byte-identical to the pre-mask bundle.


## ADR-016 — Provider selection is a deterministic recorded decision

- **Date:** 2026-08-01
- **Context:** Direct CLI construction of one provider hid unimplemented/rejected candidates and could not prove fetch fallback behavior. Resolution alone is not a sufficient provider choice.
- **Decision:** Evaluate the explicit registry under a typed policy. Licence suitability, requested semantics unless fallback is explicit, coverage, credentials, vertical datum, and resource limits are hard requirements. Rank eligible providers deterministically by semantics, completeness, resolution, credential penalty, operational risk, preference, registry order, and id. Record every evaluation and fetch attempt. Continue after a fetch error only when no destination evidence was published. Copy the exact acquisition trace into build provenance.
- **Impact:** Auto and explicit provider modes are explainable and repeatable. Fake providers verify multi-provider fallback offline while production currently instantiates Copernicus AWS. Partial evidence is preserved rather than overwritten.

## ADR-017 — Place names resolve to explicit cached candidates before AOI normalization

- **Date:** 2026-08-01
- **Context:** Same-name places make automatic first-result selection unsafe, and public Nominatim has usage, attribution, rate, and caching requirements.
- **Decision:** Use bounded Nominatim-compatible JSONv2 search with canonical cached URLs. Enforce a one-second minimum interval for the public endpoint, no autocomplete, and OSM attribution. Return zero/unique/ambiguous states. Ambiguous results require a returned candidate id. Preserve query, candidate id, display name, and exact bbox in a network-free resolved-place AOI, then run the existing normalization/provider/build pipeline.
- **Impact:** Place inputs cannot silently choose the wrong feature. Repeated search is cached, acquisition provenance includes search evidence, and bbox/center/place converge on one AOI contract.
## ADR-018 — Manufacturing preflight is a reusable core contract with explicit resource modes

- **Date:** 2026-08-01
- **Context:** Printer-aware sampling estimated cells/triangles/memory, but users could not inspect the resolved printer fit and vertical policy without generating a complete bundle. Resource-limit reductions also needed an explicit reject-versus-adapt policy.
- **Decision:** Add `resource_budget_mode: adapt | strict` and an optional exact triangle ceiling. Resolve cells, triangle topology, and memory into one deterministic sampling limit. Expose `preflight_local_terrain()` and `topoforge preflight` using the production raster/scaling path in a temporary directory. Every build publishes and cross-checks a typed `manufacturing_preflight.json` with printer volume/headroom, resource pass/utilization fields, physical spacing, vertical exaggeration, warnings, and suggested actions.
- **Alternatives:** Always preserve source pixels; always silently adapt; estimate only after mesh allocation; duplicate preflight logic in future API/Web adapters.
- **Impact:** Oversized or expensive requests are explainable before export, strict workflows fail early with corrective values, adapt workflows remain deterministic, and CLI/API/Web can share one manufacturing-resource contract.
## ADR-019 — Tile layouts partition cells with north/west stable identities and explicit overlap

- **Date:** 2026-08-01
- **Context:** Multi-tile printing needs stable cache/manifest keys, no seam gaps, and an unambiguous relationship between raster row order and manufacturing axes before mesh assembly or API workers exist.
- **Decision:** Define `topoforge-tile-layout-v1` over processed sample grids. Partition half-open cell windows from row 0 north to south and column 0 west to east; distribute remainder cells deterministically from the north/west origins. Tile IDs are fixed-width `tile-r####-c####` and keys are namespaced by a deterministic layout digest. Core sample windows share seam samples; `sampling_window` adds a clipped `overlap_cells` halo. Physical bounds use +X East/+Y North and are recorded in millimetres. Canonical JSON is sorted/separator-stable and strictly reopened against a recomputed layout.
- **Alternatives:** Slippy-map XYZ IDs; independent per-tile resampling; floating-point geographic bounds as primary identity; overlap inferred at mesh time.
- **Impact:** Tile cache, per-tile provenance, seam checks, assembly manifests, worker jobs, and Web maps can share one byte-stable layout contract. DEM extraction and multi-tile mesh assembly remain subsequent steps.

## ADR-020 — Tile extraction is source-window exact and manifest-bound before mesh assembly

- **Date:** 2026-08-01
- **Context:** A stable layout alone could not prove that independently processed tile jobs preserved the source DEM/mask, coordinate transforms, provenance identity, or deterministic bytes. API workers must not consume partial or path-unsafe tile outputs.
- **Decision:** Verify every checksum in the source build manifest before extraction. Crop each tile's declared `sampling_window` directly from the processed DEM and original NoData mask without resampling. Preserve CRS and window transforms; bind the raw source DEM, processed DEM, source manifest, layout, coverage map, tile files, and tile manifests by SHA-256. Publish canonical JSON through a staging directory, reject overwrite/unsafe paths, strictly reopen every GeoTIFF/JSON, recompute validation measurements, and compare extracted arrays to exact source windows before atomic publication. Expose the same verifier to CLI and future workers.
- **Alternatives:** Reproject/resample each tile independently; trust only the root assembly JSON; publish partial directories and validate later; let API/Web reconstruct provenance from filenames.
- **Impact:** Tile artifacts are cacheable, portable, deterministic, and independently auditable. The retained Gongga primary/repeat extraction is 23/23 files byte-identical. Numerical seam reporting and tile mesh/connector contracts remain explicit later gates rather than being implied by successful raster extraction.

## ADR-021 — Numerical tile seams are deterministic assembly-bound evidence

- **Date:** 2026-08-01
- **Context:** Exact source-window extraction strongly suggests continuity, but it does not independently expose adjacency coverage, transform drift, mask divergence, or later worker tampering. Mesh assembly must start from a published, repeatable raster-continuity gate.
- **Decision:** Enumerate every east and south adjacency once in stable row-major order. Compare both the shared core boundary and complete overlap rectangle for elevation and original NoData-mask equality, require matching CRS, and measure transform-coordinate alignment at overlap corners in the metric CRS. Use exact `0.0 m` elevation tolerance for direct source windows and a `1e-9 m` transform threshold. Publish canonical `topoforge-tile-seam-report-v1`, bind its SHA-256 from the assembly manifest, and remeasure it during strict tile-set verification. Preserve legacy manifests only when both optional seam fields are absent and report their status as `not-reported`.
- **Alternatives:** Infer continuity from source hashes only; compare only the single shared line; permit an undocumented floating tolerance; wait until mesh assembly to discover raster drift.
- **Impact:** Worker and assembly stages receive explicit deterministic raster-continuity evidence. The retained Gongga v2 tile set passes four seams with zero elevation/mask mismatch and zero transform error, and all 24 roles repeat byte-for-byte. This decision does not claim mesh assembly or connector correctness.

## ADR-022 — Tile solids preserve one global manufacturing frame before print placement

- **Date:** 2026-08-01
- **Context:** Raster continuity alone cannot prove that independently exported solids retain orientation, Z mapping, boundary heights, footprint coverage, or total terrain volume. Immediately localizing every tile to `(0,0)` would also discard the common frame needed for assembly verification.
- **Decision:** Derive geometry only from each tile's `core_sample_window`; overlap halos remain evidence for seam/connector work. Reuse the exact source `ScalingResult`, flip north-up rows once, build a closed flat-bottom solid, and translate it to the layout's global physical bounds. Publish STL, strict 3MF, and GLB per tile with source/hash/orientation metadata. Verify reopened per-format bounds, peaks, topology, and triangle counts; compare adjacent reopened STL top boundaries; require exact footprint partition, global bounds, and summed-volume agreement with the source STL; bind a deterministic north/east coverage PNG. Keep later print-local transforms separate and explicit.
- **Alternatives:** Build overlap halos as duplicate solids; independently rescale each tile; export only local-origin print files; infer assembly from raster seams; boolean-union all tiles before validation.
- **Impact:** Assembly geometry is deterministic and auditable without changing terrain or losing global identity. The retained Gongga mesh set has four zero-gap seams, zero footprint overlap, a 0.000748639 mm3 volume difference, strict 3MF passes, and 24/24 repeat-byte roles. Connector fit and per-tile slicing remain separate evidence stages and are resolved by ADR-023/ADR-024 without mutating this global-frame contract.

## ADR-023 — Printer-derived bottom dovetails preserve terrain and assembly truth

- **Date:** 2026-08-01
- **Context:** Global-frame tiles proved exact seams but had no mechanical alignment or print-local placement. Connector geometry must not change terrain tops, duplicate overlap solids, or make global assembly evidence printer placement truth.
- **Decision:** Give west/north tiles deterministic male ownership and east/south tiles bottom-open female cavities. Derive all dimensions from nozzle, layer, minimum feature, base, wall, and `connector_tolerance_mm`; define that tolerance as total lateral clearance split per side. Restrict booleans below the base top, retain immutable global source meshes, emit separate global connector and reversible print-local roles, and verify actual male/cavity/collision volumes plus topology, bed contact, walls, build volume, metadata, and top-surface identity.
- **Alternatives:** Duplicate overlap halos as solids; fabricate terrain features; use unverified fixed-size pins; discard the global frame; rely on metadata without physical fit probes.
- **Impact:** Connector identity, polarity, clearance, and placement are deterministic and worker-cacheable. The real eight-connector set has 0.0 mm top deviation/collision and 37/37 repeat bytes. Bottom-open cavities intentionally add recessed downward faces, so bed-contact planarity is measured separately from the legacy all-downward-face flat-bottom metric.

## ADR-024 — Print-local slicing and Bambu project roles are separate source-bound stages

- **Date:** 2026-08-01
- **Context:** Global-offset assembly meshes must not be sent directly to a slicer, and slicer exit code alone does not prove P2S settings or a self-contained vendor project.
- **Decision:** `tile-slice` accepts only print-local 3MF roles, copies/hashes exact profiles, invokes every tile, reparses G-code, rejects out-of-bed/empty/floating/support results, and applies the full official P2S parameter gate. Preserve the strict lib3mf print-local 3MF and export a second Bambu project 3MF; verify ZIP/embedded G-code MD5 and no-external-profile reopen/reslice separately.
- **Alternatives:** Slice global-frame tiles; treat PrusaSlicer as the P2S release boundary; replace the interoperable 3MF; skip project reopen; trust only result.json or exit code.
- **Impact:** Phase 5 provides portable geometry, assembly truth, G-code evidence, and self-contained P2S project roles without conflating them. These frozen artifact contracts are inputs to Phase 6 workers/API.

## ADR-025 — Single-user local completion precedes API and Web

- **Date:** 2026-08-01
- **Context:** The project is currently operated by one person on one local workstation. FastAPI, workers, queues, persistence, authentication, and Web deployment add maintenance without improving the validated CLI manufacturing path.
- **Decision:** Preserve the API/Web design as deferred Phase 9. Complete resumable one-command local orchestration, local configuration/report UX, overlays, recovery, packaging, CI, and offline documentation first. Physical connector calibration remains a saved, non-blocking evidence task. A future API must wrap the same core contracts and may start only after the local software product is frozen.
- **Alternatives:** Begin FastAPI immediately; cancel API permanently; build Web logic that duplicates the Python core.
- **Impact:** Current work targets local software utility without waiting for hardware measurements. No API or physical-validation plan is lost, but neither blocks near-term progress.

## ADR-026 — Local workflow stages are content-addressed and strictly reopened

- **Date:** 2026-08-01
- **Context:** Manually invoking build and five tile commands is error-prone, while overwriting stage directories would discard manufacturing evidence and a file-exists cache would trust corruption.
- **Decision:** `topoforge run` calls the existing Python core functions directly. Every stage path is keyed by its effective settings and upstream manifest SHA-256 values; reuse requires the production strict verifier. Changed inputs create another stage path. Canonical request/manifest/status and retained failure records support resume. Slicing and Bambu project export are explicit software-only stages; physical printing remains separate.
- **Alternatives:** Shell out to CLI subcommands; use modification times; overwrite fixed directories; treat file existence as completion.
- **Impact:** An interrupted local run resumes without recomputing verified work or hiding damage. ADR-027 extends the same contract to normalized global acquisition without changing the downstream stages.

## ADR-027 — Global acquisition is a first-class content-addressed workflow stage

- **Date:** 2026-08-02
- **Context:** `build-global` already had normalized AOI, provider selection, cache, transport, and provenance logic, but `topoforge run` could only start from an existing local DEM. Treating a downloaded file as an unverified local source would lose provider policy/trace and make interruption recovery ambiguous.
- **Decision:** Add an optional `acquire` stage before the shared source/build chain. Its identity contains normalized bbox/center-radius AOI, provider-selection policy, and a versioned provider contract; cache directory, timeout, retries, and rate-limit timing remain non-content operational controls. Reuse strictly verifies the projected single-band raster, NoData count, raster SHA-256, dataset/provider/selection binding, acquisition manifest, and aligned quality masks, then compares them with canonical `acquire.json`. The following source stage also binds the acquisition-manifest SHA-256. CLI arguments construct the same typed `GlobalAcquisitionConfig`; provider and build algorithms remain in their existing modules.
- **Alternatives:** Shell out to `fetch-dem`/`build-global`; duplicate provider code in workflow or CLI; trust a file-exists cache; include retry timing in terrain identity; overwrite incomplete acquisition evidence.
- **Impact:** One command can resume from local DEM, bbox, or center-radius without a second provider fetch or loss of provenance. Arbitrary raster/manifest/mask tampering is rejected. The retained Amazon replay produced 7/7 cache hits, zero network calls under a fail-on-network transport, exact elevation-array equality with retained evidence, and byte-identical repeat workflow manifests.

## ADR-028 — Local UX is a saved launch contract plus a static artifact browser

- **Date:** 2026-08-02
- **Context:** A resumable core still required users to preserve a long command line and manually locate build reports, previews, connector maps, slice/project evidence, and stage directories. A server or API would add deployment state that single-workstation use does not need.
- **Decision:** Define strict `WorkflowLaunchConfig` YAML as the complete local execution input, including local/global source selection, manufacturing settings, tile bounds, slicer/profile choices, and project evidence. `topoforge run` and `wizard` save it; `resume` executes it through the existing workflow core. After strict completion, publish a measured JSON summary and a dependency-free static HTML browser. Artifact inspection must validate workflow/status ids, completed state, every stage-manifest SHA-256, required-check booleans, and workspace path containment before linking files. Browser opening remains optional.
- **Alternatives:** Store only shell history; build a FastAPI service now; duplicate provider/build logic in a desktop application; trust directory listings without hashes; embed large artifacts into HTML.
- **Impact:** Solo use needs one reviewed setup and a short resume command. Reports, previews, maps, models, and output directories are locally browsable without a server, while canonical stage identities remain independent of invocation history. The retained Amazon workflow reused all seven stages and produced checksum-verified launch, summary, and report roles.

## ADR-029 — Workflow maintenance is measured, review-first, and checksum-bound

- **Date:** 2026-08-02
- **Context:** Content-addressed stages intentionally retain old identities, but unrestricted deletion would discard evidence; local recovery also needs more than copying an unverified directory tree.
- **Decision:** Estimate disk use from configured cell/triangle ceilings before execution and measured counts after completion. Cleanup may target only immediate stage identities absent from the current canonical manifest, defaults to review, and requires the exact workflow id to apply. Backups reject symlinks, use sorted safe ZIP paths and fixed metadata, include referenced external local source/config files, bind every file by size and SHA-256, strictly reopen before success, and restore through a staging directory before atomic publication.
- **Alternatives:** Delete the whole workspace/cache; trust modification times; archive only final models; use unverified platform ZIP defaults; require an API service for job maintenance.
- **Impact:** Solo local operation has bounded storage decisions and portable evidence without weakening stage reuse. The retained Amazon archive is byte-deterministic, includes two external source-evidence files, and restores all 57 stage files byte-for-byte.

## ADR-030 — Dependency and self-intersection hardening must preserve distribution and evidence compatibility

- **Date:** 2026-08-02
- **Context:** NumPy 2.5 exposed an upstream Rasterio masked-array deprecation, while exhaustive self-intersection candidates differed materially in accuracy, licensing, install size, and real-mesh memory. Changing report text alone also caused strict old-stage reread to reject otherwise identical evidence.
- **Decision:** Constrain NumPy below 2.5 only after before/after artifact and performance proof. Do not add a self-intersection backend unless it passes clean/adjacent/overlap/connected fixtures, deterministic real-mesh benchmarks, Apache-compatible distribution, and acceptable resource cost. Preserve existing validation field bytes when no behavior is added; record evaluated non-adoption in separate evidence and keep `not_fully_checked` literal.
- **Alternatives:** Suppress warnings; accept any backend that imports; add non-commercial/GPL runtime dependencies without distribution review; label unchecked geometry passed; rewrite historical validation fields during reuse.
- **Impact:** TopoForge 0.5.0 has a warning-free 179-test environment with byte-identical core build roles, retains Apache-2.0 distribution boundaries, and continues to reopen 0.4.0 stage evidence exactly.

## ADR-031 — Local overlays are immutable terrain companions in one fixed 3MF assembly

- **Date:** 2026-08-02
- **Context:** Local routes, roads, rivers, coasts, labels, and contours need independent provenance and preview colors while preserving exact placement on an already validated terrain. Publishing every mesh as a top-level 3MF build item made Bambu Studio treat overlays as independent floating parts.
- **Decision:** Keep the terrain and every overlay as independent named mesh resources and per-layer STL evidence. Map them through the processed metric CRS to the exact fixed-diagonal terrain surface without changing DEM values. Publish one identity-transform components object containing every mesh, one top-level build item, and one explicit Core base-material group assigned to every mesh. Strict validation measures all object/component/material/build-item counts and includes the format result in the required gate.
- **Alternatives:** Modify the terrain heightfield; fabricate or sharpen contours; flatten all identities into one unnamed mesh; publish each overlay as a separate build item; rely on file existence or slicer arrangement.
- **Impact:** Relative placement is deterministic, terrain hashes remain unchanged, strict lib3mf reread reports zero warnings, source identities remain auditable, and official P2S slicing has no floating result.

## ADR-032 — Official slicer diagnostics are classified from literal preset and baseline evidence

- **Date:** 2026-08-02
- **Context:** Bambu Studio labels `T65535` as invalid even though the exact command is the official P2S machine end G-code AMS unload sentinel. Its `ZFiller` internal polygon diagnostics also occur in accepted single-terrain Gongga build and reopen logs.
- **Decision:** Preserve complete stdout, stderr, command, result JSON, G-code, preset hashes, and baseline logs. Gate exit status, G-code creation, printer parameters, floating, empty layers, out-of-bed, and support directly. Classify `T65535` only after proving the literal sequence exists in the official resolved machine preset and generated G-code and appears in both retained baselines. Record `ZFiller` counts without suppressing them or treating an internal diagnostic alone as a geometry failure when result.json and required gates pass.
- **Alternatives:** Delete official end G-code; filter log lines; label every internal error-level line a failed print; ignore raw logs.
- **Impact:** Phase 7 makes no false claim that the diagnostics are absent, does not modify official presets, and separates measured manufacturing failures from documented upstream diagnostics.
## ADR-033 — Release archives are bounded, reproducible, and installed outside the checkout

- **Date:** 2026-08-02
- **Context:** Hatch's default sdist included private agent state, Hypothesis caches, historical verification assets, and other files unrelated to source installation. A wheel that can be built inside the repository also does not prove that an installed CLI works without source-tree import leakage.
- **Decision:** Define an explicit sdist allowlist and generated-file exclusions. Use SPDX license metadata and include code, dataset, and third-party notices. Build twice with a fixed `SOURCE_DATE_EPOCH`, require byte equality, inspect archive paths and metadata, install the wheel into a fresh Python 3.12 venv, clear `PYTHONPATH`, change to a directory outside the checkout, then run doctor, synthetic generation, a complete terrain build, and strict 3MF inspection.
- **Alternatives:** Trust Hatch defaults; publish only a wheel; run smoke commands through `uv run` in the checkout; describe installation without executing it.
- **Impact:** TopoForge 0.7.0 has measured package boundaries, byte-reproducible archives, complete license files, and direct evidence that the installed console entry point and core engine work independently of repository imports.

## ADR-034 — Benchmarks and reference regions are executable offline release contracts

- **Date:** 2026-08-02
- **Context:** Early benchmark prose was stale and real-region examples lacked one bounded catalog tying AOI normalization to retained evidence. Rebuilding or redownloading real terrain during routine release checks would be expensive and would discard the value of retained source identity.
- **Decision:** Use deterministic synthetic full builds for performance and artifact-repeat contracts, with exact shapes/triangles and generous wall/RSS ceilings. Separately freeze seven AOI definitions and hash/reread four retained real source/provenance/validation sets using a verifier with no network client. CI runs definitions only; the development workstation additionally checks retained local evidence.
- **Alternatives:** Time only isolated functions; pin fragile exact timings; rebuild every real terrain sample; require network access in CI; treat documentation examples as verification.
- **Impact:** Performance regressions, AOI projection changes, source tampering, and orientation regressions have explicit gates while real datasets remain untouched and unbundled.

## ADR-035 — The local Web surface is a loopback adapter over durable isolated workflows

- **Date:** 2026-08-02
- **Context:** Phase 8 provided complete local CLI operation, but the operator still needed a graphical local surface for AOI interaction, configuration, progress, cancellation, model inspection, and artifact access. Duplicating manufacturing algorithms in JavaScript or exposing an unrestricted service would weaken existing evidence and path boundaries.
- **Decision:** Use typed FastAPI routes and a persistent `LocalJobManager` that writes canonical request/job/event/result records, executes `execute_workflow_launch()` in bounded child processes, and strictly reopens workflow artifacts before serving them. Bind only to loopback, constrain file/config browsing to explicit input roots, require child workspaces, verify static assets by SHA-256/size, and package a complete Chinese/English React, MapLibre, and Three.js application. Keep the offline background default and make OpenStreetMap visual context an explicit option.
- **Alternatives:** Duplicate raster/mesh logic in the browser; run workflows in the HTTP process; use an unrestricted filesystem picker; build a public multi-user deployment and authentication system; keep only the static Phase 6 report.
- **Impact:** TopoForge 0.8.0 provides one-command local Web operation without changing terrain semantics or frozen manufacturing contracts. Jobs survive HTTP restarts, cancellation is process-group scoped, downloaded artifacts are checksum-bound, desktop/mobile/WebGL checks are executable, and future public deployment remains a separate contract.

## ADR-036 — Offline geographic context and model framing are executable Web contracts

- **Date:** 2026-08-02
- **Context:** Phase 9 accepted nonzero center pixels, which allowed a single-color offline map, duplicate MapLibre source errors, CSP-blocked OSM Fetch requests, and a severely cropped GLB to pass. The manufacturing GLB itself retained correct 40 x 32 x 20 mm Z-up bounds.
- **Decision:** Bundle Natural Earth 110 m country geometry through World Atlas/TopoJSON and render it with a deterministic graticule by default. Keep AOI inside the style and restore only its data after `style.load`. Permit only the explicit OSM tile origin in `connect-src` and `img-src`. Frame models with a bounding sphere and limiting horizontal/vertical FOV, and derive the grid and direction arrows from reopened bounds. Browser tests reject page/console errors, require multiple sampled colors, and intercept a real OSM URL under CSP.
- **Alternatives:** Keep a single-color background; require network tiles; suppress MapLibre errors; rotate manufacturing GLB data for Three.js; use a fixed camera/grid; retain one-pixel checks.
- **Impact:** TopoForge 0.8.1 remains useful offline, optional OSM works under the declared CSP, actual GLB solids fit narrow and wide canvases without changing model coordinates, and future regressions fail executable tests.

## ADR-037 — Local terrain maps are deterministic derivatives of completed workflow evidence

- **Date:** 2026-08-02
- **Context:** The stabilized Phase 9 map provided geographic context but did not show the actual processed terrain or manufacturing tile layout. Adding an unrelated remote map service or reprocessing DEMs in JavaScript would duplicate core logic and weaken provenance.
- **Decision:** Derive same-origin 256 x 256 XYZ PNG tiles only from the checksum-published `processed_dem.tif`, with terrain, elevation, and hillshade styles. Bind each cache record to generator version, processed DEM SHA-256, XYZ/style identity, valid pixels, elevation range, PNG SHA-256, and byte size. Derive manufacturing footprints from the published global tile bounds and source raster transform. Keep OpenStreetMap optional and visually separate.
- **Alternatives:** Require online terrain tiles; render the DEM entirely in the browser; create a second raster processing service; publish screenshots instead of interactive tiles; ignore manufacturing footprints.
- **Impact:** Offline local use shows the exact processed terrain associated with the manufacturing artifacts. Repeated requests are deterministic cache hits, corruption regenerates identical bytes, and the map cannot claim source-resolution detail that the processed DEM does not contain.

## ADR-038 — Visualization must inherit workflow trust, Web Mercator limits, and dynamic assembly bounds

- **Date:** 2026-08-02
- **Context:** Independent Phase 10 audit reproduced a canonical assembly-root tamper that still returned `required_checks_passed=true`, a Greenwich-centered antimeridian raster, inverted polar latitude bounds, and 3D assembly cropping after resize/explosion with an incomplete reset.
- **Decision:** Anchor assembly reads through the JobRecord artifact SHA, canonical workflow manifest, validated CONNECT-stage output/manifest paths and manifest SHA, assembly validation SHA, tile-manifest SHA, and per-tile GLB SHA. Never hard-code the visualization gate. Represent date-line coverage as two Web Mercator segments with a circular longitude center; record partial latitude clipping and reject fully out-of-range rasters. Compute camera frames from deterministic visible/exploded tile bounds after resize, display-state changes, and reset.
- **Alternatives:** Trust mutable directory contents; hash only GLBs; clamp latitude endpoints independently; use a near-global Mercator envelope; keep a one-time camera frame; treat a nonblank canvas as complete coverage.
- **Impact:** Tampering fails before map/assembly publication, date-line and high-latitude behavior is explicit, and multi-column exploded assemblies remain framed under narrow desktop layouts. Regression tests cover the exact audit reproductions.

## ADR-039 — Web project lifecycle reuses the checksum-bound maintenance core

- **Date:** 2026-08-02
- **Context:** The loopback Web application could build and inspect jobs but could not measure storage, remove unreferenced content-addressed stages, create portable backups, or register restored projects. Reimplementing these operations in the API or browser would diverge from the Phase 6 CLI evidence.
- **Decision:** Keep all storage, cleanup, backup, verification, and restore algorithms in topoforge.workflow. The Web adapter owns only its state/backups directory, strict typed records, path containment, exact workflow-id confirmation, response SHA headers, and completed-job registration after core atomic restore and strict reopen. The UI presents measured values and requires a user confirmation before cleanup.
- **Alternatives:** Duplicate ZIP/cleanup logic in FastAPI; expose arbitrary filesystem deletion; trust backup filenames; restore directly into final destinations; keep maintenance CLI-only.
- **Impact:** CLI and Web use one lifecycle truth. Repeated backups reuse identical verified bytes, downloads expose their SHA identity, cleanup cannot target current stages, and restored projects immediately participate in existing map/model/assembly views.

## ADR-040 — Project version and target tags drive reproducible GitHub Releases

- **Date:** 2026-08-02
- **Context:** The first public v0.9.0 CI run failed because the workflow hard-coded 0.8.0, and pushing a Git tag did not create a GitHub Release page or assets.
- **Decision:** CI reads the version from uv project metadata. A dedicated contents-write workflow selects the pushed tag or the newest reachable unpublished tag on main, checks out that target, requires tag/package identity, builds Web assets, creates two fixed-epoch package sets, runs isolated installed verification, writes and checks SHA256SUMS, and publishes the sdist, wheel, verification JSON, and checksum file. Existing releases cause a clean no-op.
- **Alternatives:** Manually edit each workflow version; publish archives from main instead of the tag; upload unverified local files; overwrite existing Release assets; leave historical v0.9.0 without a Release page.
- **Impact:** Release identity is no longer duplicated in YAML. The first Phase 11 main push can bootstrap v0.9.0, while v0.10.0 tag publication uses the same verified generic path.


## ADR-041 — Tile counts use bounded boundary tolerance and versioned cache identity

- **Date:** 2026-08-02
- **Context:** Automatic aspect calculations preserve real raster scale and can differ from a nominal tile limit by fractions of a micron. Exact `ceil(model_size / tile_limit)` turned 180.0001526 mm into two rows, creating unnecessary seams and connectors. Changing the planner without changing the content-addressed stage identity would conflict with retained layouts.
- **Decision:** Treat an axis size within 0.001 mm of an integer multiple of the tile limit as that boundary count. Preserve the exact model dimensions, use the tolerance only for tile-count selection, include `topoforge-tile-layout-planner-v2` in the layout-stage identity, and accept both current and pre-tolerance deterministic v1 layouts during strict reopen.
- **Alternatives:** Round or clamp model dimensions; silently delete old stages; bump every tiling artifact schema and invalidate all non-edge evidence; require users to increase tile limits manually.
- **Impact:** Sub-micron numerical drift no longer changes assembly topology, material overages still partition, old evidence remains readable, and reruns create a new layout stage without overwriting retained data.

## ADR-042 — Web relief presentation derives from manufacturing geometry without mutating it

- **Date:** 2026-08-03
- **Context:** The retained Great Trango STL, 3MF, and GLB had correct bounds, orientation, peak mapping, and positive volume, but a low southeast-oblique camera, south-biased key light, colorless GLB, and tiny unlabeled guides made valid ridges and valleys look inverted or ambiguous in the Web viewer.
- **Decision:** Keep the manufacturing GLB and +X East/+Y North/+Z Up coordinates unchanged. Frame the browser camera from due south at a higher elevation so East projects right and North projects up, light relief primarily from the northwest, derive deterministic display-only vertex colors from model Z, render world-space E/N guide labels, expose an icon reset control, and require an explicit loaded-model state plus colorful WebGL samples in Playwright.
- **Alternatives:** Rotate or rewrite the GLB; change vertical scale; fabricate terrain color/material data in exported manufacturing artifacts; keep a low oblique view and rely on a text legend; accept a nonblank placeholder canvas as proof.
- **Impact:** The 770,880-triangle retained model loads in 3.093 s with 98 sampled colors and zero browser errors while model.stl, model.3mf, and preview.glb remain byte-identical. The change is presentation-only and does not alter terrain, scale, slicing, or provenance.

## ADR-043 — WebGL drawing buffers and CSS canvases have separate high-DPI dimensions

- **Date:** 2026-08-03
- **Context:** At devicePixelRatio 1.5, Three.js correctly allocated a 997 x 984 drawing buffer for a 665 x 656 container, but setSize(width, height, false) did not write CSS dimensions. The browser therefore laid out the canvas at its high-resolution buffer size, overflowing the center pane and clipping an otherwise correctly framed model at the lower-right edge.
- **Decision:** Keep renderer pixel ratio capped for visual quality, but call setSize with CSS updates enabled for both the whole-model and assembly renderers. Reframe after the container size and loaded model settle, delay the loaded-model contract until the stable frame, run desktop Playwright at deviceScaleFactor 1.5, and assert canvas client size equals container size while buffer/client ratios equal device pixel ratio.
- **Alternatives:** Disable high-DPI rendering; shrink the camera until an oversized canvas appears to fit; apply ad hoc CSS transforms; fix only the whole-model viewer; rely on a manual reset button.
- **Impact:** The real 1.5-DPR view uses a 665 x 656 CSS canvas with a 997 x 984 buffer, normalized terrain center (0.4895, 0.4593), complete solid visibility, and zero browser errors. DPR 1 remains unchanged, assembly receives the same correction, and manufacturing artifacts remain byte-identical.
