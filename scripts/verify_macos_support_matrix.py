#!/usr/bin/env python3
"""Verify the frozen Phase 13 matrix without implying native runtime support."""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "topoforge-macos-support-matrix-v1"
EXPECTED_TARGETS = {
    "macos-15-arm64": ("15.7.9", "arm64", "macos-15"),
    "macos-26-arm64": ("26.6.1", "arm64", "macos-26"),
}
EXPECTED_UNVERIFIED_STATUSES = {
    "configured-unverified",
    "not-provisioned",
    "planned-unverified",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_macos_support_matrix(
    repository_root: Path,
    matrix_path: Path | None = None,
) -> dict[str, Any]:
    """Return a strict report for the frozen matrix and repository identities."""
    root = repository_root.resolve()
    path = (matrix_path or root / "docs" / "macos-support-matrix.json").resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []

    if payload.get("schema_version") != SCHEMA_VERSION:
        problems.append("matrix schema_version is not the frozen Phase 13 schema")
    if payload.get("public_support_status") != "unverified":
        problems.append("matrix must not advertise verified macOS support")

    raw_targets = payload.get("phase13a_targets")
    targets = (
        {
            item.get("id"): item
            for item in raw_targets
            if isinstance(raw_targets, list) and isinstance(item, dict)
        }
        if isinstance(raw_targets, list)
        else {}
    )
    if set(targets) != set(EXPECTED_TARGETS):
        problems.append("Phase 13A target ids differ from the frozen arm64 matrix")
    for target_id, (version, architecture, runner) in EXPECTED_TARGETS.items():
        target = targets.get(target_id, {})
        if (
            target.get("os_version"),
            target.get("architecture"),
            target.get("ci_runner"),
        ) != (version, architecture, runner):
            problems.append(f"{target_id} OS, architecture, or CI identity changed")
        for key in ("ci_status", "clean_system_status", "phase13a_status", "phase13b_status"):
            if target.get(key) not in EXPECTED_UNVERIFIED_STATUSES:
                problems.append(f"{target_id} {key} must remain explicitly unverified")

    excluded = payload.get("excluded_targets")
    exclusions = (
        {
            item.get("id"): item.get("disposition")
            for item in excluded
            if isinstance(excluded, list) and isinstance(item, dict)
        }
        if isinstance(excluded, list)
        else {}
    )
    if exclusions.get("macos-intel-x86-64") != "unsupported-for-0.12.x":
        problems.append("Intel x86_64 must have an explicit unsupported 0.12.x disposition")
    if exclusions.get("macos-14-arm64") != "unsupported-for-0.12.x":
        problems.append("macOS 14 arm64 must remain outside the frozen 0.12.x matrix")
    if exclusions.get("macos-27-beta-arm64") != "unsupported-preview":
        problems.append("macOS 27 beta must remain an unsupported preview")

    lock_path = root / "uv.lock"
    lock_sha256 = _sha256(lock_path)
    recorded_lock = payload.get("dependencies", {}).get("lock_sha256")
    if recorded_lock != lock_sha256:
        problems.append("uv.lock SHA-256 does not match the frozen dependency evidence")

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    python_requirement = project["project"]["requires-python"]
    if payload.get("runtime", {}).get("python_requirement") != python_requirement:
        problems.append("matrix Python requirement differs from pyproject.toml")

    bambu = payload.get("bambu_studio", {})
    if bambu.get("phase13b_status") != "unverified":
        problems.append("Bambu Studio integration must remain explicitly unverified")
    if bambu.get("asset_sha256") != (
        "1e54c25aefc5249d56b63711cf773bed56f14430aafcc34340cd4894aef15896"
    ):
        problems.append("Bambu Studio macOS asset digest differs from the frozen release")

    clean_system = payload.get("clean_system_capacity", {})
    if clean_system.get("status") != "not-provisioned":
        problems.append(
            "clean-system capacity must remain not-provisioned until literal evidence exists"
        )
    if clean_system.get("quarantine_gatekeeper_status") != "not-run":
        problems.append("Gatekeeper must remain not-run until clean-system evidence exists")

    return {
        "schema_version": SCHEMA_VERSION,
        "matrix_path": str(path),
        "frozen_at": payload.get("frozen_at"),
        "public_support_status": payload.get("public_support_status"),
        "target_ids": sorted(targets),
        "uv_lock_sha256": lock_sha256,
        "problems": problems,
        "required_checks_passed": not problems,
    }


def main() -> int:
    """Validate the matrix and optionally retain a canonical report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--matrix", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    report = verify_macos_support_matrix(args.repository_root, args.matrix)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["required_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
