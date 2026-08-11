"""Export, reopen, and verify per-tile Bambu Studio project 3MF evidence."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from pydantic import BaseModel, ConfigDict

from topoforge.exporters.three_mf import inspect_3mf
from topoforge.validation.manufacturing import evaluate_bambu_p2s_release_gate
from topoforge.validation.slicers import parse_gcode_generator, parse_gcode_metrics
from topoforge.validation.slicers.bambu import parse_bambu_studio_version
from topoforge.validation.slicers.base import CommandExecution, run_command

SCHEMA_VERSION = "topoforge-bambu-tile-project-assembly-v1"
TILE_SCHEMA_VERSION = "topoforge-bambu-tile-project-v1"


@dataclass(frozen=True, slots=True)
class _EvidenceArgs:
    print_set: Path
    slice_set: Path
    bambu_studio: Path
    output: Path
    timeout: float


class BambuProjectEvidenceResult(BaseModel):
    """Published Bambu project evidence paths and strict verification summary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_dir: Path
    manifest_path: Path
    verification: dict[str, Any]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    """Return the SHA-256 of a UTF-8 diagnostic stream."""
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def write_canonical(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(canonical_bytes(value))
    temporary.replace(path)
    return path


def resolve_relative(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise RuntimeError(f"path escapes evidence directory: {relative}")
    return candidate


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON root is not an object: {path}")
    return value


def execution_record(execution: CommandExecution, command: list[str]) -> dict[str, Any]:
    return {
        "command": command,
        "process_exit_code": execution.returncode,
        "duration_seconds": execution.duration_seconds,
    }


def result_passed(value: dict[str, Any]) -> bool:
    plates = value.get("sliced_plates")
    return bool(
        value.get("return_code") == 0
        and value.get("error_string") in {"Success", "Success."}
        and isinstance(plates, list)
        and len(plates) == 1
        and isinstance(plates[0], dict)
        and plates[0].get("warning_message") in {None, ""}
    )


def gcode_bambu_version(gcode: Path) -> str:
    """Parse and validate one independently generated Bambu G-code version."""
    generator = parse_gcode_generator(gcode.read_text(encoding="utf-8", errors="replace"))
    if generator is None or generator[0].casefold().replace(" ", "") != "bambustudio":
        raise RuntimeError(f"G-code does not identify Bambu Studio as its generator: {gcode}")
    return generator[1]


def release_gate(
    gcode: Path,
    *,
    expected_version: str,
    stdout: str,
    stderr: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    actual_version = gcode_bambu_version(gcode)
    if actual_version != expected_version:
        raise RuntimeError(
            f"Bambu Studio G-code version {actual_version!r} does not match the frozen "
            f"source-slice version {expected_version!r}: {gcode}"
        )
    gcode_text = gcode.read_text(encoding="utf-8", errors="replace")
    metrics = parse_gcode_metrics(
        gcode_text,
        diagnostics="\n".join((stdout, stderr)),
    )
    payload = {
        "slicer": {"name": "BambuStudio", "version": actual_version},
        "status": "succeeded",
        "exit_code": 0,
        "gcode_generated": True,
        "metrics": metrics.model_dump(mode="json"),
    }
    gate = evaluate_bambu_p2s_release_gate(payload, printer_profile_id="bambu-p2s-0.4")
    return metrics.model_dump(mode="json"), gate, actual_version


def archive_evidence(project: Path, primary_gcode: Path) -> dict[str, Any]:
    with ZipFile(project, "r") as package:
        bad_member = package.testzip()
        names = set(package.namelist())
        required = {
            "[Content_Types].xml",
            "_rels/.rels",
            "3D/3dmodel.model",
            "Metadata/plate_1.gcode",
            "Metadata/plate_1.gcode.md5",
            "Metadata/project_settings.config",
        }
        if bad_member is not None or not required.issubset(names):
            raise RuntimeError(f"Bambu project archive is invalid: {project}")
        embedded = package.read("Metadata/plate_1.gcode")
        recorded_md5 = package.read("Metadata/plate_1.gcode.md5").decode("ascii").strip().upper()
    actual_md5 = hashlib.md5(embedded, usedforsecurity=False).hexdigest().upper()
    primary = primary_gcode.read_bytes()
    return {
        "archive_test_passed": True,
        "embedded_gcode_md5": recorded_md5,
        "embedded_gcode_md5_actual": actual_md5,
        "embedded_gcode_md5_verified": recorded_md5 == actual_md5,
        "embedded_gcode_matches_primary": embedded == primary,
    }


def profile_paths(slice_root: Path, manifest: dict[str, Any]) -> tuple[list[Path], list[Path]]:
    settings: list[Path] = []
    filaments: list[Path] = []
    for item in manifest.get("profile_files", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise RuntimeError("official slice manifest has an invalid profile record")
        source = resolve_relative(slice_root, item["path"])
        if sha256(source) != item.get("sha256"):
            raise RuntimeError(f"official slice profile checksum mismatch: {source}")
        if item.get("role") == "settings":
            settings.append(source)
        elif item.get("role") == "filament":
            filaments.append(source)
        else:
            raise RuntimeError(f"unknown official slice profile role: {item.get('role')}")
    if len(settings) != 2 or len(filaments) != 1:
        raise RuntimeError("Bambu project export requires machine, process, and filament profiles")
    return settings, filaments


def frozen_source_bambu_version(manifest: Mapping[str, Any]) -> str:
    """Return the source slice version that project evidence must preserve."""
    slicer = manifest.get("slicer")
    if not isinstance(slicer, Mapping):
        raise RuntimeError("official slice manifest has no frozen slicer identity")
    version = slicer.get("version")
    if (
        slicer.get("name") != "BambuStudio"
        or slicer.get("status") != "available"
        or not isinstance(version, str)
        or not version
    ):
        raise RuntimeError("official slice manifest must freeze an available Bambu Studio version")
    return version


def isolated_environment(
    runtime: Path,
    *,
    system: str | None = None,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a platform-specific temporary Bambu Studio user environment."""
    system_name = platform.system() if system is None else system
    active_platform = platform_name
    if active_platform is None:
        if system_name == "Windows":
            active_platform = "win32"
        elif system_name == "Darwin":
            active_platform = "darwin"
        else:
            active_platform = sys.platform
    environment = dict(os.environ if environ is None else environ)
    home = runtime / "home"
    if active_platform == "win32":
        for key in (
            "APPIMAGE_EXTRACT_AND_RUN",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_RUNTIME_DIR",
        ):
            environment.pop(key, None)
        roaming = home / "AppData" / "Roaming"
        local = home / "AppData" / "Local"
        temporary = runtime / "temp"
        for path in (home, roaming, local, temporary):
            path.mkdir(parents=True, exist_ok=True)
        environment.update(
            {
                "APPDATA": str(roaming),
                "HOME": str(home),
                "LOCALAPPDATA": str(local),
                "TEMP": str(temporary),
                "TMP": str(temporary),
                "USERPROFILE": str(home),
            }
        )
        drive, tail = os.path.splitdrive(str(home))
        if drive:
            environment.update({"HOMEDRIVE": drive, "HOMEPATH": tail})
        return environment
    if active_platform == "darwin":
        application_support = home / "Library" / "Application Support"
        preferences = home / "Library" / "Preferences"
        caches = home / "Library" / "Caches"
        temporary = runtime / "tmp"
        for path in (home, application_support, preferences, caches, temporary):
            path.mkdir(parents=True, exist_ok=True)
        for key in (
            "APPIMAGE_EXTRACT_AND_RUN",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_RUNTIME_DIR",
        ):
            environment.pop(key, None)
        environment.update(
            {
                "HOME": str(home),
                "CFFIXED_USER_HOME": str(home),
                "TMPDIR": str(temporary),
            }
        )
        return environment

    config = home / ".config"
    cache = home / ".cache"
    xdg_runtime = runtime / "xdg-runtime"
    for path in (home, config, cache, xdg_runtime):
        path.mkdir(parents=True, exist_ok=True)
    xdg_runtime.chmod(0o700)
    environment.update(
        {
            "APPIMAGE_EXTRACT_AND_RUN": "1",
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config),
            "XDG_CACHE_HOME": str(cache),
            "XDG_RUNTIME_DIR": str(xdg_runtime),
        }
    )
    return environment


def probe_bambu_studio(
    executable: Path,
    *,
    runtime: Path,
    timeout_seconds: float,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    """Probe the exact executable in isolation and retain version-bearing hashes."""
    command = [str(executable), "--help"]
    execution = run_command(
        command,
        timeout_seconds=min(timeout_seconds, 30.0),
        env=isolated_environment(runtime),
    )
    combined = "\n".join((execution.stdout, execution.stderr))
    version = parse_bambu_studio_version(combined)
    if execution.returncode != 0 or version is None:
        detail = execution.stderr.strip() or execution.stdout.strip() or "no version banner"
        raise RuntimeError(
            f"Bambu Studio version probe failed with {execution.returncode}: {detail}"
        )
    record: dict[str, Any] = {
        **execution_record(execution, command),
        "version": version,
        "stdout_sha256": sha256_text(execution.stdout),
        "stderr_sha256": sha256_text(execution.stderr),
    }
    if evidence_root is not None:
        evidence_root.mkdir(parents=True, exist_ok=True)
        stdout_path = evidence_root / "bambu-studio-probe.stdout.log"
        stderr_path = evidence_root / "bambu-studio-probe.stderr.log"
        stdout_path.write_text(execution.stdout, encoding="utf-8")
        stderr_path.write_text(execution.stderr, encoding="utf-8")
        record.update(
            {
                "stdout_path": stdout_path.name,
                "stderr_path": stderr_path.name,
                "stdout_sha256": sha256(stdout_path),
                "stderr_sha256": sha256(stderr_path),
            }
        )
    return record


def run_checked(
    command: list[str],
    *,
    runtime: Path,
    timeout_seconds: float,
) -> CommandExecution:
    execution = run_command(
        command,
        timeout_seconds=timeout_seconds,
        env=isolated_environment(runtime),
    )
    if execution.returncode != 0:
        raise RuntimeError(
            f"Bambu Studio exited {execution.returncode}: "
            f"{execution.stderr.strip() or execution.stdout.strip()}"
        )
    return execution


def object_measurement(result: dict[str, Any]) -> dict[str, Any]:
    plates = result.get("sliced_plates")
    if not isinstance(plates, list) or len(plates) != 1 or not isinstance(plates[0], dict):
        raise RuntimeError("Bambu result has no single sliced plate")
    objects = plates[0].get("objects")
    if not isinstance(objects, list) or len(objects) != 1 or not isinstance(objects[0], dict):
        raise RuntimeError("Bambu result has no single terrain object")
    bbox = objects[0].get("bbox")
    if not isinstance(bbox, dict):
        raise RuntimeError("Bambu result object has no bounding box")
    return {
        "name": objects[0].get("name"),
        "triangle_count": objects[0].get("triangle_count"),
        "dimensions_mm": [bbox.get("width"), bbox.get("depth"), bbox.get("height")],
        "position_mm": [bbox.get("x"), bbox.get("y"), bbox.get("z")],
    }


def dimensions_match(first: list[Any], second: tuple[float, float, float]) -> bool:
    try:
        return all(
            abs(float(actual) - expected) <= 0.001
            for actual, expected in zip(first, second, strict=True)
        )
    except (TypeError, ValueError):
        return False


def copy_profiles(
    staging: Path, slice_root: Path, slice_manifest: dict[str, Any]
) -> tuple[list[Path], list[Path], list[dict[str, Any]]]:
    settings, filaments = profile_paths(slice_root, slice_manifest)
    destination = staging / "profiles"
    destination.mkdir()
    copied_settings: list[Path] = []
    copied_filaments: list[Path] = []
    records: list[dict[str, Any]] = []
    for role, sources, target in (
        ("settings", settings, copied_settings),
        ("filament", filaments, copied_filaments),
    ):
        for index, source in enumerate(sources):
            name = f"{role}-{index:02d}-{source.name}"
            output = destination / name
            shutil.copyfile(source, output)
            target.append(output)
            records.append(
                {
                    "role": role,
                    "index": index,
                    "path": f"profiles/{name}",
                    "sha256": sha256(output),
                }
            )
    return copied_settings, copied_filaments, records


def verify_output(
    root: Path,
    *,
    print_root: Path,
    slice_root: Path,
    executable: Path,
) -> dict[str, Any]:
    manifest_path = root / "bambu-tile-project-manifest.json"
    manifest = load_json(manifest_path)
    if manifest_path.read_bytes() != canonical_bytes(manifest):
        raise RuntimeError("Bambu tile project manifest is not canonical")
    slice_manifest = load_json(slice_root / "tile-slice-manifest.json")
    expected_version = frozen_source_bambu_version(slice_manifest)
    probe = manifest.get("bambu_studio_probe")
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("source_print_manifest_sha256")
        != sha256(print_root / "print-tile-assembly-manifest.json")
        or manifest.get("source_slice_manifest_sha256")
        != sha256(slice_root / "tile-slice-manifest.json")
        or manifest.get("bambu_studio_path") != str(executable)
        or manifest.get("bambu_studio_sha256") != sha256(executable)
        or slice_manifest.get("slicer_executable_sha256") != sha256(executable)
        or manifest.get("bambu_studio_version") != expected_version
        or not isinstance(probe, dict)
        or probe.get("process_exit_code") != 0
        or probe.get("version") != expected_version
    ):
        raise RuntimeError("Bambu tile project root identities changed")
    probe_stdout_relative = probe.get("stdout_path")
    probe_stderr_relative = probe.get("stderr_path")
    if not isinstance(probe_stdout_relative, str) or not isinstance(probe_stderr_relative, str):
        raise RuntimeError("Bambu Studio probe logs are not bound to the evidence root")
    probe_stdout = resolve_relative(root, probe_stdout_relative)
    probe_stderr = resolve_relative(root, probe_stderr_relative)
    probe_output_version = parse_bambu_studio_version(
        "\n".join(
            (
                probe_stdout.read_text(encoding="utf-8", errors="replace"),
                probe_stderr.read_text(encoding="utf-8", errors="replace"),
            )
        )
    )
    if (
        probe.get("command") != [str(executable), "--help"]
        or sha256(probe_stdout) != probe.get("stdout_sha256")
        or sha256(probe_stderr) != probe.get("stderr_sha256")
        or probe_output_version != expected_version
    ):
        raise RuntimeError("Bambu Studio probe evidence changed")
    records = manifest.get("tiles")
    if not isinstance(records, list) or len(records) != manifest.get("tile_count"):
        raise RuntimeError("Bambu tile project count changed")
    for profile in manifest.get("profile_files", []):
        path = resolve_relative(root, profile["path"])
        if sha256(path) != profile["sha256"]:
            raise RuntimeError(f"Bambu project profile checksum mismatch: {path}")
    for record in records:
        if not isinstance(record, dict):
            raise RuntimeError("Bambu tile project record is not an object")
        validation_path = resolve_relative(root, record["validation_path"])
        validation = load_json(validation_path)
        if validation_path.read_bytes() != canonical_bytes(validation):
            raise RuntimeError(f"Bambu tile validation is not canonical: {validation_path}")
        if sha256(validation_path) != record["validation_sha256"]:
            raise RuntimeError(f"Bambu tile validation checksum mismatch: {validation_path}")
        files = record.get("files")
        hashes = record.get("sha256")
        if not isinstance(files, dict) or not isinstance(hashes, dict) or set(files) != set(hashes):
            raise RuntimeError("Bambu tile project role set changed")
        for role, relative in files.items():
            path = resolve_relative(root, relative)
            if sha256(path) != hashes[role]:
                raise RuntimeError(f"Bambu tile project checksum mismatch: {role}")
        project = resolve_relative(root, files["bambu_project_3mf"])
        primary = resolve_relative(root, files["primary_gcode"])
        reopened = resolve_relative(root, files["reopen_gcode"])
        archive = archive_evidence(project, primary)
        if archive != validation.get("project_archive"):
            raise RuntimeError(f"Bambu project archive evidence changed: {record['tile_id']}")
        build_metrics, build_gate, build_version = release_gate(
            primary,
            expected_version=expected_version,
            stdout=resolve_relative(root, files["build_stdout"]).read_text(errors="replace"),
            stderr=resolve_relative(root, files["build_stderr"]).read_text(errors="replace"),
        )
        reopen_metrics, reopen_gate, reopen_version = release_gate(
            reopened,
            expected_version=expected_version,
            stdout=resolve_relative(root, files["reopen_stdout"]).read_text(errors="replace"),
            stderr=resolve_relative(root, files["reopen_stderr"]).read_text(errors="replace"),
        )
        if (
            build_metrics != validation.get("primary_metrics")
            or reopen_metrics != validation.get("reopen_metrics")
            or build_gate != validation.get("primary_release_gate")
            or reopen_gate != validation.get("reopen_release_gate")
            or build_version != validation.get("primary_bambu_studio_version")
            or reopen_version != validation.get("reopen_bambu_studio_version")
            or validation.get("expected_bambu_studio_version") != expected_version
            or validation.get("bambu_studio_versions_match") is not True
            or not validation.get("required_checks_passed")
        ):
            raise RuntimeError(f"Bambu project validation changed: {record['tile_id']}")
    if not manifest.get("required_checks_passed"):
        raise RuntimeError("Bambu tile project root gate changed")
    return {
        "status": "verified",
        "tile_count": manifest["tile_count"],
        "all_projects_reopened": manifest["all_projects_reopened"],
        "all_release_gates_passed": manifest["all_release_gates_passed"],
        "required_checks_passed": True,
    }


def _build_evidence(args: _EvidenceArgs) -> dict[str, Any]:
    print_root = args.print_set.expanduser().resolve()
    slice_root = args.slice_set.expanduser().resolve()
    executable = args.bambu_studio.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if output.exists():
        raise RuntimeError(f"Bambu tile project destination already exists: {output}")
    if not executable.is_file():
        raise RuntimeError(f"Bambu Studio executable does not exist: {executable}")
    print_manifest = load_json(print_root / "print-tile-assembly-manifest.json")
    slice_manifest = load_json(slice_root / "tile-slice-manifest.json")
    expected_version = frozen_source_bambu_version(slice_manifest)
    if (
        slice_manifest.get("release_role") != "official-p2s-release"
        or slice_manifest.get("official_p2s_release_gate_passed") is not True
        or slice_manifest.get("all_parameter_checks_passed") is not True
    ):
        raise RuntimeError("source tile slice set has not passed the official P2S release gate")
    if slice_manifest.get("source_print_tile_assembly_sha256") != sha256(
        print_root / "print-tile-assembly-manifest.json"
    ):
        raise RuntimeError("source official slice set does not bind the print tile set")
    if slice_manifest.get("slicer_executable_sha256") != sha256(executable):
        raise RuntimeError("source official slice set does not bind this Bambu executable")
    tile_by_id = {item["tile_id"]: item for item in print_manifest["tiles"]}
    slice_by_id = {item["tile_id"]: item for item in slice_manifest["tiles"]}
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.topoforge-stage-", dir=output.parent))
    runtime_root = staging / ".runtime"
    try:
        probe_record = probe_bambu_studio(
            executable,
            runtime=runtime_root / "probe",
            timeout_seconds=args.timeout,
            evidence_root=staging,
        )
        if probe_record["version"] != expected_version:
            raise RuntimeError(
                f"Bambu Studio probe version {probe_record['version']!r} does not match "
                f"the frozen source-slice version {expected_version!r}"
            )
        settings, filaments, profile_records = copy_profiles(staging, slice_root, slice_manifest)
        records: list[dict[str, Any]] = []
        for tile_id in sorted(tile_by_id):
            source_record = tile_by_id[tile_id]
            source_slice = slice_by_id[tile_id]
            input_relative = source_record["files"]["print_local_3mf"]
            input_path = resolve_relative(print_root, input_relative)
            input_inspection = inspect_3mf(input_path)
            tile_dir = staging / "tiles" / tile_id
            tile_dir.mkdir(parents=True)
            build_runtime = runtime_root / tile_id / "build"
            reopen_runtime = runtime_root / tile_id / "reopen"
            build_runtime.mkdir(parents=True)
            reopen_runtime.mkdir(parents=True)
            project_name = f"{tile_id}.bambu-p2s.3mf"
            build_command = [
                str(executable),
                "--debug",
                "2",
                "--load-settings",
                ";".join(str(path) for path in settings),
                "--load-filaments",
                ";".join(str(path) for path in filaments),
                "--load-defaultfila",
                "--curr-bed-type",
                "Textured PEI Plate",
                "--normative-check",
                "--ensure-on-bed",
                "--arrange",
                "1",
                "--slice",
                "0",
                "--export-3mf",
                project_name,
                "--outputdir",
                str(build_runtime),
                str(input_path),
            ]
            build_execution = run_checked(
                build_command, runtime=build_runtime / "environment", timeout_seconds=args.timeout
            )
            build_result_path = build_runtime / "result.json"
            build_project_path = build_runtime / project_name
            build_gcode_path = build_runtime / "plate_1.gcode"
            build_result = load_json(build_result_path)
            if (
                not result_passed(build_result)
                or not build_project_path.is_file()
                or not build_gcode_path.is_file()
            ):
                raise RuntimeError(f"Bambu project build failed for {tile_id}")
            project_path = tile_dir / "model.bambu-p2s.3mf"
            primary_gcode = tile_dir / "primary.gcode"
            shutil.copyfile(build_project_path, project_path)
            shutil.copyfile(build_gcode_path, primary_gcode)
            shutil.copyfile(build_result_path, tile_dir / "build_result.json")
            (tile_dir / "build.stdout.log").write_text(build_execution.stdout, encoding="utf-8")
            (tile_dir / "build.stderr.log").write_text(build_execution.stderr, encoding="utf-8")
            reopen_command = [
                str(executable),
                "--debug",
                "2",
                "--normative-check",
                "--slice",
                "0",
                "--outputdir",
                str(reopen_runtime),
                str(project_path),
            ]
            reopen_execution = run_checked(
                reopen_command, runtime=reopen_runtime / "environment", timeout_seconds=args.timeout
            )
            reopen_result_path = reopen_runtime / "result.json"
            reopen_gcode_path = reopen_runtime / "plate_1.gcode"
            reopen_result = load_json(reopen_result_path)
            if not result_passed(reopen_result) or not reopen_gcode_path.is_file():
                raise RuntimeError(f"Bambu project reopen failed for {tile_id}")
            reopen_gcode = tile_dir / "reopen.gcode"
            shutil.copyfile(reopen_gcode_path, reopen_gcode)
            shutil.copyfile(reopen_result_path, tile_dir / "reopen_result.json")
            (tile_dir / "reopen.stdout.log").write_text(reopen_execution.stdout, encoding="utf-8")
            (tile_dir / "reopen.stderr.log").write_text(reopen_execution.stderr, encoding="utf-8")
            primary_metrics, primary_gate, primary_version = release_gate(
                primary_gcode,
                expected_version=expected_version,
                stdout=build_execution.stdout,
                stderr=build_execution.stderr,
            )
            reopen_metrics, reopen_gate, reopen_version = release_gate(
                reopen_gcode,
                expected_version=expected_version,
                stdout=reopen_execution.stdout,
                stderr=reopen_execution.stderr,
            )
            build_object = object_measurement(build_result)
            reopen_object = object_measurement(reopen_result)
            archive = archive_evidence(project_path, primary_gcode)
            dimensions_ok = bool(
                dimensions_match(build_object["dimensions_mm"], input_inspection.dimensions_mm)
                and dimensions_match(reopen_object["dimensions_mm"], input_inspection.dimensions_mm)
            )
            triangles_ok = bool(
                build_object["triangle_count"] == input_inspection.triangle_count
                and reopen_object["triangle_count"] == input_inspection.triangle_count
            )
            required = bool(
                build_execution.returncode == 0
                and reopen_execution.returncode == 0
                and result_passed(build_result)
                and result_passed(reopen_result)
                and archive["archive_test_passed"]
                and archive["embedded_gcode_md5_verified"]
                and archive["embedded_gcode_matches_primary"]
                and primary_gate["release_gate_passed"]
                and reopen_gate["release_gate_passed"]
                and primary_version == expected_version
                and reopen_version == expected_version
                and dimensions_ok
                and triangles_ok
            )
            validation = {
                "schema_version": TILE_SCHEMA_VERSION,
                "tile_id": tile_id,
                "source_print_local_3mf_path": input_relative,
                "source_print_local_3mf_sha256": sha256(input_path),
                "source_slice_report_sha256": source_slice["report_sha256"],
                "source_dimensions_mm": list(input_inspection.dimensions_mm),
                "source_triangle_count": input_inspection.triangle_count,
                "build_execution": execution_record(build_execution, build_command),
                "reopen_execution": execution_record(reopen_execution, reopen_command),
                "build_result": build_result,
                "reopen_result": reopen_result,
                "build_object": build_object,
                "reopen_object": reopen_object,
                "dimensions_match": dimensions_ok,
                "triangle_counts_match": triangles_ok,
                "project_archive": archive,
                "primary_metrics": primary_metrics,
                "reopen_metrics": reopen_metrics,
                "primary_release_gate": primary_gate,
                "reopen_release_gate": reopen_gate,
                "expected_bambu_studio_version": expected_version,
                "primary_bambu_studio_version": primary_version,
                "reopen_bambu_studio_version": reopen_version,
                "bambu_studio_versions_match": (
                    primary_version == reopen_version == expected_version
                ),
                "external_profiles_loaded_on_reopen": False,
                "required_checks_passed": required,
            }
            if not required:
                raise RuntimeError(f"Bambu project validation failed for {tile_id}")
            validation_path = write_canonical(tile_dir / "project_validation.json", validation)
            role_names = {
                "bambu_project_3mf": "model.bambu-p2s.3mf",
                "primary_gcode": "primary.gcode",
                "reopen_gcode": "reopen.gcode",
                "build_result": "build_result.json",
                "reopen_result": "reopen_result.json",
                "build_stdout": "build.stdout.log",
                "build_stderr": "build.stderr.log",
                "reopen_stdout": "reopen.stdout.log",
                "reopen_stderr": "reopen.stderr.log",
            }
            relative_dir = f"tiles/{tile_id}"
            files = {role: f"{relative_dir}/{name}" for role, name in role_names.items()}
            hashes = {role: sha256(staging / relative) for role, relative in files.items()}
            records.append(
                {
                    "tile_id": tile_id,
                    "row": source_record["row"],
                    "column": source_record["column"],
                    "source_print_tile_manifest_sha256": source_record["tile_manifest_sha256"],
                    "source_slice_report_sha256": source_slice["report_sha256"],
                    "validation_path": f"{relative_dir}/project_validation.json",
                    "validation_sha256": sha256(validation_path),
                    "files": files,
                    "sha256": hashes,
                    "required_checks_passed": True,
                }
            )
        shutil.rmtree(runtime_root, ignore_errors=True)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "layout_id": print_manifest["layout_id"],
            "source_print_manifest_sha256": sha256(
                print_root / "print-tile-assembly-manifest.json"
            ),
            "source_slice_manifest_sha256": sha256(slice_root / "tile-slice-manifest.json"),
            "bambu_studio_path": str(executable),
            "bambu_studio_sha256": sha256(executable),
            "bambu_studio_version": expected_version,
            "bambu_studio_probe": probe_record,
            "printer_profile_id": "bambu-p2s-0.4",
            "profile_files": profile_records,
            "tile_grid_shape": print_manifest["tile_grid_shape"],
            "tile_count": len(records),
            "all_projects_reopened": all(record["required_checks_passed"] for record in records),
            "all_release_gates_passed": all(record["required_checks_passed"] for record in records),
            "claim_boundary": (
                "official Bambu Studio software export/reopen/reslice evidence; "
                "no physical print or vendor certification claim"
            ),
            "required_checks_passed": all(record["required_checks_passed"] for record in records),
            "tiles": records,
        }
        write_canonical(staging / "bambu-tile-project-manifest.json", manifest)
        verification = verify_output(
            staging, print_root=print_root, slice_root=slice_root, executable=executable
        )
        staging.replace(output)
        return {
            "status": "published",
            "output": str(output),
            "manifest": str(output / "bambu-tile-project-manifest.json"),
            "verification": verification,
        }
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_bambu_project_evidence(
    output_dir: Path,
    *,
    print_set_dir: Path,
    slice_set_dir: Path,
    bambu_studio: Path,
) -> dict[str, Any]:
    """Strictly reopen project archives, G-code, source bindings, and release gates."""
    root = output_dir.expanduser().resolve()
    print_root = print_set_dir.expanduser().resolve()
    slice_root = slice_set_dir.expanduser().resolve()
    executable = bambu_studio.expanduser().resolve()
    if not executable.is_file():
        raise RuntimeError(f"Bambu Studio executable does not exist: {executable}")
    return verify_output(
        root,
        print_root=print_root,
        slice_root=slice_root,
        executable=executable,
    )


def generate_bambu_project_evidence(
    print_set_dir: Path,
    slice_set_dir: Path,
    bambu_studio: Path,
    output_dir: Path,
    *,
    timeout_seconds: float = 1800.0,
) -> BambuProjectEvidenceResult:
    """Export one Bambu project per tile and verify no-profile reopen/reslice evidence."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    published = _build_evidence(
        _EvidenceArgs(
            print_set=print_set_dir,
            slice_set=slice_set_dir,
            bambu_studio=bambu_studio,
            output=output_dir,
            timeout=timeout_seconds,
        )
    )
    output = Path(str(published["output"])).resolve()
    manifest = Path(str(published["manifest"])).resolve()
    verification = published.get("verification")
    if not isinstance(verification, dict):
        raise RuntimeError("Bambu project verification result is not an object")
    return BambuProjectEvidenceResult(
        output_dir=output,
        manifest_path=manifest,
        verification=verification,
    )
