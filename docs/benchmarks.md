# Benchmarks

Phase 8 defines executable full-build performance contracts in
`benchmarks/baseline.json`. The runner creates deterministic analytic GeoTIFFs, uses
`source-preserving` sampling with strict resource budgets, builds and reopens every
bundle, and compares six deterministic roles across two independent builds.

| Case | Source/processed grid | Exact triangles | Wall threshold | RSS threshold |
| --- | ---: | ---: | ---: | ---: |
| small | 64 x 80 | 20,476 | 60 s | 1,536 MiB |
| medium | 128 x 160 | 81,916 | 120 s | 1,536 MiB |
| large | 256 x 320 | 327,676 | 300 s | 2,048 MiB |

The triangle values are exact closed-solid topology counts. Thresholds are deliberately
wider than retained host observations so CI detects material regression without treating
normal shared-runner noise as a failure.

The initial Phase 8 host run observed approximately 1.45 s, 4.29 s, and 15.81 s. Peak
process RSS reached approximately 919 MiB in the largest case. These are host
observations, not portable guarantees.

```bash
uv run python scripts/run_benchmarks.py \
  --baseline benchmarks/baseline.json \
  --repeat 2 \
  --report artifacts/logs/phase8-benchmarks.json
```

A passing report requires exact processed shapes and triangle counts, all normal geometry
and format checks, strict bundle reread, byte-identical STL/3MF/GLB/PNG/processed
DEM/NoData-mask roles, and resource use below every ceiling.

The benchmark uses synthetic fixtures only. Real retained regions are verified separately
and are not rebuilt during routine performance tests.
