"""Release contracts for the frozen, still-unverified Phase 13 matrix."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from scripts.verify_macos_support_matrix import verify_macos_support_matrix


def test_frozen_macos_matrix_matches_repository_identities() -> None:
    root = Path(__file__).parents[2]
    report = verify_macos_support_matrix(root)

    assert report["required_checks_passed"] is True
    assert report["public_support_status"] == "unverified"
    assert report["target_ids"] == ["macos-15-arm64", "macos-26-arm64"]


def test_matrix_keeps_targets_unverified_and_exclusions_explicit() -> None:
    root = Path(__file__).parents[2]
    matrix = json.loads((root / "docs/macos-support-matrix.json").read_text(encoding="utf-8"))

    assert all(target["architecture"] == "arm64" for target in matrix["phase13a_targets"])
    assert all(
        target["phase13a_status"] == "planned-unverified"
        and target["phase13b_status"] == "planned-unverified"
        for target in matrix["phase13a_targets"]
    )
    exclusions = {target["id"]: target["disposition"] for target in matrix["excluded_targets"]}
    assert exclusions == {
        "macos-14-arm64": "unsupported-for-0.12.x",
        "macos-intel-x86-64": "unsupported-for-0.12.x",
        "macos-27-beta-arm64": "unsupported-preview",
    }
    assert matrix["clean_system_capacity"]["status"] == "not-provisioned"
    assert matrix["bambu_studio"]["phase13b_status"] == "unverified"


def test_native_macos_ci_uses_only_frozen_arm64_runner_labels() -> None:
    root = Path(__file__).parents[2]
    workflow_path = root / ".github/workflows/macos.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    job = workflow["jobs"]["native-arm64"]
    runners = {item["runner"] for item in job["strategy"]["matrix"]["include"]}

    assert runners == {"macos-15", "macos-26"}
    assert job["env"]["MACOSX_DEPLOYMENT_TARGET"] == "15.0"
    assert "macos-latest" not in workflow_text
    assert 'test "$(uname -m)" = "arm64"' in workflow_text
    assert "scripts/verify_macos_support_matrix.py" in workflow_text
    assert "topoforge web --check --no-open" in workflow_text
    assert "TopoForge Phase 13/地形" in workflow_text
    assert workflow_text.count('cmp "') == 3
    assert "support unverified" in workflow_text


def test_documentation_does_not_promote_macos_support() -> None:
    root = Path(__file__).parents[2]
    documentation = (root / "docs/macos-support.md").read_text(encoding="utf-8")

    assert "does **not** currently claim macOS support" in documentation
    assert "unsupported today" in documentation
    assert "Intel x86_64 is unsupported for 0.12.x" in documentation
    assert "README and release metadata must continue to say macOS is" in documentation
    assert "unverified" in documentation
