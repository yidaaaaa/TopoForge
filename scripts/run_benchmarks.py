#!/usr/bin/env python3
"""Run bounded, deterministic full-build performance contracts."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from topoforge.config import get_printer_profile
from topoforge.engine import build_local_terrain, verify_artifact_bundle
from topoforge.models import (
    BuildConfig,
    DatasetType,
    ResourceBudgetMode,
    SamplingMode,
    VerticalScaleMode,
)
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff


def terrain_triangle_count(rows: int, columns: int) -> int:
    """Return the exact closed rectangular terrain triangle count."""
    return 4 * (rows - 1) * (columns - 1) + 4 * ((rows - 1) + (columns - 1))


def _peak_rss_mb() -> float:
    try:
        import resource
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Peak RSS benchmark measurement requires POSIX resource support. "
            "Run the bounded release benchmark on Linux or macOS."
        ) from exc
    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0


def _load_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return loaded


def _build_config(source: Path, output: Path, baseline: dict[str, Any]) -> BuildConfig:
    return BuildConfig(
        dem_path=source,
        output_dir=output,
        model_width_mm=float(baseline["model_width_mm"]),
        base_thickness_mm=3.0,
        max_height_mm=float(baseline["max_height_mm"]),
        vertical_scale_mode=VerticalScaleMode.FIT_HEIGHT,
        sampling_mode=SamplingMode(str(baseline["sampling_mode"])),
        max_grid_cells=100_000,
        max_estimated_triangles=400_000,
        max_estimated_memory_mb=2048.0,
        resource_budget_mode=ResourceBudgetMode(str(baseline["resource_budget_mode"])),
        dataset_type=DatasetType.DTM,
        dataset_name="TopoForge Phase 8 deterministic benchmark",
        vertical_crs="synthetic-local-metre",
        vertical_datum="synthetic-local-zero",
        data_license="Apache-2.0 synthetic fixture",
        attribution="TopoForge deterministic analytic fixture",
        printer_profile=get_printer_profile("bambu-p2s-0.4"),
    )


def run_benchmarks(baseline_path: Path, *, repeat: int) -> dict[str, Any]:
    """Execute every benchmark case and enforce exact and resource thresholds."""
    if repeat < 2:
        raise ValueError("repeat must be at least 2 for determinism verification")
    baseline = _load_json(baseline_path)
    if baseline.get("schema_version") != 1:
        raise ValueError("benchmark baseline schema_version must be 1")
    raw_cases = baseline.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("benchmark baseline cases must be a non-empty list")
    deterministic_roles = baseline.get("deterministic_roles")
    if not isinstance(deterministic_roles, list) or not deterministic_roles:
        raise ValueError("benchmark deterministic_roles must be a non-empty list")
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="topoforge-benchmarks-") as raw_temp:
        root = Path(raw_temp)
        for raw_case in raw_cases:
            if not isinstance(raw_case, dict):
                raise ValueError("benchmark case must be an object")
            case_id = str(raw_case["id"])
            rows = int(raw_case["rows"])
            columns = int(raw_case["columns"])
            expected_shape = list(raw_case["expected_processed_grid_shape"])
            expected_triangles = int(raw_case["expected_triangle_count"])
            calculated_triangles = terrain_triangle_count(rows, columns)
            if calculated_triangles != expected_triangles:
                raise ValueError(
                    f"{case_id} baseline triangle count is {expected_triangles}, "
                    f"formula gives {calculated_triangles}"
                )
            source = create_synthetic_geotiff(
                root / f"{case_id}.tif",
                SyntheticTerrain(str(baseline["terrain"])),
                rows=rows,
                columns=columns,
                pixel_size_m=float(baseline["pixel_size_m"]),
            )
            manifest_hashes: list[dict[str, str]] = []
            wall_seconds: list[float] = []
            validations: list[dict[str, Any]] = []
            for run_index in range(repeat):
                output = root / f"{case_id}-run-{run_index + 1}"
                started = time.perf_counter()
                result = build_local_terrain(_build_config(source, output, baseline))
                wall_seconds.append(time.perf_counter() - started)
                verify_artifact_bundle(output)
                validation = result.validation
                if validation.get("required_checks_passed") is not True:
                    raise ValueError(f"{case_id} run {run_index + 1} failed required checks")
                if list(validation.get("processed_grid_shape", ())) != expected_shape:
                    raise ValueError(
                        f"{case_id} processed shape is {validation.get('processed_grid_shape')}, "
                        f"expected {expected_shape}"
                    )
                if validation.get("triangle_count") != expected_triangles:
                    raise ValueError(
                        f"{case_id} triangle count is {validation.get('triangle_count')}, "
                        f"expected {expected_triangles}"
                    )
                manifest = _load_json(output / "build_manifest.json")
                hashes = manifest.get("sha256")
                if not isinstance(hashes, dict):
                    raise ValueError(f"{case_id} manifest SHA-256 field is invalid")
                manifest_hashes.append({role: str(hashes[role]) for role in deterministic_roles})
                validations.append(
                    {
                        "required_checks_passed": True,
                        "processed_grid_shape": validation["processed_grid_shape"],
                        "triangle_count": validation["triangle_count"],
                        "estimated_memory_mb": validation["manufacturing_preflight"][
                            "estimated_memory_mb"
                        ],
                    }
                )
            deterministic = all(value == manifest_hashes[0] for value in manifest_hashes[1:])
            if not deterministic:
                raise ValueError(f"{case_id} deterministic artifact hashes changed across repeats")
            maximum_wall = max(wall_seconds)
            peak_rss_mb = _peak_rss_mb()
            if maximum_wall > float(raw_case["max_wall_seconds"]):
                raise ValueError(
                    f"{case_id} wall time {maximum_wall:.3f}s exceeds "
                    f"{raw_case['max_wall_seconds']}s"
                )
            if peak_rss_mb > float(raw_case["max_peak_rss_mb"]):
                raise ValueError(
                    f"{case_id} peak RSS {peak_rss_mb:.3f} MiB exceeds "
                    f"{raw_case['max_peak_rss_mb']} MiB"
                )
            results.append(
                {
                    "id": case_id,
                    "rows": rows,
                    "columns": columns,
                    "repeat": repeat,
                    "wall_seconds": wall_seconds,
                    "max_wall_seconds_observed": maximum_wall,
                    "max_wall_seconds_threshold": raw_case["max_wall_seconds"],
                    "peak_rss_mb_observed": peak_rss_mb,
                    "max_peak_rss_mb_threshold": raw_case["max_peak_rss_mb"],
                    "validation": validations,
                    "deterministic_roles": deterministic_roles,
                    "deterministic_sha256": manifest_hashes[0],
                    "deterministic_repeat_passed": deterministic,
                    "required_checks_passed": True,
                }
            )
    return {
        "schema_version": 1,
        "baseline_path": str(baseline_path.resolve()),
        "baseline": baseline,
        "case_count": len(results),
        "cases": results,
        "required_checks_passed": True,
    }


def main() -> int:
    """Run the benchmark command."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = run_benchmarks(args.baseline.resolve(), repeat=args.repeat)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
