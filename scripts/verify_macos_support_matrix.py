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
    "macos-15-arm64": ("macOS Sequoia", "15.7.9", "arm64", "15.0", "macos-15"),
    "macos-26-arm64": ("macOS Tahoe", "26.6.1", "arm64", "15.0", "macos-26"),
}
EXPECTED_CI_STATUS = "historical-hosted-core-pass-rerun-required"
EXPECTED_SHARED_FOUNDATION = "da6999101ee28b1309798e66900edc6b53052d48"
EXPECTED_PHASE13A_STATUS = "in-progress-unverified"
EXPECTED_EXCLUSIONS = {
    "macos-14-arm64": ("macOS Sonoma", "14.8.9", "arm64", "unsupported-for-0.12.x"),
    "macos-intel-x86-64": ("macOS", "all", "x86_64", "unsupported-for-0.12.x"),
    "macos-27-beta-arm64": ("macOS 27 beta", "27 beta", "arm64", "unsupported-preview"),
}
EXPECTED_HOSTED_CI_RUN = (
    31419016599,
    "5df03c40536363d63678f0b23b69b228ee008e6a",
    "success",
)
EXPECTED_HOSTED_CI_TARGETS = {
    "macos-15-arm64": (
        "macos-15",
        93554980176,
        "success",
        "macos-macos-15-runtime-evidence",
        1071,
    ),
    "macos-26-arm64": (
        "macos-26",
        93554980175,
        "success",
        "macos-macos-26-runtime-evidence",
        1069,
    ),
}

TARGET_FIELDS = frozenset(
    {
        "id",
        "os_name",
        "os_version",
        "architecture",
        "deployment_target",
        "ci_runner",
        "ci_status",
        "clean_system_status",
        "phase13a_status",
        "phase13b_status",
    }
)
EXCLUSION_FIELDS = frozenset(
    {"id", "os_name", "os_version", "architecture", "disposition", "reason"}
)
HOSTED_TARGET_FIELDS = frozenset(
    {
        "id",
        "runner",
        "job_id",
        "job_conclusion",
        "artifact_name",
        "artifact_size_bytes",
        "report_sha256",
        "package_evidence",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _indexed_records(
    raw: Any,
    *,
    label: str,
    expected_ids: tuple[str, ...],
    fields: frozenset[str],
    problems: list[str],
) -> dict[str, dict[str, Any]]:
    """Validate one frozen object list before building its lookup index."""
    if not isinstance(raw, list):
        problems.append(f"{label} must be a list")
        return {}
    if len(raw) != len(expected_ids):
        problems.append(f"{label} length differs from the frozen matrix")
    indexed: dict[str, dict[str, Any]] = {}
    observed_ids: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append(f"{label}[{index}] must be an object")
            continue
        if set(item) != fields:
            problems.append(f"{label}[{index}] fields differ from the frozen schema")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            problems.append(f"{label}[{index}] has no valid id")
            continue
        observed_ids.append(identifier)
        if identifier in indexed:
            problems.append(f"{label} contains duplicate id: {identifier}")
            continue
        indexed[identifier] = item
    if tuple(observed_ids) != expected_ids:
        problems.append(f"{label} ids or order differ from the frozen matrix")
    return indexed


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

    targets = _indexed_records(
        payload.get("phase13a_targets"),
        label="phase13a_targets",
        expected_ids=tuple(EXPECTED_TARGETS),
        fields=TARGET_FIELDS,
        problems=problems,
    )
    if set(targets) != set(EXPECTED_TARGETS):
        problems.append("Phase 13A target ids differ from the frozen arm64 matrix")
    for target_id, expected in EXPECTED_TARGETS.items():
        target = targets.get(target_id, {})
        actual = (
            target.get("os_name"),
            target.get("os_version"),
            target.get("architecture"),
            target.get("deployment_target"),
            target.get("ci_runner"),
        )
        if actual != expected:
            problems.append(f"{target_id} OS, architecture, deployment, or CI identity changed")
        if target.get("ci_status") != EXPECTED_CI_STATUS:
            problems.append(f"{target_id} hosted CI status is not the retained passing result")
        if target.get("clean_system_status") != "not-provisioned":
            problems.append(f"{target_id} clean-system status must remain not-provisioned")
        if target.get("phase13a_status") != EXPECTED_PHASE13A_STATUS:
            problems.append(f"{target_id} Phase 13A status must remain in progress and unverified")
        if target.get("phase13b_status") != "planned-unverified":
            problems.append(f"{target_id} Phase 13B status must remain planned and unverified")

    exclusions = _indexed_records(
        payload.get("excluded_targets"),
        label="excluded_targets",
        expected_ids=tuple(EXPECTED_EXCLUSIONS),
        fields=EXCLUSION_FIELDS,
        problems=problems,
    )
    if set(exclusions) != set(EXPECTED_EXCLUSIONS):
        problems.append("excluded target ids differ from the frozen matrix")
    for target_id, expected in EXPECTED_EXCLUSIONS.items():
        exclusion = exclusions.get(target_id, {})
        actual = (
            exclusion.get("os_name"),
            exclusion.get("os_version"),
            exclusion.get("architecture"),
            exclusion.get("disposition"),
        )
        if actual != expected:
            problems.append(f"{target_id} exclusion identity or disposition changed")
        if not isinstance(exclusion.get("reason"), str) or not exclusion.get("reason"):
            problems.append(f"{target_id} exclusion reason is missing")

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

    ci_capacity = payload.get("ci_capacity", {})
    if ci_capacity.get("status") != EXPECTED_CI_STATUS:
        problems.append("CI capacity does not record the retained hosted core success")
    expected_runners = [item[4] for item in EXPECTED_TARGETS.values()]
    if ci_capacity.get("arm64_runner_labels") != expected_runners:
        problems.append("CI capacity runner labels differ from the frozen unique runner list")

    hosted_ci = payload.get("hosted_ci_evidence", {})
    if (
        hosted_ci.get("run_id"),
        hosted_ci.get("head_sha"),
        hosted_ci.get("conclusion"),
    ) != EXPECTED_HOSTED_CI_RUN:
        problems.append("hosted CI run identity differs from the retained passing evidence")
    if hosted_ci.get("status") != EXPECTED_CI_STATUS:
        problems.append("hosted CI evidence is not marked historical and rerun-required")
    if hosted_ci.get("shared_foundation_sha") != EXPECTED_SHARED_FOUNDATION:
        problems.append("hosted CI evidence does not bind the original shared foundation")
    for field, expected in (
        ("source_tree_evidence", True),
        ("package_evidence", False),
        ("persistent_release_evidence", False),
        ("report_sha256_bound", False),
        ("artifact_retention_days", 30),
        ("must_rerun_after_phase12_integration", True),
        ("final_phase13_gate_eligible", False),
    ):
        if hosted_ci.get(field) != expected:
            problems.append(f"hosted CI evidence boundary changed: {field}")

    hosted_targets = _indexed_records(
        hosted_ci.get("targets"),
        label="hosted_ci_evidence.targets",
        expected_ids=tuple(EXPECTED_HOSTED_CI_TARGETS),
        fields=HOSTED_TARGET_FIELDS,
        problems=problems,
    )
    hosted_runners = [item.get("runner") for item in hosted_targets.values()]
    if len(hosted_runners) != len(set(hosted_runners)):
        problems.append("hosted CI target evidence contains duplicate runner labels")
    if set(hosted_targets) != set(EXPECTED_HOSTED_CI_TARGETS):
        problems.append("hosted CI target evidence differs from the frozen target set")
    for target_id, expected in EXPECTED_HOSTED_CI_TARGETS.items():
        evidence = hosted_targets.get(target_id, {})
        actual = (
            evidence.get("runner"),
            evidence.get("job_id"),
            evidence.get("job_conclusion"),
            evidence.get("artifact_name"),
            evidence.get("artifact_size_bytes"),
        )
        if actual != expected:
            problems.append(f"{target_id} hosted CI job or artifact evidence changed")
        if evidence.get("report_sha256") is not None:
            problems.append(f"{target_id} historical report must not invent a SHA-256")
        if evidence.get("package_evidence") is not False:
            problems.append(f"{target_id} historical run must remain source-tree-only")

    return {
        "schema_version": SCHEMA_VERSION,
        "matrix_path": str(path),
        "frozen_at": payload.get("frozen_at"),
        "public_support_status": payload.get("public_support_status"),
        "target_ids": sorted(targets),
        "uv_lock_sha256": lock_sha256,
        "hosted_ci_status": hosted_ci.get("status"),
        "hosted_ci_run_id": hosted_ci.get("run_id"),
        "hosted_ci_head_sha": hosted_ci.get("head_sha"),
        "hosted_ci_rerun_required": hosted_ci.get("must_rerun_after_phase12_integration"),
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
