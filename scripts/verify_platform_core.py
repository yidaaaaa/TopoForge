#!/usr/bin/env python3
"""Verify the portable TopoForge core through its public CLI on one host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "topoforge-platform-core-verification-v1"
DETERMINISTIC_ROLES = ("model.stl", "model.3mf", "preview.glb")
REQUIRED_GEOMETRY_CHECKS = (
    "required_checks_passed",
    "finite_vertices",
    "finite_face_normals",
    "watertight",
    "winding_consistent",
    "manifold",
    "positive_volume",
    "flat_bottom",
    "height_limit_passed",
    "minimum_base_thickness_passed",
    "triangle_budget_passed",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(
    arguments: list[str],
    *,
    cwd: Path,
    commands: list[dict[str, Any]],
    python_executable: Path,
) -> dict[str, Any]:
    command = [
        str(python_executable),
        "-I",
        "-X",
        "utf8",
        "-m",
        "topoforge.cli.app",
        *arguments,
    ]
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    record: dict[str, Any] = {
        "command": command,
        "cwd": str(cwd),
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    commands.append(record)
    if completed.returncode != 0:
        raise RuntimeError(
            f"CLI command failed with exit code {completed.returncode}: "
            f"{' '.join(arguments)}\n{completed.stderr or completed.stdout}"
        )
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError(f"CLI command did not emit JSON: {' '.join(arguments)}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"CLI command emitted a non-object result: {' '.join(arguments)}")
    return payload


def _artifact_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name in DETERMINISTIC_ROLES:
        path = root / name
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"deterministic manufacturing role is missing: {path}")
        hashes[name] = _sha256(path)
    return hashes


def _validation(root: Path) -> dict[str, Any]:
    path = root / "validation.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"validation report is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"validation report root is not an object: {path}")
    failed = [name for name in REQUIRED_GEOMETRY_CHECKS if value.get(name) is not True]
    if failed:
        raise RuntimeError(f"manufacturing validation checks failed: {failed}")
    if value.get("connected_components") != 1:
        raise RuntimeError("manufacturing validation did not report one connected component")
    if value.get("degenerate_faces") != 0 or value.get("duplicate_faces") != 0:
        raise RuntimeError("manufacturing validation reported degenerate or duplicate faces")
    dimensions = value.get("dimensions_mm")
    if (
        not isinstance(dimensions, list)
        or len(dimensions) != 3
        or any(not isinstance(item, int | float) or item <= 0 for item in dimensions)
    ):
        raise RuntimeError("manufacturing validation dimensions are invalid")
    orientation = value.get("orientation")
    if value.get("orientation_consistent") is not True or not isinstance(orientation, dict):
        raise RuntimeError("manufacturing orientation checks did not pass")
    expected_axes = {
        "east_axis": "+X = East",
        "north_axis": "+Y = North",
        "up_axis": "+Z = Up",
        "north_edge": "y=model_depth_mm",
    }
    if any(orientation.get(key) != expected for key, expected in expected_axes.items()):
        raise RuntimeError("manufacturing orientation axis contract changed")
    return value


def _absolute_python_executable(path: Path) -> Path:
    """Return an absolute interpreter path without resolving a virtualenv symlink."""
    return Path(os.path.abspath(path.expanduser()))


def verify_platform_core(
    work_root: Path,
    *,
    python_executable: Path | None = None,
) -> dict[str, Any]:
    """Run deterministic CLI builds and strict format/Web checks below one root."""
    python = Path(sys.executable) if python_executable is None else python_executable
    python = _absolute_python_executable(python)
    if not python.is_file():
        raise FileNotFoundError(
            f"Python executable does not exist: {python}. "
            "Provide --python-executable with an installed or portable interpreter."
        )
    root = work_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    path_probe = root / "workspace with spaces" / "地形"
    path_probe.mkdir(parents=True)
    source = path_probe / "synthetic terrain.tif"
    first = path_probe / "first build"
    repeat = path_probe / "repeat build"
    commands: list[dict[str, Any]] = []

    doctor = _run(["doctor"], cwd=path_probe, commands=commands, python_executable=python)
    synthetic = _run(
        [
            "synthetic",
            "--output",
            str(source),
            "--terrain",
            "saddle",
            "--rows",
            "18",
            "--columns",
            "24",
            "--pixel-size-m",
            "20",
        ],
        cwd=path_probe,
        commands=commands,
        python_executable=python,
    )
    if synthetic.get("sha256") != _sha256(source):
        raise RuntimeError("synthetic source checksum does not match the CLI result")

    common_build = [
        "build",
        "--dem",
        str(source),
        "--size-mm",
        "64",
        "48",
        "--base-mm",
        "3",
        "--max-height-mm",
        "20",
        "--vertical-scale",
        "fit-height",
        "--sampling-mode",
        "source-preserving",
        "--max-grid-cells",
        "10000",
        "--max-estimated-triangles",
        "50000",
        "--resource-budget-mode",
        "strict",
        "--dataset-type",
        "dtm",
        "--dataset-name",
        "Phase 12 deterministic synthetic",
        "--dataset-version",
        "phase12-v1",
        "--data-license",
        "Apache-2.0 synthetic fixture",
        "--attribution",
        "TopoForge deterministic fixture",
    ]
    first_result = _run(
        [*common_build, "--output", str(first)],
        cwd=path_probe,
        commands=commands,
        python_executable=python,
    )
    repeat_result = _run(
        [*common_build, "--output", str(repeat)],
        cwd=path_probe,
        commands=commands,
        python_executable=python,
    )
    first_validation = _validation(first)
    repeat_validation = _validation(repeat)
    if first_validation["dimensions_mm"] != repeat_validation["dimensions_mm"]:
        raise RuntimeError("repeat build dimensions changed")
    if first_validation["volume_mm3"] != repeat_validation["volume_mm3"]:
        raise RuntimeError("repeat build volume changed")
    first_hashes = _artifact_hashes(first)
    repeat_hashes = _artifact_hashes(repeat)
    deterministic = {
        role: first_hashes[role] == repeat_hashes[role] for role in DETERMINISTIC_ROLES
    }
    if not all(deterministic.values()):
        raise RuntimeError(f"manufacturing artifact determinism failed: {deterministic}")

    three_mf = _run(
        ["inspect", str(first / "model.3mf")],
        cwd=path_probe,
        commands=commands,
        python_executable=python,
    )
    if three_mf.get("strict_warning_count") != 0:
        raise RuntimeError("strict 3MF reopen reported warnings")
    if three_mf.get("dimensions_mm") != first_validation["dimensions_mm"]:
        raise RuntimeError("strict 3MF dimensions differ from validation")
    stl = _run(
        ["validate", str(first / "model.stl")],
        cwd=path_probe,
        commands=commands,
        python_executable=python,
    )
    failed_stl = [
        key
        for key in ("watertight", "winding_consistent", "manifold", "positive_volume")
        if stl.get(key) is not True
    ]
    if failed_stl:
        raise RuntimeError(f"reopened STL geometry checks failed: {failed_stl}")
    glb = _run(
        ["inspect", str(first / "preview.glb")],
        cwd=path_probe,
        commands=commands,
        python_executable=python,
    )
    failed_glb = [
        key
        for key in ("watertight", "winding_consistent", "manifold", "positive_volume")
        if glb.get(key) is not True
    ]
    if failed_glb:
        raise RuntimeError(f"reopened GLB geometry checks failed: {failed_glb}")

    web_state = path_probe / "web state"
    web_workspaces = path_probe / "web workspaces"
    web_input = path_probe / "web input"
    web_input.mkdir()
    web = _run(
        [
            "web",
            "--check",
            "--state-dir",
            str(web_state),
            "--workspace-root",
            str(web_workspaces),
            "--input-root",
            str(web_input),
            "--no-open",
        ],
        cwd=path_probe,
        commands=commands,
        python_executable=python,
    )
    if web.get("required_checks_passed") is not True:
        raise RuntimeError("packaged Web installation check did not pass")
    if web.get("assets", {}).get("languages") != ["zh-CN", "en"]:
        raise RuntimeError("packaged Web installation languages are incomplete")

    return {
        "schema_version": SCHEMA_VERSION,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "python": doctor.get("python"),
            "python_executable": str(python),
        },
        "path_contract": {
            "root": str(path_probe),
            "contains_spaces": " " in str(path_probe),
            "contains_non_ascii": any(ord(character) > 127 for character in str(path_probe)),
            "required_checks_passed": True,
        },
        "doctor": doctor,
        "synthetic": synthetic,
        "builds": {
            "first": first_result,
            "repeat": repeat_result,
            "dimensions_mm": first_validation["dimensions_mm"],
            "volume_mm3": first_validation["volume_mm3"],
            "triangle_count": first_validation["triangle_count"],
            "connected_components": first_validation["connected_components"],
            "degenerate_faces": first_validation["degenerate_faces"],
            "duplicate_faces": first_validation["duplicate_faces"],
            "bottom_planarity_error_mm": first_validation["bottom_planarity_error_mm"],
            "orientation": first_validation["orientation"],
        },
        "artifacts": {
            "first_sha256": first_hashes,
            "repeat_sha256": repeat_hashes,
            "deterministic": deterministic,
        },
        "strict_reopen": {"three_mf": three_mf, "stl": stl, "glb": glb},
        "web": web,
        "commands": commands,
        "required_checks_passed": True,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    """Run the host verification and retain a report even when a gate fails."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--python-executable", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    report_path = args.report.expanduser().resolve()
    try:
        if args.work_root is not None:
            report = verify_platform_core(
                args.work_root.expanduser(),
                python_executable=args.python_executable,
            )
        else:
            with tempfile.TemporaryDirectory(prefix="topoforge-platform-core-") as temporary:
                report = verify_platform_core(
                    Path(temporary) / "verification",
                    python_executable=args.python_executable,
                )
    except Exception as exc:
        failure = {
            "schema_version": SCHEMA_VERSION,
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "python_executable": sys.executable,
                "requested_python_executable": str(args.python_executable),
            },
            "error": {"type": exc.__class__.__name__, "message": str(exc)},
            "required_checks_passed": False,
        }
        _write_report(report_path, failure)
        raise
    _write_report(report_path, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
