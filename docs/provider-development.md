# Provider development

A provider exposes an identifier, coverage probe, dataset metadata, and fetch operation. Selection weighs semantic match (DTM/DSM/bathymetry), coverage completeness, actual resolution, license mode, credentials, expected bytes, vertical datum compatibility, recency, and observed reliability; resolution alone never decides.

## Implemented Copernicus AWS boundary

`copernicus-aws` is implemented with configurable GLO-30/GLO-90 endpoints. It downloads and caches authoritative `tileList.txt` records, selects GLO-30 only when every AOI cell is present, falls back to a complete GLO-90 plan rather than blending dataset identities, stores immutable bytes by content SHA-256, and indexes canonical provider/dataset/version/URL requests atomically. The HTTP client enforces timeout, bounded attempts, exponential backoff, minimum request interval, declared/streamed byte limits, and a descriptive User-Agent.

Acquisition writes a metric AOI GeoTIFF and `source_acquisition.json` containing the exact user/normalized AOI, plan decisions, URL, ETag, Last-Modified, bytes, SHA-256, cache status, attempts, licence/liability records, and source-footprint coverage crop. For each selected tile it caches and parses the exact S3 ListObjectsV2 prefix response, then preserves exposed EDM, FLM, HEM, and WBM rasters. Each ancillary raster must align with its DEM tile before it is nearest-neighbour reprojected and cropped with the identical target transform/window; raw flags/error values are not remapped or used to alter elevations. Reprojection-only gaps are removed using an independently reprojected coverage mask; source NoData remains masked. `build-global` then reuses the local processing, sampling, orientation, export, validation, and bundle-verification path.

A production provider must:

1. Use configurable official endpoints, a descriptive User-Agent, timeouts, bounded retries, and polite concurrency.
2. Validate AOI area before requests and explain suggested splits.
3. Cache immutable assets by provider/dataset/version/URL with ETag, Last-Modified, byte length, and SHA-256.
4. Preserve original provider responses or normalized metadata fixtures for offline tests.
5. Return explicit dataset semantics, horizontal/vertical reference, acquisition period, license, attribution, URLs, checksums, NoData, and quality masks.
6. Record every attempted provider, failure reason, fallback, and selection rationale.
7. Keep keys in environment/config secret stores and redact them from URLs/logs/provenance.
8. Add coverage, metadata, retry, rate-limit, cache, checksum, missing-key, fallback, and offline tests.

The planned default no-key global route is the configurable Copernicus AWS GLO-30/GLO-90 2021 COG mirror documented in `data-sources.md`.
