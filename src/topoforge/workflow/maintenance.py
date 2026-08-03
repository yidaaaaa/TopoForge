"""Disk planning, reviewed cleanup, and verified workflow backup contracts."""

from __future__ import annotations

import hashlib
import json
import math
import shlex
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from topoforge import __version__
from topoforge.exceptions import ConfigurationError
from topoforge.provenance import write_json
from topoforge.util import sha256_bytes, sha256_file
from topoforge.workflow.local import LocalWorkflowManifest

if TYPE_CHECKING:
    from topoforge.workflow.ux import WorkflowLaunchConfig, WorkflowRunSummary

_STORAGE_SCHEMA_VERSION = "topoforge-workflow-storage-v1"
_CLEANUP_SCHEMA_VERSION = "topoforge-workflow-cleanup-v1"
_BACKUP_SCHEMA_VERSION = "topoforge-workflow-backup-v1"
_RESTORE_SCHEMA_VERSION = "topoforge-workflow-restore-v1"
_BACKUP_MANIFEST_NAME = "topoforge-backup-manifest.json"


class WorkflowStorageEstimate(BaseModel):
    """Conservative disk estimate based on configured ceilings or measured output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _STORAGE_SCHEMA_VERSION
    workspace: Path
    source_mode: Literal["local", "global"]
    estimate_basis: Literal["configured_resource_ceilings", "completed_workflow_measurements"]
    grid_cell_count: int = Field(ge=1)
    estimated_triangle_count: int = Field(ge=1)
    estimated_tile_count: int = Field(ge=1)
    current_workspace_bytes: int = Field(ge=0)
    estimated_peak_workspace_bytes: int = Field(ge=0)
    estimated_additional_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    estimated_headroom_bytes: int
    sufficient_for_estimate: bool
    cleanup_reclaimable_bytes: int = Field(ge=0)
    backup_input_bytes: int = Field(ge=0)
    component_estimates_bytes: dict[str, int]
    assumptions: tuple[str, ...]


class WorkflowCleanupCandidate(BaseModel):
    """One unreferenced stage path eligible for explicit deletion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    kind: Literal["directory", "file", "symlink"]
    size_bytes: int = Field(ge=0)
    reason: str


class WorkflowCleanupPlan(BaseModel):
    """Reviewable cleanup plan that preserves every current manifest stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _CLEANUP_SCHEMA_VERSION
    workflow_id: str
    workspace: Path
    current_workspace_bytes: int = Field(ge=0)
    reclaimable_bytes: int = Field(ge=0)
    candidates: tuple[WorkflowCleanupCandidate, ...]
    review_command: str
    apply_command: str
    required_checks_passed: bool


class WorkflowCleanupResult(BaseModel):
    """Measured result after applying an unchanged reviewed cleanup plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _CLEANUP_SCHEMA_VERSION
    workflow_id: str
    workspace: Path
    removed_paths: tuple[str, ...]
    reclaimed_bytes: int = Field(ge=0)
    remaining_workspace_bytes: int = Field(ge=0)
    required_checks_passed: bool


class WorkflowBackupFile(BaseModel):
    """One checksum-bound file stored in a workflow backup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    archive_path: str
    source_path: str
    kind: Literal["workspace", "external"]
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkflowBackupManifest(BaseModel):
    """Portable, deterministic inventory embedded in every workflow backup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _BACKUP_SCHEMA_VERSION
    backup_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_id: str
    original_workspace: Path
    topoforge_version: str
    files: tuple[WorkflowBackupFile, ...]
    required_checks_passed: bool


class WorkflowBackupResult(BaseModel):
    """Verified workflow backup artifact and its measured checksum."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    archive_path: Path
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_size_bytes: int = Field(ge=0)
    manifest: WorkflowBackupManifest


class WorkflowRestoreResult(BaseModel):
    """Strictly reopened workspace restored from a verified backup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _RESTORE_SCHEMA_VERSION
    backup_id: str
    archive_path: Path
    archive_sha256: str
    workspace: Path
    external_directory: Path | None = None
    workflow_id: str
    required_checks_passed: bool


def _directory_size(root: Path) -> int:
    if not root.exists():
        return 0
    if root.is_symlink() or root.is_file():
        return root.lstat().st_size
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink() or path.is_file():
            total += path.lstat().st_size
    return total


def _disk_probe(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _external_reference_paths(config: WorkflowLaunchConfig) -> tuple[Path, ...]:
    values = [config.build.dem_path, *config.slicer_settings, *config.slicer_filaments]
    if config.overlay is not None:
        values.extend(source.path for source in config.overlay.sources if source.path is not None)
    if config.build.source_acquisition_manifest is not None:
        values.append(config.build.source_acquisition_manifest)
    unique: dict[Path, None] = {}
    root = config.workspace_dir.expanduser().resolve()
    for value in values:
        resolved = value.expanduser().resolve()
        if resolved != root and root not in resolved.parents:
            unique[resolved] = None
    return tuple(sorted(unique, key=str))


def _measured_counts(
    config: WorkflowLaunchConfig,
    summary: WorkflowRunSummary | None,
) -> tuple[int, int, str]:
    if summary is not None:
        shape = summary.metrics.get("processed_grid_shape")
        triangles = summary.metrics.get("estimated_triangle_count")
        if (
            isinstance(shape, (list, tuple))
            and len(shape) == 2
            and all(isinstance(value, int) and value > 0 for value in shape)
            and isinstance(triangles, int)
            and triangles > 0
        ):
            return (
                int(shape[0]) * int(shape[1]),
                triangles,
                "completed_workflow_measurements",
            )
    cells = config.build.max_grid_cells
    triangles = config.build.max_estimated_triangles or max(12, cells * 4 - 4)
    return cells, triangles, "configured_resource_ceilings"


def _tile_count(config: WorkflowLaunchConfig, summary: WorkflowRunSummary | None) -> int:
    if summary is not None:
        measured = summary.metrics.get("tile_count")
        if isinstance(measured, int) and measured > 0:
            return measured
    width = config.build.model_width_mm
    depth = config.build.model_depth_mm or width
    columns = max(1, math.ceil(width / config.maximum_tile_width_mm))
    rows = max(1, math.ceil(depth / config.maximum_tile_depth_mm))
    return rows * columns


def estimate_workflow_storage(
    config: WorkflowLaunchConfig,
    *,
    summary: WorkflowRunSummary | None = None,
) -> WorkflowStorageEstimate:
    """Estimate peak local disk use without claiming exact compression ratios."""
    root = config.workspace_dir.expanduser().resolve()
    cells, triangles, basis = _measured_counts(config, summary)
    tile_count = _tile_count(config, summary)
    current_bytes = _directory_size(root)
    overlay_triangles = 0
    if config.overlay is not None:
        measured_overlay = summary.metrics.get("triangle_count") if summary is not None else None
        overlay_triangles = (
            measured_overlay
            if isinstance(measured_overlay, int) and measured_overlay > 0
            else config.overlay.max_triangles
        )
    source_mode: Literal["local", "global"] = (
        "global" if config.global_source is not None else "local"
    )
    components = {
        "source_and_stage_metadata": (cells * 8 if source_mode == "global" else 256 * 1024),
        "build_rasters_and_reports": cells * 12 + 24 * 1024 * 1024,
        "build_mesh_formats": triangles * 100,
        "tile_rasters_and_seam_evidence": int(cells * 8 * 1.15) + tile_count * 512 * 1024,
        "tile_mesh_formats": triangles * 105 + tile_count * 2 * 1024 * 1024,
        "connector_and_print_local_formats": triangles * 210 + tile_count * 3 * 1024 * 1024,
        "overlay_formats_and_reports": overlay_triangles * 180,
        "slice_outputs": triangles * 128 if config.slicing_enabled else 0,
        "project_outputs": triangles * 120 if config.project_evidence_enabled else 0,
    }
    estimated_peak = max(current_bytes, sum(components.values()))
    additional = max(0, estimated_peak - current_bytes)
    available = shutil.disk_usage(_disk_probe(root)).free
    reclaimable = 0
    if summary is not None:
        reclaimable = plan_workflow_cleanup(root).reclaimable_bytes
    external_bytes = sum(
        path.stat().st_size for path in _external_reference_paths(config) if path.is_file()
    )
    return WorkflowStorageEstimate(
        workspace=root,
        source_mode=source_mode,
        estimate_basis=basis,  # type: ignore[arg-type]
        grid_cell_count=cells,
        estimated_triangle_count=triangles,
        estimated_tile_count=tile_count,
        current_workspace_bytes=current_bytes,
        estimated_peak_workspace_bytes=estimated_peak,
        estimated_additional_bytes=additional,
        available_bytes=available,
        estimated_headroom_bytes=available - additional,
        sufficient_for_estimate=available >= additional,
        cleanup_reclaimable_bytes=reclaimable,
        backup_input_bytes=current_bytes + external_bytes,
        component_estimates_bytes=components,
        assumptions=(
            "Pre-run estimates use configured cell and triangle ceilings; completed runs "
            "use measured grid and triangle counts.",
            "STL, 3MF, GLB, G-code, and ZIP compression ratios vary with terrain and "
            "slicer settings.",
            "The estimate is conservative and is not a reservation or a guaranteed upper bound.",
            "Provider cache directories outside the workflow workspace are excluded.",
        ),
    )


def write_workflow_storage_estimate(
    estimate: WorkflowStorageEstimate,
    path: Path | None = None,
) -> Path:
    """Write and strictly reopen a storage estimate JSON record."""
    destination = (
        path if path is not None else estimate.workspace / "workflow-storage.json"
    ).resolve()
    write_json(destination, estimate.model_dump(mode="json"))
    reopened = WorkflowStorageEstimate.model_validate_json(destination.read_text(encoding="utf-8"))
    if reopened != estimate:
        raise ConfigurationError("workflow storage estimate failed strict JSON reopen")
    return destination


def _cleanup_kind(path: Path) -> Literal["directory", "file", "symlink"]:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "directory"
    return "file"


def plan_workflow_cleanup(workspace_dir: Path) -> WorkflowCleanupPlan:
    """List only stage identities not referenced by the completed manifest."""
    from topoforge.workflow.ux import inspect_workflow_workspace

    root = workspace_dir.expanduser().resolve()
    summary = inspect_workflow_workspace(root)
    manifest = LocalWorkflowManifest.model_validate_json(
        (root / "workflow-manifest.json").read_text(encoding="utf-8")
    )
    referenced = {(root / record.output_path).resolve() for record in manifest.stages}
    candidates: list[WorkflowCleanupCandidate] = []
    stages_root = root / "stages"
    if stages_root.is_dir():
        for family in sorted(stages_root.iterdir(), key=lambda path: path.name):
            if family.is_symlink() or not family.is_dir():
                if family.resolve() not in referenced:
                    candidates.append(
                        WorkflowCleanupCandidate(
                            path=family.relative_to(root).as_posix(),
                            kind=_cleanup_kind(family),
                            size_bytes=_directory_size(family),
                            reason="unexpected unreferenced entry under stages",
                        )
                    )
                continue
            for identity_path in sorted(family.iterdir(), key=lambda path: path.name):
                resolved = identity_path.resolve()
                if resolved in referenced:
                    continue
                if resolved != root and root not in resolved.parents:
                    raise ConfigurationError(
                        f"cleanup candidate escapes workflow workspace: {identity_path}"
                    )
                candidates.append(
                    WorkflowCleanupCandidate(
                        path=identity_path.relative_to(root).as_posix(),
                        kind=_cleanup_kind(identity_path),
                        size_bytes=_directory_size(identity_path),
                        reason="stage identity is not referenced by workflow-manifest.json",
                    )
                )
    quoted_root = shlex.quote(str(root))
    quoted_id = shlex.quote(summary.workflow_id)
    return WorkflowCleanupPlan(
        workflow_id=summary.workflow_id,
        workspace=root,
        current_workspace_bytes=_directory_size(root),
        reclaimable_bytes=sum(item.size_bytes for item in candidates),
        candidates=tuple(candidates),
        review_command=f"topoforge cleanup {quoted_root}",
        apply_command=(
            f"topoforge cleanup {quoted_root} --apply --confirm-workflow-id {quoted_id}"
        ),
        required_checks_passed=True,
    )


def apply_workflow_cleanup(
    workspace_dir: Path,
    *,
    confirm_workflow_id: str,
) -> WorkflowCleanupResult:
    """Apply the current cleanup plan only after an exact workflow-id confirmation."""
    from topoforge.workflow.ux import inspect_workflow_workspace

    plan = plan_workflow_cleanup(workspace_dir)
    if confirm_workflow_id != plan.workflow_id:
        raise ConfigurationError(
            "cleanup confirmation does not match the current workflow id; rerun the review command"
        )
    removed: list[str] = []
    for candidate in plan.candidates:
        path = (plan.workspace / candidate.path).resolve()
        if path != plan.workspace and plan.workspace not in path.parents:
            raise ConfigurationError(f"cleanup candidate escapes workflow workspace: {path}")
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
        removed.append(candidate.path)
    inspect_workflow_workspace(plan.workspace)
    return WorkflowCleanupResult(
        workflow_id=plan.workflow_id,
        workspace=plan.workspace,
        removed_paths=tuple(removed),
        reclaimed_bytes=plan.reclaimable_bytes,
        remaining_workspace_bytes=_directory_size(plan.workspace),
        required_checks_passed=True,
    )


def _backup_identity_payload(
    *,
    workflow_id: str,
    original_workspace: Path,
    topoforge_version: str,
    files: tuple[WorkflowBackupFile, ...],
) -> dict[str, object]:
    return {
        "schema_version": _BACKUP_SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "original_workspace": str(original_workspace),
        "topoforge_version": topoforge_version,
        "files": [item.model_dump(mode="json") for item in files],
        "required_checks_passed": True,
    }


def _canonical_json(value: BaseModel | dict[str, object]) -> bytes:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _safe_archive_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ConfigurationError(f"unsafe workflow backup path: {value}")
    return path


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (0o100644 & 0xFFFF) << 16
    return info


def _write_zip_file(archive: zipfile.ZipFile, name: str, source: Path) -> None:
    with source.open("rb") as input_stream, archive.open(_zip_info(name), "w") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)


def _workspace_backup_files(root: Path) -> list[tuple[WorkflowBackupFile, Path]]:
    files: list[tuple[WorkflowBackupFile, Path]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ConfigurationError(f"workflow backups do not follow symlinks: {path}")
        if not path.is_file():
            continue
        files.append(
            (
                WorkflowBackupFile(
                    archive_path=f"workspace/{path.relative_to(root).as_posix()}",
                    source_path=str(path),
                    kind="workspace",
                    size_bytes=path.stat().st_size,
                    sha256=sha256_file(path),
                ),
                path,
            )
        )
    return files


def _external_backup_files(
    config: WorkflowLaunchConfig,
) -> list[tuple[WorkflowBackupFile, Path]]:
    files: list[tuple[WorkflowBackupFile, Path]] = []
    for path in _external_reference_paths(config):
        if not path.is_file():
            raise ConfigurationError(f"referenced external workflow file is missing: {path}")
        digest = sha256_file(path)
        path_id = sha256_bytes(str(path).encode("utf-8"))[:12]
        archive_path = f"external/{path_id}-{digest[:12]}-{path.name}"
        files.append(
            (
                WorkflowBackupFile(
                    archive_path=archive_path,
                    source_path=str(path),
                    kind="external",
                    size_bytes=path.stat().st_size,
                    sha256=digest,
                ),
                path,
            )
        )
    return files


def create_workflow_backup(workspace_dir: Path, archive_path: Path) -> WorkflowBackupResult:
    """Create a deterministic checksum-bound backup and strictly reopen it."""
    from topoforge.workflow.ux import inspect_workflow_workspace, read_workflow_launch_config

    root = workspace_dir.expanduser().resolve()
    destination = archive_path.expanduser().resolve()
    if destination == root or root in destination.parents:
        raise ConfigurationError("workflow backup output must be outside the workspace")
    if destination.exists():
        raise ConfigurationError(f"workflow backup already exists: {destination}")
    if destination.suffix.lower() != ".zip":
        raise ConfigurationError("workflow backup path must end in .zip")
    summary = inspect_workflow_workspace(root)
    launch = read_workflow_launch_config(root / "workflow-launch.yaml")
    source_files = _workspace_backup_files(root) + _external_backup_files(launch)
    records = tuple(item for item, _ in source_files)
    identity_payload = _backup_identity_payload(
        workflow_id=summary.workflow_id,
        original_workspace=root,
        topoforge_version=__version__,
        files=records,
    )
    manifest = WorkflowBackupManifest(
        workflow_id=summary.workflow_id,
        original_workspace=root,
        topoforge_version=__version__,
        files=records,
        required_checks_passed=True,
        backup_id=sha256_bytes(_canonical_json(identity_payload)),
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            for record, source in source_files:
                _write_zip_file(archive, record.archive_path, source)
            archive.writestr(_zip_info(_BACKUP_MANIFEST_NAME), _canonical_json(manifest))
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    reopened = verify_workflow_backup(destination)
    if reopened != manifest:
        raise ConfigurationError("workflow backup failed strict manifest reopen")
    return WorkflowBackupResult(
        archive_path=destination,
        archive_sha256=sha256_file(destination),
        archive_size_bytes=destination.stat().st_size,
        manifest=manifest,
    )


def verify_workflow_backup(archive_path: Path) -> WorkflowBackupManifest:
    """Verify safe paths, exact inventory, CRCs, sizes, hashes, and backup identity."""
    source = archive_path.expanduser().resolve()
    try:
        with zipfile.ZipFile(source, mode="r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ConfigurationError("workflow backup contains duplicate archive paths")
            for name in names:
                _safe_archive_path(name)
            if _BACKUP_MANIFEST_NAME not in names:
                raise ConfigurationError("workflow backup manifest is missing")
            manifest = WorkflowBackupManifest.model_validate_json(
                archive.read(_BACKUP_MANIFEST_NAME)
            )
            expected = {item.archive_path for item in manifest.files}
            if set(names) != expected | {_BACKUP_MANIFEST_NAME}:
                raise ConfigurationError("workflow backup inventory differs from its manifest")
            for item in manifest.files:
                info = archive.getinfo(item.archive_path)
                if info.file_size != item.size_bytes:
                    raise ConfigurationError(f"workflow backup size mismatch: {item.archive_path}")
                digest = hashlib.sha256()
                with archive.open(info, "r") as stream:
                    while block := stream.read(1024 * 1024):
                        digest.update(block)
                if digest.hexdigest() != item.sha256:
                    raise ConfigurationError(
                        f"workflow backup checksum mismatch: {item.archive_path}"
                    )
    except (OSError, zipfile.BadZipFile, KeyError, ValueError) as exc:
        if isinstance(exc, ConfigurationError):
            raise
        raise ConfigurationError(f"workflow backup is unreadable: {source}") from exc
    identity_payload = _backup_identity_payload(
        workflow_id=manifest.workflow_id,
        original_workspace=manifest.original_workspace,
        topoforge_version=manifest.topoforge_version,
        files=manifest.files,
    )
    if sha256_bytes(_canonical_json(identity_payload)) != manifest.backup_id:
        raise ConfigurationError("workflow backup identity does not match its manifest")
    return manifest


def _remap_path(
    value: Path,
    *,
    original_root: Path,
    restored_root: Path,
    external_map: dict[Path, Path],
) -> Path:
    resolved = value.expanduser().resolve()
    if resolved == original_root or original_root in resolved.parents:
        return restored_root / resolved.relative_to(original_root)
    return external_map.get(resolved, value)


def restore_workflow_backup(
    archive_path: Path,
    workspace_dir: Path,
) -> WorkflowRestoreResult:
    """Atomically restore, remap launch paths, and strictly reopen a backup."""
    from topoforge.workflow.ux import (
        inspect_workflow_workspace,
        read_workflow_launch_config,
        write_workflow_launch_config,
        write_workflow_report,
    )

    source = archive_path.expanduser().resolve()
    destination = workspace_dir.expanduser().resolve()
    if destination.exists():
        raise ConfigurationError(f"restore destination already exists: {destination}")
    manifest = verify_workflow_backup(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.restore-", dir=destination.parent))
    external_map: dict[Path, Path] = {}
    external_directory: Path | None = None
    try:
        with zipfile.ZipFile(source, mode="r") as archive:
            for item in manifest.files:
                archive_name = _safe_archive_path(item.archive_path)
                if item.kind == "workspace":
                    relative = Path(*archive_name.parts[1:])
                    target = staging / relative
                else:
                    relative = Path(*archive_name.parts[1:])
                    target = staging / "backup-external" / relative
                    external_map[Path(item.source_path).expanduser().resolve()] = (
                        destination / "backup-external" / relative
                    )
                    external_directory = destination / "backup-external"
                resolved_target = target.resolve()
                if resolved_target != staging and staging not in resolved_target.parents:
                    raise ConfigurationError(
                        f"workflow backup extraction escapes destination: {item.archive_path}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with (
                    archive.open(item.archive_path, "r") as input_stream,
                    target.open("wb") as output_stream,
                ):
                    while block := input_stream.read(1024 * 1024):
                        output_stream.write(block)
                        digest.update(block)
                if digest.hexdigest() != item.sha256:
                    raise ConfigurationError(
                        f"restored workflow checksum mismatch: {item.archive_path}"
                    )

        launch_path = staging / "workflow-launch.yaml"
        launch = read_workflow_launch_config(launch_path)
        original_root = manifest.original_workspace.expanduser().resolve()
        build = launch.build.model_copy(
            update={
                "dem_path": _remap_path(
                    launch.build.dem_path,
                    original_root=original_root,
                    restored_root=destination,
                    external_map=external_map,
                ),
                "output_dir": destination,
                "source_acquisition_manifest": (
                    None
                    if launch.build.source_acquisition_manifest is None
                    else _remap_path(
                        launch.build.source_acquisition_manifest,
                        original_root=original_root,
                        restored_root=destination,
                        external_map=external_map,
                    )
                ),
            }
        )
        global_source = launch.global_source
        if global_source is not None:
            global_source = global_source.model_copy(
                update={
                    "cache_dir": _remap_path(
                        global_source.cache_dir,
                        original_root=original_root,
                        restored_root=destination,
                        external_map=external_map,
                    )
                }
            )
        restored_launch = launch.model_copy(
            update={
                "workspace_dir": destination,
                "build": build,
                "global_source": global_source,
                "overlay": (
                    None
                    if launch.overlay is None
                    else launch.overlay.model_copy(
                        update={
                            "sources": tuple(
                                source_config.model_copy(
                                    update={
                                        "path": (
                                            None
                                            if source_config.path is None
                                            else _remap_path(
                                                source_config.path,
                                                original_root=original_root,
                                                restored_root=destination,
                                                external_map=external_map,
                                            )
                                        )
                                    }
                                )
                                for source_config in launch.overlay.sources
                            )
                        }
                    )
                ),
                "slicer_settings": tuple(
                    _remap_path(
                        path,
                        original_root=original_root,
                        restored_root=destination,
                        external_map=external_map,
                    )
                    for path in launch.slicer_settings
                ),
                "slicer_filaments": tuple(
                    _remap_path(
                        path,
                        original_root=original_root,
                        restored_root=destination,
                        external_map=external_map,
                    )
                    for path in launch.slicer_filaments
                ),
            }
        )
        write_workflow_launch_config(restored_launch, launch_path)
        write_json(
            staging / "workflow-restore.json",
            {
                "schema_version": _RESTORE_SCHEMA_VERSION,
                "backup_id": manifest.backup_id,
                "archive_path": str(source),
                "archive_sha256": sha256_file(source),
                "original_workspace": str(original_root),
                "restored_workspace": str(destination),
                "external_directory": (
                    str(external_directory) if external_directory is not None else None
                ),
            },
        )
        inspect_workflow_workspace(staging)
        staging.replace(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    summary = inspect_workflow_workspace(destination)
    summary_path = destination / "workflow-summary.json"
    report_path = destination / "workflow-report.html"
    summary = summary.model_copy(
        update={
            "artifacts": {
                **summary.artifacts,
                "workflow_summary": str(summary_path),
                "workflow_report": str(report_path),
                "workflow_restore": str(destination / "workflow-restore.json"),
            }
        }
    )
    write_json(summary_path, summary.model_dump(mode="json"))
    write_workflow_report(report_path, summary)
    return WorkflowRestoreResult(
        backup_id=manifest.backup_id,
        archive_path=source,
        archive_sha256=sha256_file(source),
        workspace=destination,
        external_directory=external_directory,
        workflow_id=summary.workflow_id,
        required_checks_passed=True,
    )
