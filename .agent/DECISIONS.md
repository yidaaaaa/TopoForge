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
