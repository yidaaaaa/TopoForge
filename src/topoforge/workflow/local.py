"""Resumable content-addressed orchestration for single-workstation terrain builds."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import rasterio
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rasterio.crs import CRS

from topoforge.engine import build_local_terrain, verify_artifact_bundle
from topoforge.exceptions import ConfigurationError
from topoforge.exporters.three_mf import inspect_3mf
from topoforge.models import BuildConfig
from topoforge.overlays import (
    OverlayConfig,
    generate_overlay_bundle,
    overlay_identity_payload,
    verify_overlay_bundle,
)
from topoforge.platforms import path_is_link_like
from topoforge.providers import ElevationProvider, ProviderDescriptor
from topoforge.tiling import (
    TILE_LAYOUT_ALGORITHM_VERSION,
    ConnectorPlan,
    PrintTileAssemblyManifest,
    PrintTileSliceManifest,
    PrintTileSliceReport,
    TileLayout,
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
)
from topoforge.util import sha256_bytes, sha256_file
from topoforge.validation import bambu_projects
from topoforge.validation.bambu_projects import (
    generate_bambu_project_evidence,
    verify_bambu_project_evidence,
)
from topoforge.validation.manufacturing import evaluate_bambu_p2s_release_gate
from topoforge.validation.slicers import (
    SlicerAdapter,
    SlicerAvailability,
    SlicerInfo,
    SlicerProfile,
    SliceStatus,
    parse_gcode_metrics,
)
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


@dataclass(frozen=True, slots=True)
class _WorkspaceLease:
    """Lexical workspace root bound to the directory identity accepted at launch."""

    root: Path
    identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _PrivateStage:
    """One random stage root outside the replaceable lexical workspace."""

    parent: _WorkspaceLease
    lease: _WorkspaceLease
    output: Path


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


def _workspace_lease(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None,
) -> _WorkspaceLease:
    """Create or reopen one lexical workspace and bind every later mutation to it."""
    from topoforge.web.security import ensure_real_directory_tree, real_directory_tree_identity

    root = Path(os.path.abspath(path.expanduser()))
    try:
        observed = (
            ensure_real_directory_tree(root, context="local workflow workspace")
            if expected_identity is None
            else real_directory_tree_identity(root, context="local workflow workspace")
        )
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f"local workflow workspace is unsafe or changed: {root}; "
            "restore the exact real directory selected at launch"
        ) from exc
    if expected_identity is not None and observed != expected_identity:
        raise ConfigurationError(
            f"local workflow workspace identity changed before execution: {root}; "
            "do not continue in a replacement directory"
        )
    return _WorkspaceLease(root=root, identity=observed)


def _require_workspace(lease: _WorkspaceLease, *, context: str) -> None:
    """Reject a renamed, replaced, or link-like lexical workspace."""
    from topoforge.web.security import owned_directory_identity

    try:
        observed = owned_directory_identity(
            lease.root,
            root=lease.root,
            root_identity=lease.identity,
            context=context,
        )
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f"{context} cannot use the original workflow workspace; "
            "the directory was renamed, replaced, or became unsafe"
        ) from exc
    if observed != lease.identity:
        raise ConfigurationError(f"{context} workflow workspace identity changed")


def _create_private_stage(
    destination: Path,
    *,
    workspace: _WorkspaceLease,
    context: str,
) -> _PrivateStage:
    """Create an unpredictable output anchor outside the workspace path itself."""
    from topoforge.web.security import (
        create_owned_directory,
        owned_directory_identity,
        real_directory_tree_identity,
    )

    parent_path = workspace.root.parent
    try:
        parent_identity = real_directory_tree_identity(
            parent_path,
            context=f"{context} private parent",
        )
        parent = _WorkspaceLease(parent_path, parent_identity)
        for _ in range(4):
            root = parent.root / f".topoforge-private-stage-{uuid4().hex}"
            try:
                create_owned_directory(
                    root,
                    root=parent.root,
                    root_identity=parent.identity,
                    context=f"{context} private root",
                )
            except FileExistsError:
                continue
            root_identity = owned_directory_identity(
                root,
                root=parent.root,
                root_identity=parent.identity,
                context=f"{context} private root",
            )
            return _PrivateStage(
                parent=parent,
                lease=_WorkspaceLease(root, root_identity),
                output=root / destination.name,
            )
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"{context} could not create a private stage root") from exc
    raise ConfigurationError(f"{context} could not allocate a unique private stage root")


def _publish_private_stage(
    stage: _PrivateStage,
    destination: Path,
    *,
    workspace: _WorkspaceLease,
    context: str,
) -> None:
    """Publish a complete private output only while the original workspace still owns its path."""
    from topoforge.web.security import move_owned_path, owned_directory_identity, remove_owned_path

    try:
        output_identity = owned_directory_identity(
            stage.output,
            root=stage.lease.root,
            root_identity=stage.lease.identity,
            context=f"{context} private output",
        )
        _require_workspace(workspace, context=f"{context} workspace publication")
        _ensure_workspace_directory(
            destination.parent,
            workspace=workspace,
            context=f"{context} destination parent",
        )
        move_owned_path(
            stage.output,
            destination,
            source_root=stage.lease.root,
            source_root_identity=stage.lease.identity,
            destination_root=workspace.root,
            destination_root_identity=workspace.identity,
            expected_identity=output_identity,
            directory=True,
            context=context,
        )
        remove_owned_path(
            stage.lease.root,
            root=stage.parent.root,
            root_identity=stage.parent.identity,
            expected_identity=stage.lease.identity,
            directory=True,
            context=f"{context} private root cleanup",
        )
    except (OSError, ValueError) as exc:
        if getattr(exc, "committed", False):
            raise ConfigurationError(
                f"{context} may have committed but durable publication is uncertain; "
                f"strictly reopen {destination} before retrying"
            ) from exc
        raise ConfigurationError(
            f"{context} refused a changed workspace or private stage; "
            f"preserve {stage.lease.root} for inspection"
        ) from exc


def _ensure_workspace_directory(
    path: Path,
    *,
    workspace: _WorkspaceLease,
    context: str,
) -> tuple[int, int]:
    """Create a real directory chain through identity-bound relative operations."""
    from topoforge.web.security import create_owned_directory, owned_directory_identity

    candidate = Path(os.path.abspath(path.expanduser()))
    try:
        relative = candidate.relative_to(workspace.root)
    except ValueError as exc:
        raise ConfigurationError(f"{context} escapes the workflow workspace: {candidate}") from exc
    current = workspace.root
    try:
        for part in relative.parts:
            current /= part
            create_owned_directory(
                current,
                root=workspace.root,
                root_identity=workspace.identity,
                context=context,
                exist_ok=True,
            )
        return owned_directory_identity(
            candidate,
            root=workspace.root,
            root_identity=workspace.identity,
            context=context,
        )
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f"{context} directory is unsafe or changed: {candidate}; "
            "preserve the original workspace and retry"
        ) from exc


def _create_workspace_stage(
    parent: Path,
    *,
    prefix: str,
    workspace: _WorkspaceLease,
    context: str,
) -> tuple[Path, tuple[int, int]]:
    """Create one unpredictable staging directory below an identity-bound parent."""
    from topoforge.web.security import create_owned_directory, owned_directory_identity

    _ensure_workspace_directory(parent, workspace=workspace, context=f"{context} parent")
    for _ in range(4):
        staging = parent / f".{prefix}.stage-{uuid4().hex}"
        try:
            create_owned_directory(
                staging,
                root=workspace.root,
                root_identity=workspace.identity,
                context=context,
            )
            identity = owned_directory_identity(
                staging,
                root=workspace.root,
                root_identity=workspace.identity,
                context=context,
            )
            return staging, identity
        except FileExistsError:
            continue
        except (OSError, ValueError) as exc:
            raise ConfigurationError(
                f"{context} could not create an identity-bound staging directory"
            ) from exc
    raise ConfigurationError(f"{context} could not allocate a unique staging directory")


def _publish_workspace_stage(
    staging: Path,
    destination: Path,
    *,
    staging_identity: tuple[int, int],
    workspace: _WorkspaceLease,
    context: str,
) -> None:
    """Publish one exact staging directory without replacing an existing entry."""
    from topoforge.web.security import move_owned_path

    try:
        move_owned_path(
            staging,
            destination,
            source_root=workspace.root,
            source_root_identity=workspace.identity,
            destination_root=workspace.root,
            destination_root_identity=workspace.identity,
            expected_identity=staging_identity,
            directory=True,
            context=context,
        )
    except (OSError, ValueError) as exc:
        if getattr(exc, "committed", False):
            raise ConfigurationError(
                f"{context} may have committed but durable publication is uncertain; "
                f"strictly reopen {destination} before retrying"
            ) from exc
        raise ConfigurationError(
            f"{context} refused an unsafe, changed, or occupied destination: {destination}; "
            "preserve both stage paths for inspection"
        ) from exc


def _write_canonical(
    path: Path,
    value: BaseModel | dict[str, Any],
    *,
    workspace: _WorkspaceLease | None = None,
) -> Path:
    from topoforge.web.security import atomic_write_owned_regular_bytes, ensure_real_directory_tree

    active = workspace
    if active is None:
        parent = Path(os.path.abspath(path.parent.expanduser()))
        try:
            identity = ensure_real_directory_tree(parent, context=f"parent for {path.name}")
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"canonical output parent is unsafe: {parent}") from exc
        active = _WorkspaceLease(root=parent, identity=identity)
    else:
        _ensure_workspace_directory(
            path.parent,
            workspace=active,
            context=f"canonical output parent for {path.name}",
        )
    try:
        atomic_write_owned_regular_bytes(
            path,
            _canonical_bytes(value),
            root=active.root,
            root_identity=active.identity,
            context=f"canonical workflow output {path}",
            replace=True,
        )
    except (OSError, ValueError) as exc:
        if getattr(exc, "committed", False):
            raise ConfigurationError(
                f"canonical workflow output may have committed but is not durably verified: {path}"
            ) from exc
        raise ConfigurationError(
            f"canonical workflow output was not written because its workspace changed: {path}"
        ) from exc
    return path


def _normalize_private_build_stage(
    stage: _PrivateStage,
    *,
    final_config: BuildConfig,
) -> None:
    """Replace the private build output path with the final logical path and rebind hashes."""
    from topoforge.provenance import write_validation_html
    from topoforge.web.security import (
        atomic_write_owned_regular_bytes,
        owned_directory_identity,
        read_owned_regular_bytes,
    )

    try:
        output_identity = owned_directory_identity(
            stage.output,
            root=stage.lease.root,
            root_identity=stage.lease.identity,
            context="private build output",
        )
        output = _WorkspaceLease(stage.output, output_identity)
        resolved_payload = yaml.safe_dump(
            final_config.model_dump(mode="json"),
            sort_keys=True,
            allow_unicode=True,
        ).encode("utf-8")
        resolved_path = stage.output / "build_config.resolved.yaml"
        atomic_write_owned_regular_bytes(
            resolved_path,
            resolved_payload,
            root=output.root,
            root_identity=output.identity,
            context="private build resolved configuration",
            replace=True,
        )
        manifest_path = stage.output / "build_manifest.json"
        manifest_payload = json.loads(
            read_owned_regular_bytes(
                manifest_path,
                root=output.root,
                root_identity=output.identity,
                context="private build manifest",
            )
        )
        if not isinstance(manifest_payload, dict):
            raise ValueError("private build manifest root is not an object")
        checksums = manifest_payload.get("sha256")
        if not isinstance(checksums, dict):
            raise ValueError("private build manifest has no checksum object")
        resolved_sha256 = sha256_bytes(resolved_payload)
        rebound_payloads: dict[str, bytes] = {}
        validation_payload: dict[str, Any] | None = None
        for filename in ("validation.json", "provenance.json"):
            path = stage.output / filename
            payload = json.loads(
                read_owned_regular_bytes(
                    path,
                    root=output.root,
                    root_identity=output.identity,
                    context=f"private build {filename}",
                )
            )
            if not isinstance(payload, dict):
                raise ValueError(f"private build {filename} root is not an object")
            bindings = payload.get("artifact_bindings")
            role_sha256 = bindings.get("role_sha256") if isinstance(bindings, dict) else None
            if not isinstance(role_sha256, dict):
                raise ValueError(f"private build {filename} has no artifact role bindings")
            role_sha256["resolved_config"] = resolved_sha256
            if filename == "validation.json":
                validation_payload = payload
            rebound_payloads[filename] = (
                json.dumps(
                    payload,
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            ).encode("utf-8")
        for filename, payload in rebound_payloads.items():
            atomic_write_owned_regular_bytes(
                stage.output / filename,
                payload,
                root=output.root,
                root_identity=output.identity,
                context=f"private build rebound {filename}",
                replace=True,
            )
            checksums["validation_json" if filename == "validation.json" else "provenance"] = (
                sha256_bytes(payload)
            )
        if validation_payload is None:
            raise ValueError("private build validation payload disappeared")
        validation_html = stage.output / "validation.html"
        write_validation_html(validation_html, validation_payload)
        checksums["validation_html"] = sha256_file(validation_html)
        checksums["resolved_config"] = resolved_sha256
        manifest_payload["resolved_config_sha256"] = resolved_sha256
        manifest_bytes = (
            json.dumps(
                manifest_payload,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
            + "\n"
        ).encode("utf-8")
        atomic_write_owned_regular_bytes(
            manifest_path,
            manifest_bytes,
            root=output.root,
            root_identity=output.identity,
            context="private build rebound manifest",
            replace=True,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            f"private build stage could not be rebound to {final_config.output_dir}"
        ) from exc


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
    *,
    workspace: _WorkspaceLease,
) -> Path:
    manifest = _write_canonical(
        path,
        _acquisition_stage_payload(config, evidence),
        workspace=workspace,
    )
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
    workspace: _WorkspaceLease,
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
    return _write_canonical(root / "workflow-status.json", value, workspace=workspace)


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
    workspace: _WorkspaceLease,
    verifier: Any,
) -> dict[str, Any] | None:
    _require_workspace(workspace, context=f"{stage.value} stage reuse")
    if not path.exists():
        return None
    try:
        verification = verifier()
        _require_workspace(workspace, context=f"{stage.value} stage reuse completion")
        return verification
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
    workspace: _WorkspaceLease,
    acquisition_manifest: Path | None = None,
) -> tuple[Path, bool]:
    from topoforge.web.security import owned_entry_identity, read_owned_regular_bytes

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
    _ensure_workspace_directory(
        destination.parent,
        workspace=workspace,
        context="source stage parent",
    )
    try:
        destination_identity = owned_entry_identity(
            destination,
            root=workspace.root,
            root_identity=workspace.identity,
            directory=True,
            context="existing source stage",
        )
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f"existing source stage is unsafe or changed: {destination}"
        ) from exc
    if destination_identity is not None:
        destination_lease = _WorkspaceLease(destination, destination_identity)
        try:
            manifest_bytes = read_owned_regular_bytes(
                manifest,
                root=destination_lease.root,
                root_identity=destination_lease.identity,
                context="existing source stage manifest",
            )
        except (OSError, ValueError) as exc:
            raise ConfigurationError(
                f"existing source stage is incomplete or changed: {destination}; "
                "preserve and review it"
            ) from exc
        if manifest_bytes != _canonical_bytes(payload):
            raise ConfigurationError(
                f"existing source stage is incomplete or changed: {destination}; "
                "preserve and review it"
            )
        if sha256_file(source) != source_sha256:
            raise ConfigurationError("source DEM changed while validating workflow identity")
        return manifest, True
    staging, staging_identity = _create_workspace_stage(
        destination.parent,
        prefix=identity,
        workspace=workspace,
        context="source stage",
    )
    _write_canonical(
        staging / "source.json",
        payload,
        workspace=_WorkspaceLease(staging, staging_identity),
    )
    _publish_workspace_stage(
        staging,
        destination,
        staging_identity=staging_identity,
        workspace=workspace,
        context="source stage publication",
    )
    return manifest, False


def _read_canonical_object(path: Path, *, context: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"{context} is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError(f"{context} root is not an object: {path}")
    if path.read_bytes() != _canonical_bytes(payload):
        raise ConfigurationError(f"{context} is not canonical: {path}")
    return payload


def _workflow_relative_path(root: Path, value: str, *, context: str) -> Path:
    relative = Path(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.as_posix() != value
    ):
        raise ConfigurationError(
            f"{context} must be a canonical workspace-relative path without '..': {value}"
        )
    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current /= part
        try:
            if path_is_link_like(current):
                raise ConfigurationError(
                    f"{context} contains a link-like component: {current}; "
                    "restore the original workflow artifact"
                )
        except FileNotFoundError:
            raise ConfigurationError(f"{context} is missing: {current}") from None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ConfigurationError(f"{context} is missing or unreadable: {candidate}") from exc
    if resolved == root or root not in resolved.parents:
        raise ConfigurationError(f"{context} escapes workspace: {resolved}")
    return resolved


def _reject_link_like_tree(root: Path, *, context: str) -> None:
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise ConfigurationError(f"{context} is unreadable: {directory}") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                if path_is_link_like(path):
                    raise ConfigurationError(
                        f"{context} contains a link-like artifact: {path}; "
                        "restore the original workflow artifact"
                    )
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
            except OSError as exc:
                raise ConfigurationError(
                    f"{context} changed while it was inspected: {path}"
                ) from exc


def _stage_paths(
    root: Path,
    record: WorkflowStageRecord,
    *,
    order: int,
    stage: WorkflowStage,
    identity: str,
    manifest_name: str,
) -> tuple[Path, Path]:
    expected_output = f"stages/{order:02d}-{stage.value}/{identity}"
    expected_manifest = f"{expected_output}/{manifest_name}"
    if record.name is not stage:
        raise ConfigurationError(f"workflow stage order changed at {stage.value}")
    if record.identity_sha256 != identity:
        raise ConfigurationError(f"workflow {stage.value} stage identity changed")
    if record.output_path != expected_output or record.manifest_path != expected_manifest:
        raise ConfigurationError(
            f"workflow {stage.value} stage path does not match its content identity"
        )
    output = _workflow_relative_path(
        root,
        record.output_path,
        context=f"workflow {stage.value} output path",
    )
    manifest = _workflow_relative_path(
        root,
        record.manifest_path,
        context=f"workflow {stage.value} manifest path",
    )
    if not output.is_dir() or not manifest.is_file() or manifest.parent != output:
        raise ConfigurationError(f"workflow {stage.value} stage path shape changed")
    _reject_link_like_tree(output, context=f"workflow {stage.value} stage")
    if sha256_file(manifest) != record.manifest_sha256:
        raise ConfigurationError(f"workflow {stage.value} stage manifest checksum changed")
    return output, manifest


def _verify_stage_record(
    record: WorkflowStageRecord,
    verification: dict[str, Any],
) -> None:
    if record.required_checks_passed is not True:
        raise ConfigurationError(f"workflow {record.name.value} stage gate is not passed")
    if verification.get("required_checks_passed") is not True:
        raise ConfigurationError(f"workflow {record.name.value} artifact verification did not pass")
    if record.verification != _summary(verification):
        raise ConfigurationError(f"workflow {record.name.value} verification summary changed")


def _verify_persisted_slicer_identity(
    value: Any,
    *,
    slicing_enabled: bool,
    slicer_name: str,
    settings: Sequence[Path],
    filaments: Sequence[Path],
    verify_request_identity: bool,
) -> dict[str, Any] | None:
    if not slicing_enabled:
        if value is not None:
            raise ConfigurationError("non-slicing workflow request contains slicer identity")
        return None
    if not isinstance(value, dict) or set(value) != {
        "probe",
        "executable_sha256",
        "profile_name",
        "profile_files",
    }:
        raise ConfigurationError("workflow request slicer identity is incomplete")
    try:
        probe = SlicerInfo.model_validate(value["probe"])
    except ValueError as exc:
        raise ConfigurationError("workflow request slicer probe is invalid") from exc
    if probe.model_dump(mode="json") != value["probe"]:
        raise ConfigurationError("workflow request slicer probe is not canonical")
    expected_names = {
        "bambu-studio": {"BambuStudio"},
        "orca": {"OrcaSlicer"},
        "prusa": {"PrusaSlicer"},
        "auto": {"BambuStudio", "OrcaSlicer", "PrusaSlicer"},
    }
    if slicer_name not in expected_names or probe.name not in expected_names[slicer_name]:
        raise ConfigurationError("workflow request slicer does not match the saved launch")
    if probe.status is not SlicerAvailability.AVAILABLE or probe.executable is None:
        raise ConfigurationError("completed workflow slicer probe is not available")
    executable = probe.executable.expanduser().resolve()
    executable_sha256 = value["executable_sha256"]
    if (
        not isinstance(executable_sha256, str)
        or len(executable_sha256) != 64
        or any(character not in "0123456789abcdef" for character in executable_sha256)
    ):
        raise ConfigurationError("workflow request slicer executable checksum is invalid")
    if executable.is_file() and executable_sha256 != sha256_file(executable):
        raise ConfigurationError("recorded slicer executable checksum changed")
    profile = SlicerProfile(
        name=(
            "Bambu Lab P2S 0.4 / 0.20mm Standard / Bambu PLA Basic"
            if slicer_name == "bambu-studio"
            else None
        ),
        settings=tuple(settings),
        filaments=tuple(filaments),
    )
    recorded_files = value["profile_files"]
    expected_inputs = [
        (role, path.expanduser().resolve())
        for role, paths in (("settings", settings), ("filaments", filaments))
        for path in paths
    ]
    profiles_match = bool(
        isinstance(recorded_files, list)
        and len(recorded_files) == len(expected_inputs)
        and (
            value["profile_name"] == profile.label
            if verify_request_identity
            else isinstance(value["profile_name"], str) and value["profile_name"]
        )
    )
    if profiles_match:
        for recorded, (role, path) in zip(recorded_files, expected_inputs, strict=True):
            if (
                not isinstance(recorded, dict)
                or set(recorded) != {"role", "path", "sha256"}
                or recorded.get("role") != role
                or (verify_request_identity and recorded.get("path") != str(path))
                or not isinstance(recorded.get("sha256"), str)
                or len(recorded["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in recorded["sha256"])
                or (path.is_file() and sha256_file(path) != recorded["sha256"])
            ):
                profiles_match = False
                break
    if not profiles_match:
        raise ConfigurationError("workflow request slicer profiles changed")
    return value


def _completed_workflow_root(workspace_dir: Path) -> Path:
    lexical = Path(os.path.abspath(workspace_dir.expanduser()))
    try:
        if path_is_link_like(lexical):
            raise ConfigurationError(
                f"completed workflow root is link-like: {lexical}; use the real directory"
            )
    except FileNotFoundError as exc:
        raise ConfigurationError(f"completed workflow root is missing: {lexical}") from exc
    if not lexical.is_dir():
        raise ConfigurationError(f"completed workflow root is not a directory: {lexical}")
    return lexical.resolve(strict=True)


def _read_completed_launch(root: Path) -> tuple[Any, LocalWorkflowConfig]:
    from topoforge.workflow.ux import read_workflow_launch_config

    path = _workflow_relative_path(
        root,
        "workflow-launch.yaml",
        context="workflow launch path",
    )
    launch = read_workflow_launch_config(path)
    expected = launch.model_dump(mode="json")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"workflow launch is unreadable: {path}") from exc
    canonical = yaml.safe_dump(expected, sort_keys=True, allow_unicode=True).encode("utf-8")
    if payload != expected or path.read_bytes() != canonical:
        raise ConfigurationError("workflow launch is non-canonical or changed")
    if (
        launch.workspace_dir.expanduser().resolve() != root
        or launch.build.output_dir.expanduser().resolve() != root
        or expected.get("workspace_dir") != str(root)
        or not isinstance(expected.get("build"), dict)
        or expected["build"].get("output_dir") != str(root)
    ):
        raise ConfigurationError("workflow launch workspace/output paths changed")
    return launch, launch.workflow_config()


def _run_artifact_verifier(
    stage: WorkflowStage,
    verifier: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    try:
        verification = verifier()
    except Exception as exc:
        raise ConfigurationError(
            f"workflow {stage.value} artifact verification failed; "
            f"restore or rebuild that stage: {exc}"
        ) from exc
    if not isinstance(verification, dict):
        raise ConfigurationError(f"workflow {stage.value} verifier returned no evidence object")
    return verification


def _verify_source_record(
    root: Path,
    record: WorkflowStageRecord,
    *,
    source: Path,
    source_sha256: str,
    acquisition_manifest: Path | None,
    verify_request_identity: bool,
) -> Path:
    resolved_source = source.expanduser().resolve()
    if not resolved_source.is_file() or sha256_file(resolved_source) != source_sha256:
        raise ConfigurationError(
            f"workflow source DEM is missing or changed: {resolved_source}; "
            "restore the exact source before strict workflow verification"
        )
    expected_payload: dict[str, Any] = {
        "schema_version": _SOURCE_SCHEMA_VERSION,
        "source_dem_path": str(resolved_source),
        "source_dem_sha256": source_sha256,
        "source_size_bytes": resolved_source.stat().st_size,
    }
    if acquisition_manifest is not None:
        resolved_acquisition = acquisition_manifest.expanduser().resolve()
        if not resolved_acquisition.is_file():
            raise ConfigurationError(
                f"workflow source acquisition manifest is missing: {resolved_acquisition}"
            )
        expected_payload["source_acquisition_manifest"] = {
            "path": str(resolved_acquisition),
            "sha256": sha256_file(resolved_acquisition),
        }
    identity = _identity(expected_payload) if verify_request_identity else record.identity_sha256
    _, manifest_path = _stage_paths(
        root,
        record,
        order=5,
        stage=WorkflowStage.SOURCE,
        identity=identity,
        manifest_name="source.json",
    )
    payload = _read_canonical_object(manifest_path, context="workflow source manifest")
    if verify_request_identity:
        if payload != expected_payload:
            raise ConfigurationError("workflow source manifest no longer matches the source DEM")
    else:
        if (
            payload.get("schema_version") != _SOURCE_SCHEMA_VERSION
            or payload.get("source_dem_sha256") != source_sha256
            or payload.get("source_size_bytes") != resolved_source.stat().st_size
            or _identity(payload) != record.identity_sha256
        ):
            raise ConfigurationError(
                "restored workflow source manifest no longer binds the copied source DEM"
            )
        recorded_acquisition = payload.get("source_acquisition_manifest")
        expected_acquisition = expected_payload.get("source_acquisition_manifest")
        if (recorded_acquisition is None) != (expected_acquisition is None):
            raise ConfigurationError("restored workflow source acquisition binding changed")
        if expected_acquisition is not None and (
            not isinstance(recorded_acquisition, dict)
            or recorded_acquisition.get("sha256") != expected_acquisition["sha256"]
        ):
            raise ConfigurationError("restored workflow source acquisition checksum changed")
    _verify_stage_record(record, {"required_checks_passed": True})
    return resolved_source


def _recorded_basename(value: Any, *, context: str) -> str:
    if not isinstance(value, str):
        raise ConfigurationError(f"{context} path is not a string")
    normalized = value.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ConfigurationError(f"{context} path has no safe basename: {value}")
    return name


def _overlay_identity_without_paths(value: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(value, ensure_ascii=False))
    config = normalized.get("config")
    sources = normalized.get("sources")
    if not isinstance(config, dict) or not isinstance(sources, list):
        raise ConfigurationError("workflow overlay identity is invalid")
    configured_sources = config.get("sources")
    if not isinstance(configured_sources, list) or len(configured_sources) != len(sources):
        raise ConfigurationError("workflow overlay identity source inventory is invalid")
    for source in configured_sources:
        if not isinstance(source, dict) or "path" not in source:
            raise ConfigurationError("workflow overlay configured source is invalid")
        source["path"] = None
    for source in sources:
        if not isinstance(source, dict) or "path" not in source:
            raise ConfigurationError("workflow overlay source identity is invalid")
        source["path"] = None
    return normalized


def _verify_relocated_global_source(
    config: GlobalAcquisitionConfig,
    acquisition_dir: Path,
    stage_manifest_path: Path,
) -> GlobalSourceEvidence:
    """Reverify relocated provider evidence without trusting its old absolute paths."""
    raster_path = acquisition_dir / "global-aoi.tif"
    provider_manifest_path = raster_path.with_suffix(
        raster_path.suffix + ".source_acquisition.json"
    )
    stage = _read_canonical_object(
        stage_manifest_path,
        context="relocated acquisition stage manifest",
    )
    expected_stage_keys = {
        "schema_version",
        "acquisition_identity",
        "raster_path",
        "raster_sha256",
        "acquisition_manifest_path",
        "acquisition_manifest_sha256",
        "dataset",
        "normalized_aoi",
        "provider_selection",
        "quality_masks",
        "required_checks_passed",
    }
    if (
        set(stage) != expected_stage_keys
        or stage.get("schema_version") != _ACQUISITION_STAGE_SCHEMA_VERSION
        or stage.get("acquisition_identity") != config.identity_payload()
        or stage.get("required_checks_passed") is not True
        or _recorded_basename(stage.get("raster_path"), context="acquisition raster")
        != raster_path.name
        or _recorded_basename(
            stage.get("acquisition_manifest_path"),
            context="provider acquisition manifest",
        )
        != provider_manifest_path.name
    ):
        raise ConfigurationError("relocated acquisition stage identity or path shape changed")
    if not raster_path.is_file() or not provider_manifest_path.is_file():
        raise ConfigurationError("relocated acquisition raster or provider manifest is missing")
    raster_sha256 = sha256_file(raster_path)
    provider_manifest_sha256 = sha256_file(provider_manifest_path)
    if (
        stage.get("raster_sha256") != raster_sha256
        or stage.get("acquisition_manifest_sha256") != provider_manifest_sha256
    ):
        raise ConfigurationError("relocated acquisition root checksums changed")
    try:
        provider = json.loads(provider_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("relocated provider acquisition manifest is unreadable") from exc
    if not isinstance(provider, dict):
        raise ConfigurationError("relocated provider acquisition manifest is not an object")

    normalized_aoi = config.normalized_aoi()
    if (
        provider.get("output_raster_sha256") != raster_sha256
        or provider.get("aoi") != normalized_aoi.model_dump(mode="json")
        or provider.get("aoi") != stage.get("normalized_aoi")
        or provider.get("dataset") != stage.get("dataset")
        or provider.get("provider_selection") != stage.get("provider_selection")
        or _recorded_basename(provider.get("raster_path"), context="provider raster")
        != raster_path.name
        or _recorded_basename(
            provider.get("acquisition_manifest_path"),
            context="provider manifest",
        )
        != provider_manifest_path.name
    ):
        raise ConfigurationError("relocated provider acquisition root evidence changed")

    raw_quality = provider.get("quality_masks", [])
    stage_quality = stage.get("quality_masks")
    if not isinstance(raw_quality, list) or not isinstance(stage_quality, list):
        raise ConfigurationError("relocated acquisition quality mask inventory is invalid")
    stage_quality_by_name: dict[str, dict[str, Any]] = {}
    for item in stage_quality:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ConfigurationError("relocated acquisition stage quality record is invalid")
        name = _recorded_basename(item.get("path"), context="stage quality mask")
        if name in stage_quality_by_name:
            raise ConfigurationError("relocated acquisition quality mask names are duplicated")
        stage_quality_by_name[name] = item

    quality_paths: list[Path] = []
    present_names: set[str] = set()
    with rasterio.open(raster_path) as raster:
        values = raster.read(1, masked=True)
        finite = np.isfinite(values.data)
        valid = finite & ~np.ma.getmaskarray(values)
        if (
            raster.count != 1
            or raster.crs is None
            or raster.crs.is_geographic
            or values.size < 16
            or not np.any(valid)
        ):
            raise ConfigurationError(
                "relocated provider raster is not a non-empty single-band metric grid"
            )
        if provider.get("output_source_nodata_pixels") != int(np.count_nonzero(~valid)):
            raise ConfigurationError("relocated provider raster NoData count changed")
        raster_shape = raster.shape
        raster_crs = raster.crs
        raster_transform = raster.transform

    for record in raw_quality:
        if not isinstance(record, dict):
            raise ConfigurationError("relocated provider quality record is not an object")
        if record.get("availability") != "present":
            continue
        output = record.get("output")
        if not isinstance(output, dict):
            raise ConfigurationError("relocated present quality mask has no output record")
        name = _recorded_basename(output.get("path"), context="provider quality mask")
        if name in present_names:
            raise ConfigurationError("relocated provider quality mask names are duplicated")
        present_names.add(name)
        path = acquisition_dir / name
        stage_record = stage_quality_by_name.get(name)
        expected_sha256 = output.get("sha256")
        if (
            not path.is_file()
            or not isinstance(expected_sha256, str)
            or sha256_file(path) != expected_sha256
            or stage_record is None
            or stage_record.get("sha256") != expected_sha256
        ):
            raise ConfigurationError(f"relocated provider quality checksum changed: {name}")
        with rasterio.open(path) as quality:
            expected_shape = tuple(output.get("grid_shape", []))
            expected_transform = tuple(output.get("transform", []))
            try:
                aligned = bool(
                    quality.count == 1
                    and quality.shape == raster_shape
                    and quality.shape == expected_shape
                    and quality.crs == raster_crs
                    and quality.crs == CRS.from_user_input(output.get("crs"))
                    and quality.transform.almost_equals(raster_transform)
                    and len(expected_transform) == 6
                    and np.allclose(
                        tuple(quality.transform)[:6],
                        expected_transform,
                        atol=1e-12,
                        rtol=0.0,
                    )
                )
            except (TypeError, ValueError):
                aligned = False
            if not aligned:
                raise ConfigurationError(
                    f"relocated provider quality mask alignment changed: {name}"
                )
        quality_paths.append(path)
    if present_names != set(stage_quality_by_name):
        raise ConfigurationError("relocated acquisition quality inventory changed")

    try:
        evidence = GlobalSourceEvidence.model_validate(
            {
                "raster_path": raster_path,
                "acquisition_manifest_path": provider_manifest_path,
                "raster_sha256": raster_sha256,
                "acquisition_manifest_sha256": provider_manifest_sha256,
                "dataset": stage.get("dataset"),
                "normalized_aoi": stage.get("normalized_aoi"),
                "provider_selection": stage.get("provider_selection"),
                "quality_mask_paths": quality_paths,
                "required_checks_passed": True,
            }
        )
    except ValueError as exc:
        raise ConfigurationError("relocated acquisition typed evidence is invalid") from exc
    trace = evidence.provider_selection
    if (
        evidence.normalized_aoi.model_dump(mode="json") != normalized_aoi.model_dump(mode="json")
        or trace.policy != config.selection_policy()
        or trace.outcome != "selected"
        or trace.selected_provider != provider.get("provider_id")
        or trace.selected_provider != evidence.dataset.provider
        or trace.selected_dataset != evidence.dataset.dataset_name
        or (
            config.requested_provider_id != "auto"
            and trace.selected_provider != config.requested_provider_id
        )
    ):
        raise ConfigurationError("relocated acquisition provider selection binding changed")
    return evidence


def _canonical_print_manifest(print_dir: Path) -> PrintTileAssemblyManifest:
    path = print_dir / "print-tile-assembly-manifest.json"
    try:
        manifest = PrintTileAssemblyManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError("print tile assembly manifest is unreadable") from exc
    if path.read_bytes() != _canonical_bytes(manifest):
        raise ConfigurationError("print tile assembly manifest is not canonical")
    return manifest


def _canonical_slice_manifest(slice_dir: Path) -> PrintTileSliceManifest:
    path = slice_dir / "tile-slice-manifest.json"
    try:
        manifest = PrintTileSliceManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError("tile slice manifest is unreadable") from exc
    if path.read_bytes() != _canonical_bytes(manifest):
        raise ConfigurationError("tile slice manifest is not canonical")
    return manifest


def _verify_slice_slicer_binding(
    manifest: PrintTileSliceManifest,
    slicer_identity: dict[str, Any],
) -> None:
    if (
        manifest.slicer.model_dump(mode="json") != slicer_identity["probe"]
        or manifest.slicer_executable_sha256 != slicer_identity["executable_sha256"]
        or manifest.profile_name != slicer_identity["profile_name"]
    ):
        raise ConfigurationError("tile slice slicer identity changed from the workflow request")
    requested = slicer_identity.get("profile_files")
    if not isinstance(requested, list):
        raise ConfigurationError("workflow request slicer profile inventory is invalid")
    requested_binding: list[tuple[str, int, str]] = []
    role_indexes = {"settings": 0, "filament": 0}
    for record in requested:
        if not isinstance(record, dict):
            raise ConfigurationError("workflow request slicer profile record is invalid")
        role = record.get("role")
        normalized_role = "filament" if role == "filaments" else role
        if normalized_role not in role_indexes or not isinstance(record.get("sha256"), str):
            raise ConfigurationError("workflow request slicer profile role is invalid")
        index = role_indexes[normalized_role]
        role_indexes[normalized_role] += 1
        requested_binding.append((normalized_role, index, record["sha256"]))
    artifact_binding = [
        (record.role, record.index, record.sha256) for record in manifest.profile_files
    ]
    if artifact_binding != requested_binding:
        raise ConfigurationError("tile slice profile hashes changed from the workflow request")


def _optional_sum(values: Sequence[int | float | None]) -> int | float | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _verify_tile_slice_artifacts(
    slice_dir: Path,
    *,
    print_dir: Path,
    slicer_identity: dict[str, Any],
) -> dict[str, Any]:
    """Reopen slice evidence without requiring or executing the recorded slicer."""
    manifest = _canonical_slice_manifest(slice_dir)
    source_manifest = _canonical_print_manifest(print_dir)
    try:
        connector_plan = ConnectorPlan.model_validate_json(
            (print_dir / source_manifest.connector_plan_path).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ConfigurationError("slice source connector plan is unreadable") from exc
    if (
        manifest.layout_id != source_manifest.layout_id
        or manifest.source_print_tile_assembly_sha256
        != sha256_file(print_dir / "print-tile-assembly-manifest.json")
        or manifest.source_connector_plan_sha256 != source_manifest.connector_plan_sha256
        or manifest.tile_grid_shape != source_manifest.tile_grid_shape
        or manifest.tile_count != source_manifest.tile_count
    ):
        raise ConfigurationError("tile slice manifest does not match source print identities")
    _verify_slice_slicer_binding(manifest, slicer_identity)
    for profile_file in manifest.profile_files:
        profile_path = _workflow_relative_path(
            slice_dir,
            profile_file.path,
            context="tile slice profile path",
        )
        if not profile_path.is_file() or sha256_file(profile_path) != profile_file.sha256:
            raise ConfigurationError(f"tile slice profile checksum mismatch: {profile_file.path}")

    source_by_id = {tile.tile_id: tile for tile in source_manifest.tiles}
    reports: list[PrintTileSliceReport] = []
    total_size = 0
    for record in manifest.tiles:
        source_record = source_by_id.get(record.tile_id)
        if source_record is None or (
            (record.row, record.column) != (source_record.row, source_record.column)
            or record.source_print_tile_manifest_sha256 != source_record.tile_manifest_sha256
            or record.source_print_local_3mf_sha256 != source_record.sha256["print_local_3mf"]
        ):
            raise ConfigurationError(f"tile slice source identity mismatch: {record.tile_id}")
        report_path = _workflow_relative_path(
            slice_dir,
            record.report_path,
            context=f"tile slice report path for {record.tile_id}",
        )
        gcode_path = _workflow_relative_path(
            slice_dir,
            record.gcode_path,
            context=f"tile slice G-code path for {record.tile_id}",
        )
        if sha256_file(report_path) != record.report_sha256:
            raise ConfigurationError(f"tile slice report checksum mismatch: {record.tile_id}")
        if sha256_file(gcode_path) != record.gcode_sha256:
            raise ConfigurationError(f"tile slice G-code checksum mismatch: {record.tile_id}")
        try:
            report = PrintTileSliceReport.model_validate_json(
                report_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"tile slice report is unreadable: {record.tile_id}") from exc
        if report_path.read_bytes() != _canonical_bytes(report):
            raise ConfigurationError(f"tile slice report is not canonical: {record.tile_id}")
        source_input = _workflow_relative_path(
            print_dir,
            source_record.files["print_local_3mf"],
            context=f"print-local 3MF path for {record.tile_id}",
        )
        if (
            report.tile_id != record.tile_id
            or report.source_print_tile_manifest_sha256 != source_record.tile_manifest_sha256
            or report.source_print_local_3mf_path != source_record.files["print_local_3mf"]
            or report.source_print_local_3mf_sha256 != sha256_file(source_input)
            or report.gcode_path != record.gcode_path
            or report.gcode_sha256 != record.gcode_sha256
            or report.gcode_size_bytes != gcode_path.stat().st_size
            or report.slicer_result.input_model != Path(report.source_print_local_3mf_path)
            or report.slicer_result.output_gcode != Path(report.gcode_path)
        ):
            raise ConfigurationError(f"tile slice report identity mismatch: {record.tile_id}")
        reopened = parse_gcode_metrics(
            gcode_path.read_text(encoding="utf-8", errors="replace"),
            diagnostics="\n".join((report.slicer_result.stdout, report.slicer_result.stderr)),
        )
        if reopened != report.reopened_metrics or reopened != report.slicer_result.metrics:
            raise ConfigurationError(f"tile G-code metrics changed: {record.tile_id}")
        expected_gate = (
            evaluate_bambu_p2s_release_gate(
                report.slicer_result.model_dump(mode="json"),
                printer_profile_id=connector_plan.policy.printer_profile.profile_id,
            )
            if report.slicer_result.slicer.name == "BambuStudio"
            else None
        )
        expected_release_passed = bool(
            expected_gate is not None and expected_gate.get("release_gate_passed") is True
        )
        required = bool(
            report.required_checks_passed
            and report.slicer_result.status is SliceStatus.SUCCEEDED
            and report.slicer_result.exit_code == 0
            and report.exit_code_zero
            and report.gcode_generated
            and report.metrics_reopen_match
            and report.layer_count_positive
            and not report.out_of_bed
            and not report.empty_layer_warning
            and not report.floating_region_warning
            and report.support_material is False
            and (expected_gate is None or expected_release_passed)
        )
        if (
            report.manufacturing_release_gate != expected_gate
            or report.official_p2s_release_gate_passed != expected_release_passed
            or report.release_role
            != ("official-p2s-release" if expected_gate is not None else "diagnostic")
            or not required
            or record.layer_count != reopened.layer_count
            or record.estimated_time_seconds != reopened.estimated_time_seconds
            or record.filament_used_mm != reopened.filament_used_mm
            or record.filament_used_cm3 != reopened.filament_used_cm3
            or record.filament_used_g != reopened.filament_used_g
            or not record.required_checks_passed
        ):
            raise ConfigurationError(f"tile slice quality evidence changed: {record.tile_id}")
        total_size += gcode_path.stat().st_size
        reports.append(report)

    time_sum = _optional_sum([record.estimated_time_seconds for record in manifest.tiles])
    filament_mm_sum = _optional_sum([record.filament_used_mm for record in manifest.tiles])
    filament_cm3_sum = _optional_sum([record.filament_used_cm3 for record in manifest.tiles])
    filament_g_sum = _optional_sum([record.filament_used_g for record in manifest.tiles])
    if (
        total_size != manifest.total_gcode_size_bytes
        or manifest.total_estimated_time_seconds != (None if time_sum is None else int(time_sum))
        or manifest.total_filament_used_mm
        != (None if filament_mm_sum is None else float(filament_mm_sum))
        or manifest.total_filament_used_cm3
        != (None if filament_cm3_sum is None else float(filament_cm3_sum))
        or manifest.total_filament_used_g
        != (None if filament_g_sum is None else float(filament_g_sum))
        or manifest.maximum_layer_count != max(record.layer_count for record in manifest.tiles)
        or manifest.printer_profile_id != connector_plan.policy.printer_profile.profile_id
        or manifest.release_role
        != ("official-p2s-release" if manifest.slicer.name == "BambuStudio" else "diagnostic")
        or manifest.official_p2s_release_gate_passed
        != bool(
            manifest.slicer.name == "BambuStudio"
            and all(report.official_p2s_release_gate_passed for report in reports)
        )
        or manifest.all_parameter_checks_passed
        != bool(
            manifest.slicer.name == "BambuStudio"
            and all(
                report.manufacturing_release_gate is not None
                and report.manufacturing_release_gate.get("parameter_checks_passed") is True
                for report in reports
            )
        )
        or not manifest.all_exit_codes_zero
        or not manifest.no_out_of_bed
        or not manifest.no_empty_layers
        or not manifest.no_floating_regions
        or not manifest.no_support_material
        or not manifest.required_checks_passed
    ):
        raise ConfigurationError("tile slice aggregate summary changed")
    return {
        "status": "verified",
        "output_dir": str(slice_dir),
        "layout_id": manifest.layout_id,
        "tile_grid_shape": manifest.tile_grid_shape,
        "tile_count": manifest.tile_count,
        "slicer": manifest.slicer.model_dump(mode="json"),
        "profile": manifest.profile_name,
        "printer_profile_id": manifest.printer_profile_id,
        "release_role": manifest.release_role,
        "official_p2s_release_gate_passed": manifest.official_p2s_release_gate_passed,
        "all_parameter_checks_passed": manifest.all_parameter_checks_passed,
        "total_gcode_size_bytes": manifest.total_gcode_size_bytes,
        "total_estimated_time_seconds": manifest.total_estimated_time_seconds,
        "total_filament_used_mm": manifest.total_filament_used_mm,
        "total_filament_used_cm3": manifest.total_filament_used_cm3,
        "total_filament_used_g": manifest.total_filament_used_g,
        "maximum_layer_count": manifest.maximum_layer_count,
        "all_exit_codes_zero": True,
        "no_out_of_bed": True,
        "no_empty_layers": True,
        "no_floating_regions": True,
        "no_support_material": True,
        "required_checks_passed": True,
    }


def _verify_bambu_project_artifacts(
    project_dir: Path,
    *,
    print_dir: Path,
    slice_dir: Path,
    slicer_identity: dict[str, Any],
) -> dict[str, Any]:
    """Reopen project evidence without requiring or executing Bambu Studio."""
    print_manifest = _canonical_print_manifest(print_dir)
    slice_manifest = _canonical_slice_manifest(slice_dir)
    _verify_slice_slicer_binding(slice_manifest, slicer_identity)
    manifest_path = project_dir / "bambu-tile-project-manifest.json"
    try:
        manifest = bambu_projects.load_json(manifest_path)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("Bambu project manifest is unreadable") from exc
    if manifest_path.read_bytes() != bambu_projects.canonical_bytes(manifest):
        raise ConfigurationError("Bambu project manifest is not canonical")
    probe = SlicerInfo.model_validate(slicer_identity["probe"])
    expected_executable_sha256 = slicer_identity["executable_sha256"]
    if (
        manifest.get("schema_version") != bambu_projects.SCHEMA_VERSION
        or manifest.get("source_print_manifest_sha256")
        != sha256_file(print_dir / "print-tile-assembly-manifest.json")
        or manifest.get("source_slice_manifest_sha256")
        != sha256_file(slice_dir / "tile-slice-manifest.json")
        or manifest.get("bambu_studio_path") != str(probe.executable)
        or manifest.get("bambu_studio_sha256") != expected_executable_sha256
        or manifest.get("bambu_studio_version") != probe.version
        or manifest.get("layout_id") != print_manifest.layout_id
        or manifest.get("tile_grid_shape") != list(print_manifest.tile_grid_shape)
        or manifest.get("tile_count") != print_manifest.tile_count
        or manifest.get("printer_profile_id") != slice_manifest.printer_profile_id
    ):
        raise ConfigurationError("Bambu project root identities changed")
    project_profiles = manifest.get("profile_files")
    if not isinstance(project_profiles, list):
        raise ConfigurationError("Bambu project profile inventory changed")
    project_profile_binding: list[tuple[Any, Any, Any]] = []
    for profile in project_profiles:
        if not isinstance(profile, dict) or set(profile) != {"role", "index", "path", "sha256"}:
            raise ConfigurationError("Bambu project profile record is invalid")
        path = _workflow_relative_path(
            project_dir,
            profile["path"],
            context="Bambu project profile path",
        )
        if not path.is_file() or sha256_file(path) != profile["sha256"]:
            raise ConfigurationError(f"Bambu project profile checksum changed: {path}")
        project_profile_binding.append((profile["role"], profile["index"], profile["sha256"]))
    slice_profile_binding = [
        (profile.role, profile.index, profile.sha256) for profile in slice_manifest.profile_files
    ]
    if project_profile_binding != slice_profile_binding:
        raise ConfigurationError("Bambu project profiles changed from slice evidence")

    records = manifest.get("tiles")
    if not isinstance(records, list) or len(records) != print_manifest.tile_count:
        raise ConfigurationError("Bambu project tile count changed")
    print_by_id = {record.tile_id: record for record in print_manifest.tiles}
    slice_by_id = {record.tile_id: record for record in slice_manifest.tiles}
    expected_roles = {
        "bambu_project_3mf",
        "primary_gcode",
        "reopen_gcode",
        "build_result",
        "reopen_result",
        "build_stdout",
        "build_stderr",
        "reopen_stdout",
        "reopen_stderr",
    }
    seen_ids: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            raise ConfigurationError("Bambu project tile record is not an object")
        tile_id = record.get("tile_id")
        validation_relative = record.get("validation_path")
        if not isinstance(tile_id, str) or not isinstance(validation_relative, str):
            raise ConfigurationError("Bambu project tile id or validation path is invalid")
        source_print = print_by_id.get(tile_id)
        source_slice = slice_by_id.get(tile_id)
        if (
            source_print is None
            or source_slice is None
            or (
                record.get("row") != source_print.row
                or record.get("column") != source_print.column
                or record.get("source_print_tile_manifest_sha256")
                != source_print.tile_manifest_sha256
                or record.get("source_slice_report_sha256") != source_slice.report_sha256
                or record.get("required_checks_passed") is not True
            )
        ):
            raise ConfigurationError(f"Bambu project source identity changed: {tile_id}")
        seen_ids.append(tile_id)
        validation_path = _workflow_relative_path(
            project_dir,
            validation_relative,
            context=f"Bambu project validation path for {tile_id}",
        )
        validation = _read_canonical_object(
            validation_path,
            context=f"Bambu project validation for {tile_id}",
        )
        if sha256_file(validation_path) != record.get("validation_sha256"):
            raise ConfigurationError(f"Bambu project validation checksum changed: {tile_id}")
        files = record.get("files")
        hashes = record.get("sha256")
        if (
            not isinstance(files, dict)
            or not isinstance(hashes, dict)
            or set(files) != expected_roles
            or set(hashes) != expected_roles
        ):
            raise ConfigurationError(f"Bambu project file role set changed: {tile_id}")
        paths: dict[str, Path] = {}
        for role in sorted(expected_roles):
            relative = files.get(role)
            if not isinstance(relative, str):
                raise ConfigurationError(f"Bambu project path is invalid: {tile_id}/{role}")
            path = _workflow_relative_path(
                project_dir,
                relative,
                context=f"Bambu project {role} path for {tile_id}",
            )
            if not path.is_file() or sha256_file(path) != hashes.get(role):
                raise ConfigurationError(f"Bambu project checksum changed: {tile_id}/{role}")
            paths[role] = path

        source_path = _workflow_relative_path(
            print_dir,
            source_print.files["print_local_3mf"],
            context=f"Bambu project source 3MF path for {tile_id}",
        )
        source_inspection = inspect_3mf(source_path)
        try:
            build_result = bambu_projects.load_json(paths["build_result"])
            reopen_result = bambu_projects.load_json(paths["reopen_result"])
            build_object = bambu_projects.object_measurement(build_result)
            reopen_object = bambu_projects.object_measurement(reopen_result)
            archive = bambu_projects.archive_evidence(
                paths["bambu_project_3mf"], paths["primary_gcode"]
            )
            primary_metrics, primary_gate, primary_slicer = bambu_projects.release_gate(
                paths["primary_gcode"],
                stdout=paths["build_stdout"].read_text(errors="replace"),
                stderr=paths["build_stderr"].read_text(errors="replace"),
            )
            reopen_metrics, reopen_gate, reopen_slicer = bambu_projects.release_gate(
                paths["reopen_gcode"],
                stdout=paths["reopen_stdout"].read_text(errors="replace"),
                stderr=paths["reopen_stderr"].read_text(errors="replace"),
            )
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"Bambu project artifact is unreadable: {tile_id}") from exc
        dimensions_ok = bool(
            bambu_projects.dimensions_match(
                build_object["dimensions_mm"], source_inspection.dimensions_mm
            )
            and bambu_projects.dimensions_match(
                reopen_object["dimensions_mm"], source_inspection.dimensions_mm
            )
        )
        triangles_ok = bool(
            build_object["triangle_count"] == source_inspection.triangle_count
            and reopen_object["triangle_count"] == source_inspection.triangle_count
        )
        executions_ok = all(
            isinstance(validation.get(key), dict) and validation[key].get("process_exit_code") == 0
            for key in ("build_execution", "reopen_execution")
        )
        if (
            validation.get("schema_version") != bambu_projects.TILE_SCHEMA_VERSION
            or validation.get("tile_id") != tile_id
            or validation.get("source_print_local_3mf_path")
            != source_print.files["print_local_3mf"]
            or validation.get("source_print_local_3mf_sha256") != sha256_file(source_path)
            or validation.get("source_slice_report_sha256") != source_slice.report_sha256
            or validation.get("source_dimensions_mm") != list(source_inspection.dimensions_mm)
            or validation.get("source_triangle_count") != source_inspection.triangle_count
            or validation.get("build_result") != build_result
            or validation.get("reopen_result") != reopen_result
            or validation.get("build_object") != build_object
            or validation.get("reopen_object") != reopen_object
            or validation.get("dimensions_match") is not dimensions_ok
            or validation.get("triangle_counts_match") is not triangles_ok
            or validation.get("project_archive") != archive
            or validation.get("primary_metrics") != primary_metrics
            or validation.get("reopen_metrics") != reopen_metrics
            or validation.get("primary_release_gate") != primary_gate
            or validation.get("reopen_release_gate") != reopen_gate
            or validation.get("primary_slicer") != primary_slicer
            or validation.get("reopen_slicer") != reopen_slicer
            or primary_slicer != reopen_slicer
            or primary_slicer.get("name") != "BambuStudio"
            or primary_slicer.get("version") != manifest.get("bambu_studio_version")
            or not bambu_projects.result_passed(build_result)
            or not bambu_projects.result_passed(reopen_result)
            or not executions_ok
            or archive.get("archive_test_passed") is not True
            or archive.get("embedded_gcode_md5_verified") is not True
            or archive.get("embedded_gcode_matches_primary") is not True
            or primary_gate.get("release_gate_passed") is not True
            or reopen_gate.get("release_gate_passed") is not True
            or not dimensions_ok
            or not triangles_ok
            or validation.get("external_profiles_loaded_on_reopen") is not False
            or validation.get("required_checks_passed") is not True
        ):
            raise ConfigurationError(f"Bambu project validation changed: {tile_id}")
    if seen_ids != [record.tile_id for record in print_manifest.tiles] or (
        manifest.get("all_projects_reopened") is not True
        or manifest.get("all_release_gates_passed") is not True
        or manifest.get("required_checks_passed") is not True
    ):
        raise ConfigurationError("Bambu project aggregate evidence changed")
    return {
        "status": "verified",
        "tile_count": manifest["tile_count"],
        "all_projects_reopened": True,
        "all_release_gates_passed": True,
        "required_checks_passed": True,
    }


def verify_completed_workflow(
    workspace_dir: Path,
    *,
    verify_request_identity: bool = True,
) -> dict[str, Any]:
    """Strictly and offline reverify one completed workflow's full identity chain.

    The verifier performs no provider download and launches no slicer. The PROJECT
    gate reopens persisted archives, G-code, profile hashes, and measured release
    evidence with the recorded Bambu Studio executable identity. Relocated restores may
    set ``verify_request_identity=False``: persisted request/workflow identities remain
    authoritative while the remapped launch inputs are checked by content hash.
    """
    root = _completed_workflow_root(workspace_dir)
    launch, config = _read_completed_launch(root)
    request_path = _workflow_relative_path(
        root,
        "workflow-request.json",
        context="workflow request path",
    )
    request = _read_canonical_object(request_path, context="workflow request")

    workflow_path = _workflow_relative_path(
        root,
        "workflow-manifest.json",
        context="workflow manifest path",
    )
    workflow_payload = _read_canonical_object(workflow_path, context="workflow manifest")
    try:
        workflow = LocalWorkflowManifest.model_validate(workflow_payload)
    except ValueError as exc:
        raise ConfigurationError("workflow manifest schema is invalid") from exc
    if (
        workflow.schema_version != _WORKFLOW_SCHEMA_VERSION
        or workflow.model_dump(mode="json") != workflow_payload
    ):
        raise ConfigurationError("workflow manifest schema or canonical fields changed")

    expected_stages = (
        *((WorkflowStage.ACQUIRE,) if config.global_source is not None else ()),
        WorkflowStage.SOURCE,
        WorkflowStage.BUILD,
        *((WorkflowStage.OVERLAY,) if config.overlay is not None else ()),
        WorkflowStage.LAYOUT,
        WorkflowStage.EXTRACT,
        WorkflowStage.MESH,
        WorkflowStage.CONNECT,
        *((WorkflowStage.SLICE,) if config.slicing_enabled else ()),
        *((WorkflowStage.PROJECT,) if config.project_evidence_enabled else ()),
    )
    if tuple(record.name for record in workflow.stages) != expected_stages:
        raise ConfigurationError("workflow stage set/order does not match the saved launch")
    if not workflow.required_checks_passed or not all(
        record.required_checks_passed for record in workflow.stages
    ):
        raise ConfigurationError("completed workflow contains an unpassed stage gate")
    records = iter(workflow.stages)

    source: Path
    source_sha256: str
    acquisition_manifest: Path | None = None
    global_evidence: GlobalSourceEvidence | None = None
    effective_build: BuildConfig
    if config.global_source is not None:
        acquisition_config = config.global_source
        acquisition_identity = _identity(
            {
                "schema_version": _WORKFLOW_SCHEMA_VERSION,
                "global_source": acquisition_config.identity_payload(),
            }
        )
        acquisition_record = next(records)
        acquisition_dir, acquisition_stage_manifest = _stage_paths(
            root,
            acquisition_record,
            order=0,
            stage=WorkflowStage.ACQUIRE,
            identity=acquisition_identity,
            manifest_name="acquire.json",
        )
        try:
            if verify_request_identity:
                acquisition_evidence = verify_global_source(
                    acquisition_config,
                    acquisition_dir / "global-aoi.tif",
                )
                _verify_acquisition_stage_manifest(
                    acquisition_stage_manifest,
                    acquisition_config,
                    acquisition_evidence,
                )
            else:
                acquisition_evidence = _verify_relocated_global_source(
                    acquisition_config,
                    acquisition_dir,
                    acquisition_stage_manifest,
                )
        except Exception as exc:
            raise ConfigurationError(
                "workflow acquire artifact verification failed; restore or reacquire "
                f"the exact source stage: {exc}"
            ) from exc
        acquisition_verification = {
            "status": "ready",
            "selected_provider": acquisition_evidence.provider_selection.selected_provider,
            "dataset_name": acquisition_evidence.dataset.dataset_name,
            "raster_sha256": acquisition_evidence.raster_sha256,
            "acquisition_manifest_sha256": acquisition_evidence.acquisition_manifest_sha256,
            "quality_mask_count": len(acquisition_evidence.quality_mask_paths),
            "required_checks_passed": acquisition_evidence.required_checks_passed,
        }
        _verify_stage_record(acquisition_record, acquisition_verification)
        source = acquisition_evidence.raster_path
        source_sha256 = acquisition_evidence.raster_sha256
        acquisition_manifest = acquisition_evidence.acquisition_manifest_path
        global_evidence = acquisition_evidence
        effective_build = _global_build_config(
            config.build,
            acquisition_config,
            acquisition_evidence,
        )
    else:
        source = config.build.dem_path.expanduser().resolve()
        if not source.is_file():
            raise ConfigurationError(
                f"saved workflow source DEM is unavailable: {source}; "
                "restore it before strict workflow verification"
            )
        source_sha256 = sha256_file(source)
        effective_build = config.build

    source_record = next(records)
    source = _verify_source_record(
        root,
        source_record,
        source=source,
        source_sha256=source_sha256,
        acquisition_manifest=acquisition_manifest,
        verify_request_identity=verify_request_identity,
    )

    slicer_identity = _verify_persisted_slicer_identity(
        request.get("slicer"),
        slicing_enabled=config.slicing_enabled,
        slicer_name=launch.slicer_name,
        settings=launch.slicer_settings,
        filaments=launch.slicer_filaments,
        verify_request_identity=verify_request_identity,
    )
    if verify_request_identity:
        try:
            expected_request = _request_payload(
                config,
                source_sha256=(None if config.global_source is not None else source_sha256),
                slicer_identity=slicer_identity,
            )
        except Exception as exc:
            raise ConfigurationError(
                f"saved launch inputs cannot reproduce the workflow request: {exc}"
            ) from exc
        if request != expected_request:
            raise ConfigurationError("workflow request does not match the saved launch and source")
    else:
        expected_common = {
            "schema_version": _WORKFLOW_SCHEMA_VERSION,
            "maximum_tile_width_mm": config.maximum_tile_width_mm,
            "maximum_tile_depth_mm": config.maximum_tile_depth_mm,
            "overlap_cells": config.overlap_cells,
            "slicing_enabled": config.slicing_enabled,
            "project_evidence_enabled": config.project_evidence_enabled,
            "slicer": slicer_identity,
        }
        if any(request.get(key) != value for key, value in expected_common.items()):
            raise ConfigurationError("restored workflow launch changed non-path request settings")
        if config.global_source is None:
            build_request = request.get("build")
            if (
                "global_source" in request
                or not isinstance(build_request, dict)
                or build_request.get("source_dem_sha256") != source_sha256
            ):
                raise ConfigurationError("restored local workflow source request changed")
        elif request.get("global_source") != config.global_source.identity_payload():
            raise ConfigurationError("restored global workflow acquisition request changed")
        if (request.get("overlay") is None) != (config.overlay is None):
            raise ConfigurationError("restored workflow overlay stage selection changed")
    request_sha256 = _identity(request)
    prefix = "global" if config.global_source is not None else "local"
    workflow_id = f"{prefix}-{request_sha256[:24]}"
    expected_final_stage = (
        WorkflowStage.PROJECT
        if config.project_evidence_enabled
        else WorkflowStage.SLICE
        if config.slicing_enabled
        else WorkflowStage.CONNECT
    )
    if (
        workflow.request_sha256 != request_sha256
        or workflow.workflow_id != workflow_id
        or workflow.source_dem_sha256 != source_sha256
        or (verify_request_identity and workflow.source_dem_path != str(source))
        or workflow.slicing_enabled is not config.slicing_enabled
        or workflow.final_stage is not expected_final_stage
    ):
        raise ConfigurationError("workflow manifest root identity does not match the request")

    status_path = _workflow_relative_path(
        root,
        "workflow-status.json",
        context="workflow status path",
    )
    status_payload = _read_canonical_object(status_path, context="workflow status")
    try:
        status = LocalWorkflowStatus.model_validate(status_payload)
    except ValueError as exc:
        raise ConfigurationError("workflow status schema is invalid") from exc
    if (
        status.schema_version != _WORKFLOW_STATUS_SCHEMA_VERSION
        or status.model_dump(mode="json") != status_payload
        or status.workflow_id != workflow_id
        or status.state is not WorkflowState.COMPLETED
        or status.current_stage is not None
        or status.ready_stages != expected_stages
        or status.failure_path is not None
    ):
        raise ConfigurationError("workflow completed status does not match the identity chain")

    build_record = next(records)
    resolved_build_path = _workflow_relative_path(
        root,
        f"{build_record.output_path}/build_config.resolved.yaml",
        context="workflow resolved build configuration path",
    )
    try:
        resolved_build_payload = yaml.safe_load(resolved_build_path.read_text(encoding="utf-8"))
        if not isinstance(resolved_build_payload, dict):
            raise ValueError("resolved build root is not an object")
        artifact_build = BuildConfig.model_validate(resolved_build_payload)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ConfigurationError("workflow resolved build configuration is unreadable") from exc
    artifact_build_identity = _build_identity_payload(artifact_build, source_sha256)
    if str(artifact_build.dem_path) != workflow.source_dem_path:
        raise ConfigurationError("workflow BUILD source path changed from the source manifest")
    if (
        not verify_request_identity
        and config.global_source is None
        and request.get("build") != artifact_build_identity
    ):
        raise ConfigurationError("restored workflow BUILD configuration changed from its request")
    if not verify_request_identity and config.global_source is not None:
        if global_evidence is None:
            raise AssertionError("verified global acquisition evidence disappeared")
        expected_global_build = _global_build_config(
            config.build,
            config.global_source,
            global_evidence,
        ).model_dump(mode="json")
        artifact_global_build = artifact_build.model_dump(mode="json")
        for path_field in ("dem_path", "output_dir", "source_acquisition_manifest"):
            expected_global_build.pop(path_field, None)
            artifact_global_build.pop(path_field, None)
        if artifact_global_build != expected_global_build:
            raise ConfigurationError(
                "restored global workflow BUILD configuration changed from acquisition evidence"
            )
        if request.get("build_template") != _global_build_template_payload(config.build):
            raise ConfigurationError("restored global workflow build template changed")
    build_identity = (
        _identity(_build_identity_payload(effective_build, source_sha256))
        if verify_request_identity
        else _identity(artifact_build_identity)
    )
    build_dir, build_manifest = _stage_paths(
        root,
        build_record,
        order=10,
        stage=WorkflowStage.BUILD,
        identity=build_identity,
        manifest_name="build_manifest.json",
    )
    resolved_build = (
        effective_build.model_copy(update={"dem_path": source, "output_dir": build_dir})
        if verify_request_identity
        else artifact_build
    )
    build_verification = _run_artifact_verifier(
        WorkflowStage.BUILD,
        lambda: _verify_build_stage(
            build_dir,
            expected_config=resolved_build,
            expected_source_sha256=source_sha256,
        ),
    )
    _verify_stage_record(build_record, build_verification)

    if config.overlay is not None:
        overlay_request = (
            overlay_identity_payload(config.overlay)
            if verify_request_identity
            else request.get("overlay")
        )
        if not isinstance(overlay_request, dict):
            raise ConfigurationError("workflow overlay request identity is missing")
        if not verify_request_identity:
            current_overlay_request = overlay_identity_payload(config.overlay)
            if _overlay_identity_without_paths(overlay_request) != _overlay_identity_without_paths(
                current_overlay_request
            ):
                raise ConfigurationError(
                    "restored workflow overlay configuration or source content changed"
                )
        overlay_identity = _identity(
            {
                "source_build_manifest_sha256": sha256_file(build_manifest),
                "overlay": overlay_request,
            }
        )
        overlay_record = next(records)
        overlay_dir, _ = _stage_paths(
            root,
            overlay_record,
            order=15,
            stage=WorkflowStage.OVERLAY,
            identity=overlay_identity,
            manifest_name="overlay_manifest.json",
        )
        overlay_verification = _run_artifact_verifier(
            WorkflowStage.OVERLAY,
            lambda: verify_overlay_bundle(overlay_dir, build_dir),
        )
        _verify_stage_record(overlay_record, overlay_verification)

    with rasterio.open(build_dir / "processed_dem.tif") as dataset:
        source_grid_shape = (dataset.height, dataset.width)
    try:
        validation = json.loads((build_dir / "validation.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("workflow build validation is unreadable") from exc
    if not isinstance(validation, dict):
        raise ConfigurationError("workflow build validation root is not an object")
    dimensions = validation.get("dimensions_mm")
    if not isinstance(dimensions, list) or len(dimensions) < 2:
        raise ConfigurationError("workflow build validation has no model dimensions")
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
    layout_record = next(records)
    _, layout_path = _stage_paths(
        root,
        layout_record,
        order=20,
        stage=WorkflowStage.LAYOUT,
        identity=layout_identity,
        manifest_name="tile-layout.json",
    )
    try:
        layout = read_tile_layout(layout_path)
        if layout_path.read_bytes() != canonical_tile_layout_bytes(
            layout
        ) or layout != plan_tile_layout(layout_config):
            raise ConfigurationError("tile layout changed from its requested configuration")
    except Exception as exc:
        raise ConfigurationError(
            f"workflow layout artifact verification failed; restore or rebuild that stage: {exc}"
        ) from exc
    layout_verification = {
        "layout_id": layout.layout_id,
        "tile_grid_shape": layout.tile_grid_shape,
        "tile_count": layout.tile_count,
        "required_checks_passed": True,
    }
    _verify_stage_record(layout_record, layout_verification)

    extract_identity = _identity(
        {
            "source_build_manifest_sha256": sha256_file(build_manifest),
            "layout_sha256": sha256_file(layout_path),
        }
    )
    extract_record = next(records)
    tile_dir, tile_manifest = _stage_paths(
        root,
        extract_record,
        order=30,
        stage=WorkflowStage.EXTRACT,
        identity=extract_identity,
        manifest_name="assembly_manifest.json",
    )
    extract_verification = _run_artifact_verifier(
        WorkflowStage.EXTRACT,
        lambda: verify_tile_set(tile_dir, build_dir),
    )
    _verify_stage_record(extract_record, extract_verification)

    mesh_identity = _identity(
        {
            "source_build_manifest_sha256": sha256_file(build_manifest),
            "source_tile_manifest_sha256": sha256_file(tile_manifest),
        }
    )
    mesh_record = next(records)
    mesh_dir, mesh_manifest = _stage_paths(
        root,
        mesh_record,
        order=40,
        stage=WorkflowStage.MESH,
        identity=mesh_identity,
        manifest_name="tile-mesh-assembly-manifest.json",
    )
    mesh_verification = _run_artifact_verifier(
        WorkflowStage.MESH,
        lambda: verify_tile_mesh_set(mesh_dir, tile_dir, build_dir),
    )
    _verify_stage_record(mesh_record, mesh_verification)

    connect_identity = _identity(
        {
            "source_build_manifest_sha256": sha256_file(build_manifest),
            "source_tile_manifest_sha256": sha256_file(tile_manifest),
            "source_mesh_manifest_sha256": sha256_file(mesh_manifest),
        }
    )
    connect_record = next(records)
    print_dir, print_manifest = _stage_paths(
        root,
        connect_record,
        order=50,
        stage=WorkflowStage.CONNECT,
        identity=connect_identity,
        manifest_name="print-tile-assembly-manifest.json",
    )
    connect_verification = _run_artifact_verifier(
        WorkflowStage.CONNECT,
        lambda: verify_print_tile_set(print_dir, mesh_dir, tile_dir, build_dir),
    )
    _verify_stage_record(connect_record, connect_verification)

    slice_dir: Path | None = None
    slice_manifest: Path | None = None
    if config.slicing_enabled:
        if slicer_identity is None:
            raise AssertionError("validated persisted slicer identity disappeared")
        slice_identity = _identity(
            {
                "source_print_manifest_sha256": sha256_file(print_manifest),
                "slicer": slicer_identity,
            }
        )
        slice_record = next(records)
        slice_dir, slice_manifest = _stage_paths(
            root,
            slice_record,
            order=60,
            stage=WorkflowStage.SLICE,
            identity=slice_identity,
            manifest_name="tile-slice-manifest.json",
        )
        slice_verification = _run_artifact_verifier(
            WorkflowStage.SLICE,
            lambda: _verify_tile_slice_artifacts(
                slice_dir,
                print_dir=print_dir,
                slicer_identity=slicer_identity,
            ),
        )
        _verify_stage_record(slice_record, slice_verification)

    if config.project_evidence_enabled:
        if slicer_identity is None or slice_dir is None or slice_manifest is None:
            raise AssertionError("project workflow slice identity disappeared")
        executable_sha256 = slicer_identity["executable_sha256"]
        project_identity = _identity(
            {
                "source_print_manifest_sha256": sha256_file(print_manifest),
                "source_slice_manifest_sha256": sha256_file(slice_manifest),
                "bambu_studio_sha256": executable_sha256,
            }
        )
        project_record = next(records)
        project_dir, _ = _stage_paths(
            root,
            project_record,
            order=70,
            stage=WorkflowStage.PROJECT,
            identity=project_identity,
            manifest_name="bambu-tile-project-manifest.json",
        )
        project_verification = _run_artifact_verifier(
            WorkflowStage.PROJECT,
            lambda: _verify_bambu_project_artifacts(
                project_dir,
                print_dir=print_dir,
                slice_dir=slice_dir,
                slicer_identity=slicer_identity,
            ),
        )
        _verify_stage_record(project_record, project_verification)

    try:
        next(records)
    except StopIteration:
        pass
    else:
        raise ConfigurationError("workflow manifest contains unexpected trailing stages")
    return {
        "status": "verified",
        "workflow_id": workflow_id,
        "request_sha256": request_sha256,
        "source_dem_sha256": source_sha256,
        "stages": [stage.value for stage in expected_stages],
        "external_processes_executed": False,
        "required_checks_passed": True,
    }


def run_local_workflow(
    config: LocalWorkflowConfig,
    *,
    workspace_identity: tuple[int, int] | None = None,
    adapter: SlicerAdapter | None = None,
    profile: SlicerProfile | None = None,
    acquisition_providers: Mapping[str, ElevationProvider] | None = None,
    acquisition_descriptors: Sequence[ProviderDescriptor] | None = None,
) -> LocalWorkflowResult:
    """Run or resume acquisition, build, tiling, connector, and slicing stages.

    Every generated stage is content-addressed by its complete upstream manifests and
    settings. Existing stages are reused only after the same strict reopen checks used
    at publication time. A changed configuration selects a new stage directory instead
    of overwriting prior evidence. Callers that already opened the workspace should pass
    its ``(device, inode/file-id)`` identity so execution cannot continue in a renamed
    replacement directory.
    """
    root = Path(os.path.abspath(config.workspace_dir.expanduser()))
    workspace = (
        None
        if workspace_identity is None
        else _workspace_lease(root, expected_identity=workspace_identity)
    )
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
    if workspace is None:
        workspace = _workspace_lease(root, expected_identity=None)
    _write_canonical(root / "workflow-request.json", request, workspace=workspace)

    records: list[WorkflowStageRecord] = []
    completed: list[WorkflowStage] = []
    reused: list[WorkflowStage] = []
    outputs: dict[WorkflowStage, Path] = {}
    current = WorkflowStage.ACQUIRE if config.global_source is not None else WorkflowStage.SOURCE
    _status(
        root,
        workspace=workspace,
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
                _ensure_workspace_directory(
                    acquisition_dir,
                    workspace=workspace,
                    context="acquisition stage output",
                )
                _require_workspace(workspace, context="acquisition stage launch")
                acquisition_evidence = acquire_global_source(
                    acquisition_config,
                    acquisition_raster,
                    providers=acquisition_providers,
                    descriptors=acquisition_descriptors,
                )
                _require_workspace(workspace, context="acquisition stage completion")
                _write_acquisition_stage_manifest(
                    acquisition_stage_manifest,
                    acquisition_config,
                    acquisition_evidence,
                    workspace=workspace,
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
            workspace=workspace,
            workflow_id=workflow_id,
            state=WorkflowState.RUNNING,
            current_stage=current,
            records=records,
        )
        source_manifest, was_reused = _publish_source(
            root,
            source,
            source_sha256,
            workspace=workspace,
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
            workspace=workspace,
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
            workspace,
            lambda: _verify_build_stage(
                build_dir,
                expected_config=resolved_build,
                expected_source_sha256=source_sha256,
            ),
        )
        if build_verification is None:
            _ensure_workspace_directory(
                build_dir.parent,
                workspace=workspace,
                context="build stage parent",
            )
            _require_workspace(workspace, context="build stage launch")
            private_build = _create_private_stage(
                build_dir,
                workspace=workspace,
                context="build stage",
            )
            build_local_terrain(
                resolved_build.model_copy(
                    update={"dem_path": source, "output_dir": private_build.output}
                )
            )
            _require_workspace(workspace, context="build stage completion")
            _normalize_private_build_stage(private_build, final_config=resolved_build)
            _verify_build_stage(
                private_build.output,
                expected_config=resolved_build,
                expected_source_sha256=source_sha256,
            )
            _publish_private_stage(
                private_build,
                build_dir,
                workspace=workspace,
                context="build stage publication",
            )
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
                workspace=workspace,
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
                workspace,
                lambda: verify_overlay_bundle(overlay_dir, build_dir),
            )
            if overlay_verification is None:
                _ensure_workspace_directory(
                    overlay_dir.parent,
                    workspace=workspace,
                    context="overlay stage parent",
                )
                _require_workspace(workspace, context="overlay stage launch")
                private_overlay = _create_private_stage(
                    overlay_dir,
                    workspace=workspace,
                    context="overlay stage",
                )
                generate_overlay_bundle(build_dir, config.overlay, private_overlay.output)
                _require_workspace(workspace, context="overlay stage completion")
                _publish_private_stage(
                    private_overlay,
                    overlay_dir,
                    workspace=workspace,
                    context="overlay stage publication",
                )
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
            workspace=workspace,
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
        from topoforge.web.security import owned_entry_identity, read_owned_regular_bytes

        _ensure_workspace_directory(
            layout_dir.parent,
            workspace=workspace,
            context="layout stage parent",
        )
        try:
            layout_directory_identity = owned_entry_identity(
                layout_dir,
                root=workspace.root,
                root_identity=workspace.identity,
                directory=True,
                context="existing layout stage",
            )
        except (OSError, ValueError) as exc:
            raise ConfigurationError(
                f"existing layout stage is unsafe or changed: {layout_dir}"
            ) from exc
        if layout_directory_identity is not None:
            try:
                layout_bytes = read_owned_regular_bytes(
                    layout_path,
                    root=layout_dir,
                    root_identity=layout_directory_identity,
                    context="existing layout stage manifest",
                )
                layout = TileLayout.model_validate_json(layout_bytes)
                if layout_bytes != canonical_tile_layout_bytes(layout):
                    raise ConfigurationError("tile layout is not canonical")
                if layout != plan_tile_layout(layout_config):
                    raise ConfigurationError("tile layout does not match requested configuration")
            except Exception as exc:
                raise ConfigurationError(
                    f"existing layout stage failed strict reuse at {layout_dir}: {exc}"
                ) from exc
            reused.append(current)
        else:
            staging, staging_identity = _create_workspace_stage(
                layout_dir.parent,
                prefix=layout_identity,
                workspace=workspace,
                context="layout stage",
            )
            layout = plan_tile_layout(layout_config)
            _write_canonical(
                staging / "tile-layout.json",
                layout,
                workspace=_WorkspaceLease(staging, staging_identity),
            )
            _publish_workspace_stage(
                staging,
                layout_dir,
                staging_identity=staging_identity,
                workspace=workspace,
                context="layout stage publication",
            )
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
            workspace=workspace,
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
            workspace,
            lambda: verify_tile_set(tile_dir, build_dir),
        )
        if extract_verification is None:
            _ensure_workspace_directory(
                tile_dir.parent,
                workspace=workspace,
                context="extract stage parent",
            )
            _require_workspace(workspace, context="extract stage launch")
            private_extract = _create_private_stage(
                tile_dir,
                workspace=workspace,
                context="extract stage",
            )
            extract_tile_set(build_dir, layout_path, private_extract.output)
            _require_workspace(workspace, context="extract stage completion")
            _publish_private_stage(
                private_extract,
                tile_dir,
                workspace=workspace,
                context="extract stage publication",
            )
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
            workspace=workspace,
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
            workspace,
            lambda: verify_tile_mesh_set(mesh_dir, tile_dir, build_dir),
        )
        if mesh_verification is None:
            _ensure_workspace_directory(
                mesh_dir.parent,
                workspace=workspace,
                context="mesh stage parent",
            )
            _require_workspace(workspace, context="mesh stage launch")
            private_mesh = _create_private_stage(
                mesh_dir,
                workspace=workspace,
                context="mesh stage",
            )
            generate_tile_mesh_set(tile_dir, build_dir, private_mesh.output)
            _require_workspace(workspace, context="mesh stage completion")
            _publish_private_stage(
                private_mesh,
                mesh_dir,
                workspace=workspace,
                context="mesh stage publication",
            )
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
            workspace=workspace,
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
            workspace,
            lambda: verify_print_tile_set(print_dir, mesh_dir, tile_dir, build_dir),
        )
        if connect_verification is None:
            _ensure_workspace_directory(
                print_dir.parent,
                workspace=workspace,
                context="connect stage parent",
            )
            _require_workspace(workspace, context="connect stage launch")
            private_connect = _create_private_stage(
                print_dir,
                workspace=workspace,
                context="connect stage",
            )
            generate_print_tile_set(mesh_dir, tile_dir, build_dir, private_connect.output)
            _require_workspace(workspace, context="connect stage completion")
            _publish_private_stage(
                private_connect,
                print_dir,
                workspace=workspace,
                context="connect stage publication",
            )
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
                workspace=workspace,
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
                workspace,
                lambda: verify_tile_slice_set(slice_dir, print_dir, mesh_dir, tile_dir, build_dir),
            )
            if slice_verification is None:
                _ensure_workspace_directory(
                    slice_dir.parent,
                    workspace=workspace,
                    context="slice stage parent",
                )
                _require_workspace(workspace, context="slice stage launch")
                private_slice = _create_private_stage(
                    slice_dir,
                    workspace=workspace,
                    context="slice stage",
                )
                slice_print_tile_set(
                    print_dir,
                    mesh_dir,
                    tile_dir,
                    build_dir,
                    private_slice.output,
                    adapter=adapter,
                    profile=profile,
                    timeout_seconds=config.slice_timeout_seconds,
                )
                _require_workspace(workspace, context="slice stage completion")
                _publish_private_stage(
                    private_slice,
                    slice_dir,
                    workspace=workspace,
                    context="slice stage publication",
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
                workspace=workspace,
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
                workspace,
                lambda: verify_bambu_project_evidence(
                    project_dir,
                    print_set_dir=print_dir,
                    slice_set_dir=slice_dir,
                    bambu_studio=bambu_executable,
                ),
            )
            if project_verification is None:
                _ensure_workspace_directory(
                    project_dir.parent,
                    workspace=workspace,
                    context="project stage parent",
                )
                _require_workspace(workspace, context="project stage launch")
                private_project = _create_private_stage(
                    project_dir,
                    workspace=workspace,
                    context="project stage",
                )
                generate_bambu_project_evidence(
                    print_dir,
                    slice_dir,
                    bambu_executable,
                    private_project.output,
                    timeout_seconds=config.project_timeout_seconds,
                )
                _require_workspace(workspace, context="project stage completion")
                _publish_private_stage(
                    private_project,
                    project_dir,
                    workspace=workspace,
                    context="project stage publication",
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
        manifest_path = _write_canonical(
            root / "workflow-manifest.json",
            manifest,
            workspace=workspace,
        )
        reopened = LocalWorkflowManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        if reopened != manifest or manifest_path.read_bytes() != _canonical_bytes(manifest):
            raise ConfigurationError("workflow manifest failed canonical strict reopen")
        status_path = _status(
            root,
            workspace=workspace,
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
        try:
            failure_path = _write_canonical(
                root / "failures" / f"{failure_identity}.json",
                failure,
                workspace=workspace,
            )
            _status(
                root,
                workspace=workspace,
                workflow_id=workflow_id,
                state=WorkflowState.FAILED,
                current_stage=current,
                records=records,
                failure_path=failure_path,
            )
        except Exception as persistence_exc:
            exc.add_note(
                "workflow failure evidence was not written because the original "
                f"workspace could not be safely reopened: {persistence_exc}"
            )
        raise
