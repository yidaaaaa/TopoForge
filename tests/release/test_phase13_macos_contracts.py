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
    assert report["hosted_ci_status"] == "passed-hosted-core-unverified"
    assert report["hosted_ci_run_id"] == 31419016599
    assert report["hosted_ci_head_sha"] == ("5df03c40536363d63678f0b23b69b228ee008e6a")


def test_matrix_keeps_targets_unverified_and_exclusions_explicit() -> None:
    root = Path(__file__).parents[2]
    matrix = json.loads((root / "docs/macos-support-matrix.json").read_text(encoding="utf-8"))

    assert all(target["architecture"] == "arm64" for target in matrix["phase13a_targets"])
    assert all(
        target["ci_status"] == "passed-hosted-core-unverified"
        and target["phase13a_status"] == "in-progress-unverified"
        and target["phase13b_status"] == "planned-unverified"
        for target in matrix["phase13a_targets"]
    )
    hosted_ci = matrix["hosted_ci_evidence"]
    assert hosted_ci["run_id"] == 31419016599
    assert hosted_ci["head_sha"] == "5df03c40536363d63678f0b23b69b228ee008e6a"
    assert hosted_ci["conclusion"] == "success"
    assert {
        item["id"]: (
            item["runner"],
            item["job_conclusion"],
            item["artifact_name"],
        )
        for item in hosted_ci["targets"]
    } == {
        "macos-15-arm64": ("macos-15", "success", "macos-macos-15-runtime-evidence"),
        "macos-26-arm64": ("macos-26", "success", "macos-macos-26-runtime-evidence"),
    }
    exclusions = {target["id"]: target["disposition"] for target in matrix["excluded_targets"]}
    assert exclusions == {
        "macos-14-arm64": "unsupported-for-0.12.x",
        "macos-intel-x86-64": "unsupported-for-0.12.x",
        "macos-27-beta-arm64": "unsupported-preview",
    }
    assert matrix["ci_capacity"]["status"] == "passed-hosted-core-unverified"
    assert matrix["clean_system_capacity"]["status"] == "not-provisioned"
    assert matrix["bambu_studio"]["phase13b_status"] == "unverified"


def test_native_macos_ci_uses_only_frozen_arm64_runner_labels() -> None:
    root = Path(__file__).parents[2]
    workflow_path = root / ".github/workflows/macos.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    job = workflow["jobs"]["native-arm64"]
    runners = {item["runner"] for item in job["strategy"]["matrix"]["include"]}
    step_runs = {step.get("name"): step.get("run") for step in job["steps"]}

    assert runners == {"macos-15", "macos-26"}
    assert job["env"]["MACOSX_DEPLOYMENT_TARGET"] == "15.0"
    assert step_runs["Run Ruff lint gate"] == "uv run ruff check ."
    assert step_runs["Run Ruff format gate"] == "uv run ruff format --check ."
    assert step_runs["Run Pyright gate"] == "uv run pyright"
    assert step_runs["Run release regression suite"] == "uv run pytest tests/release"
    assert step_runs["Run slicer regression suite"] == "uv run pytest tests/slicer"
    assert step_runs["Run unit regression suite"] == "uv run pytest tests/unit"
    assert step_runs["Run Web API regression suite"] == "uv run pytest tests/web/test_api.py"
    assert step_runs["Run recovered Web job cancellation regression"] == (
        "uv run pytest "
        "tests/web/test_jobs.py::test_running_job_recovers_and_cancels_after_manager_restart"
    )
    assert step_runs["Run remaining Web job regressions"] == (
        'uv run pytest tests/web/test_jobs.py -k "not '
        'running_job_recovers_and_cancels_after_manager_restart"'
    )
    assert (
        step_runs["Run Web map tile regression suite"]
        == "uv run pytest tests/web/test_map_tiles.py"
    )
    assert (
        step_runs["Run Web process regression suite"] == "uv run pytest tests/web/test_processes.py"
    )
    assert step_runs["Run CLI, geometry, and property regression suite"] == (
        "uv run pytest tests/cli tests/geometry tests/property"
    )
    assert step_runs["Run provider regression suite"] == "uv run pytest tests/providers"
    assert step_runs["Run integration regression suite"] == "uv run pytest tests/integration"
    assert "macos-latest" not in workflow_text
    assert 'test "$(uname -m)" = "arm64"' in workflow_text
    assert "scripts/verify_macos_support_matrix.py" in workflow_text
    assert "scripts/collect_macos_ci_evidence.py" in workflow_text
    assert "actions/upload-artifact@v4" in workflow_text
    assert "if: always()" in workflow_text
    assert "runtime-evidence" in workflow_text
    assert "topoforge web --check --no-open" in workflow_text
    assert "TopoForge Phase 13/地形" in workflow_text
    assert workflow_text.count('cmp "') == 3
    assert "support unverified" in workflow_text


def test_documentation_does_not_promote_macos_support() -> None:
    root = Path(__file__).parents[2]
    documentation = (root / "docs/macos-support.md").read_text(encoding="utf-8")

    assert "does **not** currently claim macOS support" in documentation
    assert "Native hosted macOS core CI has passed" in documentation
    assert "actions/runs/31419016599" in documentation
    assert "This closes only the hosted native core portion of Phase 13A" in documentation
    assert "unsupported today" in documentation
    assert "Intel x86_64 is unsupported for 0.12.x" in documentation
    assert "README and release metadata must continue to say macOS is" in documentation
    assert "unverified" in documentation
