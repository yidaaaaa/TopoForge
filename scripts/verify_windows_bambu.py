#!/usr/bin/env python3
"""Run official Bambu Studio slice, project export, reopen, and reslice acceptance."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import topoforge
from topoforge.models import BuildConfig, ResourceBudgetMode, SamplingMode
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.util import sha256_file
from topoforge.validation import verify_bambu_project_evidence
from topoforge.validation.slicers import BambuStudioAdapter, SlicerAvailability
from topoforge.validation.slicers._bambu_profiles import (
    PreparedBambuProfiles,
    prepare_bambu_profiles,
)
from topoforge.validation.slicers._bambu_windows import discover_bambu_profiles_root
from topoforge.workflow import (
    WorkflowLaunchConfig,
    WorkflowStage,
    WorkflowState,
    execute_workflow_launch,
)

SCHEMA_VERSION = "topoforge-windows-bambu-verification-v1"


def _platform_record(*, require_windows: bool) -> dict[str, Any]:
    system = platform.system()
    machine = platform.machine()
    return {
        "system": system,
        "release": platform.release(),
        "version": platform.version(),
        "machine": machine,
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "topoforge": topoforge.__version__,
        "native_windows_required": require_windows,
        "native_windows_verified": system == "Windows",
    }


def _require_host(platform_record: dict[str, Any]) -> None:
    if platform_record["system"] != "Windows":
        raise RuntimeError("--require-windows requires a native Windows host")
    if str(platform_record["machine"]).casefold() not in {"amd64", "x86_64"}:
        raise RuntimeError("--require-windows requires a native Windows x64 host")


def _file_record(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError(f"required acceptance artifact is missing: {resolved}")
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"acceptance JSON is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"acceptance JSON root is not an object: {path}")
    return value


def _relative_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if path == root or root not in path.parents:
        raise RuntimeError(f"acceptance manifest path escapes its root: {relative}")
    if not path.is_file():
        raise RuntimeError(f"acceptance manifest file is missing: {path}")
    return path


@contextlib.contextmanager
def _bambu_override(executable: Path) -> Iterator[None]:
    key = "TOPOFORGE_BAMBU_STUDIO"
    previous = os.environ.get(key)
    os.environ[key] = str(executable)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _resolve_installation(
    *,
    executable: Path | None,
    profiles_root: Path | None,
    profile_cache: Path,
) -> tuple[Path, Path, PreparedBambuProfiles, dict[str, Any]]:
    adapter = BambuStudioAdapter(executable)
    probe = adapter.probe(refresh=True)
    if (
        probe.status is not SlicerAvailability.AVAILABLE
        or probe.executable is None
        or probe.version is None
    ):
        detail = probe.detail or "version probe did not return an executable and version"
        raise RuntimeError(f"official Bambu Studio is not ready: {detail}")
    resolved_executable = probe.executable.expanduser().resolve()
    resolved_profiles = discover_bambu_profiles_root(
        resolved_executable,
        explicit=profiles_root,
    )
    if resolved_profiles is None:
        raise RuntimeError(
            "official Bambu profiles were not found; pass --bambu-profiles-root "
            "for the resources/profiles/BBL directory"
        )
    prepared = prepare_bambu_profiles(
        resolved_profiles,
        profile_cache,
        executable=resolved_executable,
        executable_version=probe.version,
    )
    return (
        resolved_executable,
        resolved_profiles,
        prepared,
        probe.model_dump(mode="json"),
    )


def _project_contract(
    project_dir: Path,
    *,
    executable: Path,
    expected_version: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = project_dir / "bambu-tile-project-manifest.json"
    manifest = _json_object(manifest_path)
    if (
        manifest.get("required_checks_passed") is not True
        or manifest.get("all_projects_reopened") is not True
        or manifest.get("all_release_gates_passed") is not True
        or manifest.get("bambu_studio_path") != str(executable)
        or manifest.get("bambu_studio_sha256") != sha256_file(executable)
        or manifest.get("bambu_studio_version") != expected_version
    ):
        raise RuntimeError("official Bambu project root contract did not pass")
    tiles = manifest.get("tiles")
    if not isinstance(tiles, list) or not tiles:
        raise RuntimeError("official Bambu project manifest contains no tile evidence")
    tile_records: list[dict[str, Any]] = []
    for record in tiles:
        if not isinstance(record, dict) or not isinstance(record.get("validation_path"), str):
            raise RuntimeError("official Bambu project tile record is invalid")
        validation_path = _relative_file(project_dir, record["validation_path"])
        validation = _json_object(validation_path)
        if (
            validation.get("required_checks_passed") is not True
            or validation.get("external_profiles_loaded_on_reopen") is not False
            or validation.get("primary_slicer") != validation.get("reopen_slicer")
            or validation.get("primary_slicer", {}).get("version") != expected_version
        ):
            raise RuntimeError(
                f"official Bambu no-profile reopen contract failed: {validation_path}"
            )
        tile_records.append(
            {
                "tile_id": record.get("tile_id"),
                "validation": _file_record(validation_path),
                "external_profiles_loaded_on_reopen": False,
                "required_checks_passed": True,
            }
        )
    return manifest, tile_records


def verify_windows_bambu(
    work_root: Path,
    *,
    executable: Path | None = None,
    profiles_root: Path | None = None,
    require_windows: bool = False,
    slice_timeout_seconds: float = 1200.0,
    project_timeout_seconds: float = 1800.0,
) -> dict[str, Any]:
    """Run one exact official P2S workflow and retain strict reopen evidence."""
    platform_record = _platform_record(require_windows=require_windows)
    if require_windows:
        _require_host(platform_record)
    if slice_timeout_seconds <= 0 or project_timeout_seconds <= 0:
        raise ValueError("Bambu acceptance timeouts must be positive")

    root = work_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    path_probe = root / "Windows Bambu path with spaces" / "地形"
    path_probe.mkdir(parents=True)
    resolved_executable, resolved_profiles, prepared, probe = _resolve_installation(
        executable=executable,
        profiles_root=profiles_root,
        profile_cache=path_probe / "profile-cache",
    )

    source_dir = path_probe / "inputs"
    source_dir.mkdir()
    source = create_synthetic_geotiff(
        source_dir / "official P2S terrain.tif",
        SyntheticTerrain.SADDLE,
        rows=12,
        columns=16,
        pixel_size_m=20.0,
    )
    workspace = path_probe / "workspaces" / "official P2S workflow"
    launch = WorkflowLaunchConfig(
        workspace_dir=workspace,
        build=BuildConfig(
            dem_path=source,
            output_dir=workspace,
            model_width_mm=40.0,
            max_height_mm=20.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=10_000,
            max_estimated_triangles=50_000,
            max_estimated_memory_mb=1024.0,
            resource_budget_mode=ResourceBudgetMode.STRICT,
        ),
        maximum_tile_width_mm=180.0,
        maximum_tile_depth_mm=180.0,
        overlap_cells=1,
        slicing_enabled=True,
        slicer_name="bambu-studio",
        slicer_settings=(prepared.machine_profile, prepared.process_profile),
        slicer_filaments=(prepared.filament_profile,),
        slice_timeout_seconds=slice_timeout_seconds,
        project_evidence_enabled=True,
        project_timeout_seconds=project_timeout_seconds,
    )
    with _bambu_override(resolved_executable):
        execution = execute_workflow_launch(launch)
    result = execution.workflow
    if (
        not result.required_checks_passed
        or execution.summary.state is not WorkflowState.COMPLETED
        or execution.summary.final_stage is not WorkflowStage.PROJECT
    ):
        raise RuntimeError("official Bambu workflow did not complete every required stage")
    required_stages = {WorkflowStage.CONNECT, WorkflowStage.SLICE, WorkflowStage.PROJECT}
    if not required_stages <= set(result.stage_outputs):
        raise RuntimeError("official Bambu workflow omitted required manufacturing stages")

    print_dir = result.stage_outputs[WorkflowStage.CONNECT]
    slice_dir = result.stage_outputs[WorkflowStage.SLICE]
    project_dir = result.stage_outputs[WorkflowStage.PROJECT]
    project_verification = verify_bambu_project_evidence(
        project_dir,
        print_set_dir=print_dir,
        slice_set_dir=slice_dir,
        bambu_studio=resolved_executable,
    )
    slice_manifest_path = slice_dir / "tile-slice-manifest.json"
    slice_manifest = _json_object(slice_manifest_path)
    if (
        slice_manifest.get("release_role") != "official-p2s-release"
        or slice_manifest.get("official_p2s_release_gate_passed") is not True
        or slice_manifest.get("all_parameter_checks_passed") is not True
    ):
        raise RuntimeError("official Bambu slice release gate did not pass")
    project_manifest, tile_records = _project_contract(
        project_dir,
        executable=resolved_executable,
        expected_version=str(probe["version"]),
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "platform": platform_record,
        "path_contract": {
            "root": str(path_probe),
            "contains_spaces": " " in str(path_probe),
            "contains_non_ascii": any(ord(character) > 127 for character in str(path_probe)),
            "required_checks_passed": True,
        },
        "bambu_studio": {
            "probe": probe,
            "executable": _file_record(resolved_executable),
            "profiles_root": str(resolved_profiles),
            "required_checks_passed": True,
        },
        "profile_bundle": {
            "manifest": _file_record(prepared.manifest_path),
            "machine": _file_record(prepared.machine_profile),
            "process": _file_record(prepared.process_profile),
            "filament": _file_record(prepared.filament_profile),
            "required_checks_passed": True,
        },
        "workflow": {
            "workflow_id": result.workflow_id,
            "state": execution.summary.state.value,
            "final_stage": execution.summary.final_stage.value,
            "completed_stages": [stage.value for stage in result.completed_stages],
            "reused_stages": [stage.value for stage in result.reused_stages],
            "manifest": _file_record(result.manifest_path),
            "status": _file_record(result.status_path),
            "summary": _file_record(execution.summary_path),
            "report": _file_record(execution.report_path),
            "source": _file_record(source),
            "required_checks_passed": True,
        },
        "official_slice": {
            "manifest": _file_record(slice_manifest_path),
            "tile_count": slice_manifest.get("tile_count"),
            "release_role": slice_manifest["release_role"],
            "official_p2s_release_gate_passed": True,
            "all_parameter_checks_passed": True,
            "required_checks_passed": True,
        },
        "official_project": {
            "manifest": _file_record(project_dir / "bambu-tile-project-manifest.json"),
            "tile_count": project_manifest.get("tile_count"),
            "bambu_studio_version": project_manifest["bambu_studio_version"],
            "all_projects_reopened": True,
            "all_release_gates_passed": True,
            "external_profiles_loaded_on_reopen": False,
            "verification": project_verification,
            "tiles": tile_records,
            "required_checks_passed": True,
        },
        "claim_boundary": (
            "official Bambu Studio software slice/export/reopen/reslice evidence on this "
            "recorded host only; no physical-print, vendor-certification, or other-Windows-"
            "version claim"
        ),
        "required_checks_passed": True,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def main() -> int:
    """Run native official-Bambu acceptance and retain success or failure evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--bambu-studio-executable", type=Path)
    parser.add_argument("--bambu-profiles-root", type=Path)
    parser.add_argument("--slice-timeout-seconds", type=float, default=1200.0)
    parser.add_argument("--project-timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--require-windows", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify_windows_bambu(
            args.work_root,
            executable=args.bambu_studio_executable,
            profiles_root=args.bambu_profiles_root,
            require_windows=args.require_windows,
            slice_timeout_seconds=args.slice_timeout_seconds,
            project_timeout_seconds=args.project_timeout_seconds,
        )
    except BaseException as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "platform": _platform_record(require_windows=args.require_windows),
            "work_root": str(args.work_root.expanduser().resolve()),
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "required_checks_passed": False,
        }
        _write_report(args.report, report)
        print(json.dumps(report, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    _write_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
