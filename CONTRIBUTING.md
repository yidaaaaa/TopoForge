# Contributing

1. Read `AGENTS.md` and `.agent/{PLANS,STATE,DECISIONS,ISSUES}.md`.
2. Install with `uv sync --locked --all-groups` on CPython 3.11–3.14.
3. Keep units in names and preserve CRS, vertical-datum, NoData, license, and attribution fields.
4. Add measured tests for every geometry, raster, provider, slicer, release, or benchmark behavior.
5. Run the static, test, release archive, reference-region, and benchmark gates from the README.
6. Keep DEM downloads, meshes, caches, G-code, benchmark outputs, and wheelhouses out of Git.
7. Do not add API/Web dependencies or services before the saved Phase 9 contracts are started.

Provider changes also update `docs/data-sources.md`, `DATA_LICENSES.md`, offline fixtures,
retry/cache tests, and provenance fields. A feature is described as printable only after
geometry validation and an executed slicer test.
