#!/usr/bin/env python3
"""Verify deterministic AOI reference definitions and retained local evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import yaml

from topoforge.models import AreaOfInterestInput
from topoforge.raster import normalize_area_of_interest


def sha256_file(path: Path) -> str:
    """Return a streaming SHA-256 digest."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _request(aoi: dict[str, Any]) -> AreaOfInterestInput:
    if "bbox_wgs84" in aoi:
        return AreaOfInterestInput(bbox_wgs84=tuple(aoi["bbox_wgs84"]))
    return AreaOfInterestInput(
        center_wgs84=tuple(aoi["center_wgs84"]),
        radius_m=float(aoi["radius_m"]),
    )


def _equal_number(actual: object, expected: object, label: str) -> None:
    if isinstance(expected, int) and not isinstance(expected, bool):
        if actual != expected:
            raise ValueError(f"{label} is {actual!r}, expected {expected!r}")
        return
    if not math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-9):
        raise ValueError(f"{label} is {actual!r}, expected {expected!r}")


def _verify_metrics(validation: dict[str, Any], expected: dict[str, Any], region_id: str) -> None:
    for key, expected_value in expected.items():
        actual = validation.get(key)
        label = f"{region_id} validation {key}"
        if isinstance(expected_value, list):
            if actual != expected_value:
                raise ValueError(f"{label} is {actual!r}, expected {expected_value!r}")
        else:
            _equal_number(actual, expected_value, label)


def verify_reference_catalog(
    catalog_path: Path,
    *,
    repository_root: Path,
    definitions_only: bool,
) -> dict[str, Any]:
    """Verify all AOIs and optionally reread retained real data without network access."""
    loaded = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    catalog = _mapping(loaded, "catalog")
    if catalog.get("schema_version") != 1:
        raise ValueError("reference catalog schema_version must be 1")
    raw_regions = catalog.get("regions")
    if not isinstance(raw_regions, list) or not raw_regions:
        raise ValueError("reference catalog regions must be a non-empty list")
    identifiers: set[str] = set()
    results: list[dict[str, Any]] = []
    retained_count = 0
    for raw_region in raw_regions:
        region = _mapping(raw_region, "region")
        region_id = str(region.get("id", ""))
        if not region_id or region_id in identifiers:
            raise ValueError(f"reference region id is empty or duplicated: {region_id!r}")
        identifiers.add(region_id)
        expected = _mapping(region.get("expected"), f"{region_id} expected")
        normalized = normalize_area_of_interest(_request(_mapping(region.get("aoi"), region_id)))
        if normalized.kind != expected.get("kind"):
            raise ValueError(f"{region_id} kind changed to {normalized.kind!r}")
        expected_bounds = expected.get("bounds_wgs84")
        if not isinstance(expected_bounds, list) or len(expected_bounds) != 4:
            raise ValueError(f"{region_id} expected bounds must contain four values")
        for index, (actual, wanted) in enumerate(
            zip(normalized.bounds_wgs84, expected_bounds, strict=True)
        ):
            _equal_number(actual, wanted, f"{region_id} bounds[{index}]")
        if normalized.crosses_antimeridian is not expected.get("crosses_antimeridian"):
            raise ValueError(f"{region_id} antimeridian classification changed")
        if normalized.target_local_crs != expected.get("target_local_crs"):
            raise ValueError(
                f"{region_id} CRS changed to {normalized.target_local_crs!r}; "
                f"expected {expected.get('target_local_crs')!r}"
            )
        _equal_number(normalized.area_m2, expected.get("area_m2"), f"{region_id} area_m2")
        normalized_payload = normalized.model_dump(mode="json")
        canonical = json.dumps(
            normalized_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        result: dict[str, Any] = {
            "id": region_id,
            "kind": normalized.kind,
            "bounds_wgs84": list(normalized.bounds_wgs84),
            "crosses_antimeridian": normalized.crosses_antimeridian,
            "target_local_crs": normalized.target_local_crs,
            "area_m2": normalized.area_m2,
            "normalized_sha256": hashlib.sha256(canonical).hexdigest(),
            "retained_evidence": None,
        }
        retained = region.get("retained_evidence")
        if retained is not None and not definitions_only:
            evidence = _mapping(retained, f"{region_id} retained_evidence")
            source = (repository_root / str(evidence["source_path"])).resolve()
            provenance_path = (repository_root / str(evidence["provenance_path"])).resolve()
            validation_path = (repository_root / str(evidence["validation_path"])).resolve()
            for path in (source, provenance_path, validation_path):
                if not path.is_file():
                    raise ValueError(f"{region_id} retained evidence is missing: {path}")
            source_sha256 = sha256_file(source)
            if source_sha256 != evidence.get("source_sha256"):
                raise ValueError(f"{region_id} retained source SHA-256 changed")
            provenance = _mapping(
                json.loads(provenance_path.read_text(encoding="utf-8")),
                f"{region_id} provenance",
            )
            validation = _mapping(
                json.loads(validation_path.read_text(encoding="utf-8")),
                f"{region_id} validation",
            )
            if validation.get("required_checks_passed") is not True:
                raise ValueError(f"{region_id} retained validation no longer passes")
            orientation = _mapping(provenance.get("orientation"), f"{region_id} orientation")
            if orientation.get("east_axis") != "+X = East":
                raise ValueError(f"{region_id} east-axis evidence changed")
            if orientation.get("north_axis") != "+Y = North":
                raise ValueError(f"{region_id} north-axis evidence changed")
            _verify_metrics(
                validation,
                _mapping(evidence.get("expected_metrics"), f"{region_id} metrics"),
                region_id,
            )
            result["retained_evidence"] = {
                "source_path": str(source),
                "source_sha256": source_sha256,
                "provenance_path": str(provenance_path),
                "provenance_sha256": sha256_file(provenance_path),
                "validation_path": str(validation_path),
                "validation_sha256": sha256_file(validation_path),
                "required_checks_passed": True,
                "orientation_passed": True,
            }
            retained_count += 1
        results.append(result)
    return {
        "schema_version": 1,
        "catalog_path": str(catalog_path.resolve()),
        "catalog_sha256": sha256_file(catalog_path),
        "definitions_only": definitions_only,
        "network_attempts": 0,
        "region_count": len(results),
        "retained_evidence_count": retained_count,
        "regions": results,
        "required_checks_passed": True,
    }


def main() -> int:
    """Run the reference-region verifier."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--definitions-only", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    report = verify_reference_catalog(
        args.catalog.resolve(),
        repository_root=repository_root,
        definitions_only=args.definitions_only,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
