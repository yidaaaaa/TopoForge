"""Contracts for bounded native macOS hosted-runner evidence."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from scripts.collect_macos_ci_evidence import (
    NATIVE_IMPORTS,
    evaluate_macos_ci_snapshot,
    write_evidence_report,
)


def _matrix() -> dict[str, Any]:
    root = Path(__file__).parents[2]
    return json.loads((root / "docs/macos-support-matrix.json").read_text(encoding="utf-8"))


def _snapshot() -> dict[str, Any]:
    return {
        "system": "Darwin",
        "machine": "arm64",
        "uname_machine": "arm64",
        "macos_version": "15.7.9",
        "macos_build": "24G999",
        "python_version": "3.12.10",
        "python_implementation": "CPython",
        "python_executable": "/opt/hostedtoolcache/Python/3.12.10/arm64/bin/python",
        "python_executable_identity": "Mach-O 64-bit executable arm64",
        "python_platform": "macosx-15.0-arm64",
        "deployment_target": "15.0",
        "translated": "0",
        "source_commit_sha": "a" * 40,
        "github_sha": "a" * 40,
        "source_tree_dirty": False,
        "matrix_sha256": "b" * 64,
        "collector_sha256": "c" * 64,
        "workflow_sha256": "d" * 64,
    }


def _packages() -> dict[str, dict[str, Any]]:
    return {
        distribution: {
            "version": "fixture",
            "module": module,
            "module_path": f"/fixture/{module}.so",
            "imported": True,
            "error": None,
        }
        for distribution, module in NATIVE_IMPORTS.items()
    }


@pytest.mark.parametrize(
    ("runner_label", "macos_version", "target_id"),
    [
        ("macos-15", "15.7.9", "macos-15-arm64"),
        ("macos-26", "26.6.1", "macos-26-arm64"),
    ],
)
def test_native_arm64_snapshot_passes_without_promoting_support(
    runner_label: str,
    macos_version: str,
    target_id: str,
) -> None:
    snapshot = _snapshot()
    snapshot["macos_version"] = macos_version
    report = evaluate_macos_ci_snapshot(
        _matrix(),
        runner_label=runner_label,
        snapshot=snapshot,
        packages=_packages(),
    )

    assert report["required_checks_passed"] is True
    assert report["target_id"] == target_id
    assert report["target_architecture"] == "arm64"
    assert report["public_support_status"] == "unverified"
    assert report["clean_system_evidence"] is False
    assert report["package_evidence"] is False
    assert report["source_tree_evidence"] is True
    assert report["persistent_release_evidence"] is False
    assert report["gatekeeper_evidence"] is False
    assert report["bambu_phase13b_evidence"] is False


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("system", "Linux", "not Darwin"),
        ("machine", "x86_64", "not native Apple Silicon"),
        ("uname_machine", "x86_64", "uname does not report"),
        ("macos_version", "26.6.1", "does not match target"),
        ("python_version", "3.13.5", "outside the frozen 3.12"),
        ("deployment_target", "14.0", "differs from the frozen target"),
        ("translated", "1", "translated under Rosetta"),
    ],
)
def test_snapshot_rejects_identity_drift(field: str, value: str, problem: str) -> None:
    snapshot = _snapshot()
    snapshot[field] = value

    report = evaluate_macos_ci_snapshot(
        _matrix(),
        runner_label="macos-15",
        snapshot=snapshot,
        packages=_packages(),
    )

    assert report["required_checks_passed"] is False
    assert any(problem in item for item in report["problems"])


def test_snapshot_rejects_unknown_runner_and_failed_native_import() -> None:
    packages = deepcopy(_packages())
    packages["rasterio"]["imported"] = False
    packages["rasterio"]["error"] = "ImportError: fixture"

    report = evaluate_macos_ci_snapshot(
        _matrix(),
        runner_label="macos-latest",
        snapshot=_snapshot(),
        packages=packages,
    )

    assert report["required_checks_passed"] is False
    assert "runner label is outside the frozen matrix: macos-latest" in report["problems"]
    assert "native dependency import failed: rasterio" in report["problems"]


def test_snapshot_rejects_unbound_or_dirty_source_identity() -> None:
    snapshot = _snapshot()
    snapshot["source_tree_dirty"] = True
    snapshot["github_sha"] = None
    snapshot["workflow_sha256"] = None

    report = evaluate_macos_ci_snapshot(
        _matrix(),
        runner_label="macos-15",
        snapshot=snapshot,
        packages=_packages(),
    )

    assert report["required_checks_passed"] is False
    assert "source tree is dirty during hosted evidence collection" in report["problems"]
    assert "GitHub workflow SHA is missing or invalid" in report["problems"]
    assert any("workflow_sha256" in problem for problem in report["problems"])


def test_evidence_writer_emits_detached_report_hash(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    rendered = write_evidence_report(path, {"required_checks_passed": True})

    assert path.read_text(encoding="utf-8") == rendered
    sidecar = path.with_name("runtime.json.sha256")
    digest, name = sidecar.read_text(encoding="utf-8").split()
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert name == path.name
