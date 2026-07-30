# Provider development

A provider exposes an identifier, coverage probe, dataset metadata, and fetch operation. Selection weighs semantic match (DTM/DSM/bathymetry), coverage completeness, actual resolution, license mode, credentials, expected bytes, vertical datum compatibility, recency, and observed reliability; resolution alone never decides.

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
