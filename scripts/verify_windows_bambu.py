#!/usr/bin/env python3
"""Run official Bambu Studio slice, project export, reopen, and reslice acceptance."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import subprocess
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

_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(_SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIRECTORY))
import windows_acceptance as _windows_evidence  # noqa: E402

WINDOWS_TARGETS = _windows_evidence.WINDOWS_TARGETS
load_candidate_binding = _windows_evidence.load_candidate_binding
runtime_platform_record = _windows_evidence.runtime_platform_record
windows_target_record = _windows_evidence.windows_target_record

SCHEMA_VERSION = "topoforge-windows-bambu-verification-v2"


def _platform_record(*, require_windows: bool) -> dict[str, Any]:
    record = runtime_platform_record(require_windows=require_windows)
    record["topoforge"] = topoforge.__version__
    return record


def _file_record(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError(f"required acceptance artifact is missing: {resolved}")
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _read_authenticode_signature(executable: Path) -> dict[str, Any]:
    script = r"""
$ErrorActionPreference = "Stop"
$signature = Get-AuthenticodeSignature -LiteralPath $env:TOPOFORGE_AUTHENTICODE_PATH
$certificate = $signature.SignerCertificate
$subject = if ($null -eq $certificate) { $null } else { $certificate.Subject }
$thumbprint = if ($null -eq $certificate) { $null } else { $certificate.Thumbprint }
$notBefore = if ($null -eq $certificate) { $null } else {
  $certificate.NotBefore.ToUniversalTime().ToString("o")
}
$notAfter = if ($null -eq $certificate) { $null } else {
  $certificate.NotAfter.ToUniversalTime().ToString("o")
}
[ordered]@{
  Status = $signature.Status.ToString()
  StatusMessage = $signature.StatusMessage
  Subject = $subject
  Thumbprint = $thumbprint
  NotBefore = $notBefore
  NotAfter = $notAfter
} | ConvertTo-Json -Compress
""".strip()
    environment = os.environ.copy()
    environment["TOPOFORGE_AUTHENTICODE_PATH"] = str(executable)
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8-sig",
        errors="replace",
        env=environment,
        timeout=60.0,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Get-AuthenticodeSignature failed; use an official signed Bambu Studio Windows "
            f"installation: {completed.stderr or completed.stdout}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Get-AuthenticodeSignature returned unreadable JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Get-AuthenticodeSignature returned a non-object")
    return payload


def _normalize_thumbprint(value: str) -> str:
    normalized = "".join(character for character in value if not character.isspace()).upper()
    if len(normalized) not in {40, 64} or any(
        character not in "0123456789ABCDEF" for character in normalized
    ):
        raise ValueError("expected Authenticode thumbprint must be 40 or 64 hexadecimal characters")
    return normalized


def _authenticode_record(
    executable: Path,
    *,
    expected_publisher_subjects: tuple[str, ...],
    expected_thumbprints: tuple[str, ...],
) -> dict[str, Any]:
    subjects = tuple(value.strip() for value in expected_publisher_subjects if value.strip())
    thumbprints = tuple(_normalize_thumbprint(value) for value in expected_thumbprints)
    if len(subjects) != 1 or len(thumbprints) != 1:
        raise RuntimeError(
            "official Windows Bambu acceptance requires exactly one operator-frozen "
            "--expected-publisher-subject and exactly one "
            "--expected-certificate-thumbprint; inspect the official installer signature "
            "first and never guess this identity"
        )
    executable_sha256 = sha256_file(executable)
    payload = _read_authenticode_signature(executable)
    if sha256_file(executable) != executable_sha256:
        raise RuntimeError("Bambu Studio executable changed during Authenticode verification")
    status = payload.get("Status")
    subject = payload.get("Subject")
    raw_thumbprint = payload.get("Thumbprint")
    if status != "Valid" or not isinstance(subject, str) or not isinstance(raw_thumbprint, str):
        raise RuntimeError(
            f"Bambu Studio Authenticode status is not Valid: status={status!r}, "
            f"message={payload.get('StatusMessage')!r}"
        )
    thumbprint = _normalize_thumbprint(raw_thumbprint)
    subject_match = not subjects or any(
        subject.casefold() == expected.casefold() for expected in subjects
    )
    thumbprint_match = not thumbprints or thumbprint in thumbprints
    if not subject_match or not thumbprint_match:
        raise RuntimeError(
            "Bambu Studio Authenticode signer does not match the operator-frozen publisher "
            f"identity: subject={subject!r}, thumbprint={thumbprint}"
        )
    return {
        "status": status,
        "status_message": payload.get("StatusMessage"),
        "executable_sha256": executable_sha256,
        "publisher_subject": subject,
        "certificate_thumbprint": thumbprint,
        "certificate_not_before": payload.get("NotBefore"),
        "certificate_not_after": payload.get("NotAfter"),
        "expected_publisher_subjects": list(subjects),
        "expected_certificate_thumbprints": list(thumbprints),
        "publisher_subject_matched": subject_match,
        "certificate_thumbprint_matched": thumbprint_match,
        "operator_identity_frozen": True,
        "required_checks_passed": True,
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


def _normalize_expected_sha256(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{label} must be exactly 64 hexadecimal characters")
    return normalized


def _profile_hash_expectations(
    *,
    content_identity_sha256: str | None,
    machine_sha256: str | None,
    process_sha256: str | None,
    filament_sha256: str | None,
    require_frozen: bool,
) -> dict[str, str | None]:
    expected = {
        "content_identity": _normalize_expected_sha256(
            content_identity_sha256,
            "--expected-profile-content-identity-sha256",
        ),
        "machine": _normalize_expected_sha256(
            machine_sha256,
            "--expected-machine-profile-sha256",
        ),
        "process": _normalize_expected_sha256(
            process_sha256,
            "--expected-process-profile-sha256",
        ),
        "filament": _normalize_expected_sha256(
            filament_sha256,
            "--expected-filament-profile-sha256",
        ),
    }
    frozen = all(value is not None for value in expected.values())
    partially_frozen = any(value is not None for value in expected.values()) and not frozen
    if partially_frozen or (require_frozen and not frozen):
        raise RuntimeError(
            "official Bambu profile identity requires all four frozen hashes: "
            "--expected-profile-content-identity-sha256, "
            "--expected-machine-profile-sha256, --expected-process-profile-sha256, "
            "and --expected-filament-profile-sha256"
        )
    return expected


def _canonical_sha256(value: Any) -> str:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _profile_selection_mode(explicit_profiles_root: Path | None) -> str:
    if explicit_profiles_root is not None:
        return "explicit-cli-override"
    if os.environ.get("TOPOFORGE_BAMBU_PROFILES"):
        return "environment-override"
    return "executable-sibling-discovery"


def _profile_bundle_binding(
    *,
    executable: Path,
    executable_version: str,
    profiles_root: Path,
    prepared: PreparedBambuProfiles,
    expectations: dict[str, str | None],
    selection_mode: str,
    require_executable_sibling: bool,
) -> dict[str, Any]:
    resolved_executable = executable.expanduser().resolve()
    resolved_profiles_root = profiles_root.expanduser().resolve()
    expected_sibling_root = (
        resolved_executable.parent / "resources" / "profiles" / "BBL"
    ).resolve()
    is_executable_sibling = resolved_profiles_root == expected_sibling_root
    override_requested = selection_mode != "executable-sibling-discovery"
    identity_frozen = all(
        expectations.get(field) is not None
        for field in (
            "content_identity",
            "machine",
            "process",
            "filament",
        )
    )
    if override_requested and not identity_frozen:
        raise RuntimeError(
            "an explicit or environment Bambu profiles override is allowed only when all "
            "four expected profile hashes are frozen"
        )
    if require_executable_sibling and not is_executable_sibling:
        raise RuntimeError(
            "clean Windows Bambu evidence requires profiles from the authenticated "
            "executable sibling resources/profiles/BBL directory"
        )

    manifest = _json_object(prepared.manifest_path)
    manifest_sha256 = sha256_file(prepared.manifest_path)
    if (
        prepared.manifest_sha256 != manifest_sha256
        or manifest.get("schema_version") != "topoforge-bambu-profile-bundle-v1"
        or manifest.get("source_root") != str(resolved_profiles_root)
        or manifest.get("required_checks_passed") is not True
    ):
        raise RuntimeError("prepared Bambu profile manifest identity is invalid")
    executable_record = {
        **_file_record(resolved_executable),
        "version": executable_version,
    }
    if manifest.get("executable") != executable_record:
        raise RuntimeError(
            "prepared Bambu profile manifest executable identity does not match "
            "the authenticated binary"
        )
    manifest_profiles = manifest.get("profiles")
    if not isinstance(manifest_profiles, dict) or set(manifest_profiles) != {
        "machine",
        "process",
        "filament",
    }:
        raise RuntimeError("prepared Bambu profile manifest profile set is invalid")
    manifest_identity = {
        "schema_version": manifest["schema_version"],
        "source_root": manifest["source_root"],
        "executable": manifest["executable"],
        "profiles": manifest_profiles,
    }
    if manifest.get("bundle_id") != _canonical_sha256(manifest_identity):
        raise RuntimeError("prepared Bambu profile manifest bundle identity is invalid")

    executable_content = {
        "sha256": executable_record["sha256"],
        "size_bytes": executable_record["size_bytes"],
        "version": executable_record["version"],
    }
    profile_content_identity = {
        "schema_version": manifest["schema_version"],
        "executable": executable_content,
        "profiles": manifest_profiles,
    }
    profile_content_identity_sha256 = _canonical_sha256(profile_content_identity)

    expected_content_identity_sha256 = expectations.get("content_identity")
    content_identity_matched = (
        None
        if expected_content_identity_sha256 is None
        else profile_content_identity_sha256 == expected_content_identity_sha256
    )
    if content_identity_matched is False:
        raise RuntimeError(
            "prepared Bambu profile content identity SHA-256 differs from "
            "--expected-profile-content-identity-sha256"
        )

    prepared_paths = {
        "machine": prepared.machine_profile,
        "process": prepared.process_profile,
        "filament": prepared.filament_profile,
    }
    resolved_records: dict[str, dict[str, Any]] = {}
    source_records: dict[str, list[dict[str, Any]]] = {}
    for kind, profile_path in prepared_paths.items():
        profile = manifest_profiles.get(kind)
        if not isinstance(profile, dict):
            raise RuntimeError(f"prepared Bambu {kind} profile record is invalid")
        record = _file_record(profile_path)
        if (
            profile.get("resolved_path") != f"{kind}.json"
            or profile.get("resolved_sha256") != record["sha256"]
            or profile.get("resolved_size_bytes") != record["size_bytes"]
            or not isinstance(profile.get("name"), str)
            or not profile["name"]
        ):
            raise RuntimeError(f"prepared Bambu {kind} resolved profile identity is invalid")
        expected_sha256 = expectations.get(kind)
        sha256_matched = None if expected_sha256 is None else record["sha256"] == expected_sha256
        if sha256_matched is False:
            raise RuntimeError(
                f"prepared Bambu {kind} profile SHA-256 differs from the frozen expectation"
            )
        sources = profile.get("sources")
        if not isinstance(sources, list) or not sources:
            raise RuntimeError(f"prepared Bambu {kind} profile has no source records")
        validated_sources: list[dict[str, Any]] = []
        seen_sources: set[tuple[str, str]] = set()
        for raw_source in sources:
            if not isinstance(raw_source, dict):
                raise RuntimeError(f"prepared Bambu {kind} source record is invalid")
            source_kind = raw_source.get("kind")
            source_name = raw_source.get("name")
            source_relative_path = raw_source.get("path")
            source_sha256 = raw_source.get("sha256")
            source_size_bytes = raw_source.get("size_bytes")
            if (
                source_kind not in {"machine", "process", "filament"}
                or not isinstance(source_name, str)
                or not source_name
                or not isinstance(source_relative_path, str)
                or not source_relative_path.startswith(f"{source_kind}/")
                or not isinstance(source_sha256, str)
                or len(source_sha256) != 64
                or any(character not in "0123456789abcdef" for character in source_sha256)
                or not isinstance(source_size_bytes, int)
                or isinstance(source_size_bytes, bool)
                or source_size_bytes < 1
            ):
                raise RuntimeError(f"prepared Bambu {kind} source record is invalid")
            source_key = (source_kind, source_relative_path)
            if source_key in seen_sources:
                raise RuntimeError(f"prepared Bambu {kind} source record is duplicated")
            seen_sources.add(source_key)
            source_path = _relative_file(
                resolved_profiles_root,
                source_relative_path,
            )
            if (
                sha256_file(source_path) != source_sha256
                or source_path.stat().st_size != source_size_bytes
            ):
                raise RuntimeError(f"official Bambu source profile changed: {source_relative_path}")
            validated_sources.append(
                {
                    "kind": source_kind,
                    "name": source_name,
                    "path": source_relative_path,
                    "sha256": source_sha256,
                    "size_bytes": source_size_bytes,
                }
            )
        source_records[kind] = validated_sources
        resolved_records[kind] = {
            **record,
            "name": profile["name"],
            "expected_sha256": expected_sha256,
            "sha256_matched": sha256_matched,
            "source_count": len(validated_sources),
        }

    source_records_sha256 = _canonical_sha256(source_records)
    source_root_identity = {
        "relative_to_executable": ("resources/profiles/BBL" if is_executable_sibling else None),
        "is_executable_sibling": is_executable_sibling,
        "profile_content_identity_sha256": profile_content_identity_sha256,
        "source_records_sha256": source_records_sha256,
    }
    return {
        "path": str(resolved_profiles_root),
        "selection_mode": selection_mode,
        "expected_executable_sibling_path": str(expected_sibling_root),
        "relative_to_executable": ("resources/profiles/BBL" if is_executable_sibling else None),
        "is_executable_sibling": is_executable_sibling,
        "override_requested": override_requested,
        "override_authorized_by_frozen_hashes": (identity_frozen if override_requested else None),
        "profile_identity_frozen": identity_frozen,
        "profile_manifest_sha256": manifest_sha256,
        "profile_content_identity_sha256": profile_content_identity_sha256,
        "expected_profile_content_identity_sha256": expected_content_identity_sha256,
        "profile_content_identity_sha256_matched": content_identity_matched,
        "resolved_profiles": resolved_records,
        "expected_resolved_profile_sha256": {
            kind: expectations.get(kind) for kind in ("machine", "process", "filament")
        },
        "source_records": source_records,
        "source_records_sha256": source_records_sha256,
        "source_root_identity_sha256": _canonical_sha256(source_root_identity),
        "required_checks_passed": True,
    }


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
    expected_executable_sha256: str | None,
    require_executable_sibling: bool,
) -> tuple[Path, Path, PreparedBambuProfiles, dict[str, Any]]:
    requested_executable = None if executable is None else executable.expanduser().resolve()
    if expected_executable_sha256 is not None and (
        requested_executable is None
        or sha256_file(requested_executable) != expected_executable_sha256
    ):
        raise RuntimeError(
            "Bambu Studio executable changed after Authenticode verification and before "
            "its version probe"
        )
    adapter = BambuStudioAdapter(requested_executable)
    probe = adapter.probe(refresh=True)
    if (
        probe.status is not SlicerAvailability.AVAILABLE
        or probe.executable is None
        or probe.version is None
    ):
        detail = probe.detail or "version probe did not return an executable and version"
        raise RuntimeError(f"official Bambu Studio is not ready: {detail}")
    resolved_executable = probe.executable.expanduser().resolve()
    if requested_executable is not None and resolved_executable != requested_executable:
        raise RuntimeError("Bambu Studio version probe resolved a different executable")
    if (
        expected_executable_sha256 is not None
        and sha256_file(resolved_executable) != expected_executable_sha256
    ):
        raise RuntimeError("Bambu Studio executable changed during its version probe")
    resolved_profiles = discover_bambu_profiles_root(
        resolved_executable,
        explicit=profiles_root,
    )
    if resolved_profiles is None:
        raise RuntimeError(
            "official Bambu profiles were not found; install the official sibling "
            "resources/profiles/BBL directory"
        )
    expected_profiles_root = (
        resolved_executable.parent / "resources" / "profiles" / "BBL"
    ).resolve()
    if require_executable_sibling and resolved_profiles != expected_profiles_root:
        raise RuntimeError(
            "clean Windows Bambu evidence requires profiles from the authenticated "
            "executable sibling resources/profiles/BBL directory"
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
    expected_target: str | None = None,
    candidate_binding: Path | None = None,
    expected_publisher_subjects: tuple[str, ...] = (),
    expected_certificate_thumbprints: tuple[str, ...] = (),
    expected_profile_content_identity_sha256: str | None = None,
    expected_machine_profile_sha256: str | None = None,
    expected_process_profile_sha256: str | None = None,
    expected_filament_profile_sha256: str | None = None,
    slice_timeout_seconds: float = 1200.0,
    project_timeout_seconds: float = 1800.0,
) -> dict[str, Any]:
    """Run one authenticated official P2S workflow and strict reopen evidence."""
    platform_record = _platform_record(
        require_windows=require_windows or expected_target is not None
    )
    if require_windows and expected_target is None:
        raise RuntimeError("--require-windows also requires --expected-target")
    if candidate_binding is not None and expected_target is None:
        raise RuntimeError("--candidate-binding requires --expected-target")
    target = (
        None
        if expected_target is None
        else windows_target_record(expected_target, require_windows=True)
    )
    if expected_target is not None and candidate_binding is None:
        raise RuntimeError(
            "clean --expected-target Bambu acceptance requires --candidate-binding from the "
            "portable archive verifier"
        )
    binding = (
        None
        if candidate_binding is None
        else load_candidate_binding(
            candidate_binding,
            verifier_role="bambu",
            verifier_path=Path(__file__),
            expected_target=str(expected_target),
        )
    )
    if slice_timeout_seconds <= 0 or project_timeout_seconds <= 0:
        raise ValueError("Bambu acceptance timeouts must be positive")
    profile_selection_mode = _profile_selection_mode(profiles_root)
    profile_expectations = _profile_hash_expectations(
        content_identity_sha256=expected_profile_content_identity_sha256,
        machine_sha256=expected_machine_profile_sha256,
        process_sha256=expected_process_profile_sha256,
        filament_sha256=expected_filament_profile_sha256,
        require_frozen=(
            expected_target is not None or profile_selection_mode != "executable-sibling-discovery"
        ),
    )

    discovered = BambuStudioAdapter(executable).executable
    if discovered is None:
        raise RuntimeError(
            "Bambu Studio executable was not discovered; pass --bambu-studio-executable"
        )
    discovered = discovered.expanduser().resolve()
    if platform.system() == "Windows":
        authenticode = _authenticode_record(
            discovered,
            expected_publisher_subjects=expected_publisher_subjects,
            expected_thumbprints=expected_certificate_thumbprints,
        )
    else:
        authenticode = {
            "status": "not-applicable",
            "operator_identity_frozen": False,
            "required_checks_passed": True,
            "claim_boundary": "non-Windows contract run; not Windows official-binary evidence",
        }

    root = work_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    path_probe = root / "Windows Bambu path with spaces" / "地形"
    path_probe.mkdir(parents=True)
    resolved_executable, resolved_profiles, prepared, probe = _resolve_installation(
        executable=discovered,
        profiles_root=profiles_root,
        profile_cache=path_probe / "profile-cache",
        expected_executable_sha256=(
            authenticode.get("executable_sha256") if platform.system() == "Windows" else None
        ),
        require_executable_sibling=expected_target is not None,
    )
    profiles_root_binding = _profile_bundle_binding(
        executable=resolved_executable,
        executable_version=str(probe["version"]),
        profiles_root=resolved_profiles,
        prepared=prepared,
        expectations=profile_expectations,
        selection_mode=profile_selection_mode,
        require_executable_sibling=expected_target is not None,
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
    if (
        platform.system() == "Windows"
        and sha256_file(resolved_executable) != authenticode["executable_sha256"]
    ):
        raise RuntimeError("authenticated Bambu Studio executable changed during acceptance")
    if (
        _profile_bundle_binding(
            executable=resolved_executable,
            executable_version=str(probe["version"]),
            profiles_root=resolved_profiles,
            prepared=prepared,
            expectations=profile_expectations,
            selection_mode=profile_selection_mode,
            require_executable_sibling=expected_target is not None,
        )
        != profiles_root_binding
    ):
        raise RuntimeError("official Bambu profile identity changed during acceptance")

    return {
        "schema_version": SCHEMA_VERSION,
        "platform": {**platform_record, "target": target},
        "expected_target": None if target is None else target["target_id"],
        "windows_target": target,
        "candidate_binding": binding,
        "path_contract": {
            "root": str(path_probe),
            "contains_spaces": " " in str(path_probe),
            "contains_non_ascii": any(ord(character) > 127 for character in str(path_probe)),
            "required_checks_passed": True,
        },
        "bambu_studio": {
            "probe": probe,
            "executable": _file_record(resolved_executable),
            "authenticode": authenticode,
            "profiles_root": str(resolved_profiles),
            "profiles_root_binding": profiles_root_binding,
            "required_checks_passed": True,
        },
        "profile_bundle": {
            "manifest": _file_record(prepared.manifest_path),
            "profile_content_identity_sha256": profiles_root_binding[
                "profile_content_identity_sha256"
            ],
            "expected_profile_content_identity_sha256": profile_expectations["content_identity"],
            "profile_content_identity_sha256_matched": profiles_root_binding[
                "profile_content_identity_sha256_matched"
            ],
            "machine": profiles_root_binding["resolved_profiles"]["machine"],
            "process": profiles_root_binding["resolved_profiles"]["process"],
            "filament": profiles_root_binding["resolved_profiles"]["filament"],
            "source_records_sha256": profiles_root_binding["source_records_sha256"],
            "profile_identity_frozen": profiles_root_binding["profile_identity_frozen"],
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
            "authenticated official Bambu Studio software slice/export/reopen/reslice evidence "
            "only when Authenticode is Valid, an operator-frozen signer identity matches, "
            "the path-independent profile content identity and resolved profiles match "
            "frozen hashes, the profile root is the authenticated executable sibling, and "
            "windows_target.target_verified is true; no physical-print or "
            "vendor-certification claim"
        ),
        "required_checks_passed": True,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    _windows_evidence.write_canonical_json(path, report)


def main() -> int:
    """Run native official-Bambu acceptance and retain success or failure evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--bambu-studio-executable", type=Path)
    parser.add_argument("--bambu-profiles-root", type=Path)
    parser.add_argument("--slice-timeout-seconds", type=float, default=1200.0)
    parser.add_argument("--project-timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--require-windows", action="store_true")
    parser.add_argument("--expected-target", choices=WINDOWS_TARGETS)
    parser.add_argument("--candidate-binding", type=Path)
    parser.add_argument("--expected-publisher-subject", action="append", default=[])
    parser.add_argument("--expected-certificate-thumbprint", action="append", default=[])
    parser.add_argument("--expected-profile-content-identity-sha256")
    parser.add_argument("--expected-machine-profile-sha256")
    parser.add_argument("--expected-process-profile-sha256")
    parser.add_argument("--expected-filament-profile-sha256")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify_windows_bambu(
            args.work_root,
            executable=args.bambu_studio_executable,
            profiles_root=args.bambu_profiles_root,
            require_windows=args.require_windows,
            expected_target=args.expected_target,
            candidate_binding=args.candidate_binding,
            expected_publisher_subjects=tuple(args.expected_publisher_subject),
            expected_certificate_thumbprints=tuple(args.expected_certificate_thumbprint),
            expected_profile_content_identity_sha256=(
                args.expected_profile_content_identity_sha256
            ),
            expected_machine_profile_sha256=args.expected_machine_profile_sha256,
            expected_process_profile_sha256=args.expected_process_profile_sha256,
            expected_filament_profile_sha256=args.expected_filament_profile_sha256,
            slice_timeout_seconds=args.slice_timeout_seconds,
            project_timeout_seconds=args.project_timeout_seconds,
        )
    except BaseException as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "platform": _platform_record(
                require_windows=args.require_windows or args.expected_target is not None
            ),
            "work_root": str(args.work_root.expanduser().resolve()),
            "expected_target": args.expected_target,
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
