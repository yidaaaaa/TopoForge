#!/usr/bin/env python3
"""Collect bounded native macOS CI evidence without promoting support."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "topoforge-macos-ci-runtime-v1"
NATIVE_IMPORTS = {
    "lib3mf": "lib3mf",
    "numpy": "numpy",
    "pillow": "PIL",
    "pyproj": "pyproj",
    "rasterio": "rasterio",
    "scipy": "scipy",
    "shapely": "shapely",
    "trimesh": "trimesh",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _valid_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _valid_git_sha(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 40:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _command_output(command: Sequence[str], *, optional: bool = False) -> str | None:
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode == 0:
        return completed.stdout.strip()
    if optional:
        return None
    detail = completed.stderr.strip() or completed.stdout.strip() or "no command output"
    raise RuntimeError(f"{' '.join(command)} exited {completed.returncode}: {detail}")


def _package_imports() -> dict[str, dict[str, Any]]:
    packages: dict[str, dict[str, Any]] = {}
    for distribution, module_name in NATIVE_IMPORTS.items():
        try:
            module = importlib.import_module(module_name)
            packages[distribution] = {
                "version": importlib.metadata.version(distribution),
                "module": module_name,
                "module_path": str(getattr(module, "__file__", None)),
                "imported": True,
                "error": None,
            }
        except Exception as exc:
            packages[distribution] = {
                "version": None,
                "module": module_name,
                "module_path": None,
                "imported": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return packages


def evaluate_macos_ci_snapshot(
    matrix: Mapping[str, Any],
    *,
    runner_label: str,
    snapshot: Mapping[str, Any],
    packages: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate one hosted-runner snapshot against the frozen candidate matrix."""
    problems: list[str] = []
    raw_targets = matrix.get("phase13a_targets")
    targets = raw_targets if isinstance(raw_targets, list) else []
    target = next(
        (
            item
            for item in targets
            if isinstance(item, dict) and item.get("ci_runner") == runner_label
        ),
        None,
    )
    if target is None:
        problems.append(f"runner label is outside the frozen matrix: {runner_label}")
        target = {}

    if matrix.get("public_support_status") != "unverified":
        problems.append("matrix must remain explicitly unverified during hosted CI")
    if snapshot.get("system") != "Darwin":
        problems.append("runtime system is not Darwin")
    if str(snapshot.get("machine", "")).casefold() not in {"arm64", "aarch64"}:
        problems.append("runtime machine is not native Apple Silicon arm64")
    if str(snapshot.get("uname_machine", "")).casefold() not in {"arm64", "aarch64"}:
        problems.append("uname does not report native Apple Silicon arm64")

    target_version = str(target.get("os_version", ""))
    actual_version = str(snapshot.get("macos_version", ""))
    if target_version and actual_version.split(".", 1)[0] != target_version.split(".", 1)[0]:
        problems.append(
            f"runner macOS major {actual_version!r} does not match target {target_version!r}"
        )
    if not str(snapshot.get("python_version", "")).startswith("3.12."):
        problems.append("runtime Python is outside the frozen 3.12 minor")
    if snapshot.get("deployment_target") != target.get("deployment_target"):
        problems.append("MACOSX_DEPLOYMENT_TARGET differs from the frozen target")
    if snapshot.get("translated") == "1":
        problems.append("runtime is translated under Rosetta instead of native arm64")

    source_commit = snapshot.get("source_commit_sha")
    if not _valid_git_sha(source_commit):
        problems.append("source commit SHA is missing or invalid")
    github_sha = snapshot.get("github_sha")
    if not _valid_git_sha(github_sha):
        problems.append("GitHub workflow SHA is missing or invalid")
    elif github_sha != source_commit:
        problems.append("source commit differs from the GitHub workflow SHA")
    if snapshot.get("source_tree_dirty") is not False:
        problems.append("source tree is dirty during hosted evidence collection")
    for field in ("matrix_sha256", "collector_sha256", "workflow_sha256"):
        if not _valid_sha256(snapshot.get(field)):
            problems.append(f"source identity hash is missing or invalid: {field}")

    for distribution in NATIVE_IMPORTS:
        package = packages.get(distribution)
        if not isinstance(package, Mapping) or package.get("imported") is not True:
            problems.append(f"native dependency import failed: {distribution}")

    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_scope": "github-hosted-native-ci-only",
        "public_support_status": "unverified",
        "runner_label": runner_label,
        "target_id": target.get("id"),
        "target_os_version": target.get("os_version"),
        "target_architecture": target.get("architecture"),
        "snapshot": dict(snapshot),
        "packages": {key: dict(value) for key, value in packages.items()},
        "source_tree_evidence": True,
        "clean_system_evidence": False,
        "package_evidence": False,
        "persistent_release_evidence": False,
        "gatekeeper_evidence": False,
        "bambu_phase13b_evidence": False,
        "limitations": [
            "Hosted runner patch versions are implementation evidence, not the public matrix.",
            "This report does not establish clean-system, package, signing, notarization, "
            "Gatekeeper, or Bambu Studio support.",
            "This source-tree run must be repeated after the audited Phase 12 foundation "
            "is integrated and cannot close a final Phase 13 gate.",
        ],
        "problems": problems,
        "required_checks_passed": not problems,
    }


def collect_macos_ci_evidence(matrix_path: Path, runner_label: str) -> dict[str, Any]:
    """Collect and validate the active native macOS runner."""
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix_path = matrix_path.resolve()
    repository_root = matrix_path.parents[1]
    collector_path = Path(__file__).resolve()
    workflow_path = repository_root / ".github" / "workflows" / "macos.yml"
    source_commit = _command_output(("git", "-C", str(repository_root), "rev-parse", "HEAD"))
    source_status = _command_output(
        (
            "git",
            "-C",
            str(repository_root),
            "status",
            "--porcelain",
            "--untracked-files=all",
        )
    )
    snapshot = {
        "system": platform.system(),
        "machine": platform.machine(),
        "uname_machine": _command_output(("uname", "-m")),
        "macos_version": _command_output(("sw_vers", "-productVersion")),
        "macos_build": _command_output(("sw_vers", "-buildVersion")),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_executable": str(Path(sys.executable).resolve()),
        "python_executable_identity": _command_output(("file", "-b", sys.executable)),
        "python_platform": sysconfig.get_platform(),
        "deployment_target": os.environ.get("MACOSX_DEPLOYMENT_TARGET"),
        "translated": _command_output(("sysctl", "-in", "sysctl.proc_translated"), optional=True),
        "source_commit_sha": source_commit,
        "github_sha": os.environ.get("GITHUB_SHA"),
        "source_tree_dirty": bool(source_status),
        "matrix_sha256": _sha256(matrix_path),
        "collector_sha256": _sha256(collector_path),
        "workflow_sha256": _sha256(workflow_path),
    }
    return evaluate_macos_ci_snapshot(
        matrix,
        runner_label=runner_label,
        snapshot=snapshot,
        packages=_package_imports(),
    )


def write_evidence_report(path: Path, report: Mapping[str, Any]) -> str:
    """Write canonical JSON plus a detached SHA-256 sidecar and return the JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    path.write_text(rendered, encoding="utf-8")
    digest = _sha256(path)
    sidecar = path.with_name(f"{path.name}.sha256")
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return rendered


def main() -> int:
    """Write one canonical hosted-runner evidence report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=Path("docs/macos-support-matrix.json"))
    parser.add_argument("--runner-label", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    report = collect_macos_ci_evidence(args.matrix.resolve(), args.runner_label)
    rendered = write_evidence_report(args.report, report)
    print(rendered, end="")
    return 0 if report["required_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
