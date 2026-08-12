"""Disk planning, reviewed cleanup, and verified workflow backup contracts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO, Literal, NamedTuple
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field

from topoforge import __version__
from topoforge.exceptions import ConfigurationError
from topoforge.platforms import path_is_link_like, stat_result_is_link_like
from topoforge.util import preflight_zip_central_directory, sha256_bytes
from topoforge.workflow.local import LocalWorkflowManifest

if TYPE_CHECKING:
    from topoforge.workflow.ux import WorkflowLaunchConfig, WorkflowRunSummary

_STORAGE_SCHEMA_VERSION = "topoforge-workflow-storage-v1"
_CLEANUP_SCHEMA_VERSION = "topoforge-workflow-cleanup-v3"
_BACKUP_SCHEMA_VERSION = "topoforge-workflow-backup-v1"
_RESTORE_SCHEMA_VERSION = "topoforge-workflow-restore-v1"
_RESTORE_EVIDENCE_SCHEMA_VERSION = "topoforge-workflow-restore-evidence-v2"
_BACKUP_MANIFEST_NAME = "topoforge-backup-manifest.json"

# These are safety ceilings, not in-memory allocation targets. Backup payloads are
# always hashed and copied in chunks. The limits intentionally exceed the project's
# configured manufacturing ceilings while bounding hostile ZIP metadata.
_MAX_BACKUP_ARCHIVE_BYTES = 64 * 1024**3
_MAX_BACKUP_MEMBER_COUNT = 50_000
_MAX_BACKUP_MEMBER_NAME_BYTES = 1_024
_MAX_WINDOWS_COMPONENT_UTF16_UNITS = 255
_MAX_BACKUP_MEMBER_BYTES = 32 * 1024**3
_MAX_BACKUP_CENTRAL_DIRECTORY_BYTES = 32 * 1024**2


# Lazy adapters avoid importing topoforge.web.__init__ while topoforge.workflow is
# still initializing. The implementation remains centralized in web.security until
# these generic filesystem primitives are moved to topoforge.util.
def create_owned_directory(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
    exist_ok: bool = False,
) -> None:
    from topoforge.web.security import create_owned_directory as implementation

    implementation(
        path,
        root=root,
        root_identity=root_identity,
        context=context,
        exist_ok=exist_ok,
    )


def ensure_real_directory_tree(path: Path, *, context: str) -> tuple[int, int]:
    """Create and identity-bind an absolute directory tree without following links."""
    from topoforge.web.security import ensure_real_directory_tree as implementation

    return implementation(path, context=context)


def owned_entry_identity(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    directory: bool,
    context: str,
) -> tuple[int, int] | None:
    """Return one exact owned entry identity, or ``None`` only when it is absent."""
    from topoforge.web.security import owned_entry_identity as implementation

    return implementation(
        path,
        root=root,
        root_identity=root_identity,
        directory=directory,
        context=context,
    )


def _owned_move_endpoint(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    expected_identity: tuple[int, int],
    directory: bool,
    context: str,
) -> Literal["match", "missing", "other", "unknown"]:
    """Classify one native-move endpoint without following its lexical path."""
    try:
        observed = owned_entry_identity(
            path,
            root=root,
            root_identity=root_identity,
            directory=directory,
            context=context,
        )
    except (OSError, ValueError):
        return "unknown"
    if observed is None:
        return "missing"
    return "match" if observed == expected_identity else "other"


def move_owned_path(
    source: Path,
    destination: Path,
    *,
    source_root: Path,
    source_root_identity: tuple[int, int],
    destination_root: Path,
    destination_root_identity: tuple[int, int],
    expected_identity: tuple[int, int],
    directory: bool,
    context: str,
) -> None:
    from topoforge.web.security import move_owned_path as implementation

    implementation(
        source,
        destination,
        source_root=source_root,
        source_root_identity=source_root_identity,
        destination_root=destination_root,
        destination_root_identity=destination_root_identity,
        expected_identity=expected_identity,
        directory=directory,
        context=context,
    )


def remove_owned_path(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    expected_identity: tuple[int, int],
    directory: bool,
    context: str,
    missing_ok: bool = False,
) -> None:
    from topoforge.web.security import remove_owned_path as implementation

    implementation(
        path,
        root=root,
        root_identity=root_identity,
        expected_identity=expected_identity,
        directory=directory,
        context=context,
        missing_ok=missing_ok,
    )


def atomic_write_owned_regular_bytes(
    path: Path,
    payload: bytes,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
    replace: bool = True,
) -> None:
    from topoforge.web.security import atomic_write_owned_regular_bytes as implementation

    implementation(
        path,
        payload,
        root=root,
        root_identity=root_identity,
        context=context,
        replace=replace,
    )


@contextmanager
def open_owned_regular_binary(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
    expected_identity: tuple[int, int] | None = None,
) -> Iterator[BinaryIO]:
    from topoforge.web.security import open_owned_regular_binary as implementation

    with implementation(
        path,
        root=root,
        root_identity=root_identity,
        context=context,
        expected_identity=expected_identity,
    ) as stream:
        yield stream


@contextmanager
def open_exclusive_owned_regular_binary(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
) -> Iterator[BinaryIO]:
    from topoforge.web.security import (
        open_exclusive_owned_regular_binary as implementation,
    )

    with implementation(
        path,
        root=root,
        root_identity=root_identity,
        context=context,
    ) as stream:
        yield stream


_MAX_BACKUP_EXPANDED_BYTES = 128 * 1024**3
_MAX_BACKUP_COMPRESSION_RATIO = 200.0
_MAX_BACKUP_MANIFEST_BYTES = 16 * 1024**2
_ZIP_READ_CHUNK_BYTES = 1024 * 1024


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
    entry_count: int = Field(ge=1)
    inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    modified_time_ns: int = Field(ge=0)
    reason: str


class WorkflowCleanupPlan(BaseModel):
    """Reviewable cleanup plan that preserves every current manifest stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _CLEANUP_SCHEMA_VERSION
    plan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
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


class WorkflowRestoreEvidence(BaseModel):
    """Checksum-bound authority for verifying one relocated restored workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _RESTORE_EVIDENCE_SCHEMA_VERSION
    backup_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_path: Path
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_workspace: Path
    restored_workspace: Path
    external_directory: Path | None = None
    workflow_id: str
    original_launch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    restored_launch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_status_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _BackupSource(NamedTuple):
    record: WorkflowBackupFile
    path: Path
    root: Path
    root_identity: tuple[int, int]
    identity: tuple[int, int]


class VerifiedWorkflowBackup(NamedTuple):
    """One private pinned snapshot available only inside its verification context.

    ``stream`` is owned by the context manager, is never the caller's original stream,
    and must not be retained or closed by callers after the context exits.
    ``source_stream`` is the caller-owned pinned source and is valid only during the
    same context.
    """

    source: Path
    source_stream: BinaryIO
    stream: BinaryIO
    archive: zipfile.ZipFile
    manifest: WorkflowBackupManifest
    sha256: str
    size_bytes: int
    source_identity: tuple[int, int]


class _OwnedAtomicWrite(NamedTuple):
    """One publication bound to the parent identity used for its write."""

    path: Path
    root: Path
    root_identity: tuple[int, int]


_ACTIVE_VERIFIED_ARCHIVE: ContextVar[VerifiedWorkflowBackup | None] = ContextVar(
    "topoforge_active_verified_workflow_archive",
    default=None,
)


def _identity(result: os.stat_result) -> tuple[int, int]:
    return result.st_dev, result.st_ino


def _capture_directory_identity(path: Path, *, context: str) -> tuple[int, int]:
    try:
        result = path.lstat()
    except OSError as exc:
        raise ConfigurationError(f"{context} is unavailable: {path}") from exc
    if stat_result_is_link_like(result) or not stat.S_ISDIR(result.st_mode):
        raise ConfigurationError(f"{context} must be a real directory: {path}")
    return _identity(result)


def _require_directory_identity(
    path: Path,
    expected_identity: tuple[int, int],
    *,
    context: str,
) -> None:
    observed = _capture_directory_identity(path, context=context)
    if observed != expected_identity:
        raise ConfigurationError(f"{context} changed during the operation: {path}")


def _ensure_owned_directory_tree(path: Path, *, context: str) -> tuple[Path, tuple[int, int]]:
    """Create a directory chain relative to its existing filesystem anchor."""
    candidate = Path(os.path.abspath(path.expanduser()))
    if not candidate.anchor:
        raise ConfigurationError(f"{context} must be an absolute path: {candidate}")
    try:
        identity = ensure_real_directory_tree(candidate, context=context)
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f"{context} contains an unsafe or changed directory: {candidate}"
        ) from exc
    return candidate, identity


def _ensure_owned_relative_parent(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
) -> None:
    """Create every missing parent below one identity-bound root."""
    candidate = Path(os.path.abspath(path.expanduser()))
    try:
        relative_parent = candidate.parent.relative_to(root)
    except ValueError as exc:
        raise ConfigurationError(f"{context} escapes its trusted root: {candidate}") from exc
    current = root
    for part in relative_parent.parts:
        current /= part
        try:
            create_owned_directory(
                current,
                root=root,
                root_identity=root_identity,
                context=context,
                exist_ok=True,
            )
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"{context} parent is unsafe or changed: {current}") from exc


def _remove_owned_tree(
    tree: Path,
    *,
    parent_root: Path,
    parent_root_identity: tuple[int, int],
    expected_tree_identity: tuple[int, int],
    context: str,
) -> None:
    """Remove one exact tree without following a path or parent-directory swap."""

    def remove_children(
        directory: Path,
        directory_identity: tuple[int, int],
    ) -> None:
        _require_directory_identity(
            directory,
            directory_identity,
            context=context,
        )
        try:
            with os.scandir(directory) as entries:
                child_paths = sorted(
                    (Path(entry.path) for entry in entries),
                    key=lambda item: item.name,
                )
                children = [(child, child.lstat()) for child in child_paths]
        except OSError as exc:
            raise ConfigurationError(f"{context} is unreadable: {directory}") from exc
        for child, child_stat in children:
            if stat_result_is_link_like(child_stat):
                raise ConfigurationError(f"{context} contains a link-like entry: {child}")
            child_identity = _identity(child_stat)
            if stat.S_ISDIR(child_stat.st_mode):
                remove_children(child, child_identity)
                try:
                    remove_owned_path(
                        child,
                        root=tree,
                        root_identity=expected_tree_identity,
                        expected_identity=child_identity,
                        directory=True,
                        context=context,
                    )
                except (OSError, ValueError) as exc:
                    raise ConfigurationError(
                        f"{context} directory changed before removal: {child}"
                    ) from exc
            elif stat.S_ISREG(child_stat.st_mode) and child_stat.st_nlink == 1:
                try:
                    remove_owned_path(
                        child,
                        root=tree,
                        root_identity=expected_tree_identity,
                        expected_identity=child_identity,
                        directory=False,
                        context=context,
                    )
                except (OSError, ValueError) as exc:
                    raise ConfigurationError(
                        f"{context} file changed before removal: {child}"
                    ) from exc
            else:
                raise ConfigurationError(
                    f"{context} contains a special or hard-linked entry: {child}"
                )
        _require_directory_identity(
            directory,
            directory_identity,
            context=context,
        )

    remove_children(tree, expected_tree_identity)
    try:
        remove_owned_path(
            tree,
            root=parent_root,
            root_identity=parent_root_identity,
            expected_identity=expected_tree_identity,
            directory=True,
            context=context,
        )
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"{context} root changed before removal: {tree}") from exc


def _owned_file_sha256(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    expected_identity: tuple[int, int] | None = None,
    context: str,
) -> tuple[int, str, tuple[int, int]]:
    digest = hashlib.sha256()
    try:
        with open_owned_regular_binary(
            path,
            root=root,
            root_identity=root_identity,
            expected_identity=expected_identity,
            context=context,
        ) as stream:
            before = os.fstat(stream.fileno())
            while block := stream.read(_ZIP_READ_CHUNK_BYTES):
                digest.update(block)
            after = os.fstat(stream.fileno())
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"{context} is unsafe or unreadable: {path}") from exc
    if _stable_stat_fields(after) != _stable_stat_fields(before):
        raise ConfigurationError(f"{context} changed while it was read: {path}")
    return before.st_size, digest.hexdigest(), _identity(before)


def _read_owned_file_bytes(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
    max_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    observed = 0
    try:
        with open_owned_regular_binary(
            path,
            root=root,
            root_identity=root_identity,
            context=context,
        ) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > max_bytes:
                raise ConfigurationError(f"{context} exceeds the {max_bytes}-byte safety limit")
            while observed <= max_bytes:
                block = stream.read(min(_ZIP_READ_CHUNK_BYTES, max_bytes + 1 - observed))
                if not block:
                    break
                chunks.append(block)
                observed += len(block)
                if observed > max_bytes:
                    raise ConfigurationError(f"{context} exceeds the {max_bytes}-byte safety limit")
            after = os.fstat(stream.fileno())
    except ConfigurationError:
        raise
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"{context} is unsafe or unreadable: {path}") from exc
    if _stable_stat_fields(after) != _stable_stat_fields(before) or observed != before.st_size:
        raise ConfigurationError(f"{context} changed while it was read: {path}")
    return b"".join(chunks)


def _directory_size(root: Path) -> int:
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return 0
    if stat_result_is_link_like(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        return root_stat.st_size
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                entry_stat = entry.stat(follow_symlinks=False)
                if stat_result_is_link_like(entry_stat) or not stat.S_ISDIR(entry_stat.st_mode):
                    total += entry_stat.st_size
                else:
                    pending.append(Path(entry.path))
    return total


def _atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    root: Path | None = None,
    root_identity: tuple[int, int] | None = None,
) -> _OwnedAtomicWrite:
    """Atomically replace one regular file through an identity-bound parent."""
    destination = Path(os.path.abspath(path.expanduser()))
    if (root is None) != (root_identity is None):
        raise ConfigurationError("output root and root identity must be provided together")
    if root is None:
        parent, parent_identity = _ensure_owned_directory_tree(
            destination.parent,
            context=f"output parent for {destination.name}",
        )
    else:
        parent = Path(os.path.abspath(root.expanduser()))
        assert root_identity is not None
        parent_identity = root_identity
    try:
        atomic_write_owned_regular_bytes(
            destination,
            payload,
            root=parent,
            root_identity=parent_identity,
            context=f"output file {destination}",
            replace=True,
        )
    except (OSError, ValueError) as exc:
        if getattr(exc, "committed", False):
            raise ConfigurationError(
                "output publication committed but its durability is uncertain; "
                f"reopen and verify before retrying: {destination}"
            ) from exc
        raise ConfigurationError(f"output file could not be safely written: {destination}") from exc
    return _OwnedAtomicWrite(destination, parent, parent_identity)


def _reopen_atomic_write_bytes(
    written: _OwnedAtomicWrite,
    *,
    max_bytes: int,
) -> bytes:
    """Read one just-published file through its original parent identity."""
    from topoforge.web.security import read_owned_regular_bytes

    try:
        return read_owned_regular_bytes(
            written.path,
            root=written.root,
            root_identity=written.root_identity,
            context=f"workflow maintenance strict reopen {written.path}",
            max_bytes=max_bytes,
        )
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f"workflow output parent or file changed before strict reopen: {written.path}"
        ) from exc


def _atomic_write_json(
    path: Path,
    value: BaseModel | dict[str, object],
    *,
    root: Path | None = None,
    root_identity: tuple[int, int] | None = None,
) -> _OwnedAtomicWrite:
    return _atomic_write_bytes(
        path,
        _canonical_json(value),
        root=root,
        root_identity=root_identity,
    )


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
        lexical = Path(os.path.abspath(value.expanduser()))
        try:
            if path_is_link_like(lexical):
                raise ConfigurationError(
                    f"referenced external workflow file is link-like: {lexical}; "
                    "use the original regular file"
                )
        except FileNotFoundError:
            pass
        resolved = lexical.resolve()
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
    *,
    root: Path | None = None,
    root_identity: tuple[int, int] | None = None,
) -> Path:
    """Write and strictly reopen a storage estimate JSON record."""
    destination = Path(
        os.path.abspath(
            (
                path if path is not None else estimate.workspace / "workflow-storage.json"
            ).expanduser()
        )
    )
    written = _atomic_write_json(
        destination,
        estimate,
        root=root,
        root_identity=root_identity,
    )
    reopened = WorkflowStorageEstimate.model_validate_json(
        _reopen_atomic_write_bytes(written, max_bytes=_MAX_BACKUP_MANIFEST_BYTES)
    )
    if reopened != estimate:
        raise ConfigurationError("workflow storage estimate failed strict JSON reopen")
    return destination


def _cleanup_kind(path: Path) -> Literal["directory", "file", "symlink"]:
    mode = path.lstat().st_mode
    if path_is_link_like(path):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    return "file"


def _workspace_root(workspace_dir: Path) -> Path:
    expanded = workspace_dir.expanduser()
    lexical = Path(os.path.abspath(expanded))
    try:
        if path_is_link_like(lexical):
            raise ConfigurationError(
                f"workflow workspace root is link-like: {lexical}; use the real directory"
            )
    except FileNotFoundError as exc:
        raise ConfigurationError(f"workflow workspace is missing: {lexical}") from exc
    return lexical.resolve()


def _lexical_workspace_path(
    root: Path,
    relative_path: str | Path,
    *,
    context: str = "workflow path",
    require_exists: bool = True,
) -> Path:
    """Validate lexical containment and components before resolving a workspace path."""
    value = Path(relative_path)
    if value.is_absolute() or ".." in value.parts:
        raise ConfigurationError(f"{context} must be workspace-relative without '..': {value}")
    candidate = Path(os.path.abspath(root / value))
    if candidate == root or root not in candidate.parents:
        raise ConfigurationError(f"{context} escapes workflow workspace: {candidate}")
    current = root
    missing = False
    for part in value.parts:
        current /= part
        try:
            if path_is_link_like(current):
                raise ConfigurationError(
                    f"{context} contains a link-like component: {current}; "
                    "replace it with the original file or directory"
                )
        except FileNotFoundError:
            missing = True
            if require_exists:
                raise ConfigurationError(f"{context} is missing: {current}") from None
            break
    resolved = candidate.resolve(strict=require_exists)
    if resolved == root or root not in resolved.parents:
        raise ConfigurationError(f"{context} escapes workflow workspace: {resolved}")
    if missing and require_exists:
        raise ConfigurationError(f"{context} is missing: {candidate}")
    return resolved


def _stable_stat_fields(result: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        result.st_mode,
        result.st_dev,
        result.st_ino,
        result.st_nlink,
        result.st_size,
        result.st_mtime_ns,
        result.st_ctime_ns,
    )


def _tree_inventory(root: Path, *, context: str) -> tuple[int, int, str]:
    """Hash one no-follow regular-file tree and reject hard links or special files."""
    records: list[dict[str, object]] = []
    total_size = 0

    def visit(path: Path, relative: str) -> None:
        nonlocal total_size
        try:
            before = path.lstat()
        except FileNotFoundError:
            raise ConfigurationError(f"{context} changed while it was inspected: {path}") from None
        if stat_result_is_link_like(before):
            raise ConfigurationError(
                f"{context} contains a link-like entry: {path}; remove it before cleanup"
            )
        if stat.S_ISDIR(before.st_mode):
            try:
                with os.scandir(path) as iterator:
                    children = sorted(
                        (Path(entry.path) for entry in iterator), key=lambda item: item.name
                    )
            except OSError as exc:
                raise ConfigurationError(f"{context} directory is unreadable: {path}") from exc
            for child in children:
                child_relative = child.name if relative == "." else f"{relative}/{child.name}"
                visit(child, child_relative)
            try:
                after = path.lstat()
            except FileNotFoundError:
                raise ConfigurationError(
                    f"{context} changed while it was inspected: {path}"
                ) from None
            if stat_result_is_link_like(after) or _stable_stat_fields(after) != _stable_stat_fields(
                before
            ):
                raise ConfigurationError(f"{context} changed while it was inspected: {path}")
            records.append(
                {
                    # Directory st_size is allocation metadata, not content size. On XFS a
                    # cross-parent rename can change a shortform directory's encoded '..'
                    # entry and therefore st_size without changing any tree content.
                    "path": relative,
                    "kind": "directory",
                    "mode": before.st_mode,
                    "device": before.st_dev,
                    "inode": before.st_ino,
                    "link_count": before.st_nlink,
                    "size_bytes": 0,
                    "modified_time_ns": before.st_mtime_ns,
                }
            )
            return
        if not stat.S_ISREG(before.st_mode):
            raise ConfigurationError(f"{context} contains a non-regular filesystem entry: {path}")
        if before.st_nlink != 1:
            raise ConfigurationError(
                f"{context} contains a hard-linked file: {path}; copy it to a unique file first"
            )
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if _stable_stat_fields(opened) != _stable_stat_fields(before):
                    raise ConfigurationError(f"{context} changed while it was opened: {path}")
                while block := stream.read(1024 * 1024):
                    digest.update(block)
                finished = os.fstat(stream.fileno())
        except OSError as exc:
            raise ConfigurationError(f"{context} file is unreadable: {path}") from exc
        try:
            after = path.lstat()
        except FileNotFoundError:
            raise ConfigurationError(f"{context} changed while it was inspected: {path}") from None
        if (
            stat_result_is_link_like(after)
            or _stable_stat_fields(finished) != _stable_stat_fields(before)
            or _stable_stat_fields(after) != _stable_stat_fields(before)
        ):
            raise ConfigurationError(f"{context} changed while it was inspected: {path}")
        total_size += before.st_size
        records.append(
            {
                "path": relative,
                "kind": "file",
                "mode": before.st_mode,
                "device": before.st_dev,
                "inode": before.st_ino,
                "link_count": before.st_nlink,
                "size_bytes": before.st_size,
                "modified_time_ns": before.st_mtime_ns,
                "sha256": digest.hexdigest(),
            }
        )

    visit(root, ".")
    ordered = sorted(records, key=lambda item: str(item["path"]))
    inventory_sha256 = sha256_bytes(_canonical_json({"entries": ordered}))
    return total_size, len(ordered), inventory_sha256


def _reject_link_like_tree(root: Path, *, context: str) -> None:
    pending = [root]
    while pending:
        path = pending.pop()
        path_stat = path.lstat()
        if stat_result_is_link_like(path_stat):
            raise ConfigurationError(
                f"{context} contains a link-like entry: {path}; remove it before cleanup"
            )
        if stat.S_ISREG(path_stat.st_mode):
            if path_stat.st_nlink != 1:
                raise ConfigurationError(
                    f"{context} contains a hard-linked file: {path}; copy it to a unique file first"
                )
            continue
        if not stat.S_ISDIR(path_stat.st_mode):
            raise ConfigurationError(f"{context} contains a non-regular filesystem entry: {path}")
        with os.scandir(path) as entries:
            pending.extend(Path(entry.path) for entry in entries)


def _cleanup_candidate(
    root: Path,
    path: Path,
    *,
    reason: str,
) -> WorkflowCleanupCandidate:
    relative = path.relative_to(root).as_posix()
    resolved = _lexical_workspace_path(
        root,
        relative,
        context=f"cleanup candidate {relative}",
    )
    size_bytes, entry_count, inventory_sha256 = _tree_inventory(
        resolved,
        context=f"cleanup candidate {relative}",
    )
    path_stat = resolved.lstat()
    return WorkflowCleanupCandidate(
        path=relative,
        kind=_cleanup_kind(resolved),
        size_bytes=size_bytes,
        entry_count=entry_count,
        inventory_sha256=inventory_sha256,
        device=path_stat.st_dev,
        inode=path_stat.st_ino,
        modified_time_ns=path_stat.st_mtime_ns,
        reason=reason,
    )


def _cleanup_plan_id(
    *,
    workflow_id: str,
    workspace: Path,
    candidates: tuple[WorkflowCleanupCandidate, ...],
) -> str:
    payload = {
        "schema_version": _CLEANUP_SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "workspace": str(workspace),
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
    }
    return sha256_bytes(_canonical_json(payload))


def plan_workflow_cleanup(workspace_dir: Path) -> WorkflowCleanupPlan:
    """List only stage identities not referenced by the completed manifest."""
    from topoforge.workflow.ux import inspect_workflow_workspace

    root = _workspace_root(workspace_dir)
    summary = inspect_workflow_workspace(root)
    manifest = LocalWorkflowManifest.model_validate_json(
        (root / "workflow-manifest.json").read_text(encoding="utf-8")
    )
    referenced = {
        _lexical_workspace_path(
            root,
            record.output_path,
            context=f"workflow {record.name.value} output path",
        )
        for record in manifest.stages
    }
    candidates: list[WorkflowCleanupCandidate] = []
    stages_root = _lexical_workspace_path(
        root,
        "stages",
        context="workflow stages root",
    )
    if stages_root.is_dir():
        for family in sorted(stages_root.iterdir(), key=lambda path: path.name):
            family = _lexical_workspace_path(
                root,
                family.relative_to(root),
                context="workflow stage family",
            )
            if not family.is_dir():
                if family not in referenced:
                    candidates.append(
                        _cleanup_candidate(
                            root,
                            family,
                            reason="unexpected unreferenced entry under stages",
                        )
                    )
                continue
            for identity_path in sorted(family.iterdir(), key=lambda path: path.name):
                identity_path = _lexical_workspace_path(
                    root,
                    identity_path.relative_to(root),
                    context="workflow stage identity",
                )
                if identity_path in referenced:
                    _reject_link_like_tree(
                        identity_path,
                        context="referenced workflow stage identity",
                    )
                    continue
                candidates.append(
                    _cleanup_candidate(
                        root,
                        identity_path,
                        reason="stage identity is not referenced by workflow-manifest.json",
                    )
                )
    candidate_tuple = tuple(candidates)
    plan_id = _cleanup_plan_id(
        workflow_id=summary.workflow_id,
        workspace=root,
        candidates=candidate_tuple,
    )
    quoted_root = shlex.quote(str(root))
    quoted_id = shlex.quote(summary.workflow_id)
    quoted_plan_id = shlex.quote(plan_id)
    return WorkflowCleanupPlan(
        plan_id=plan_id,
        workflow_id=summary.workflow_id,
        workspace=root,
        current_workspace_bytes=_directory_size(root),
        reclaimable_bytes=sum(item.size_bytes for item in candidates),
        candidates=candidate_tuple,
        review_command=f"topoforge cleanup {quoted_root}",
        apply_command=(
            f"topoforge cleanup {quoted_root} --apply --confirm-workflow-id {quoted_id} "
            f"--confirm-plan-id {quoted_plan_id}"
        ),
        required_checks_passed=True,
    )


def apply_workflow_cleanup(
    workspace_dir: Path,
    *,
    confirm_workflow_id: str,
    confirm_plan_id: str | None = None,
) -> WorkflowCleanupResult:
    """Apply only the exact cleanup plan confirmed after review."""
    from topoforge.workflow.ux import inspect_workflow_workspace

    plan = plan_workflow_cleanup(workspace_dir)
    if confirm_workflow_id != plan.workflow_id:
        raise ConfigurationError(
            "cleanup confirmation does not match the current workflow id; rerun the review command"
        )
    if confirm_plan_id is None:
        raise ConfigurationError(
            "cleanup now requires --confirm-plan-id from the latest review command; "
            "rerun cleanup review before applying"
        )
    if confirm_plan_id != plan.plan_id:
        raise ConfigurationError(
            "cleanup plan changed after review or its confirmation is incorrect; "
            "rerun cleanup review"
        )
    workspace_identity = _capture_directory_identity(
        plan.workspace,
        context="workflow cleanup workspace",
    )
    quarantine = plan.workspace / (f".topoforge-cleanup-{plan.plan_id[:12]}-{uuid4().hex}")
    try:
        create_owned_directory(
            quarantine,
            root=plan.workspace,
            root_identity=workspace_identity,
            context="workflow cleanup quarantine",
        )
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            "workflow cleanup could not create an identity-bound quarantine"
        ) from exc
    quarantine_identity = _capture_directory_identity(
        quarantine,
        context="workflow cleanup quarantine",
    )
    checked: list[tuple[WorkflowCleanupCandidate, Path]] = []
    moved: list[tuple[WorkflowCleanupCandidate, Path, Path]] = []

    def move_location(
        candidate: WorkflowCleanupCandidate,
        path: Path,
        *,
        root: Path,
        root_identity: tuple[int, int],
    ) -> Literal["match", "missing", "unknown"]:
        """Classify a failed move endpoint without treating uncertainty as absence."""
        identity_location = _owned_move_endpoint(
            path,
            root=root,
            root_identity=root_identity,
            expected_identity=(candidate.device, candidate.inode),
            directory=candidate.kind == "directory",
            context="workflow cleanup failed-move reconciliation",
        )
        if identity_location == "missing":
            return "missing"
        if identity_location != "match":
            return "unknown"
        try:
            current = _cleanup_candidate(root, path, reason=candidate.reason)
        except (OSError, ValueError, ConfigurationError):
            return "unknown"
        return "match" if current == candidate else "unknown"

    try:
        for candidate in plan.candidates:
            _require_directory_identity(
                plan.workspace,
                workspace_identity,
                context="workflow cleanup workspace",
            )
            path = _lexical_workspace_path(
                plan.workspace,
                candidate.path,
                context=f"cleanup candidate {candidate.path}",
            )
            current = _cleanup_candidate(
                plan.workspace,
                path,
                reason=candidate.reason,
            )
            if current != candidate:
                raise ConfigurationError(
                    f"cleanup candidate changed after review: {candidate.path}; "
                    "rerun cleanup review"
                )
            checked.append((candidate, path))

        for candidate, path in checked:
            current = _cleanup_candidate(
                plan.workspace,
                path,
                reason=candidate.reason,
            )
            if current != candidate:
                raise ConfigurationError(
                    f"cleanup candidate changed during apply: {candidate.path}; "
                    "rerun cleanup review"
                )
            quarantined = quarantine / Path(candidate.path)
            _ensure_owned_relative_parent(
                quarantined,
                root=quarantine,
                root_identity=quarantine_identity,
                context="workflow cleanup quarantine",
            )
            # Register the intent before the native rename. The owned move can
            # commit successfully and then fail a parent-path postcondition; in
            # that case an exception must still drive this candidate through
            # rollback instead of purging an apparently empty quarantine.
            moved.append((candidate, path, quarantined))
            try:
                move_owned_path(
                    path,
                    quarantined,
                    source_root=plan.workspace,
                    source_root_identity=workspace_identity,
                    destination_root=quarantine,
                    destination_root_identity=quarantine_identity,
                    expected_identity=(candidate.device, candidate.inode),
                    directory=candidate.kind == "directory",
                    context=f"cleanup candidate {candidate.path}",
                )
            except (OSError, ValueError) as exc:
                source_location = move_location(
                    candidate,
                    path,
                    root=plan.workspace,
                    root_identity=workspace_identity,
                )
                quarantine_location = move_location(
                    candidate,
                    quarantined,
                    root=quarantine,
                    root_identity=quarantine_identity,
                )
                if source_location == "match" and quarantine_location == "missing":
                    moved.pop()
                if getattr(exc, "committed", False):
                    raise ConfigurationError(
                        "cleanup candidate move committed but its durability is uncertain: "
                        f"{candidate.path}; rollback will be attempted"
                    ) from exc
                raise ConfigurationError(
                    f"cleanup candidate parent or identity changed: {candidate.path}"
                ) from exc
            reopened = _cleanup_candidate(
                quarantine,
                quarantined,
                reason=candidate.reason,
            )
            if reopened != candidate:
                raise ConfigurationError(
                    f"cleanup candidate identity changed while quarantining: {candidate.path}"
                )

        inspect_workflow_workspace(plan.workspace)
        _require_directory_identity(
            plan.workspace,
            workspace_identity,
            context="workflow cleanup workspace",
        )
    except BaseException as failure:
        rollback_failures: list[str] = []
        for candidate, original, quarantined in reversed(moved):
            try:
                _ensure_owned_relative_parent(
                    original,
                    root=plan.workspace,
                    root_identity=workspace_identity,
                    context="workflow cleanup rollback",
                )
                try:
                    move_owned_path(
                        quarantined,
                        original,
                        source_root=quarantine,
                        source_root_identity=quarantine_identity,
                        destination_root=plan.workspace,
                        destination_root_identity=workspace_identity,
                        expected_identity=(candidate.device, candidate.inode),
                        directory=candidate.kind == "directory",
                        context=f"cleanup rollback {candidate.path}",
                    )
                except (OSError, ValueError) as rollback_error:
                    original_location = move_location(
                        candidate,
                        original,
                        root=plan.workspace,
                        root_identity=workspace_identity,
                    )
                    quarantine_location = move_location(
                        candidate,
                        quarantined,
                        root=quarantine,
                        root_identity=quarantine_identity,
                    )
                    if getattr(rollback_error, "committed", False):
                        raise ConfigurationError(
                            "cleanup rollback committed but its durability is uncertain: "
                            f"{candidate.path}"
                        ) from rollback_error
                    if not (original_location == "match" and quarantine_location == "missing"):
                        raise
                restored = _cleanup_candidate(
                    plan.workspace,
                    original,
                    reason=candidate.reason,
                )
                if restored != candidate:
                    raise ConfigurationError(
                        f"cleanup rollback identity mismatch: {candidate.path}"
                    )
            except Exception as exc:
                rollback_failures.append(f"{candidate.path}: {exc}")
        if not rollback_failures:
            try:
                _remove_owned_tree(
                    quarantine,
                    parent_root=plan.workspace,
                    parent_root_identity=workspace_identity,
                    expected_tree_identity=quarantine_identity,
                    context="workflow cleanup quarantine",
                )
            except Exception as exc:
                rollback_failures.append(f"quarantine cleanup: {exc}")
        if rollback_failures:
            raise ConfigurationError(
                "cleanup failed and rollback was incomplete; preserve the quarantine at "
                f"{quarantine}: {'; '.join(rollback_failures)}"
            ) from None
        if isinstance(failure, ConfigurationError):
            raise failure
        if isinstance(failure, Exception):
            raise ConfigurationError(
                "cleanup transaction failed before commit and all candidates were restored; "
                "rerun cleanup review before trying again"
            ) from failure
        raise failure

    try:
        _remove_owned_tree(
            quarantine,
            parent_root=plan.workspace,
            parent_root_identity=workspace_identity,
            expected_tree_identity=quarantine_identity,
            context="workflow cleanup quarantine",
        )
    except Exception as exc:
        raise ConfigurationError(
            f"cleanup candidates were quarantined but could not be purged at {quarantine}; "
            "inspect and remove only that plan-bound quarantine"
        ) from exc

    return WorkflowCleanupResult(
        workflow_id=plan.workflow_id,
        workspace=plan.workspace,
        removed_paths=tuple(candidate.path for candidate, _ in checked),
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
    try:
        encoded_name = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ConfigurationError(f"unsafe workflow backup path: {value!r}") from exc
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or len(encoded_name) > _MAX_BACKUP_MEMBER_NAME_BYTES
    ):
        raise ConfigurationError(f"unsafe workflow backup path: {value!r}")
    if unicodedata.normalize("NFC", value) != value:
        raise ConfigurationError(f"workflow backup path is not canonical Unicode NFC: {value}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or "." in path.parts
        or path.as_posix() != value
        or value.endswith("/")
    ):
        raise ConfigurationError(f"unsafe workflow backup path: {value}")
    windows_reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
        *(f"COM{index}" for index in "¹²³"),
        *(f"LPT{index}" for index in "¹²³"),
    }
    for part in path.parts:
        windows_part = part.rstrip(" .")
        try:
            windows_units = len(part.encode("utf-16-le")) // 2
        except UnicodeEncodeError as exc:
            raise ConfigurationError(
                f"workflow backup path is not portable to Windows: {value}"
            ) from exc
        if (
            not windows_part
            or windows_part != part
            or windows_units > _MAX_WINDOWS_COMPONENT_UTF16_UNITS
            or any(ord(character) < 32 or character in '<>:"|?*' for character in part)
            or windows_part.split(".", 1)[0].upper() in windows_reserved
        ):
            raise ConfigurationError(f"workflow backup path is not portable to Windows: {value}")
    return path


def _archive_aliases(value: str) -> tuple[str, str]:
    path = _safe_archive_path(value)
    unicode_alias = unicodedata.normalize("NFC", value).casefold()
    windows_alias = "/".join(part.rstrip(" .").casefold() for part in path.parts)
    return unicode_alias, windows_alias


def _validate_backup_central_directory(
    archive: zipfile.ZipFile,
    *,
    archive_size_bytes: int,
) -> dict[str, zipfile.ZipInfo]:
    """Validate every member before any member payload is opened."""
    infos = archive.infolist()
    if len(infos) > _MAX_BACKUP_MEMBER_COUNT:
        raise ConfigurationError(
            f"workflow backup has too many members; maximum is {_MAX_BACKUP_MEMBER_COUNT}"
        )
    if archive.comment:
        raise ConfigurationError("workflow backup ZIP comments are not supported")
    exact: dict[str, zipfile.ZipInfo] = {}
    unicode_aliases: dict[str, str] = {}
    windows_aliases: dict[str, str] = {}
    expanded_bytes = 0
    for info in infos:
        name = info.filename
        _safe_archive_path(name)
        if name in exact:
            raise ConfigurationError("workflow backup contains duplicate archive paths")
        unicode_alias, windows_alias = _archive_aliases(name)
        prior_unicode = unicode_aliases.get(unicode_alias)
        if prior_unicode is not None:
            raise ConfigurationError(
                "workflow backup contains Unicode/casefold path aliases: "
                f"{prior_unicode!r} and {name!r}"
            )
        prior_windows = windows_aliases.get(windows_alias)
        if prior_windows is not None:
            raise ConfigurationError(
                f"workflow backup contains Windows path aliases: {prior_windows!r} and {name!r}"
            )
        if info.flag_bits & 0x1:
            raise ConfigurationError(f"workflow backup member is encrypted: {name}")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ConfigurationError(f"workflow backup member uses unsupported compression: {name}")
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        if info.is_dir() or file_type not in {0, stat.S_IFREG}:
            raise ConfigurationError(f"workflow backup member is not a regular file: {name}")
        if (
            info.file_size < 0
            or info.compress_size < 0
            or info.file_size > _MAX_BACKUP_MEMBER_BYTES
        ):
            raise ConfigurationError(f"workflow backup member exceeds the size limit: {name}")
        expanded_bytes += info.file_size
        if expanded_bytes > _MAX_BACKUP_EXPANDED_BYTES:
            raise ConfigurationError("workflow backup expanded size exceeds the safety limit")
        if (
            info.file_size >= _ZIP_READ_CHUNK_BYTES
            and info.file_size / max(1, info.compress_size) > _MAX_BACKUP_COMPRESSION_RATIO
        ):
            raise ConfigurationError(f"workflow backup member compression ratio is unsafe: {name}")
        if info.header_offset < 0 or info.header_offset >= archive_size_bytes:
            raise ConfigurationError(
                f"workflow backup member has an invalid local-header offset: {name}"
            )
        exact[name] = info
        unicode_aliases[unicode_alias] = name
        windows_aliases[windows_alias] = name

    member_names = set(exact)
    for name in member_names:
        parts = PurePosixPath(name).parts
        for end in range(1, len(parts)):
            prefix = "/".join(parts[:end])
            if prefix in member_names:
                raise ConfigurationError(
                    "workflow backup contains a file/directory prefix collision: "
                    f"{prefix!r} and {name!r}"
                )
    return exact


def _read_zip_member_bounded(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    expected_size: int,
    context: str,
) -> bytes:
    if expected_size < 0 or expected_size > _MAX_BACKUP_MANIFEST_BYTES:
        raise ConfigurationError(f"{context} exceeds its safety limit")
    chunks: list[bytes] = []
    observed = 0
    try:
        with archive.open(info, "r") as stream:
            while observed <= expected_size:
                block = stream.read(min(_ZIP_READ_CHUNK_BYTES, expected_size + 1 - observed))
                if not block:
                    break
                chunks.append(block)
                observed += len(block)
                if observed > expected_size:
                    raise ConfigurationError(f"{context} exceeds its declared size")
            if stream.read(1):
                raise ConfigurationError(f"{context} exceeds its declared size")
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ConfigurationError(f"{context} is unreadable") from exc
    if observed != expected_size:
        raise ConfigurationError(
            f"{context} size differs from the central directory: "
            f"expected {expected_size}, read {observed}"
        )
    return b"".join(chunks)


def _copy_zip_member_bounded(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    output_stream: BinaryIO,
    *,
    expected_size: int,
    expected_sha256: str,
    context: str,
) -> None:
    digest = hashlib.sha256()
    observed = 0
    try:
        with archive.open(info, "r") as input_stream:
            while observed <= expected_size:
                block = input_stream.read(min(_ZIP_READ_CHUNK_BYTES, expected_size + 1 - observed))
                if not block:
                    break
                observed += len(block)
                if observed > expected_size:
                    raise ConfigurationError(f"{context} exceeds its declared size")
                output_stream.write(block)
                digest.update(block)
            if input_stream.read(1):
                raise ConfigurationError(f"{context} exceeds its declared size")
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ConfigurationError(f"{context} is unreadable") from exc
    if observed != expected_size:
        raise ConfigurationError(
            f"{context} size differs from its manifest: expected {expected_size}, read {observed}"
        )
    if digest.hexdigest() != expected_sha256:
        raise ConfigurationError(f"{context} checksum mismatch")


def _hash_zip_member_bounded(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    expected_size: int,
    context: str,
) -> str:
    digest = hashlib.sha256()
    observed = 0
    try:
        with archive.open(info, "r") as stream:
            while observed <= expected_size:
                block = stream.read(min(_ZIP_READ_CHUNK_BYTES, expected_size + 1 - observed))
                if not block:
                    break
                digest.update(block)
                observed += len(block)
                if observed > expected_size:
                    raise ConfigurationError(f"{context} exceeds its declared size")
            if stream.read(1):
                raise ConfigurationError(f"{context} exceeds its declared size")
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ConfigurationError(f"{context} is unreadable") from exc
    if observed != expected_size:
        raise ConfigurationError(
            f"{context} size differs from its manifest: expected {expected_size}, read {observed}"
        )
    return digest.hexdigest()


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (0o100644 & 0xFFFF) << 16
    return info


def _verified_backup_source(path: Path, *, context: str) -> tuple[int, str]:
    """Hash one unique regular file through an identity-bound parent handle."""
    lexical = Path(os.path.abspath(path.expanduser()))
    parent_identity = _capture_directory_identity(
        lexical.parent,
        context=f"{context} parent",
    )
    size_bytes, digest, _ = _owned_file_sha256(
        lexical,
        root=lexical.parent,
        root_identity=parent_identity,
        context=context,
    )
    return size_bytes, digest


def _write_zip_file(archive: zipfile.ZipFile, source: _BackupSource) -> None:
    digest = hashlib.sha256()
    observed = 0
    try:
        with (
            open_owned_regular_binary(
                source.path,
                root=source.root,
                root_identity=source.root_identity,
                expected_identity=source.identity,
                context="workflow backup source",
            ) as input_stream,
            archive.open(_zip_info(source.record.archive_path), "w") as output_stream,
        ):
            before = os.fstat(input_stream.fileno())
            while block := input_stream.read(_ZIP_READ_CHUNK_BYTES):
                output_stream.write(block)
                digest.update(block)
                observed += len(block)
            after = os.fstat(input_stream.fileno())
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f"workflow backup source is unsafe or changed: {source.path}"
        ) from exc
    if (
        _stable_stat_fields(after) != _stable_stat_fields(before)
        or observed != source.record.size_bytes
        or digest.hexdigest() != source.record.sha256
    ):
        raise ConfigurationError(
            f"workflow backup source changed while reading it: {source.path}; retry the backup"
        )


def _workspace_backup_files(
    root: Path,
    root_identity: tuple[int, int],
) -> list[_BackupSource]:
    regular_files: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = Path(entry.path)
                # Windows DirEntry caches WIN32_FIND_DATA for ordinary files;
                # that record has no hard-link count and may expose st_nlink=0.
                # A real lstat supplies the identity/link metadata later bound
                # again by the pinned native handle in _owned_file_sha256().
                entry_stat = path.lstat()
                if stat_result_is_link_like(entry_stat):
                    raise ConfigurationError(
                        f"workflow backups do not follow link-like entries: {path}"
                    )
                if stat.S_ISDIR(entry_stat.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(entry_stat.st_mode):
                    if entry_stat.st_nlink != 1:
                        raise ConfigurationError(
                            f"workflow backup source is hard-linked: {path}; copy it first"
                        )
                    regular_files.append(path)
                else:
                    raise ConfigurationError(
                        f"workflow backups require regular files and directories: {path}"
                    )

    files: list[_BackupSource] = []
    for path in sorted(regular_files, key=lambda item: item.relative_to(root).as_posix()):
        size_bytes, digest, identity = _owned_file_sha256(
            path,
            root=root,
            root_identity=root_identity,
            context="workflow backup source",
        )
        files.append(
            _BackupSource(
                WorkflowBackupFile(
                    archive_path=f"workspace/{path.relative_to(root).as_posix()}",
                    source_path=str(path),
                    kind="workspace",
                    size_bytes=size_bytes,
                    sha256=digest,
                ),
                path,
                root,
                root_identity,
                identity,
            )
        )
    return files


def _external_backup_files(
    config: WorkflowLaunchConfig,
) -> list[_BackupSource]:
    files: list[_BackupSource] = []
    for path in _external_reference_paths(config):
        parent_identity = _capture_directory_identity(
            path.parent,
            context="referenced external workflow file parent",
        )
        size_bytes, digest, identity = _owned_file_sha256(
            path,
            root=path.parent,
            root_identity=parent_identity,
            context="referenced external workflow file",
        )
        path_id = sha256_bytes(str(path).encode("utf-8"))[:12]
        archive_path = f"external/{path_id}-{digest[:12]}-{path.name}"
        files.append(
            _BackupSource(
                WorkflowBackupFile(
                    archive_path=archive_path,
                    source_path=str(path),
                    kind="external",
                    size_bytes=size_bytes,
                    sha256=digest,
                ),
                path,
                path.parent,
                parent_identity,
                identity,
            )
        )
    return files


def _hash_open_archive_stream(stream: BinaryIO) -> tuple[int, str, tuple[int, int]]:
    try:
        stream.seek(0)
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > _MAX_BACKUP_ARCHIVE_BYTES
        ):
            raise ConfigurationError("workflow backup archive is not a unique bounded regular file")
        digest = hashlib.sha256()
        while block := stream.read(_ZIP_READ_CHUNK_BYTES):
            digest.update(block)
        after = os.fstat(stream.fileno())
        stream.seek(0)
    except OSError as exc:
        raise ConfigurationError("workflow backup archive cannot be read safely") from exc
    if _stable_stat_fields(after) != _stable_stat_fields(before):
        raise ConfigurationError("workflow backup archive changed while it was hashed")
    return before.st_size, digest.hexdigest(), _identity(before)


def _verify_backup_members(
    archive: zipfile.ZipFile,
    *,
    archive_size_bytes: int,
) -> WorkflowBackupManifest:
    infos = _validate_backup_central_directory(
        archive,
        archive_size_bytes=archive_size_bytes,
    )
    manifest_info = infos.get(_BACKUP_MANIFEST_NAME)
    if manifest_info is None:
        raise ConfigurationError("workflow backup manifest is missing")
    if manifest_info.file_size > _MAX_BACKUP_MANIFEST_BYTES:
        raise ConfigurationError("workflow backup manifest exceeds its safety limit")
    manifest_payload = _read_zip_member_bounded(
        archive,
        manifest_info,
        expected_size=manifest_info.file_size,
        context="workflow backup manifest",
    )
    try:
        manifest = WorkflowBackupManifest.model_validate_json(manifest_payload)
    except ValueError as exc:
        raise ConfigurationError("workflow backup manifest is invalid") from exc
    if manifest_payload != _canonical_json(manifest):
        raise ConfigurationError("workflow backup manifest is non-canonical")
    if manifest.schema_version != _BACKUP_SCHEMA_VERSION:
        raise ConfigurationError("workflow backup schema version is unsupported")
    if manifest.required_checks_passed is not True:
        raise ConfigurationError("workflow backup required checks are not passed")

    record_paths = [item.archive_path for item in manifest.files]
    if len(record_paths) != len(set(record_paths)) or _BACKUP_MANIFEST_NAME in record_paths:
        raise ConfigurationError("workflow backup manifest contains duplicate archive paths")
    expected = set(record_paths) | {_BACKUP_MANIFEST_NAME}
    if set(infos) != expected:
        raise ConfigurationError("workflow backup inventory differs from its manifest")
    for item in manifest.files:
        archive_path = _safe_archive_path(item.archive_path)
        expected_prefix = item.kind
        if len(archive_path.parts) < 2 or archive_path.parts[0] != expected_prefix:
            raise ConfigurationError(
                f"workflow backup member kind/path mismatch: {item.archive_path}"
            )
        info = infos[item.archive_path]
        if info.file_size != item.size_bytes:
            raise ConfigurationError(f"workflow backup size mismatch: {item.archive_path}")
        digest = _hash_zip_member_bounded(
            archive,
            info,
            expected_size=item.size_bytes,
            context=f"workflow backup member {item.archive_path}",
        )
        if digest != item.sha256:
            raise ConfigurationError(f"workflow backup checksum mismatch: {item.archive_path}")

    identity_payload = _backup_identity_payload(
        workflow_id=manifest.workflow_id,
        original_workspace=manifest.original_workspace,
        topoforge_version=manifest.topoforge_version,
        files=manifest.files,
    )
    if sha256_bytes(_canonical_json(identity_payload)) != manifest.backup_id:
        raise ConfigurationError("workflow backup identity does not match its manifest")
    return manifest


def _workflow_backup_stream_source(
    stream: BinaryIO,
    source_path: Path | None,
) -> Path:
    if source_path is None:
        stream_name = getattr(stream, "name", None)
        if isinstance(stream_name, (str, os.PathLike)):
            source_path = Path(stream_name)
        else:
            raise ConfigurationError(
                "an opened workflow backup stream requires source_path metadata"
            )
    return Path(os.path.abspath(source_path.expanduser()))


def _require_stream_source_identity(
    source: Path,
    *,
    expected_identity: tuple[int, int],
) -> None:
    """Bind caller-supplied source metadata to the stream's exact file object."""
    parent_identity = _capture_directory_identity(
        source.parent,
        context="workflow backup source parent",
    )
    try:
        observed = owned_entry_identity(
            source,
            root=source.parent,
            root_identity=parent_identity,
            directory=False,
            context="workflow backup source",
        )
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f"workflow backup source path is unsafe or unavailable: {source}"
        ) from exc
    if observed is None:
        raise ConfigurationError(f"workflow backup source path is unavailable: {source}")
    if observed != expected_identity:
        raise ConfigurationError(
            f"workflow backup source_path does not name the opened archive stream: {source}"
        )


@contextmanager
def _private_workflow_backup_snapshot(
    stream: BinaryIO,
    *,
    source: Path,
) -> Iterator[tuple[BinaryIO, int, str, tuple[int, int]]]:
    """Copy one stable source handle into a private inode used for all parsing."""
    original_offset: int | None = None
    try:
        original_offset = stream.tell()
        before = os.fstat(stream.fileno())
        _require_stream_source_identity(
            source,
            expected_identity=_identity(before),
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > _MAX_BACKUP_ARCHIVE_BYTES
        ):
            raise ConfigurationError("workflow backup archive is not a unique bounded regular file")
        digest = hashlib.sha256()
        observed = 0
        stream.seek(0)
        snapshot = tempfile.TemporaryFile(  # noqa: SIM115 - close must not fail the transaction
            mode="w+b",
            prefix="topoforge-workflow-backup-",
        )
        try:
            while block := stream.read(_ZIP_READ_CHUNK_BYTES):
                observed += len(block)
                if observed > before.st_size or observed > _MAX_BACKUP_ARCHIVE_BYTES:
                    raise ConfigurationError(
                        "workflow backup archive grew while its private snapshot was created"
                    )
                snapshot.write(block)
                digest.update(block)
            after = os.fstat(stream.fileno())
            if observed != before.st_size or _stable_stat_fields(after) != _stable_stat_fields(
                before
            ):
                raise ConfigurationError(
                    "workflow backup archive changed while its private snapshot was created"
                )
            snapshot.flush()
            snapshot.seek(0)
            snapshot_information = os.fstat(snapshot.fileno())
            if (
                not stat.S_ISREG(snapshot_information.st_mode)
                or snapshot_information.st_size != observed
            ):
                raise ConfigurationError("workflow backup private snapshot is invalid")
            archive_sha256 = digest.hexdigest()
            yield snapshot, observed, archive_sha256, _identity(before)
        finally:
            with suppress(OSError):
                snapshot.close()
    except ConfigurationError:
        raise
    except (AttributeError, OSError, ValueError) as exc:
        raise ConfigurationError(
            f"workflow backup private snapshot could not be created: {source}"
        ) from exc
    finally:
        if original_offset is not None:
            with suppress(OSError, ValueError):
                stream.seek(original_offset)


@contextmanager
def _verify_open_workflow_backup_stream(
    stream: BinaryIO,
    *,
    source: Path,
) -> Iterator[VerifiedWorkflowBackup]:
    """Verify and yield one caller-owned, already pinned regular-file stream."""
    try:
        with _private_workflow_backup_snapshot(stream, source=source) as (
            snapshot,
            size_bytes,
            archive_sha256,
            source_identity,
        ):
            try:
                central_directory = preflight_zip_central_directory(
                    snapshot,
                    maximum_entries=_MAX_BACKUP_MEMBER_COUNT,
                    maximum_central_directory_bytes=_MAX_BACKUP_CENTRAL_DIRECTORY_BYTES,
                    maximum_comment_bytes=0,
                    label="workflow backup",
                )
            except ValueError as exc:
                raise ConfigurationError(str(exc)) from exc
            if central_directory.file_size != size_bytes:
                raise ConfigurationError("workflow backup size changed before ZIP parsing")
            snapshot.seek(0)
            with zipfile.ZipFile(snapshot, mode="r") as archive:
                manifest = _verify_backup_members(
                    archive,
                    archive_size_bytes=size_bytes,
                )
                snapshot.seek(0)
                yield VerifiedWorkflowBackup(
                    source=source,
                    source_stream=stream,
                    stream=snapshot,
                    archive=archive,
                    manifest=manifest,
                    sha256=archive_sha256,
                    size_bytes=size_bytes,
                    source_identity=source_identity,
                )
    except ConfigurationError:
        raise
    except (
        AttributeError,
        OSError,
        ValueError,
        RuntimeError,
        zipfile.BadZipFile,
        KeyError,
    ) as exc:
        raise ConfigurationError(f"workflow backup is unreadable: {source}") from exc


@contextmanager
def open_verified_workflow_backup(
    archive_path: Path | BinaryIO,
    *,
    source_path: Path | None = None,
) -> Iterator[VerifiedWorkflowBackup]:
    """Yield one verified pinned archive without closing a caller-owned stream.

    Path inputs are opened without following links and are owned by this context.
    Already-open streams must be seekable unique regular files; they remain caller
    owned. Their source_path must name the same file object and is required when the
    stream has no path name.
    """
    if isinstance(archive_path, Path):
        if source_path is not None:
            raise ConfigurationError(
                "source_path is only valid with an opened workflow backup stream"
            )
        source = Path(os.path.abspath(archive_path.expanduser()))
        parent_identity = _capture_directory_identity(
            source.parent,
            context="workflow backup archive parent",
        )
        try:
            with (
                open_owned_regular_binary(
                    source,
                    root=source.parent,
                    root_identity=parent_identity,
                    context="workflow backup archive",
                ) as stream,
                _verify_open_workflow_backup_stream(
                    stream,
                    source=source,
                ) as verified,
            ):
                yield verified
        except ConfigurationError:
            raise
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"workflow backup is unreadable: {source}") from exc
        return

    source = _workflow_backup_stream_source(archive_path, source_path)
    with _verify_open_workflow_backup_stream(
        archive_path,
        source=source,
    ) as verified:
        yield verified


def _require_verified_backup_source(verified: VerifiedWorkflowBackup) -> None:
    """Require durable evidence to still name the exact verified source archive."""
    parent_identity = _capture_directory_identity(
        verified.source.parent,
        context="workflow restore archive parent",
    )
    try:
        observed_identity = owned_entry_identity(
            verified.source,
            root=verified.source.parent,
            root_identity=parent_identity,
            directory=False,
            context="workflow restore archive publication check",
        )
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            "workflow restore archive path changed before destination publication"
        ) from exc
    if observed_identity != verified.source_identity:
        raise ConfigurationError(
            "workflow restore archive path changed before destination publication"
        )
    size_bytes, archive_sha256, source_identity = _hash_open_archive_stream(verified.source_stream)
    if (
        size_bytes != verified.size_bytes
        or archive_sha256 != verified.sha256
        or source_identity != verified.source_identity
    ):
        raise ConfigurationError(
            "workflow restore archive content changed before destination publication"
        )


@contextmanager
def _open_restore_transaction_backup(
    archive_path: Path | BinaryIO,
    *,
    source_path: Path | None,
    on_context_exit_failure: Callable[[BaseException], None],
) -> Iterator[VerifiedWorkflowBackup]:
    """Rollback through a callback only when a successful body has a failed exit."""
    body_completed = False
    try:
        with open_verified_workflow_backup(archive_path, source_path=source_path) as verified:
            yield verified
            body_completed = True
    except BaseException as failure:
        if body_completed:
            on_context_exit_failure(failure)
        raise


@contextmanager
def _open_verified_backup_archive(
    archive_path: Path,
) -> Iterator[VerifiedWorkflowBackup]:
    """Compatibility wrapper for existing internal path callers."""
    with open_verified_workflow_backup(archive_path) as verified:
        yield verified


def _publish_backup_no_clobber(
    temporary: Path,
    destination: Path,
    *,
    parent: Path,
    parent_identity: tuple[int, int],
    temporary_identity: tuple[int, int],
) -> None:
    move_error: OSError | ValueError | None = None
    try:
        move_owned_path(
            temporary,
            destination,
            source_root=parent,
            source_root_identity=parent_identity,
            destination_root=parent,
            destination_root_identity=parent_identity,
            expected_identity=temporary_identity,
            directory=False,
            context="workflow backup publication",
        )
    except FileExistsError as exc:
        move_error = exc
    except (OSError, ValueError) as exc:
        move_error = exc
    if move_error is None:
        return

    temporary_location = _owned_move_endpoint(
        temporary,
        root=parent,
        root_identity=parent_identity,
        expected_identity=temporary_identity,
        directory=False,
        context="workflow backup publication source reconciliation",
    )
    destination_location = _owned_move_endpoint(
        destination,
        root=parent,
        root_identity=parent_identity,
        expected_identity=temporary_identity,
        directory=False,
        context="workflow backup publication destination reconciliation",
    )
    if getattr(move_error, "committed", False):
        raise ConfigurationError(
            "workflow backup publication committed but its durability is uncertain; "
            f"preserve and inspect {destination} before retrying"
        ) from move_error
    if temporary_location == "missing" and destination_location == "match":
        return
    if isinstance(move_error, FileExistsError) and temporary_location == "match":
        raise ConfigurationError(
            f"workflow backup already exists: {destination}; choose another output path"
        ) from move_error
    if temporary_location == "match" and destination_location == "missing":
        raise ConfigurationError(
            "workflow backup could not be published through its identity-bound parent"
        ) from move_error
    raise ConfigurationError(
        "workflow backup publication outcome is uncertain; preserve and inspect both "
        f"{temporary} and {destination} before retrying"
    ) from move_error


def _create_workflow_backup_stream(
    stream: BinaryIO,
    *,
    source: Path,
    manifest: WorkflowBackupManifest,
    source_files: list[_BackupSource],
) -> tuple[int, str]:
    """Write and strictly reopen one caller-owned pinned output stream."""
    try:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != 0
            or not stream.readable()
            or not stream.writable()
            or not stream.seekable()
        ):
            raise ConfigurationError(
                "workflow backup output stream must be an empty, unique, "
                "seekable read/write regular file"
            )
        stream.seek(0)
        with zipfile.ZipFile(
            stream,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            for backup_source in source_files:
                _write_zip_file(archive, backup_source)
            archive.writestr(
                _zip_info(_BACKUP_MANIFEST_NAME),
                _canonical_json(manifest),
            )
        stream.flush()
        os.fsync(stream.fileno())
        written = os.fstat(stream.fileno())
        if (
            _identity(written) != _identity(before)
            or not stat.S_ISREG(written.st_mode)
            or written.st_nlink != 1
        ):
            raise ConfigurationError(
                "workflow backup output stream identity changed while it was written"
            )
        with _verify_open_workflow_backup_stream(
            stream,
            source=source,
        ) as verified:
            if verified.manifest != manifest:
                raise ConfigurationError("workflow backup failed strict manifest reopen")
            archive_size_bytes = verified.size_bytes
            archive_sha256 = verified.sha256
        return archive_size_bytes, archive_sha256
    except ConfigurationError:
        raise
    except (AttributeError, OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ConfigurationError(
            "workflow backup output stream could not be safely written and reopened"
        ) from exc


def _verify_backup_semantic_snapshot(
    archive_path: Path | BinaryIO,
    *,
    source_path: Path | None,
    scratch_root: Path,
    scratch_root_identity: tuple[int, int],
) -> None:
    """Restore and semantically verify the exact pending archive before publication."""
    validation = scratch_root / f".topoforge-backup-verify-{uuid4().hex}"
    validation_identity: tuple[int, int] | None = None
    try:
        restore_workflow_backup(
            archive_path,
            validation,
            source_path=source_path,
            destination_root=scratch_root,
            destination_root_identity=scratch_root_identity,
        )
        validation_identity = _capture_directory_identity(
            validation,
            context="workflow backup semantic validation workspace",
        )
    finally:
        if validation_identity is not None:
            try:
                _remove_owned_tree(
                    validation,
                    parent_root=scratch_root,
                    parent_root_identity=scratch_root_identity,
                    expected_tree_identity=validation_identity,
                    context="workflow backup semantic validation workspace",
                )
            except Exception as exc:
                raise ConfigurationError(
                    "workflow backup semantic validation passed but its private workspace "
                    f"could not be safely removed; preserve and inspect {validation}"
                ) from exc


def create_workflow_backup(
    workspace_dir: Path,
    archive_path: Path | BinaryIO,
    *,
    source_path: Path | None = None,
) -> WorkflowBackupResult:
    """Create a deterministic checksum-bound backup and strictly reopen one handle."""
    from topoforge.workflow.ux import inspect_workflow_workspace, read_workflow_launch_config

    root = _workspace_root(workspace_dir)
    root_identity = _capture_directory_identity(root, context="workflow backup workspace")
    if isinstance(archive_path, Path):
        if source_path is not None:
            raise ConfigurationError(
                "source_path is only valid with an opened workflow backup stream"
            )
        destination = Path(os.path.abspath(archive_path.expanduser()))
    else:
        destination = _workflow_backup_stream_source(archive_path, source_path)
    if destination == root or root in destination.parents:
        raise ConfigurationError("workflow backup output must be outside the workspace")
    if destination.suffix.lower() != ".zip":
        raise ConfigurationError("workflow backup path must end in .zip")
    summary = inspect_workflow_workspace(root)
    _require_directory_identity(root, root_identity, context="workflow backup workspace")
    launch = read_workflow_launch_config(root / "workflow-launch.yaml")
    source_files = _workspace_backup_files(root, root_identity) + _external_backup_files(launch)
    records = tuple(source.record for source in source_files)
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
    if not isinstance(archive_path, Path):
        parent, parent_identity = _ensure_owned_directory_tree(
            destination.parent,
            context="workflow backup stream parent",
        )
        try:
            archive_size_bytes, archive_sha256 = _create_workflow_backup_stream(
                archive_path,
                source=destination,
                manifest=manifest,
                source_files=source_files,
            )
            _verify_backup_semantic_snapshot(
                archive_path,
                source_path=destination,
                scratch_root=parent,
                scratch_root_identity=parent_identity,
            )
            archive_path.seek(0)
        except BaseException:
            with suppress(OSError):
                archive_path.seek(0)
                archive_path.truncate(0)
                archive_path.flush()
                os.fsync(archive_path.fileno())
            raise
        return WorkflowBackupResult(
            archive_path=destination,
            archive_sha256=archive_sha256,
            archive_size_bytes=archive_size_bytes,
            manifest=manifest,
        )

    parent, parent_identity = _ensure_owned_directory_tree(
        destination.parent,
        context="workflow backup destination parent",
    )
    temporary = parent / f".{destination.name}.{uuid4().hex}.publishing"
    temporary_identity: tuple[int, int] | None = None
    archive_size_bytes = 0
    archive_sha256 = ""
    try:
        with open_exclusive_owned_regular_binary(
            temporary,
            root=parent,
            root_identity=parent_identity,
            context="workflow backup temporary",
        ) as temporary_stream:
            temporary_identity = _identity(os.fstat(temporary_stream.fileno()))
            with zipfile.ZipFile(
                temporary_stream,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
                strict_timestamps=True,
            ) as archive:
                for source in source_files:
                    _write_zip_file(archive, source)
                archive.writestr(_zip_info(_BACKUP_MANIFEST_NAME), _canonical_json(manifest))
            temporary_stream.flush()
            os.fsync(temporary_stream.fileno())
        if temporary_identity is None:
            raise ConfigurationError("workflow backup temporary identity was not captured")
        with open_owned_regular_binary(
            temporary,
            root=parent,
            root_identity=parent_identity,
            expected_identity=temporary_identity,
            context="workflow backup temporary verification",
        ) as temporary_read_stream:
            archive_size_bytes, archive_sha256, reopened_identity = _hash_open_archive_stream(
                temporary_read_stream
            )
            if reopened_identity != temporary_identity:
                raise ConfigurationError("workflow backup temporary identity changed")
            with zipfile.ZipFile(temporary_read_stream, mode="r") as archive:
                reopened = _verify_backup_members(
                    archive,
                    archive_size_bytes=archive_size_bytes,
                )
            if reopened != manifest:
                raise ConfigurationError("workflow backup failed strict manifest reopen")
        _verify_backup_semantic_snapshot(
            temporary,
            source_path=None,
            scratch_root=parent,
            scratch_root_identity=parent_identity,
        )
        _publish_backup_no_clobber(
            temporary,
            destination,
            parent=parent,
            parent_identity=parent_identity,
            temporary_identity=temporary_identity,
        )
    except BaseException:
        if temporary_identity is not None:
            with suppress(OSError, ValueError):
                remove_owned_path(
                    temporary,
                    root=parent,
                    root_identity=parent_identity,
                    expected_identity=temporary_identity,
                    directory=False,
                    context="workflow backup temporary cleanup",
                    missing_ok=True,
                )
        raise
    return WorkflowBackupResult(
        archive_path=destination,
        archive_sha256=archive_sha256,
        archive_size_bytes=archive_size_bytes,
        manifest=manifest,
    )


def verify_workflow_backup(
    archive_path: Path | BinaryIO,
    *,
    source_path: Path | None = None,
) -> WorkflowBackupManifest:
    """Verify one bounded archive completely through a single pinned handle."""
    with open_verified_workflow_backup(archive_path, source_path=source_path) as verified:
        return verified.manifest


def verify_workflow_restore_evidence(workspace_dir: Path) -> WorkflowRestoreEvidence:
    """Verify that one relocated workspace is bound to its still-available backup."""
    root = _workspace_root(workspace_dir)
    evidence_path = _lexical_workspace_path(
        root,
        "workflow-restore.json",
        context="workflow restore evidence",
    )
    try:
        evidence = WorkflowRestoreEvidence.model_validate_json(
            evidence_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f"workflow restore evidence is unreadable: {evidence_path}"
        ) from exc
    if evidence.schema_version != _RESTORE_EVIDENCE_SCHEMA_VERSION:
        raise ConfigurationError("workflow restore evidence schema version is unsupported")
    if evidence_path.read_bytes() != _canonical_json(evidence):
        raise ConfigurationError("workflow restore evidence is non-canonical or changed")
    if evidence.restored_workspace.expanduser().resolve() != root:
        raise ConfigurationError("workflow restore evidence names a different restored workspace")
    original_root = evidence.original_workspace.expanduser().resolve()
    if original_root == root:
        raise ConfigurationError("workflow restore evidence does not describe a relocation")

    archive_lexical = Path(os.path.abspath(evidence.archive_path.expanduser()))
    if archive_lexical == root or root in archive_lexical.parents:
        raise ConfigurationError(
            "workflow restore archive must remain outside the restored workspace"
        )
    active_archive = _ACTIVE_VERIFIED_ARCHIVE.get()
    if active_archive is not None:
        if (
            Path(os.path.abspath(active_archive.source.expanduser())) != archive_lexical
            or active_archive.sha256 != evidence.archive_sha256
        ):
            raise ConfigurationError(
                "workflow restore evidence does not match the active pinned archive"
            )
        manifest = active_archive.manifest
    else:
        try:
            if path_is_link_like(archive_lexical):
                raise ConfigurationError(
                    f"workflow restore archive is link-like: {archive_lexical}; "
                    "use the original archive"
                )
        except FileNotFoundError as exc:
            raise ConfigurationError(
                f"workflow restore archive is unavailable: {archive_lexical}; "
                "restore it before browsing relocated artifacts"
            ) from exc
        archive = archive_lexical.resolve(strict=True)
        with _open_verified_backup_archive(archive) as verified:
            if verified.sha256 != evidence.archive_sha256:
                raise ConfigurationError("workflow restore archive checksum changed")
            manifest = verified.manifest
    if (
        manifest.backup_id != evidence.backup_id
        or manifest.workflow_id != evidence.workflow_id
        or manifest.original_workspace.expanduser().resolve() != original_root
    ):
        raise ConfigurationError("workflow restore evidence does not match the verified backup")

    records = {item.archive_path: item for item in manifest.files}
    if len(records) != len(manifest.files):
        raise ConfigurationError("workflow restore backup inventory contains duplicate paths")

    identity_files = {
        "workflow-launch.yaml": evidence.original_launch_sha256,
        "workflow-request.json": evidence.request_sha256,
        "workflow-manifest.json": evidence.workflow_manifest_sha256,
        "workflow-status.json": evidence.workflow_status_sha256,
    }
    for relative, expected_sha256 in identity_files.items():
        record = records.get(f"workspace/{relative}")
        if record is None or record.kind != "workspace" or record.sha256 != expected_sha256:
            raise ConfigurationError(
                f"workflow restore identity file is not bound by the backup: {relative}"
            )

    restored_launch = _lexical_workspace_path(
        root,
        "workflow-launch.yaml",
        context="restored workflow launch",
    )
    _, restored_launch_sha256 = _verified_backup_source(
        restored_launch,
        context="restored workflow launch",
    )
    if restored_launch_sha256 != evidence.restored_launch_sha256:
        raise ConfigurationError("restored workflow launch checksum changed")

    mutable_workspace_files = {
        "workflow-launch.yaml",
        "workflow-summary.json",
        "workflow-report.html",
        "workflow-storage.json",
        "workflow-restore.json",
    }
    has_external = any(item.kind == "external" for item in manifest.files)
    expected_external = root / "backup-external" if has_external else None
    if (evidence.external_directory is None) != (expected_external is None) or (
        evidence.external_directory is not None
        and evidence.external_directory.expanduser().resolve() != expected_external
    ):
        raise ConfigurationError("workflow restore external-directory evidence changed")

    for item in manifest.files:
        archive_path = _safe_archive_path(item.archive_path)
        if item.kind == "workspace":
            if len(archive_path.parts) < 2 or archive_path.parts[0] != "workspace":
                raise ConfigurationError(f"invalid restored workspace path: {item.archive_path}")
            relative = Path(*archive_path.parts[1:])
            if relative.as_posix() in mutable_workspace_files:
                continue
        else:
            if len(archive_path.parts) < 2 or archive_path.parts[0] != "external":
                raise ConfigurationError(f"invalid restored external path: {item.archive_path}")
            relative = Path("backup-external", *archive_path.parts[1:])
        restored_file = _lexical_workspace_path(
            root,
            relative,
            context=f"restored backup file {item.archive_path}",
        )
        size_bytes, restored_sha256 = _verified_backup_source(
            restored_file,
            context=f"restored backup file {item.archive_path}",
        )
        if size_bytes != item.size_bytes or restored_sha256 != item.sha256:
            raise ConfigurationError(f"restored backup file changed: {item.archive_path}")

    workflow = LocalWorkflowManifest.model_validate_json(
        (root / "workflow-manifest.json").read_text(encoding="utf-8")
    )
    if workflow.workflow_id != evidence.workflow_id:
        raise ConfigurationError("restored workflow id does not match restore evidence")
    return evidence


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


def _remap_restored_launch(
    launch: WorkflowLaunchConfig,
    *,
    manifest: WorkflowBackupManifest,
    destination: Path,
    external_map: dict[Path, Path],
) -> WorkflowLaunchConfig:
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
    return launch.model_copy(
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


def _restore_target_relative(item: WorkflowBackupFile) -> Path:
    archive_path = _safe_archive_path(item.archive_path)
    if item.kind == "workspace":
        return Path(*archive_path.parts[1:])
    return Path("backup-external", *archive_path.parts[1:])


def _validate_restore_targets(manifest: WorkflowBackupManifest) -> None:
    exact: dict[str, str] = {}
    unicode_aliases: dict[str, str] = {}
    windows_aliases: dict[str, str] = {}
    for item in manifest.files:
        target = _restore_target_relative(item).as_posix()
        unicode_alias, windows_alias = _archive_aliases(target)
        if target in exact:
            raise ConfigurationError(f"workflow backup restores multiple members to {target}")
        if unicode_alias in unicode_aliases or windows_alias in windows_aliases:
            raise ConfigurationError(
                f"workflow backup restore target aliases another member: {target}"
            )
        exact[target] = item.archive_path
        unicode_aliases[unicode_alias] = target
        windows_aliases[windows_alias] = target
    targets = set(exact)
    for target in targets:
        parts = PurePosixPath(target).parts
        for end in range(1, len(parts)):
            if "/".join(parts[:end]) in targets:
                raise ConfigurationError(
                    f"workflow backup restore target has a prefix collision: {target}"
                )


def _rollback_restore_publication(
    destination: Path,
    staging: Path,
    *,
    publication_root: Path,
    publication_root_identity: tuple[int, int],
    staging_identity: tuple[int, int],
    failure: BaseException,
) -> None:
    """Move a published restore back to staging and reconcile late native errors."""
    try:
        move_owned_path(
            destination,
            staging,
            source_root=publication_root,
            source_root_identity=publication_root_identity,
            destination_root=publication_root,
            destination_root_identity=publication_root_identity,
            expected_identity=staging_identity,
            directory=True,
            context="workflow restore rollback",
        )
    except (OSError, ValueError) as rollback_error:
        destination_location = _owned_move_endpoint(
            destination,
            root=publication_root,
            root_identity=publication_root_identity,
            expected_identity=staging_identity,
            directory=True,
            context="workflow restore rollback source reconciliation",
        )
        staging_location = _owned_move_endpoint(
            staging,
            root=publication_root,
            root_identity=publication_root_identity,
            expected_identity=staging_identity,
            directory=True,
            context="workflow restore rollback destination reconciliation",
        )
        if getattr(rollback_error, "committed", False):
            raise ConfigurationError(
                "workflow restore rollback committed but its durability is uncertain; "
                f"preserve and inspect both {destination} and {staging}"
            ) from failure
        if destination_location == "missing" and staging_location == "match":
            return
        if destination_location == "match" and staging_location == "missing":
            raise ConfigurationError(
                "restored workflow failed post-publication validation and rollback "
                f"did not commit; preserve and inspect {destination}: {rollback_error}"
            ) from failure
        raise ConfigurationError(
            "restored workflow failed post-publication validation and rollback outcome "
            f"is uncertain; preserve and inspect both {destination} and {staging}: "
            f"{rollback_error}"
        ) from failure


def restore_workflow_backup(
    archive_path: Path | BinaryIO,
    workspace_dir: Path,
    *,
    source_path: Path | None = None,
    destination_root: Path | None = None,
    destination_root_identity: tuple[int, int] | None = None,
) -> WorkflowRestoreResult:
    """Restore one verified archive through pinned source and destination handles."""
    from topoforge.workflow.ux import (
        WorkflowLaunchConfig,
        inspect_workflow_workspace,
        render_workflow_report_bytes,
    )

    destination = Path(os.path.abspath(workspace_dir.expanduser()))
    trusted_root: Path | None = None
    if (destination_root is None) != (destination_root_identity is None):
        raise ConfigurationError(
            "restore destination_root and destination_root_identity must be provided together"
        )
    if destination_root is not None:
        trusted_root = Path(os.path.abspath(destination_root.expanduser()))
        if destination == trusted_root:
            raise ConfigurationError(
                "restore destination must be a child of its trusted destination root"
            )
        try:
            destination.relative_to(trusted_root)
        except ValueError as exc:
            raise ConfigurationError(
                "restore destination escapes its trusted destination root"
            ) from exc
        assert destination_root_identity is not None
        _require_directory_identity(
            trusted_root,
            destination_root_identity,
            context="workflow restore trusted destination root",
        )
    try:
        destination.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ConfigurationError(f"restore destination is unreadable: {destination}") from exc
    else:
        raise ConfigurationError(f"restore destination already exists: {destination}")

    publication_root: Path | None = None
    publication_root_identity: tuple[int, int] | None = None
    staging: Path | None = None
    staging_identity: tuple[int, int] | None = None
    published = False

    def rollback_after_context_exit(failure: BaseException) -> None:
        nonlocal published
        if not published:
            return
        if (
            publication_root is None
            or publication_root_identity is None
            or staging is None
            or staging_identity is None
        ):
            raise ConfigurationError(
                "workflow restore archive context failed after publication, but rollback "
                "state is incomplete; preserve and inspect the destination"
            ) from failure
        _rollback_restore_publication(
            destination,
            staging,
            publication_root=publication_root,
            publication_root_identity=publication_root_identity,
            staging_identity=staging_identity,
            failure=failure,
        )
        published = False
        try:
            _remove_owned_tree(
                staging,
                parent_root=publication_root,
                parent_root_identity=publication_root_identity,
                expected_tree_identity=staging_identity,
                context="workflow restore staging cleanup after archive context failure",
            )
        except Exception as cleanup_error:
            raise ConfigurationError(
                "workflow restore archive context failed after publication and rollback "
                f"staging could not be removed; preserve and inspect {staging}: "
                f"{cleanup_error}"
            ) from failure

    with _open_restore_transaction_backup(
        archive_path,
        source_path=source_path,
        on_context_exit_failure=rollback_after_context_exit,
    ) as verified:
        source = verified.source
        manifest = verified.manifest
        _validate_restore_targets(manifest)
        if trusted_root is None:
            publication_root, publication_root_identity = _ensure_owned_directory_tree(
                destination.parent,
                context="workflow restore destination parent",
            )
        else:
            assert destination_root_identity is not None
            _require_directory_identity(
                trusted_root,
                destination_root_identity,
                context="workflow restore trusted destination root",
            )
            _ensure_owned_relative_parent(
                destination,
                root=trusted_root,
                root_identity=destination_root_identity,
                context="workflow restore destination parent",
            )
            publication_root = trusted_root
            publication_root_identity = destination_root_identity
        staging = destination.parent / f".{destination.name}.restore-{uuid4().hex}"
        try:
            create_owned_directory(
                staging,
                root=publication_root,
                root_identity=publication_root_identity,
                context="workflow restore staging",
            )
        except (OSError, ValueError) as exc:
            raise ConfigurationError(
                "workflow restore could not create identity-bound staging"
            ) from exc
        staging_identity = _capture_directory_identity(
            staging,
            context="workflow restore staging",
        )
        external_map: dict[Path, Path] = {}
        external_directory: Path | None = None
        published = False
        publication_outcome_uncertain = False
        summary = None
        try:
            infos = {info.filename: info for info in verified.archive.infolist()}
            for item in manifest.files:
                relative = _restore_target_relative(item)
                target = staging / relative
                if item.kind == "external":
                    external_map[Path(item.source_path).expanduser().resolve()] = (
                        destination / relative
                    )
                    external_directory = destination / "backup-external"
                _ensure_owned_relative_parent(
                    target,
                    root=staging,
                    root_identity=staging_identity,
                    context=f"workflow restore output {item.archive_path}",
                )
                try:
                    with open_exclusive_owned_regular_binary(
                        target,
                        root=staging,
                        root_identity=staging_identity,
                        context=f"workflow restore output {item.archive_path}",
                    ) as output_stream:
                        _copy_zip_member_bounded(
                            verified.archive,
                            infos[item.archive_path],
                            output_stream,
                            expected_size=item.size_bytes,
                            expected_sha256=item.sha256,
                            context=f"restored workflow member {item.archive_path}",
                        )
                        output_stream.flush()
                        os.fsync(output_stream.fileno())
                except (OSError, ValueError) as exc:
                    raise ConfigurationError(
                        f"workflow restore output is unsafe: {item.archive_path}"
                    ) from exc

            launch_path = staging / "workflow-launch.yaml"
            launch_payload = _read_owned_file_bytes(
                launch_path,
                root=staging,
                root_identity=staging_identity,
                context="restored workflow launch",
                max_bytes=_MAX_BACKUP_MANIFEST_BYTES,
            )
            try:
                launch_raw = yaml.safe_load(launch_payload)
                if not isinstance(launch_raw, dict):
                    raise ValueError("workflow launch root is not a mapping")
                launch = WorkflowLaunchConfig.model_validate(launch_raw)
            except (ValueError, yaml.YAMLError) as exc:
                raise ConfigurationError("restored workflow launch is invalid") from exc
            restored_launch = _remap_restored_launch(
                launch,
                manifest=manifest,
                destination=destination,
                external_map=external_map,
            )
            restored_launch_payload = yaml.safe_dump(
                restored_launch.model_dump(mode="json"),
                sort_keys=True,
                allow_unicode=True,
            ).encode("utf-8")
            try:
                atomic_write_owned_regular_bytes(
                    launch_path,
                    restored_launch_payload,
                    root=staging,
                    root_identity=staging_identity,
                    context="restored workflow launch",
                    replace=True,
                )
            except (OSError, ValueError) as exc:
                raise ConfigurationError(
                    "restored workflow launch could not be safely remapped"
                ) from exc

            original_launch = next(
                (
                    item
                    for item in manifest.files
                    if item.kind == "workspace"
                    and item.archive_path == "workspace/workflow-launch.yaml"
                ),
                None,
            )
            if original_launch is None:
                raise ConfigurationError("workflow backup has no original launch identity")
            _, restored_launch_sha256, _ = _owned_file_sha256(
                launch_path,
                root=staging,
                root_identity=staging_identity,
                context="restored workflow launch",
            )
            identity_hashes: dict[str, str] = {}
            for name in (
                "workflow-request.json",
                "workflow-manifest.json",
                "workflow-status.json",
            ):
                _, digest, _ = _owned_file_sha256(
                    staging / name,
                    root=staging,
                    root_identity=staging_identity,
                    context=f"restored workflow identity {name}",
                )
                identity_hashes[name] = digest
            evidence = WorkflowRestoreEvidence(
                backup_id=manifest.backup_id,
                archive_path=source,
                archive_sha256=verified.sha256,
                original_workspace=manifest.original_workspace.expanduser().resolve(),
                restored_workspace=destination,
                external_directory=external_directory,
                workflow_id=manifest.workflow_id,
                original_launch_sha256=original_launch.sha256,
                restored_launch_sha256=restored_launch_sha256,
                request_sha256=identity_hashes["workflow-request.json"],
                workflow_manifest_sha256=identity_hashes["workflow-manifest.json"],
                workflow_status_sha256=identity_hashes["workflow-status.json"],
            )

            _require_verified_backup_source(verified)
            publication_error: OSError | ValueError | None = None
            try:
                move_owned_path(
                    staging,
                    destination,
                    source_root=publication_root,
                    source_root_identity=publication_root_identity,
                    destination_root=publication_root,
                    destination_root_identity=publication_root_identity,
                    expected_identity=staging_identity,
                    directory=True,
                    context="workflow restore publication",
                )
            except FileExistsError as exc:
                publication_error = exc
            except (OSError, ValueError) as exc:
                publication_error = exc
            if publication_error is None:
                published = True
            else:
                staging_location = _owned_move_endpoint(
                    staging,
                    root=publication_root,
                    root_identity=publication_root_identity,
                    expected_identity=staging_identity,
                    directory=True,
                    context="workflow restore publication source reconciliation",
                )
                destination_location = _owned_move_endpoint(
                    destination,
                    root=publication_root,
                    root_identity=publication_root_identity,
                    expected_identity=staging_identity,
                    directory=True,
                    context="workflow restore publication destination reconciliation",
                )
                if getattr(publication_error, "committed", False):
                    publication_outcome_uncertain = True
                    raise ConfigurationError(
                        "workflow restore publication committed but its durability is "
                        f"uncertain; preserve and inspect {destination} before retrying"
                    ) from publication_error
                if staging_location == "missing" and destination_location == "match":
                    published = True
                elif staging_location == "match":
                    if isinstance(publication_error, FileExistsError):
                        raise ConfigurationError(
                            f"restore destination already exists: {destination}"
                        ) from publication_error
                    raise ConfigurationError(
                        "workflow restore destination parent changed during publication"
                    ) from publication_error
                else:
                    publication_outcome_uncertain = True
                    raise ConfigurationError(
                        "workflow restore publication outcome is uncertain; preserve and "
                        f"inspect both {staging} and {destination} before retrying"
                    ) from publication_error
            try:
                atomic_write_owned_regular_bytes(
                    destination / "workflow-restore.json",
                    _canonical_json(evidence),
                    root=destination,
                    root_identity=staging_identity,
                    context="workflow restore evidence",
                    replace=True,
                )
            except (OSError, ValueError) as exc:
                raise ConfigurationError(
                    "workflow restore evidence could not be safely published"
                ) from exc
            active_token = _ACTIVE_VERIFIED_ARCHIVE.set(verified)
            try:
                summary = inspect_workflow_workspace(destination)
            finally:
                _ACTIVE_VERIFIED_ARCHIVE.reset(active_token)
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
            try:
                atomic_write_owned_regular_bytes(
                    summary_path,
                    _canonical_json(summary),
                    root=destination,
                    root_identity=staging_identity,
                    context="restored workflow summary",
                    replace=True,
                )
            except (OSError, ValueError) as exc:
                raise ConfigurationError(
                    "restored workflow summary could not be safely written"
                ) from exc
            try:
                report_payload = render_workflow_report_bytes(
                    summary,
                    report_path=report_path,
                )
                atomic_write_owned_regular_bytes(
                    report_path,
                    report_payload,
                    root=destination,
                    root_identity=staging_identity,
                    context="restored workflow report",
                    replace=True,
                )
            except (ConfigurationError, OSError, ValueError) as exc:
                raise ConfigurationError(
                    "restored workflow report could not be safely written"
                ) from exc
            _require_directory_identity(
                destination,
                staging_identity,
                context="restored workflow",
            )
            _require_verified_backup_source(verified)
        except BaseException as failure:
            if publication_outcome_uncertain:
                raise
            if published:
                _rollback_restore_publication(
                    destination,
                    staging,
                    publication_root=publication_root,
                    publication_root_identity=publication_root_identity,
                    staging_identity=staging_identity,
                    failure=failure,
                )
                published = False
            try:
                _remove_owned_tree(
                    staging,
                    parent_root=publication_root,
                    parent_root_identity=publication_root_identity,
                    expected_tree_identity=staging_identity,
                    context="workflow restore staging cleanup",
                )
            except Exception as cleanup_error:
                raise ConfigurationError(
                    "workflow restore failed and identity-bound staging cleanup also failed; "
                    f"preserve and inspect {staging}: {cleanup_error}"
                ) from failure
            raise

    if summary is None:
        raise ConfigurationError("workflow restore did not produce a verified summary")
    return WorkflowRestoreResult(
        backup_id=manifest.backup_id,
        archive_path=source,
        archive_sha256=verified.sha256,
        workspace=destination,
        external_directory=external_directory,
        workflow_id=summary.workflow_id,
        required_checks_passed=True,
    )
