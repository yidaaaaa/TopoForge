# Contributing

1. Read `AGENTS.md` and `.agent/{PLANS,STATE,DECISIONS,ISSUES}.md`.
2. Install with `uv sync` on Python 3.12.
3. Keep units in names and preserve CRS, vertical-datum, NoData, license, and attribution fields.
4. Add measured tests for every geometry, raster, provider, or slicer behavior.
5. Run all four quality gates from the README.
6. Keep DEM downloads, meshes, caches, and G-code out of Git.

Provider changes also update `docs/data-sources.md`, `DATA_LICENSES.md`, offline fixtures, retry/cache tests, and provenance fields. A feature is described as printable only after geometry validation and an executed slicer test.
