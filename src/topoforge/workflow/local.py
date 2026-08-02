"""Resumable content-addressed orchestration for single-workstation terrain builds."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

import rasterio
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from topoforge.engine import build_local_terrain, verify_artifact_bundle
from topoforge.exceptions import ConfigurationError
from topoforge.models import BuildConfig
from topoforge.overlays import (
    OverlayConfig,
    generate_overlay_bundle,
    overlay_identity_payload,
    verify_overlay_bundle,
)
from topoforge.providers import ElevationProvider, ProviderDescriptor
from topoforge.tiling import (
    TILE_LAYOUT_ALGORITHM_VERSION,
    TileLayoutConfig,
    canonical_tile_layout_bytes,
    extract_tile_set,
    generate_print_tile_set,
    generate_tile_mesh_set,
    plan_tile_layout,
    read_tile_layout,
    slice_print_tile_set,
    verify_print_tile_set,
    verify_tile_mesh_set,
    verify_tile_set,
    verify_tile_slice_set,
    write_tile_layout,
)
from topoforge.util import sha256_bytes, sha256_file
from topoforge.validation.bambu_projects import (
    generate_bambu_project_evidence,
    verify_bambu_project_evidence,
)
from topoforge.validation.slicers import SlicerAdapter, SlicerProfile
from topoforge.workflow.acquisition import (
    GlobalAcquisitionConfig,
    GlobalSourceEvidence,
    acquire_global_source,
    verify_global_source,
)

_WORKFLOW_SCHEMA_VERSION = "topoforge-local-workflow-v1"
_WORKFLOW_STATUS_SCHEMA_VERSION = "topoforge-local-workflow-status-v1"
_SOURCE_SCHEMA_VERSION = "topoforge-local-source-v1"
_ACQUISITION_STAGE_SCHEMA_VERSION = "topoforge-global-acquisition-stage-v1"


class WorkflowStage(StrEnum):
    """Ordered local workflow stages."""

    ACQUIRE = "acquire"
    SOURCE = "source"
    BUILD = "build"
    OVERLAY = "overlay"
    LAYOUT = "layout"
    EXTRACT = "extract"
    MESH = "mesh"
    CONNECT = "connect"
    SLICE = "slice"
    PROJECT = "project"


class WorkflowState(StrEnum):
    """Persisted operational state for the latest invocation."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class LocalWorkflowConfig(BaseModel):
    """Fully resolved settings for one resumable local manufacturing workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_dir: Path
    build: BuildConfig
    global_source: GlobalAcquisitionConfig | None = None
    overlay: OverlayConfig | None = None
    maximum_tile_width_mm: float = Field(default=180.0, gt=0)
    maximum_tile_depth_mm: float = Field(default=180.0, gt=0)
    overlap_cells: int = Field(default=1, ge=0)
    slicing_enabled: bool = True
    slice_timeout_seconds: float = Field(default=1200.0, gt=0)
    project_evidence_enabled: bool = False
    project_timeout_seconds: float = Field(default=1800.0, gt=0)

    @model_validator(mode="after")
    def validate_tile_limits(self) -> LocalWorkflowConfig:
        """Require tile dimensions to fit the selected printer build plate."""
        build_x, build_y, _ = self.build.printer_profile.build_volume_mm
        if self.maximum_tile_width_mm > build_x:
            raise ValueError("maximum_tile_width_mm exceeds the printer build width")
        if self.maximum_tile_depth_mm > build_y:
            raise ValueError("maximum_tile_depth_mm exceeds the printer build depth")
        required_formats = {"stl", "3mf", "glb"}
        if set(self.build.output_formats) != required_formats:
            raise ValueError(
                "local workflow build.output_formats must contain stl, 3mf, and glb "
                "because downstream tile verification reopens every format"
            )
        if self.project_evidence_enabled and not self.slicing_enabled:
            raise ValueError("project_evidence_enabled requires slicing_enabled")
        return self


class WorkflowStageRecord(BaseModel):
    """Stable identity and evidence binding for one ready workflow stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: WorkflowStage
    identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_path: str
    manifest_path: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_checks_passed: bool
    verification: dict[str, Any] = Field(default_factory=dict)


class LocalWorkflowManifest(BaseModel):
    """Canonical final manifest linking all local workflow stages."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _WORKFLOW_SCHEMA_VERSION
    workflow_id: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_dem_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_dem_path: str
    slicing_enabled: bool
    stages: tuple[WorkflowStageRecord, ...]
    final_stage: WorkflowStage
    required_checks_passed: bool


class LocalWorkflowStatus(BaseModel):
    """Latest resumable status, updated atomically after every stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _WORKFLOW_STATUS_SCHEMA_VERSION
    workflow_id: str
    state: WorkflowState
    current_stage: WorkflowStage | None
    ready_stages: tuple[WorkflowStage, ...]
    failure_path: str | None = None


class LocalWorkflowResult(BaseModel):
    """Execution-specific summary returned without changing the canonical manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_dir: Path
    workflow_id: str
    manifest_path: Path
    status_path: Path
    completed_stages: tuple[WorkflowStage, ...]
    reused_stages: tuple[WorkflowStage, ...]
    stage_outputs: dict[WorkflowStage, Path]
    required_checks_passed: bool


def _canonical_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _identity(value: BaseModel | dict[str, Any]) -> str:
    return sha256_bytes(_canonical_bytes(value))


def _write_canonical(path: Path, value: BaseModel | dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(_canonical_bytes(value))
    temporary.replace(path)
    return path


def _relative(root: Path, path: Path) -> str:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ConfigurationError(f"workflow artifact escapes workspace: {resolved}")
    return resolved.relative_to(resolved_root).as_posix()


def _stage_directory(root: Path, order: int, stage: WorkflowStage, identity: str) -> Path:
    return root / "stages" / f"{order:02d}-{stage.value}" / identity


def _build_identity_payload(config: BuildConfig, source_sha256: str) -> dict[str, Any]:
    build_payload = config.model_dump(mode="json")
    build_payload.pop("output_dir", None)
    return {
        "schema_version": _WORKFLOW_SCHEMA_VERSION,
        "source_dem_sha256": source_sha256,
        "build": build_payload,
    }


_GLOBAL_SOURCE_BUILD_FIELDS = {
    "acquisition_period",
    "aoi",
    "data_license",
    "dataset_name",
    "dataset_type",
    "dataset_version",
    "dem_path",
    "output_dir",
    "source_acquisition_manifest",
    "source_checksums",
    "source_download_time",
    "source_provider",
    "source_urls",
    "terrain_mode",
    "vertical_crs",
    "vertical_datum",
}


def _global_build_template_payload(config: BuildConfig) -> dict[str, Any]:
    """Return manufacturing settings without ignored local-source placeholders."""
    payload = config.model_dump(mode="json")
    for field in _GLOBAL_SOURCE_BUILD_FIELDS:
        payload.pop(field, None)
    return payload


def _global_build_config(
    template: BuildConfig,
    acquisition: GlobalAcquisitionConfig,
    evidence: GlobalSourceEvidence,
) -> BuildConfig:
    """Bind verified provider metadata and AOI to the manufacturing template."""
    dataset = evidence.dataset
    return template.model_copy(
        update={
            "dem_path": evidence.raster_path,
            "aoi": acquisition.aoi,
            "terrain_mode": acquisition.terrain_mode,
            "dataset_type": dataset.dataset_type,
            "dataset_name": dataset.dataset_name,
            "dataset_version": dataset.dataset_version,
            "acquisition_period": dataset.acquisition_period,
            "source_urls": dataset.source_urls,
            "vertical_crs": dataset.vertical_crs,
            "vertical_datum": dataset.vertical_datum,
            "data_license": dataset.license,
            "attribution": dataset.attribution,
            "source_provider": dataset.provider,
            "source_download_time": dataset.download_time,
            "source_checksums": dataset.checksums,
            "source_acquisition_manifest": evidence.acquisition_manifest_path,
        }
    )


def _acquisition_stage_payload(
    config: GlobalAcquisitionConfig,
    evidence: GlobalSourceEvidence,
) -> dict[str, Any]:
    """Return canonical, strictly reopenable acquisition-stage evidence."""
    return {
        "schema_version": _ACQUISITION_STAGE_SCHEMA_VERSION,
        "acquisition_identity": config.identity_payload(),
        "raster_path": str(evidence.raster_path),
        "raster_sha256": evidence.raster_sha256,
        "acquisition_manifest_path": str(evidence.acquisition_manifest_path),
        "acquisition_manifest_sha256": evidence.acquisition_manifest_sha256,
        "dataset": evidence.dataset.model_dump(mode="json"),
        "normalized_aoi": evidence.normalized_aoi.model_dump(mode="json"),
        "provider_selection": evidence.provider_selection.model_dump(mode="json"),
        "quality_masks": [
            {"path": str(path), "sha256": sha256_file(path)} for path in evidence.quality_mask_paths
        ],
        "required_checks_passed": evidence.required_checks_passed,
    }


def _write_acquisition_stage_manifest(
    path: Path,
    config: GlobalAcquisitionConfig,
    evidence: GlobalSourceEvidence,
) -> Path:
    manifest = _write_canonical(path, _acquisition_stage_payload(config, evidence))
    _verify_acquisition_stage_manifest(manifest, config, evidence)
    return manifest


def _verify_acquisition_stage_manifest(
    path: Path,
    config: GlobalAcquisitionConfig,
    evidence: GlobalSourceEvidence,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"acquisition stage manifest is unreadable: {path}") from exc
    expected = _acquisition_stage_payload(config, evidence)
    if payload != expected or path.read_bytes() != _canonical_bytes(expected):
        raise ConfigurationError("acquisition stage manifest is non-canonical or changed")
    return expected


def _slicer_identity(
    adapter: SlicerAdapter | None,
    profile: SlicerProfile | None,
    *,
    slicing_enabled: bool,
) -> dict[str, Any] | None:
    if not slicing_enabled:
        return None
    if adapter is None:
        raise ConfigurationError(
            "slicing_enabled requires a slicer adapter; select a slicer or use --no-slice"
        )
    resolved_profile = profile or SlicerProfile()
    profile_files: list[dict[str, str]] = []
    for role, paths in (
        ("settings", resolved_profile.settings),
        ("filaments", resolved_profile.filaments),
    ):
        for path in paths:
            resolved = path.expanduser().resolve()
            if not resolved.is_file():
                raise ConfigurationError(f"slicer profile file does not exist: {resolved}")
            profile_files.append(
                {"role": role, "path": str(resolved), "sha256": sha256_file(resolved)}
            )
    probe = adapter.probe()
    executable = None if probe.executable is None else probe.executable.expanduser().resolve()
    executable_sha256 = (
        sha256_file(executable) if executable is not None and executable.is_file() else None
    )
    return {
        "probe": probe.model_dump(mode="json"),
        "executable_sha256": executable_sha256,
        "profile_name": resolved_profile.label,
        "profile_files": profile_files,
    }


def _request_payload(
    config: LocalWorkflowConfig,
    *,
    source_sha256: str | None,
    slicer_identity: dict[str, Any] | None,
) -> dict[str, Any]:
    overlay_payload = None if config.overlay is None else overlay_identity_payload(config.overlay)
    common = {
        "schema_version": _WORKFLOW_SCHEMA_VERSION,
        "maximum_tile_width_mm": config.maximum_tile_width_mm,
        "maximum_tile_depth_mm": config.maximum_tile_depth_mm,
        "overlap_cells": config.overlap_cells,
        "slicing_enabled": config.slicing_enabled,
        "project_evidence_enabled": config.project_evidence_enabled,
        "slicer": slicer_identity,
    }
    if config.global_source is None:
        if source_sha256 is None:
            raise AssertionError("local workflow source SHA-256 disappeared")
        payload = {
            "schema_version": _WORKFLOW_SCHEMA_VERSION,
            "build": _build_identity_payload(config.build, source_sha256),
            "maximum_tile_width_mm": config.maximum_tile_width_mm,
            "maximum_tile_depth_mm": config.maximum_tile_depth_mm,
            "overlap_cells": config.overlap_cells,
            "slicing_enabled": config.slicing_enabled,
            "project_evidence_enabled": config.project_evidence_enabled,
            "slicer": slicer_identity,
        }
        if overlay_payload is not None:
            payload["overlay"] = overlay_payload
        return payload
    payload = {
        **common,
        "global_source": config.global_source.identity_payload(),
        "build_template": _global_build_template_payload(config.build),
    }
    if overlay_payload is not None:
        payload["overlay"] = overlay_payload
    return payload


def _summary(value: dict[str, Any]) -> dict[str, Any]:
    useful = (
        "status",
        "selected_provider",
        "dataset_name",
        "raster_sha256",
        "acquisition_manifest_sha256",
        "quality_mask_count",
        "raster_shape",
        "source_count",
        "layer_count",
        "feature_count",
        "triangle_count",
        "combined_3mf_object_count",
        "combined_glb_geometry_count",
        "terrain_artifacts_unchanged",
        "layout_id",
        "tile_grid_shape",
        "tile_count",
        "seam_count",
        "connector_count",
        "mesh_seam_count",
        "terrain_seam_status",
        "mesh_seam_status",
        "connector_fit_status",
        "collision_status",
        "slicer",
        "profile",
        "release_role",
        "official_p2s_release_gate_passed",
        "all_parameter_checks_passed",
        "maximum_layer_count",
        "total_gcode_size_bytes",
        "total_estimated_time_seconds",
        "total_filament_used_g",
        "all_exit_codes_zero",
        "no_out_of_bed",
        "no_empty_layers",
        "no_floating_regions",
        "no_support_material",
        "required_checks_passed",
    )
    selected = {key: value[key] for key in useful if key in value}
    normalized = json.loads(json.dumps(selected, ensure_ascii=False, default=str))
    if not isinstance(normalized, dict):
        raise AssertionError("workflow verification summary is not an object")
    return normalized


def _record(
    root: Path,
    *,
    stage: WorkflowStage,
    identity: str,
    output: Path,
    manifest: Path,
    verification: dict[str, Any],
) -> WorkflowStageRecord:
    passed = verification.get("required_checks_passed") is True
    if not passed:
        raise ConfigurationError(f"{stage.value} verification did not pass required checks")
    return WorkflowStageRecord(
        name=stage,
        identity_sha256=identity,
        output_path=_relative(root, output),
        manifest_path=_relative(root, manifest),
        manifest_sha256=sha256_file(manifest),
        required_checks_passed=True,
        verification=_summary(verification),
    )


def _status(
    root: Path,
    *,
    workflow_id: str,
    state: WorkflowState,
    current_stage: WorkflowStage | None,
    records: list[WorkflowStageRecord],
    failure_path: Path | None = None,
) -> Path:
    value = LocalWorkflowStatus(
        workflow_id=workflow_id,
        state=state,
        current_stage=current_stage,
        ready_stages=tuple(record.name for record in records),
        failure_path=None if failure_path is None else _relative(root, failure_path),
    )
    return _write_canonical(root / "workflow-status.json", value)


def _verify_build_stage(
    build_dir: Path,
    *,
    expected_config: BuildConfig,
    expected_source_sha256: str,
) -> dict[str, Any]:
    verification = verify_artifact_bundle(
        build_dir, required_formats=expected_config.output_formats
    )
    manifest = json.loads((build_dir / "build_manifest.json").read_text(encoding="utf-8"))
    resolved = yaml.safe_load(
        (build_dir / "build_config.resolved.yaml").read_text(encoding="utf-8")
    )
    if not isinstance(manifest, dict) or not isinstance(resolved, dict):
        raise ConfigurationError("build manifest or resolved configuration is not an object")
    if manifest.get("source_sha256") != expected_source_sha256:
        raise ConfigurationError("build manifest source SHA-256 does not match the workflow DEM")
    if resolved != expected_config.model_dump(mode="json"):
        raise ConfigurationError("resolved build configuration does not match workflow settings")
    return verification


def _existing_stage(
    stage: WorkflowStage,
    path: Path,
    verifier: Any,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return verifier()
    except Exception as exc:
        raise ConfigurationError(
            f"existing {stage.value} stage failed strict reuse at {path}; preserve it for "
            f"inspection and choose a different workspace or remove only that reviewed stage: {exc}"
        ) from exc


def _publish_source(
    root: Path,
    source: Path,
    source_sha256: str,
    *,
    acquisition_manifest: Path | None = None,
) -> tuple[Path, bool]:
    payload: dict[str, Any] = {
        "schema_version": _SOURCE_SCHEMA_VERSION,
        "source_dem_path": str(source),
        "source_dem_sha256": source_sha256,
        "source_size_bytes": source.stat().st_size,
    }
    if acquisition_manifest is not None:
        payload["source_acquisition_manifest"] = {
            "path": str(acquisition_manifest.resolve()),
            "sha256": sha256_file(acquisition_manifest),
        }
    identity = _identity(payload)
    destination = _stage_directory(root, 5, WorkflowStage.SOURCE, identity)
    manifest = destination / "source.json"
    if destination.exists():
        if not manifest.is_file() or manifest.read_bytes() != _canonical_bytes(payload):
            raise ConfigurationError(
                f"existing source stage is incomplete or changed: {destination}; "
                "preserve and review it"
            )
        if sha256_file(source) != source_sha256:
            raise ConfigurationError("source DEM changed while validating workflow identity")
        return manifest, True
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{identity}.stage-", dir=destination.parent))
    try:
        _write_canonical(staging / "source.json", payload)
        staging.replace(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest, False


def run_local_workflow(
    config: LocalWorkflowConfig,
    *,
    adapter: SlicerAdapter | None = None,
    profile: SlicerProfile | None = None,
    acquisition_providers: Mapping[str, ElevationProvider] | None = None,
    acquisition_descriptors: Sequence[ProviderDescriptor] | None = None,
) -> LocalWorkflowResult:
    """Run or resume acquisition, build, tiling, connector, and slicing stages.

    Every generated stage is content-addressed by its complete upstream manifests and
    settings. Existing stages are reused only after the same strict reopen checks used
    at publication time. A changed configuration selects a new stage directory instead
    of overwriting prior evidence.
    """
    root = config.workspace_dir.expanduser().resolve()
    source: Path | None = None
    source_sha256: str | None = None
    effective_build: BuildConfig | None = None
    acquisition_manifest: Path | None = None
    if config.global_source is None:
        if acquisition_providers is not None or acquisition_descriptors is not None:
            raise ConfigurationError(
                "acquisition provider overrides require LocalWorkflowConfig.global_source"
            )
        source = config.build.dem_path.expanduser().resolve()
        if not source.is_file():
            raise ConfigurationError(f"source DEM does not exist: {source}")
        source_sha256 = sha256_file(source)
        effective_build = config.build
    slicer_value = _slicer_identity(
        adapter,
        profile,
        slicing_enabled=config.slicing_enabled,
    )
    bambu_executable: Path | None = None
    if config.project_evidence_enabled:
        if slicer_value is None:
            raise AssertionError("project evidence slicer identity disappeared")
        probe = slicer_value.get("probe")
        executable_value = probe.get("executable") if isinstance(probe, dict) else None
        if not isinstance(probe, dict) or probe.get("name") != "BambuStudio":
            raise ConfigurationError(
                "project evidence requires the official Bambu Studio slicer adapter"
            )
        if not isinstance(executable_value, str):
            raise ConfigurationError("Bambu Studio probe did not resolve an executable path")
        bambu_executable = Path(executable_value).expanduser().resolve()
        if not bambu_executable.is_file():
            raise ConfigurationError(f"Bambu Studio executable does not exist: {bambu_executable}")
    request = _request_payload(
        config,
        source_sha256=source_sha256,
        slicer_identity=slicer_value,
    )
    request_sha256 = _identity(request)
    prefix = "global" if config.global_source is not None else "local"
    workflow_id = f"{prefix}-{request_sha256[:24]}"
    root.mkdir(parents=True, exist_ok=True)
    _write_canonical(root / "workflow-request.json", request)

    records: list[WorkflowStageRecord] = []
    completed: list[WorkflowStage] = []
    reused: list[WorkflowStage] = []
    outputs: dict[WorkflowStage, Path] = {}
    current = WorkflowStage.ACQUIRE if config.global_source is not None else WorkflowStage.SOURCE
    _status(
        root,
        workflow_id=workflow_id,
        state=WorkflowState.RUNNING,
        current_stage=current,
        records=records,
    )

    try:
        if config.global_source is not None:
            acquisition_config = config.global_source
            acquisition_identity = _identity(
                {
                    "schema_version": _WORKFLOW_SCHEMA_VERSION,
                    "global_source": acquisition_config.identity_payload(),
                }
            )
            acquisition_dir = _stage_directory(root, 0, current, acquisition_identity)
            acquisition_raster = acquisition_dir / "global-aoi.tif"
            acquisition_stage_manifest = acquisition_dir / "acquire.json"
            if acquisition_dir.exists() and any(acquisition_dir.iterdir()):
                try:
                    acquisition_evidence = verify_global_source(
                        acquisition_config, acquisition_raster
                    )
                    _verify_acquisition_stage_manifest(
                        acquisition_stage_manifest,
                        acquisition_config,
                        acquisition_evidence,
                    )
                except Exception as exc:
                    raise ConfigurationError(
                        "existing acquire stage failed strict reuse at "
                        f"{acquisition_dir}; preserve it for inspection and choose a "
                        f"different workspace or remove only that reviewed stage: {exc}"
                    ) from exc
                reused.append(current)
            else:
                acquisition_evidence = acquire_global_source(
                    acquisition_config,
                    acquisition_raster,
                    providers=acquisition_providers,
                    descriptors=acquisition_descriptors,
                )
                _write_acquisition_stage_manifest(
                    acquisition_stage_manifest,
                    acquisition_config,
                    acquisition_evidence,
                )
                completed.append(current)
            acquisition_verification = {
                "status": "ready",
                "selected_provider": acquisition_evidence.provider_selection.selected_provider,
                "dataset_name": acquisition_evidence.dataset.dataset_name,
                "raster_sha256": acquisition_evidence.raster_sha256,
                "acquisition_manifest_sha256": (acquisition_evidence.acquisition_manifest_sha256),
                "quality_mask_count": len(acquisition_evidence.quality_mask_paths),
                "required_checks_passed": acquisition_evidence.required_checks_passed,
            }
            records.append(
                _record(
                    root,
                    stage=current,
                    identity=acquisition_identity,
                    output=acquisition_dir,
                    manifest=acquisition_stage_manifest,
                    verification=acquisition_verification,
                )
            )
            outputs[current] = acquisition_dir
            source = acquisition_evidence.raster_path
            source_sha256 = acquisition_evidence.raster_sha256
            acquisition_manifest = acquisition_evidence.acquisition_manifest_path
            effective_build = _global_build_config(
                config.build, acquisition_config, acquisition_evidence
            )

        if source is None or source_sha256 is None or effective_build is None:
            raise AssertionError("workflow source resolution disappeared")
        current = WorkflowStage.SOURCE
        _status(
            root,
            workflow_id=workflow_id,
            state=WorkflowState.RUNNING,
            current_stage=current,
            records=records,
        )
        source_manifest, was_reused = _publish_source(
            root,
            source,
            source_sha256,
            acquisition_manifest=acquisition_manifest,
        )
        source_identity = source_manifest.parent.name
        source_verification = {"required_checks_passed": True}
        records.append(
            _record(
                root,
                stage=WorkflowStage.SOURCE,
                identity=source_identity,
                output=source_manifest.parent,
                manifest=source_manifest,
                verification=source_verification,
            )
        )
        outputs[WorkflowStage.SOURCE] = source_manifest.parent
        (reused if was_reused else completed).append(WorkflowStage.SOURCE)

        current = WorkflowStage.BUILD
        _status(
            root,
            workflow_id=workflow_id,
            state=WorkflowState.RUNNING,
            current_stage=current,
            records=records,
        )
        build_identity = _identity(_build_identity_payload(effective_build, source_sha256))
        build_dir = _stage_directory(root, 10, current, build_identity)
        resolved_build = effective_build.model_copy(
            update={"dem_path": source, "output_dir": build_dir}
        )
        build_verification = _existing_stage(
            current,
            build_dir,
            lambda: _verify_build_stage(
                build_dir,
                expected_config=resolved_build,
                expected_source_sha256=source_sha256,
            ),
        )
        if build_verification is None:
            build_local_terrain(resolved_build)
            build_verification = _verify_build_stage(
                build_dir,
                expected_config=resolved_build,
                expected_source_sha256=source_sha256,
            )
            completed.append(current)
        else:
            reused.append(current)
        build_manifest = build_dir / "build_manifest.json"
        records.append(
            _record(
                root,
                stage=current,
                identity=build_identity,
                output=build_dir,
                manifest=build_manifest,
                verification=build_verification,
            )
        )
        outputs[current] = build_dir

        if config.overlay is not None:
            current = WorkflowStage.OVERLAY
            _status(
                root,
                workflow_id=workflow_id,
                state=WorkflowState.RUNNING,
                current_stage=current,
                records=records,
            )
            overlay_identity = _identity(
                {
                    "source_build_manifest_sha256": sha256_file(build_manifest),
                    "overlay": overlay_identity_payload(config.overlay),
                }
            )
            overlay_dir = _stage_directory(root, 15, current, overlay_identity)
            overlay_verification = _existing_stage(
                current,
                overlay_dir,
                lambda: verify_overlay_bundle(overlay_dir, build_dir),
            )
            if overlay_verification is None:
                generate_overlay_bundle(build_dir, config.overlay, overlay_dir)
                overlay_verification = verify_overlay_bundle(overlay_dir, build_dir)
                completed.append(current)
            else:
                reused.append(current)
            overlay_manifest = overlay_dir / "overlay_manifest.json"
            records.append(
                _record(
                    root,
                    stage=current,
                    identity=overlay_identity,
                    output=overlay_dir,
                    manifest=overlay_manifest,
                    verification=overlay_verification,
                )
            )
            outputs[current] = overlay_dir

        current = WorkflowStage.LAYOUT
        _status(
            root,
            workflow_id=workflow_id,
            state=WorkflowState.RUNNING,
            current_stage=current,
            records=records,
        )
        with rasterio.open(build_dir / "processed_dem.tif") as dataset:
            source_grid_shape = (dataset.height, dataset.width)
        validation = json.loads((build_dir / "validation.json").read_text(encoding="utf-8"))
        dimensions = validation.get("dimensions_mm")
        if not isinstance(dimensions, list) or len(dimensions) < 2:
            raise ConfigurationError("build validation does not contain model dimensions")
        layout_config = TileLayoutConfig(
            source_grid_shape=source_grid_shape,
            model_width_mm=float(dimensions[0]),
            model_depth_mm=float(dimensions[1]),
            maximum_tile_width_mm=config.maximum_tile_width_mm,
            maximum_tile_depth_mm=config.maximum_tile_depth_mm,
            overlap_cells=config.overlap_cells,
        )
        layout_identity = _identity(
            {
                "source_build_manifest_sha256": sha256_file(build_manifest),
                "layout_algorithm_version": TILE_LAYOUT_ALGORITHM_VERSION,
                "layout": layout_config.model_dump(mode="json"),
            }
        )
        layout_dir = _stage_directory(root, 20, current, layout_identity)
        layout_path = layout_dir / "tile-layout.json"
        if layout_dir.exists():
            try:
                layout = read_tile_layout(layout_path)
                if layout_path.read_bytes() != canonical_tile_layout_bytes(layout):
                    raise ConfigurationError("tile layout is not canonical")
                if layout != plan_tile_layout(layout_config):
                    raise ConfigurationError("tile layout does not match requested configuration")
            except Exception as exc:
                raise ConfigurationError(
                    f"existing layout stage failed strict reuse at {layout_dir}: {exc}"
                ) from exc
            reused.append(current)
        else:
            layout_dir.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(
                tempfile.mkdtemp(prefix=f".{layout_identity}.stage-", dir=layout_dir.parent)
            )
            try:
                layout = plan_tile_layout(layout_config)
                write_tile_layout(layout, staging / "tile-layout.json")
                staging.replace(layout_dir)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            completed.append(current)
        layout_verification = {
            "layout_id": layout.layout_id,
            "tile_grid_shape": layout.tile_grid_shape,
            "tile_count": layout.tile_count,
            "required_checks_passed": True,
        }
        records.append(
            _record(
                root,
                stage=current,
                identity=layout_identity,
                output=layout_dir,
                manifest=layout_path,
                verification=layout_verification,
            )
        )
        outputs[current] = layout_dir

        current = WorkflowStage.EXTRACT
        _status(
            root,
            workflow_id=workflow_id,
            state=WorkflowState.RUNNING,
            current_stage=current,
            records=records,
        )
        extract_identity = _identity(
            {
                "source_build_manifest_sha256": sha256_file(build_manifest),
                "layout_sha256": sha256_file(layout_path),
            }
        )
        tile_dir = _stage_directory(root, 30, current, extract_identity)
        extract_verification = _existing_stage(
            current,
            tile_dir,
            lambda: verify_tile_set(tile_dir, build_dir),
        )
        if extract_verification is None:
            extract_tile_set(build_dir, layout_path, tile_dir)
            extract_verification = verify_tile_set(tile_dir, build_dir)
            completed.append(current)
        else:
            reused.append(current)
        tile_manifest = tile_dir / "assembly_manifest.json"
        records.append(
            _record(
                root,
                stage=current,
                identity=extract_identity,
                output=tile_dir,
                manifest=tile_manifest,
                verification=extract_verification,
            )
        )
        outputs[current] = tile_dir

        current = WorkflowStage.MESH
        _status(
            root,
            workflow_id=workflow_id,
            state=WorkflowState.RUNNING,
            current_stage=current,
            records=records,
        )
        mesh_identity = _identity(
            {
                "source_build_manifest_sha256": sha256_file(build_manifest),
                "source_tile_manifest_sha256": sha256_file(tile_manifest),
            }
        )
        mesh_dir = _stage_directory(root, 40, current, mesh_identity)
        mesh_verification = _existing_stage(
            current,
            mesh_dir,
            lambda: verify_tile_mesh_set(mesh_dir, tile_dir, build_dir),
        )
        if mesh_verification is None:
            generate_tile_mesh_set(tile_dir, build_dir, mesh_dir)
            mesh_verification = verify_tile_mesh_set(mesh_dir, tile_dir, build_dir)
            completed.append(current)
        else:
            reused.append(current)
        mesh_manifest = mesh_dir / "tile-mesh-assembly-manifest.json"
        records.append(
            _record(
                root,
                stage=current,
                identity=mesh_identity,
                output=mesh_dir,
                manifest=mesh_manifest,
                verification=mesh_verification,
            )
        )
        outputs[current] = mesh_dir

        current = WorkflowStage.CONNECT
        _status(
            root,
            workflow_id=workflow_id,
            state=WorkflowState.RUNNING,
            current_stage=current,
            records=records,
        )
        connect_identity = _identity(
            {
                "source_build_manifest_sha256": sha256_file(build_manifest),
                "source_tile_manifest_sha256": sha256_file(tile_manifest),
                "source_mesh_manifest_sha256": sha256_file(mesh_manifest),
            }
        )
        print_dir = _stage_directory(root, 50, current, connect_identity)
        connect_verification = _existing_stage(
            current,
            print_dir,
            lambda: verify_print_tile_set(print_dir, mesh_dir, tile_dir, build_dir),
        )
        if connect_verification is None:
            generate_print_tile_set(mesh_dir, tile_dir, build_dir, print_dir)
            connect_verification = verify_print_tile_set(print_dir, mesh_dir, tile_dir, build_dir)
            completed.append(current)
        else:
            reused.append(current)
        print_manifest = print_dir / "print-tile-assembly-manifest.json"
        records.append(
            _record(
                root,
                stage=current,
                identity=connect_identity,
                output=print_dir,
                manifest=print_manifest,
                verification=connect_verification,
            )
        )
        outputs[current] = print_dir

        if config.slicing_enabled:
            if adapter is None or slicer_value is None:
                raise AssertionError("validated slicer adapter disappeared")
            current = WorkflowStage.SLICE
            _status(
                root,
                workflow_id=workflow_id,
                state=WorkflowState.RUNNING,
                current_stage=current,
                records=records,
            )
            slice_identity = _identity(
                {
                    "source_print_manifest_sha256": sha256_file(print_manifest),
                    "slicer": slicer_value,
                }
            )
            slice_dir = _stage_directory(root, 60, current, slice_identity)
            slice_verification = _existing_stage(
                current,
                slice_dir,
                lambda: verify_tile_slice_set(slice_dir, print_dir, mesh_dir, tile_dir, build_dir),
            )
            if slice_verification is None:
                slice_print_tile_set(
                    print_dir,
                    mesh_dir,
                    tile_dir,
                    build_dir,
                    slice_dir,
                    adapter=adapter,
                    profile=profile,
                    timeout_seconds=config.slice_timeout_seconds,
                )
                slice_verification = verify_tile_slice_set(
                    slice_dir, print_dir, mesh_dir, tile_dir, build_dir
                )
                completed.append(current)
            else:
                reused.append(current)
            slice_manifest = slice_dir / "tile-slice-manifest.json"
            records.append(
                _record(
                    root,
                    stage=current,
                    identity=slice_identity,
                    output=slice_dir,
                    manifest=slice_manifest,
                    verification=slice_verification,
                )
            )
            outputs[current] = slice_dir

        if config.project_evidence_enabled:
            if bambu_executable is None:
                raise AssertionError("validated Bambu Studio executable disappeared")
            current = WorkflowStage.PROJECT
            _status(
                root,
                workflow_id=workflow_id,
                state=WorkflowState.RUNNING,
                current_stage=current,
                records=records,
            )
            slice_dir = outputs[WorkflowStage.SLICE]
            slice_manifest = slice_dir / "tile-slice-manifest.json"
            project_identity = _identity(
                {
                    "source_print_manifest_sha256": sha256_file(print_manifest),
                    "source_slice_manifest_sha256": sha256_file(slice_manifest),
                    "bambu_studio_sha256": sha256_file(bambu_executable),
                }
            )
            project_dir = _stage_directory(root, 70, current, project_identity)
            project_verification = _existing_stage(
                current,
                project_dir,
                lambda: verify_bambu_project_evidence(
                    project_dir,
                    print_set_dir=print_dir,
                    slice_set_dir=slice_dir,
                    bambu_studio=bambu_executable,
                ),
            )
            if project_verification is None:
                generate_bambu_project_evidence(
                    print_dir,
                    slice_dir,
                    bambu_executable,
                    project_dir,
                    timeout_seconds=config.project_timeout_seconds,
                )
                project_verification = verify_bambu_project_evidence(
                    project_dir,
                    print_set_dir=print_dir,
                    slice_set_dir=slice_dir,
                    bambu_studio=bambu_executable,
                )
                completed.append(current)
            else:
                reused.append(current)
            project_manifest = project_dir / "bambu-tile-project-manifest.json"
            records.append(
                _record(
                    root,
                    stage=current,
                    identity=project_identity,
                    output=project_dir,
                    manifest=project_manifest,
                    verification=project_verification,
                )
            )
            outputs[current] = project_dir

        final_stage = (
            WorkflowStage.PROJECT
            if config.project_evidence_enabled
            else WorkflowStage.SLICE
            if config.slicing_enabled
            else WorkflowStage.CONNECT
        )
        manifest = LocalWorkflowManifest(
            workflow_id=workflow_id,
            request_sha256=request_sha256,
            source_dem_sha256=source_sha256,
            source_dem_path=str(source),
            slicing_enabled=config.slicing_enabled,
            stages=tuple(records),
            final_stage=final_stage,
            required_checks_passed=all(record.required_checks_passed for record in records),
        )
        manifest_path = _write_canonical(root / "workflow-manifest.json", manifest)
        reopened = LocalWorkflowManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        if reopened != manifest or manifest_path.read_bytes() != _canonical_bytes(manifest):
            raise ConfigurationError("workflow manifest failed canonical strict reopen")
        status_path = _status(
            root,
            workflow_id=workflow_id,
            state=WorkflowState.COMPLETED,
            current_stage=None,
            records=records,
        )
        return LocalWorkflowResult(
            workspace_dir=root,
            workflow_id=workflow_id,
            manifest_path=manifest_path,
            status_path=status_path,
            completed_stages=tuple(completed),
            reused_stages=tuple(reused),
            stage_outputs=outputs,
            required_checks_passed=manifest.required_checks_passed,
        )
    except Exception as exc:
        failure = {
            "schema_version": _WORKFLOW_STATUS_SCHEMA_VERSION,
            "workflow_id": workflow_id,
            "failed_stage": current.value,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "ready_stages": [record.name.value for record in records],
            "corrective_action": (
                "Correct the reported input or external-tool problem and rerun the same command; "
                "strictly verified stages will be reused."
            ),
        }
        failure_identity = _identity(failure)
        failure_path = _write_canonical(
            root / "failures" / f"{failure_identity}.json",
            failure,
        )
        _status(
            root,
            workflow_id=workflow_id,
            state=WorkflowState.FAILED,
            current_stage=current,
            records=records,
            failure_path=failure_path,
        )
        raise
