"""Durable isolated local workflow job manager."""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import logging
import mimetypes
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, TypeVar, cast
from uuid import uuid4

from pydantic import BaseModel

from topoforge.exceptions import ConfigurationError
from topoforge.platforms import path_is_link_like, stat_result_is_link_like
from topoforge.util import sha256_file
from topoforge.validation.slicers import (
    BambuStudioAdapter,
    OrcaSlicerAdapter,
    PrusaSlicerAdapter,
    SlicerAdapter,
    SlicerAvailability,
    SlicerInfo,
    select_slicer,
)
from topoforge.web.models import (
    FileEntry,
    FileListing,
    JobArtifact,
    JobBatchDeleteApplyRequest,
    JobBatchDeleteMode,
    JobBatchDeletePlan,
    JobBatchDeletePlanItem,
    JobBatchDeletePlanRequest,
    JobCreateRequest,
    JobDeleteRequest,
    JobDeleteResult,
    JobDeletionInventory,
    JobError,
    JobEvent,
    JobMaintenanceOverview,
    JobRecord,
    JobState,
    JobTrashActionKind,
    JobTrashActionPhase,
    JobTrashActionRequest,
    JobTrashActionResult,
    JobTrashActionTransaction,
    JobTrashJobInventory,
    JobTrashPurgeEntry,
    JobTrashRecord,
    JobTrashTransaction,
    JobTrashTransactionMove,
    JobTrashWorkspace,
    WebAppConfig,
    WorkerReady,
    WorkerResult,
    WorkflowBackupRecord,
    WorkflowRestoreRequest,
    utc_now,
)
from topoforge.web.processes import (
    process_containment_is_alive,
    process_is_alive,
    terminate_process_tree,
    worker_process_options,
)
from topoforge.web.processes import (
    process_identity as inspect_process_identity,
)
from topoforge.web.security import (
    WebManagerLease,
    atomic_write_owned_regular_bytes,
    atomic_write_regular_bytes,
    canonical_json_bytes,
    create_owned_directory,
    ensure_real_directory_tree,
    move_owned_path,
    open_exclusive_owned_regular_binary,
    open_owned_regular_binary,
    owned_directory_identity,
    read_canonical_model,
    read_owned_regular_bytes,
    real_directory_tree_identity,
    remove_owned_path,
    write_exclusive_owned_regular_bytes,
)
from topoforge.workflow import (
    LocalWorkflowStatus,
    WorkflowCleanupResult,
    WorkflowStage,
    apply_workflow_cleanup,
    create_workflow_backup,
    estimate_workflow_storage,
    inspect_workflow_workspace,
    plan_workflow_cleanup,
    read_workflow_launch_config,
    restore_workflow_backup,
    verify_workflow_backup,
)

_WORKER_GATE_TIMEOUT_SECONDS = 30.0
_WORKER_GATE_SCHEMA_VERSION = "topoforge-web-worker-launch-gate-v3"
_MAX_WORKER_READY_BYTES = 4096
_MAX_TRASH_ACTION_BYTES = 8 * 1024 * 1024
_MAX_PURGE_MANIFEST_ENTRIES = 20_000

_SELECTABLE_SUFFIXES = {
    ".geojson",
    ".gpx",
    ".json",
    ".tif",
    ".tiff",
    ".yaml",
    ".yml",
}
_ModelT = TypeVar("_ModelT", bound=BaseModel)
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class VerifiedFileDownload:
    """One checksum-verified stream whose exact handle remains open for HTTP."""

    stream: BinaryIO
    size_bytes: int
    sha256: str
    _context: contextlib.AbstractContextManager[Any]
    _closed: bool = False

    def close(self) -> None:
        """Close the pinned stream exactly once."""
        if self._closed:
            return
        self._closed = True
        self._context.__exit__(None, None, None)


def expected_workflow_stages(request: JobCreateRequest) -> tuple[WorkflowStage, ...]:
    """Return the ordered stage set implied by one launch configuration."""
    launch = request.launch
    stages: list[WorkflowStage] = [
        WorkflowStage.ACQUIRE if launch.global_source is not None else WorkflowStage.SOURCE,
        WorkflowStage.BUILD,
    ]
    if launch.overlay is not None:
        stages.append(WorkflowStage.OVERLAY)
    stages.extend(
        (
            WorkflowStage.LAYOUT,
            WorkflowStage.EXTRACT,
            WorkflowStage.MESH,
            WorkflowStage.CONNECT,
        )
    )
    if launch.slicing_enabled:
        stages.append(WorkflowStage.SLICE)
    if launch.project_evidence_enabled:
        stages.append(WorkflowStage.PROJECT)
    return tuple(stages)


def _canonical_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    return canonical_json_bytes(value)


def _atomic_write(path: Path, value: BaseModel | dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_regular_bytes(
        path,
        _canonical_bytes(value),
        context="Web durable record",
    )


def _atomic_write_payload(path: Path, payload: bytes, *, context: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_regular_bytes(path, payload, context=context)


def _read_model(
    path: Path,
    model_type: type[_ModelT],
    *,
    require_canonical: bool = False,
    max_bytes: int = 8 * 1024 * 1024,
) -> _ModelT:
    try:
        return read_canonical_model(
            path,
            model_type,
            context=f"Web durable {model_type.__name__}",
            max_bytes=max_bytes,
        )
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"Web job record is unreadable: {path}") from exc


def _within(root: Path, path: Path) -> bool:
    resolved = path.expanduser().resolve()
    return resolved == root or root in resolved.parents


def _manifest_relative_file(
    *,
    workspace: Path,
    project_root: Path,
    relative: Any,
    role: str,
) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ConfigurationError(f"Bambu project {role} path must be relative")
    path = (project_root / relative).resolve()
    if not _within(workspace, path) or not _within(project_root, path):
        raise ConfigurationError(f"Bambu project {role} escapes the workflow workspace")
    if not path.is_file():
        raise ConfigurationError(f"Bambu project {role} is missing: {path}")
    return path


def _bambu_project_artifact_paths(workspace: Path, manifest_path: Path) -> dict[str, Path]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"Bambu project manifest is unreadable: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise ConfigurationError("Bambu project manifest root must be an object")
    if (
        manifest.get("schema_version") != "topoforge-bambu-tile-project-assembly-v1"
        or manifest.get("required_checks_passed") is not True
    ):
        raise ConfigurationError("Bambu project manifest has not passed its required checks")
    tiles = manifest.get("tiles")
    tile_count = manifest.get("tile_count")
    if (
        not isinstance(tiles, list)
        or not tiles
        or not isinstance(tile_count, int)
        or tile_count != len(tiles)
    ):
        raise ConfigurationError("Bambu project manifest has an invalid tile set")

    project_root = manifest_path.parent.resolve()
    resolved: dict[str, Path] = {}
    seen_tile_ids: set[str] = set()
    single_tile = tile_count == 1
    for tile in tiles:
        if not isinstance(tile, dict) or tile.get("required_checks_passed") is not True:
            raise ConfigurationError("Bambu project tile has not passed its required checks")
        tile_id = tile.get("tile_id")
        if not isinstance(tile_id, str):
            raise ConfigurationError("Bambu project manifest has an invalid tile id")
        parts = tile_id.split("-")
        if (
            len(parts) != 3
            or parts[0] != "tile"
            or len(parts[1]) != 5
            or not parts[1].startswith("r")
            or not parts[1][1:].isdigit()
            or len(parts[2]) != 5
            or not parts[2].startswith("c")
            or not parts[2][1:].isdigit()
            or tile_id in seen_tile_ids
        ):
            raise ConfigurationError("Bambu project manifest has an invalid tile id")
        seen_tile_ids.add(tile_id)
        files = tile.get("files")
        hashes = tile.get("sha256")
        if not isinstance(files, dict) or not isinstance(hashes, dict):
            raise ConfigurationError(f"Bambu project tile roles are invalid: {tile_id}")

        suffix = "" if single_tile else f"_{tile_id.replace('-', '_')}"
        project = _manifest_relative_file(
            workspace=workspace,
            project_root=project_root,
            relative=files.get("bambu_project_3mf"),
            role=f"{tile_id} project 3MF",
        )
        expected_project_hash = hashes.get("bambu_project_3mf")
        if (
            not isinstance(expected_project_hash, str)
            or sha256_file(project) != expected_project_hash
        ):
            raise ConfigurationError(f"Bambu project checksum mismatch: {tile_id}")
        resolved[f"bambu_project_3mf{suffix}"] = project

        validation = _manifest_relative_file(
            workspace=workspace,
            project_root=project_root,
            relative=tile.get("validation_path"),
            role=f"{tile_id} validation",
        )
        expected_validation_hash = tile.get("validation_sha256")
        if (
            not isinstance(expected_validation_hash, str)
            or sha256_file(validation) != expected_validation_hash
        ):
            raise ConfigurationError(f"Bambu project validation checksum mismatch: {tile_id}")
        resolved[f"bambu_project_validation{suffix}"] = validation
    return resolved


def _progress(expected: tuple[WorkflowStage, ...], ready: tuple[WorkflowStage, ...]) -> float:
    if not expected:
        return 0.0
    complete = sum(stage in ready for stage in expected)
    return min(0.99, complete / len(expected))


def _path_size_bytes(path: Path) -> int:
    """Measure one path without following symlinks outside its tree."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return 0
    if path.is_symlink() or not path.is_dir():
        return metadata.st_size
    return metadata.st_size + sum(_path_size_bytes(child) for child in path.iterdir())


def _strict_child_path(
    root: Path,
    path: Path,
    *,
    context: str,
    require_exists: bool,
) -> Path:
    """Return a lexical child after rejecting every existing link-like component."""
    lexical_root = _lexical_absolute(root)
    candidate = _lexical_absolute(path)
    if candidate == lexical_root or lexical_root not in candidate.parents:
        raise ConfigurationError(f"{context} is outside its configured root: {candidate}")
    try:
        if path_is_link_like(lexical_root):
            raise ConfigurationError(f"{context} root is link-like: {lexical_root}")
    except FileNotFoundError:
        raise ConfigurationError(f"{context} root is missing: {lexical_root}") from None
    current = lexical_root
    missing = False
    for part in candidate.relative_to(lexical_root).parts:
        current /= part
        if missing:
            continue
        try:
            if path_is_link_like(current):
                raise ConfigurationError(f"{context} contains a link-like component: {current}")
        except FileNotFoundError:
            missing = True
    if require_exists and missing:
        raise ConfigurationError(f"{context} is missing: {candidate}")
    return candidate


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _stable_stat_fields(result: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        result.st_mode,
        result.st_dev,
        result.st_ino,
        result.st_nlink,
        result.st_size,
        result.st_mtime_ns,
    )


def _deletion_inventory(root: Path, *, context: str) -> JobDeletionInventory:
    """Hash a no-follow regular-file tree and reject links or special entries."""
    records: list[dict[str, object]] = []
    total_size = 0

    def visit(path: Path, relative: str) -> None:
        nonlocal total_size
        try:
            before = path.lstat()
        except FileNotFoundError:
            raise ConfigurationError(f"{context} changed while it was inspected: {path}") from None
        if stat_result_is_link_like(before):
            raise ConfigurationError(f"{context} contains a link-like entry: {path}")
        if stat.S_ISDIR(before.st_mode):
            try:
                with os.scandir(path) as iterator:
                    children = sorted(
                        (Path(entry.path) for entry in iterator),
                        key=lambda item: item.name,
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
            if stat_result_is_link_like(after) or _stable_stat_fields(after) != (
                _stable_stat_fields(before)
            ):
                raise ConfigurationError(f"{context} changed while it was inspected: {path}")
            # Directory allocation size is filesystem- and parent-location-dependent;
            # keep the field canonical while the real stat size remains part of the
            # within-scan stability check above.
            records.append(
                {
                    "path": relative,
                    "kind": "directory",
                    "size_bytes": 0,
                    "modified_time_ns": before.st_mtime_ns,
                    "device": before.st_dev,
                    "inode": before.st_ino,
                    "link_count": before.st_nlink,
                }
            )
            return
        if not stat.S_ISREG(before.st_mode):
            raise ConfigurationError(f"{context} contains a special filesystem entry: {path}")
        if before.st_nlink != 1:
            raise ConfigurationError(f"{context} contains a hard-linked file: {path}")
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
                "size_bytes": before.st_size,
                "modified_time_ns": before.st_mtime_ns,
                "device": before.st_dev,
                "inode": before.st_ino,
                "link_count": before.st_nlink,
                "sha256": digest.hexdigest(),
            }
        )

    root_stat = root.lstat()
    if stat_result_is_link_like(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise ConfigurationError(f"{context} root must be a real directory: {root}")
    visit(root, ".")
    try:
        final_root_stat = root.lstat()
    except FileNotFoundError:
        raise ConfigurationError(f"{context} changed while it was inspected: {root}") from None
    if stat_result_is_link_like(final_root_stat) or _stable_stat_fields(
        final_root_stat
    ) != _stable_stat_fields(root_stat):
        raise ConfigurationError(f"{context} changed while it was inspected: {root}")
    ordered = sorted(records, key=lambda item: str(item["path"]))
    return JobDeletionInventory(
        size_bytes=total_size,
        entry_count=len(ordered),
        inventory_sha256=hashlib.sha256(_canonical_bytes({"entries": ordered})).hexdigest(),
        device=root_stat.st_dev,
        inode=root_stat.st_ino,
        link_count=root_stat.st_nlink,
        modified_time_ns=root_stat.st_mtime_ns,
    )


def _remove_tree_no_follow(root: Path, *, context: str) -> None:
    """Remove a possibly partial real directory tree without traversing links."""
    try:
        root_metadata = root.lstat()
    except FileNotFoundError:
        return
    if stat_result_is_link_like(root_metadata) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ConfigurationError(f"{context} root must be a real directory: {root}")
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise ConfigurationError(f"{context} directory is unreadable: {root}") from exc
    for child in children:
        try:
            metadata = child.lstat()
        except FileNotFoundError:
            continue
        if stat_result_is_link_like(metadata):
            raise ConfigurationError(f"{context} contains a link-like entry: {child}")
        if stat.S_ISDIR(metadata.st_mode):
            _remove_tree_no_follow(child, context=context)
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ConfigurationError(f"{context} contains an unsafe entry: {child}")
        child.unlink()
    final_metadata = root.lstat()
    if (
        stat_result_is_link_like(final_metadata)
        or not stat.S_ISDIR(final_metadata.st_mode)
        or final_metadata.st_dev != root_metadata.st_dev
        or final_metadata.st_ino != root_metadata.st_ino
    ):
        raise ConfigurationError(f"{context} root changed during removal: {root}")
    root.rmdir()


def _purge_manifest(
    root: Path,
    *,
    root_name: str,
    context: str,
) -> tuple[JobTrashPurgeEntry, ...]:
    """Build a deepest-first, no-follow deletion manifest for one real tree."""
    entries: list[JobTrashPurgeEntry] = []

    def append_entry(entry: JobTrashPurgeEntry) -> None:
        entries.append(entry)
        if len(entries) > _MAX_PURGE_MANIFEST_ENTRIES:
            raise ConfigurationError(
                "trash purge manifest exceeds the 20,000-entry safety limit; "
                "restore the batch and purge smaller batches, or inspect and remove "
                "the retained quarantine manually"
            )

    def visit(path: Path) -> None:
        try:
            before = path.lstat()
        except FileNotFoundError:
            raise ConfigurationError(f"{context} changed during manifest creation") from None
        if stat_result_is_link_like(before):
            raise ConfigurationError(f"{context} contains a link-like entry: {path}")
        relative = "." if path == root else path.relative_to(root).as_posix()
        if stat.S_ISDIR(before.st_mode):
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child)
            after = path.lstat()
            if (
                stat_result_is_link_like(after)
                or after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or after.st_mtime_ns != before.st_mtime_ns
            ):
                raise ConfigurationError(f"{context} changed during manifest creation")
            append_entry(
                JobTrashPurgeEntry(
                    root=root_name,
                    relative_path=relative,
                    kind="directory",
                    size_bytes=0,
                    device=before.st_dev,
                    inode=before.st_ino,
                    link_count=before.st_nlink,
                    modified_time_ns=before.st_mtime_ns,
                )
            )
            return
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ConfigurationError(f"{context} contains an unsafe entry: {path}")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if _stable_stat_fields(opened) != _stable_stat_fields(before):
                raise ConfigurationError(f"{context} changed while a file was opened")
            digest = hashlib.sha256()
            while block := os.read(descriptor, 1024 * 1024):
                digest.update(block)
            finished = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = path.lstat()
        if (
            stat_result_is_link_like(after)
            or _stable_stat_fields(finished) != _stable_stat_fields(before)
            or _stable_stat_fields(after) != _stable_stat_fields(before)
        ):
            raise ConfigurationError(f"{context} changed during manifest creation")
        append_entry(
            JobTrashPurgeEntry(
                root=root_name,
                relative_path=relative,
                kind="file",
                size_bytes=before.st_size,
                sha256=digest.hexdigest(),
                device=before.st_dev,
                inode=before.st_ino,
                link_count=before.st_nlink,
                modified_time_ns=before.st_mtime_ns,
            )
        )

    visit(root)
    return tuple(
        sorted(
            entries,
            key=lambda item: (
                -len(PurePosixPath(item.relative_path).parts),
                item.kind == "directory",
                item.root,
                item.relative_path,
            ),
        )
    )


class LocalJobManager:
    """Run, recover, cancel, and inspect local workflows in child processes."""

    def __init__(self, config: WebAppConfig) -> None:
        self.config = config.resolved()
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._slicer_probes: dict[str, SlicerInfo] = {}
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._lease: WebManagerLease | None = None
        self._owned_root_identities: dict[Path, tuple[int, int]] = {}

    @property
    def jobs_dir(self) -> Path:
        """Return the durable job record directory."""
        return self.config.state_dir / "jobs"

    @property
    def backups_dir(self) -> Path:
        """Return the adapter-owned verified workflow backup directory."""
        return self.config.state_dir / "backups"

    @property
    def map_tiles_dir(self) -> Path:
        """Return the adapter-owned derived map-tile cache root."""
        return self.config.state_dir / "map-tiles"

    @property
    def trash_dir(self) -> Path:
        """Return the durable state root for recoverable task batches."""
        return self.config.state_dir / "trash"

    @property
    def deletion_audit_dir(self) -> Path:
        """Return the append-only result root for completed trash actions."""
        return self.config.state_dir / "deletion-audit"

    @property
    def trash_transactions_dir(self) -> Path:
        """Return the durable intent root for interrupted trash operations."""
        return self.config.state_dir / "trash-transactions"

    @property
    def workspace_trash_dir(self) -> Path:
        """Return the same-filesystem quarantine root below the workspace root."""
        return self.config.workspace_root / ".topoforge-trash"

    @property
    def manager_lease_path(self) -> Path:
        """Return the stable lifetime-lock file, which is never removed or replaced."""
        return self.config.state_dir / "manager.lock"

    def _state_owned_roots(self) -> tuple[Path, ...]:
        return (
            self.jobs_dir,
            self.backups_dir,
            self.map_tiles_dir,
            self.trash_dir,
            self.deletion_audit_dir,
            self.trash_transactions_dir,
        )

    def _owned_root_paths(self) -> tuple[Path, ...]:
        return (
            self.config.state_dir,
            self.config.workspace_root,
            *self._state_owned_roots(),
            self.workspace_trash_dir,
        )

    def _initialize_owned_roots(self) -> None:
        state_identity = self._owned_root_identities.get(self.config.state_dir)
        if state_identity is None:
            raise ConfigurationError("Web state root identity was not established before startup")
        workspace_identity = ensure_real_directory_tree(
            self.config.workspace_root,
            context="Web workspace root",
        )
        self._owned_root_identities[self.config.workspace_root] = workspace_identity
        for path in self._state_owned_roots():
            create_owned_directory(
                path,
                root=self.config.state_dir,
                root_identity=state_identity,
                context="Web adapter-owned state root",
                exist_ok=True,
            )
            self._owned_root_identities[path] = owned_directory_identity(
                path,
                root=self.config.state_dir,
                root_identity=state_identity,
                context="Web adapter-owned state root",
            )
        create_owned_directory(
            self.workspace_trash_dir,
            root=self.config.workspace_root,
            root_identity=workspace_identity,
            context="Web workspace quarantine root",
            exist_ok=True,
        )
        self._owned_root_identities[self.workspace_trash_dir] = owned_directory_identity(
            self.workspace_trash_dir,
            root=self.config.workspace_root,
            root_identity=workspace_identity,
            context="Web workspace quarantine root",
        )

    def _validate_owned_roots(self) -> None:
        if self._lease is None or not self._owned_root_identities:
            raise ConfigurationError(
                "Web manager does not own its state roots; call start() before maintenance"
            )
        for path, expected in self._owned_root_identities.items():
            try:
                if path in {self.config.state_dir, self.config.workspace_root}:
                    observed = real_directory_tree_identity(
                        path,
                        context="Web adapter-owned root",
                    )
                else:
                    parent = (
                        self.config.workspace_root
                        if path == self.workspace_trash_dir
                        else self.config.state_dir
                    )
                    observed = owned_directory_identity(
                        path,
                        root=parent,
                        root_identity=self._owned_root_identities[parent],
                        context="Web adapter-owned root",
                    )
            except (OSError, ValueError) as exc:
                raise ConfigurationError(
                    f"Web adapter-owned root is missing or unsafe: {path}"
                ) from exc
            if observed != expected:
                raise ConfigurationError(
                    f"Web adapter-owned root identity changed during manager lifetime: {path}"
                )
        locked_metadata = os.fstat(self._lease.descriptor)
        try:
            with open_owned_regular_binary(
                self.manager_lease_path,
                root=self.config.state_dir,
                root_identity=self._owned_root_identities[self.config.state_dir],
                context="Web manager lease validation",
                expected_identity=(locked_metadata.st_dev, locked_metadata.st_ino),
            ) as lease_stream:
                path_metadata = os.fstat(lease_stream.fileno())
        except (OSError, ValueError) as exc:
            raise ConfigurationError("Web manager lease path changed or became unsafe") from exc
        if (path_metadata.st_dev, path_metadata.st_ino) != (
            locked_metadata.st_dev,
            locked_metadata.st_ino,
        ):
            raise ConfigurationError("Web manager lease path changed or became unsafe")

    def _validate_if_owned(self) -> None:
        """Revalidate roots before an operation when this manager has been started."""
        if self._lease is not None:
            self._validate_owned_roots()

    def _owned_identity(self, root: Path) -> tuple[int, int]:
        """Return one manager-owned root identity after lifetime revalidation."""
        self._validate_owned_roots()
        candidate = _lexical_absolute(root)
        try:
            return self._owned_root_identities[candidate]
        except KeyError as exc:
            raise ConfigurationError(
                f"filesystem operation used an unowned root: {candidate}"
            ) from exc

    def _create_owned_directory(
        self,
        path: Path,
        *,
        root: Path,
        context: str,
        exist_ok: bool = False,
    ) -> None:
        try:
            create_owned_directory(
                path,
                root=root,
                root_identity=self._owned_identity(root),
                context=context,
                exist_ok=exist_ok,
            )
        except FileExistsError:
            raise
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"{context} could not be created safely: {path}") from exc

    def _move_owned_directory(
        self,
        source: Path,
        destination: Path,
        *,
        source_root: Path,
        destination_root: Path,
        inventory: JobDeletionInventory,
        context: str,
    ) -> None:
        try:
            move_owned_path(
                source,
                destination,
                source_root=source_root,
                source_root_identity=self._owned_identity(source_root),
                destination_root=destination_root,
                destination_root_identity=self._owned_identity(destination_root),
                expected_identity=(inventory.device, inventory.inode),
                directory=True,
                context=context,
            )
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"{context} could not be moved safely") from exc

    def _remove_owned_entry(
        self,
        path: Path,
        *,
        root: Path,
        metadata: os.stat_result,
        directory: bool,
        context: str,
        missing_ok: bool = False,
    ) -> None:
        try:
            remove_owned_path(
                path,
                root=root,
                root_identity=self._owned_identity(root),
                expected_identity=(int(metadata.st_dev), int(metadata.st_ino)),
                directory=directory,
                context=context,
                missing_ok=missing_ok,
            )
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"{context} could not be removed safely: {path}") from exc

    def _write_owned_payload(
        self,
        path: Path,
        payload: bytes,
        *,
        root: Path,
        context: str,
        replace: bool = True,
    ) -> None:
        try:
            atomic_write_owned_regular_bytes(
                path,
                payload,
                root=root,
                root_identity=self._owned_identity(root),
                context=context,
                replace=replace,
            )
        except FileExistsError:
            raise
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"{context} could not be written safely: {path}") from exc

    def _write_owned_model(
        self,
        path: Path,
        value: BaseModel | dict[str, Any],
        *,
        root: Path,
        context: str,
        replace: bool = True,
    ) -> None:
        self._write_owned_payload(
            path,
            _canonical_bytes(value),
            root=root,
            context=context,
            replace=replace,
        )

    def _read_owned_payload(
        self,
        path: Path,
        *,
        root: Path,
        context: str,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> bytes:
        try:
            return read_owned_regular_bytes(
                path,
                root=root,
                root_identity=self._owned_identity(root),
                context=context,
                max_bytes=max_bytes,
            )
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"{context} is unreadable: {path}") from exc

    def _read_owned_model(
        self,
        path: Path,
        model_type: type[_ModelT],
        *,
        root: Path,
        context: str,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> _ModelT:
        payload = self._read_owned_payload(
            path,
            root=root,
            context=context,
            max_bytes=max_bytes,
        )
        try:
            result = model_type.model_validate_json(payload)
        except ValueError as exc:
            raise ConfigurationError(f"{context} is invalid: {path}") from exc
        if payload != _canonical_bytes(result):
            raise ConfigurationError(f"{context} is not canonical: {path}")
        return result

    def _read_optional_owned_payload(
        self,
        path: Path,
        *,
        root: Path,
        context: str,
        max_bytes: int,
    ) -> bytes | None:
        """Read one optional owned file while distinguishing absence from unsafe state."""
        try:
            return read_owned_regular_bytes(
                path,
                root=root,
                root_identity=self._owned_identity(root),
                context=context,
                max_bytes=max_bytes,
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"{context} is unsafe or unreadable: {path}") from exc

    def _read_optional_owned_model(
        self,
        path: Path,
        model_type: type[_ModelT],
        *,
        root: Path,
        context: str,
        max_bytes: int,
    ) -> _ModelT | None:
        payload = self._read_optional_owned_payload(
            path,
            root=root,
            context=context,
            max_bytes=max_bytes,
        )
        if payload is None:
            return None
        try:
            result = model_type.model_validate_json(payload)
        except ValueError as exc:
            raise ConfigurationError(f"{context} is invalid: {path}") from exc
        if payload != _canonical_bytes(result):
            raise ConfigurationError(f"{context} is not canonical: {path}")
        return result

    def open_owned_download(
        self,
        path: Path,
        *,
        root: Path,
        expected_sha256: str,
        expected_size: int | None,
        context: str,
    ) -> VerifiedFileDownload:
        """Open, hash, rewind, and retain one file from a manager-owned root."""
        stream_context = open_owned_regular_binary(
            path,
            root=root,
            root_identity=self._owned_identity(root),
            context=context,
        )
        stream: BinaryIO | None = None
        try:
            stream = stream_context.__enter__()
            before = os.fstat(stream.fileno())
            if expected_size is not None and before.st_size != expected_size:
                raise ConfigurationError(f"{context} byte count changed after publication: {path}")
            digest = hashlib.sha256()
            while block := stream.read(1024 * 1024):
                digest.update(block)
            finished = os.fstat(stream.fileno())
            if _stable_stat_fields(finished) != _stable_stat_fields(before):
                raise ConfigurationError(f"{context} changed while it was verified: {path}")
            observed_sha256 = digest.hexdigest()
            if observed_sha256 != expected_sha256:
                raise ConfigurationError(f"{context} checksum changed after publication")
            stream.seek(0)
            return VerifiedFileDownload(
                stream=stream,
                size_bytes=before.st_size,
                sha256=observed_sha256,
                _context=stream_context,
            )
        except BaseException:
            stream_context.__exit__(*sys.exc_info())
            raise

    def read_workspace_file(
        self,
        path: Path,
        *,
        expected_sha256: str,
        max_bytes: int,
        context: str,
    ) -> bytes:
        """Read one bounded checksum-bound workspace file from its pinned handle."""
        download = self.open_owned_download(
            path,
            root=self.config.workspace_root,
            expected_sha256=expected_sha256,
            expected_size=None,
            context=context,
        )
        try:
            if download.size_bytes > max_bytes:
                raise ConfigurationError(f"{context} exceeds the {max_bytes}-byte safety limit")
            payload = download.stream.read(max_bytes + 1)
            if len(payload) != download.size_bytes:
                raise ConfigurationError(f"{context} changed while it was read")
            return payload
        finally:
            download.close()

    def ensure_owned_directory_tree(
        self,
        path: Path,
        *,
        root: Path,
        context: str,
    ) -> None:
        """Create each missing directory below one identity-bound owned root."""
        lexical_root = _lexical_absolute(root)
        candidate = _lexical_absolute(path)
        try:
            parts = candidate.relative_to(lexical_root).parts
        except ValueError as exc:
            raise ConfigurationError(f"{context} escapes its owned root") from exc
        self._owned_identity(lexical_root)
        current = lexical_root
        for part in parts:
            if part in {"", ".", ".."}:
                raise ConfigurationError(f"{context} has an unsafe directory component")
            current /= part
            self._create_owned_directory(
                current,
                root=lexical_root,
                context=context,
                exist_ok=True,
            )

    def read_optional_owned_file(
        self,
        path: Path,
        *,
        root: Path,
        context: str,
        max_bytes: int,
    ) -> bytes | None:
        """Read one optional bounded file below an identity-bound owned root."""
        return self._read_optional_owned_payload(
            path,
            root=root,
            context=context,
            max_bytes=max_bytes,
        )

    def write_owned_file(
        self,
        path: Path,
        payload: bytes,
        *,
        root: Path,
        context: str,
        replace: bool = True,
    ) -> None:
        """Atomically publish bytes below one identity-bound owned root."""
        self._write_owned_payload(
            path,
            payload,
            root=root,
            context=context,
            replace=replace,
        )

    def _slicer_adapter(self, name: str) -> SlicerAdapter:
        if name == "bambu-studio":
            return BambuStudioAdapter(self.config.bambu_studio_executable)
        if name == "orca":
            return OrcaSlicerAdapter()
        if name == "prusa":
            return PrusaSlicerAdapter()
        if name == "auto":
            return select_slicer(bambu_executable=self.config.bambu_studio_executable)
        raise ConfigurationError(f"unsupported slicer name: {name}")

    def probe_slicer(self, name: str, *, refresh: bool = False) -> SlicerInfo:
        """Return one cached executable probe used by validation and workers."""
        with self._lock:
            if not refresh and name in self._slicer_probes:
                return self._slicer_probes[name]
            probe = self._slicer_adapter(name).probe(refresh=refresh)
            self._slicer_probes[name] = probe
            return probe

    def validate_request(
        self,
        request: JobCreateRequest,
    ) -> tuple[JobCreateRequest, SlicerInfo | None]:
        """Normalize one launch and reject unavailable external slicers before queueing."""
        workspace = _lexical_absolute(request.launch.workspace_dir)
        workspace_root = _lexical_absolute(self.config.workspace_root)
        if workspace == workspace_root or workspace_root not in workspace.parents:
            raise ConfigurationError(f"workspace must be a child of {self.config.workspace_root}")
        if workspace_root.exists() or workspace_root.is_symlink():
            workspace = _strict_child_path(
                workspace_root,
                workspace,
                context="workflow workspace",
                require_exists=False,
            )
        launch = request.launch
        if launch.slicing_enabled and launch.slicer_name == "bambu-studio":
            settings = launch.slicer_settings
            filaments = launch.slicer_filaments
            configured_profiles = (
                self.config.bambu_machine_profile,
                self.config.bambu_process_profile,
                self.config.bambu_filament_profile,
            )
            if not settings and all(path is not None for path in configured_profiles):
                settings = (
                    self.config.bambu_machine_profile,
                    self.config.bambu_process_profile,
                )
                filaments = (self.config.bambu_filament_profile,)
            if (
                len(settings) != 2
                or len(filaments) != 1
                or any(path is None for path in (*settings, *filaments))
            ):
                raise ConfigurationError(
                    "Bambu Studio slicing requires one machine, one process, and one "
                    "filament profile. Restart topoforge web with "
                    "--bambu-machine-profile, --bambu-process-profile, and "
                    "--bambu-filament-profile."
                )
            launch = launch.model_copy(
                update={
                    "slicer_settings": settings,
                    "slicer_filaments": filaments,
                }
            )
        slicer: SlicerInfo | None = None
        if launch.slicing_enabled:
            slicer = self.probe_slicer(launch.slicer_name)
            if slicer.status is not SlicerAvailability.AVAILABLE:
                detail = slicer.detail or "the version probe did not succeed"
                option = (
                    " Restart topoforge web with --bambu-studio-executable PATH."
                    if launch.slicer_name == "bambu-studio"
                    else ""
                )
                raise ConfigurationError(f"{slicer.name} is not ready: {detail}.{option}")
        normalized_launch = launch.model_copy(
            update={
                "workspace_dir": workspace,
                "build": launch.build.model_copy(update={"output_dir": workspace}),
            }
        )
        return JobCreateRequest(launch=normalized_launch), slicer

    def start(self) -> None:
        """Create roots, reconcile retained records, and start background polling."""
        with self._lock:
            if self._lease is None:
                try:
                    state_identity = ensure_real_directory_tree(
                        self.config.state_dir,
                        context="Web state root",
                    )
                except (OSError, ValueError) as exc:
                    raise ConfigurationError(
                        "Web state root must be a real directory and could not be created "
                        f"safely: {self.config.state_dir}"
                    ) from exc
                self._owned_root_identities = {self.config.state_dir: state_identity}
                manager_pid = os.getpid()
                manager_identity = inspect_process_identity(manager_pid)
                if manager_identity is None:
                    self._owned_root_identities.clear()
                    raise ConfigurationError(
                        "the operating system did not expose a stable Web manager identity"
                    )
                try:
                    self._lease = WebManagerLease.acquire(
                        self.manager_lease_path,
                        {
                            "schema_version": "topoforge-web-manager-lease-v1",
                            "manager_nonce": uuid4().hex,
                            "pid": manager_pid,
                            "process_identity": manager_identity,
                            "started_at": utc_now().isoformat(),
                        },
                        root=self.config.state_dir,
                        root_identity=state_identity,
                    )
                except RuntimeError as exc:
                    self._owned_root_identities.clear()
                    raise ConfigurationError(str(exc)) from exc
            try:
                try:
                    self._initialize_owned_roots()
                except (OSError, ValueError) as exc:
                    raise ConfigurationError(
                        "Each Web adapter-owned root must be a real directory and could not "
                        "be initialized safely"
                    ) from exc
                self._validate_owned_roots()
                self._recover_trash_transactions()
                self._recover_trash_action_transactions()
                self.refresh()
            except BaseException:
                lease = self._lease
                self._lease = None
                self._owned_root_identities.clear()
                if lease is not None:
                    lease.release()
                raise
            if self._monitor is None or not self._monitor.is_alive():
                self._stop.clear()
                self._monitor = threading.Thread(
                    target=self._monitor_loop,
                    name="topoforge-web-jobs",
                    daemon=True,
                )
                self._monitor.start()

    def close(self) -> None:
        """Stop polling while leaving isolated workflows running for recovery."""
        self._stop.set()
        monitor = self._monitor
        if monitor is not None:
            monitor.join(timeout=5)
        self._monitor = None
        with self._lock:
            lease = self._lease
            self._lease = None
            self._owned_root_identities.clear()
            if lease is not None:
                lease.release()

    def _monitor_loop(self) -> None:
        while not self._stop.wait(self.config.poll_interval_seconds):
            try:
                self.refresh()
            except Exception:
                _LOGGER.exception("Web job monitor refresh failed")
                continue

    def _job_dir(self, job_id: str) -> Path:
        if (
            len(job_id) != 32
            or not job_id
            or any(character not in "0123456789abcdef" for character in job_id)
        ):
            raise KeyError(job_id)
        return self.jobs_dir / job_id

    @staticmethod
    def _validate_batch_id(batch_id: str) -> str:
        if len(batch_id) != 32 or any(
            character not in "0123456789abcdef" for character in batch_id
        ):
            raise KeyError(batch_id)
        return batch_id

    def _trash_batch_dir(self, batch_id: str) -> Path:
        return self.trash_dir / self._validate_batch_id(batch_id)

    def _workspace_trash_batch_dir(self, batch_id: str) -> Path:
        return self.workspace_trash_dir / self._validate_batch_id(batch_id)

    def _trash_record_path(self, batch_id: str) -> Path:
        return self._trash_batch_dir(batch_id) / "trash.json"

    def _trash_transaction_dir(self, batch_id: str) -> Path:
        return self.trash_transactions_dir / self._validate_batch_id(batch_id)

    def _trash_transaction_path(self, batch_id: str) -> Path:
        return self._trash_transaction_dir(batch_id) / "transaction.json"

    def _trash_action_path(self, batch_id: str) -> Path:
        return self._trash_transaction_dir(batch_id) / "action.json"

    def _state_purging_path(self, batch_id: str) -> Path:
        return self.trash_dir / f".{self._validate_batch_id(batch_id)}.purging"

    def _workspace_purging_path(self, batch_id: str) -> Path:
        return self.workspace_trash_dir / f".{self._validate_batch_id(batch_id)}.purging"

    @staticmethod
    def _trash_audit_payload(
        record: JobTrashRecord,
        *,
        action: str,
        affected_bytes: int,
        occurred_at: object,
    ) -> dict[str, object]:
        return {
            "schema_version": "topoforge-web-job-trash-audit-v1",
            "batch_id": record.batch_id,
            "plan_id": record.plan_id,
            "action": action,
            "occurred_at": occurred_at,
            "job_ids": record.job_ids,
            "affected_bytes": affected_bytes,
            "backup_ids": record.backup_ids,
            "backups_preserved": True,
            "required_checks_passed": True,
        }

    def _validate_trash_action(
        self,
        transaction: JobTrashActionTransaction,
    ) -> JobTrashActionTransaction:
        batch_id = self._validate_batch_id(transaction.batch_id)
        record = transaction.trash_record
        if record.batch_id != batch_id:
            raise ConfigurationError("trash action batch does not match its embedded record")
        if record.schema_version != "topoforge-web-job-trash-v2":
            raise ConfigurationError("trash action requires an inventory-bound v2 record")
        if hashlib.sha256(_canonical_bytes(record)).hexdigest() != transaction.record_sha256:
            raise ConfigurationError("trash action embedded record hash does not match")
        label = "restored" if transaction.action is JobTrashActionKind.RESTORE else "purged"
        expected_paths = (
            (transaction.state_batch, self._trash_batch_dir(batch_id)),
            (transaction.workspace_batch, self._workspace_trash_batch_dir(batch_id)),
            (transaction.state_purging, self._state_purging_path(batch_id)),
            (transaction.workspace_purging, self._workspace_purging_path(batch_id)),
            (
                transaction.audit_path,
                self.deletion_audit_dir / f"{batch_id}-{label}.json",
            ),
        )
        if any(not self._same_path(actual, expected) for actual, expected in expected_paths):
            raise ConfigurationError("trash action contains an unexpected derived path")
        state_identity = self._owned_root_identities.get(self.config.state_dir)
        workspace_identity = self._owned_root_identities.get(self.config.workspace_root)
        if state_identity != (
            transaction.state_root_device,
            transaction.state_root_inode,
        ) or workspace_identity != (
            transaction.workspace_root_device,
            transaction.workspace_root_inode,
        ):
            raise ConfigurationError("trash action configured root identity changed")
        audit_payload = self._trash_audit_payload(
            record,
            action=label,
            affected_bytes=transaction.affected_bytes,
            occurred_at=transaction.created_at.isoformat(),
        )
        if hashlib.sha256(_canonical_bytes(audit_payload)).hexdigest() != (
            transaction.audit_sha256
        ):
            raise ConfigurationError("trash action terminal audit identity changed")
        purge_keys = tuple((entry.root, entry.relative_path) for entry in transaction.purge_entries)
        if transaction.action is JobTrashActionKind.PURGE:
            if (
                not purge_keys
                or len(set(purge_keys)) != len(purge_keys)
                or transaction.purge_index > len(purge_keys)
            ):
                raise ConfigurationError("trash purge manifest or cursor is invalid")
        elif transaction.purge_entries or transaction.purge_index != 0:
            raise ConfigurationError("trash restore must not contain a purge manifest")
        return transaction

    @staticmethod
    def _check_trash_action_size(transaction: JobTrashActionTransaction) -> None:
        if len(_canonical_bytes(transaction)) > _MAX_TRASH_ACTION_BYTES:
            raise ConfigurationError(
                "trash action transaction exceeds the 8 MiB safety limit; restore "
                "the batch and purge smaller batches, or inspect and remove the "
                "retained quarantine manually"
            )

    def _read_trash_action(self, batch_id: str) -> JobTrashActionTransaction:
        path = self._trash_action_path(batch_id)
        _strict_child_path(
            self.trash_transactions_dir,
            path,
            context="trash action transaction",
            require_exists=True,
        )
        return self._validate_trash_action(
            self._read_owned_model(
                path,
                JobTrashActionTransaction,
                root=self.trash_transactions_dir,
                context="trash action transaction",
                max_bytes=_MAX_TRASH_ACTION_BYTES,
            )
        )

    def _read_trash_record(
        self,
        batch_id: str,
        *,
        expected_path: Path | None = None,
    ) -> JobTrashRecord:
        validated_batch_id = self._validate_batch_id(batch_id)
        derived_path = _lexical_absolute(self._trash_record_path(validated_batch_id))
        path = derived_path if expected_path is None else _lexical_absolute(expected_path)
        if path != derived_path:
            raise ConfigurationError("trash record path does not match the requested batch")
        _strict_child_path(
            self.trash_dir,
            path,
            context="trash record",
            require_exists=True,
        )
        try:
            payload = self._read_owned_payload(
                path,
                root=self.trash_dir,
                context="job trash record",
                max_bytes=8 * 1024 * 1024,
            )
            envelope = json.loads(payload)
        except (OSError, ValueError, ConfigurationError) as exc:
            raise ConfigurationError(f"job trash record is unreadable: {path}") from exc
        schema_version = envelope.get("schema_version") if isinstance(envelope, dict) else None
        if schema_version == "topoforge-web-job-trash-v1":
            raise ConfigurationError(
                "legacy trash v1 is preserved but cannot be restored or purged automatically; "
                "export it for manual inventory verification and migration to trash v2"
            )
        if schema_version != "topoforge-web-job-trash-v2":
            raise ConfigurationError("job trash record has an unsupported schema version")
        try:
            record = JobTrashRecord.model_validate_json(payload)
        except ValueError as exc:
            raise ConfigurationError(f"job trash record is invalid: {path}") from exc
        if payload != _canonical_bytes(record):
            raise ConfigurationError(f"job trash record is not canonical: {path}")
        return self._verify_trash_record(
            record,
            expected_batch_id=validated_batch_id,
            expected_path=path,
        )

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        return _lexical_absolute(left) == _lexical_absolute(right)

    def _validate_trash_transaction(
        self,
        transaction: JobTrashTransaction,
    ) -> JobTrashTransaction:
        batch_id = self._validate_batch_id(transaction.batch_id)
        expected_paths = (
            (transaction.state_temporary, self.trash_dir / f".{batch_id}.creating"),
            (transaction.state_destination, self._trash_batch_dir(batch_id)),
            (
                transaction.workspace_temporary,
                self.workspace_trash_dir / f".{batch_id}.creating",
            ),
            (
                transaction.workspace_destination,
                self._workspace_trash_batch_dir(batch_id),
            ),
        )
        if any(not self._same_path(actual, expected) for actual, expected in expected_paths):
            raise ConfigurationError("trash transaction contains an unexpected batch path")
        if transaction.trash_record.batch_id != batch_id:
            raise ConfigurationError("trash transaction batch identity does not match its record")

        expected_job_ids = set(transaction.trash_record.job_ids)
        actual_job_ids: set[str] = set()
        for move in transaction.job_moves:
            job_id = move.source.name
            self._job_dir(job_id)
            expected = JobTrashTransactionMove(
                source=self._job_dir(job_id),
                temporary=transaction.state_temporary / "jobs" / job_id,
                destination=transaction.state_destination / "jobs" / job_id,
            )
            if move != expected:
                raise ConfigurationError("trash transaction contains an unexpected job path")
            actual_job_ids.add(job_id)
        if actual_job_ids != expected_job_ids or len(actual_job_ids) != len(transaction.job_moves):
            raise ConfigurationError("trash transaction job identities do not match its record")

        expected_workspace_moves = {
            (
                _lexical_absolute(workspace.original_workspace),
                _lexical_absolute(workspace.quarantined_workspace),
            )
            for workspace in transaction.trash_record.workspaces
            if workspace.quarantined_workspace is not None
        }
        actual_workspace_moves: set[tuple[Path, Path]] = set()
        workspace_root = _lexical_absolute(self.config.workspace_root)
        for move in transaction.workspace_moves:
            source = _lexical_absolute(move.source)
            temporary = _lexical_absolute(move.temporary)
            destination = _lexical_absolute(move.destination)
            if source == workspace_root or workspace_root not in source.parents:
                raise ConfigurationError("trash transaction workspace escaped its configured root")
            if (
                _lexical_absolute(transaction.workspace_temporary) not in temporary.parents
                or _lexical_absolute(transaction.workspace_destination) not in destination.parents
                or temporary.relative_to(_lexical_absolute(transaction.workspace_temporary))
                != destination.relative_to(_lexical_absolute(transaction.workspace_destination))
            ):
                raise ConfigurationError(
                    "trash transaction contains an unexpected workspace quarantine path"
                )
            actual_workspace_moves.add((source, destination))
        if actual_workspace_moves != expected_workspace_moves or len(actual_workspace_moves) != len(
            transaction.workspace_moves
        ):
            raise ConfigurationError(
                "trash transaction workspaces do not match its recovery record"
            )
        return transaction

    @staticmethod
    def _transaction_move_location(move: JobTrashTransactionMove) -> Path:
        locations: list[Path] = []
        for path in (move.source, move.temporary, move.destination):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if stat_result_is_link_like(metadata):
                raise ConfigurationError(
                    f"trash transaction moved path must not be link-like: {path}"
                )
            locations.append(path)
        if len(locations) != 1:
            raise ConfigurationError(
                "trash transaction recovery requires exactly one copy of every moved path"
            )
        return locations[0]

    def _transaction_move_inventory(
        self,
        transaction: JobTrashTransaction,
        move: JobTrashTransactionMove,
    ) -> JobDeletionInventory:
        job_inventories = {
            item.job_id: item.inventory for item in transaction.trash_record.job_inventories
        }
        if move.source.parent == self.jobs_dir and move.source.name in job_inventories:
            return job_inventories[move.source.name]
        for workspace in transaction.trash_record.workspaces:
            if workspace.inventory is not None and _lexical_absolute(
                workspace.original_workspace
            ) == _lexical_absolute(move.source):
                return workspace.inventory
        raise ConfigurationError("trash transaction move has no bound inventory")

    def _remove_trash_transaction(self, transaction: JobTrashTransaction) -> None:
        path = self._trash_transaction_path(transaction.batch_id)
        metadata = path.lstat()
        self._remove_owned_entry(
            path,
            root=self.trash_transactions_dir,
            metadata=metadata,
            directory=False,
            context="trash apply transaction",
        )
        try:
            parent_metadata = path.parent.lstat()
        except FileNotFoundError:
            return
        if any(path.parent.iterdir()):
            return
        self._remove_owned_entry(
            path.parent,
            root=self.trash_transactions_dir,
            metadata=parent_metadata,
            directory=True,
            context="trash apply transaction directory",
        )

    def _rollback_trash_transaction(self, transaction: JobTrashTransaction) -> None:
        if self._lease is not None:
            self._validate_owned_roots()
        for move in reversed((*transaction.job_moves, *transaction.workspace_moves)):
            current = self._transaction_move_location(move)
            expected_inventory = self._transaction_move_inventory(transaction, move)
            if (
                _deletion_inventory(current, context="trash transaction rollback")
                != expected_inventory
            ):
                if current == move.source:
                    continue
                raise ConfigurationError(
                    f"trash transaction path changed before rollback: {current}"
                )
            if current != move.source:
                if self._lease is not None:
                    self._validate_owned_roots()
                if move.source.parent == self.jobs_dir:
                    source_root = self.trash_dir
                    destination_root = self.jobs_dir
                else:
                    source_root = self.workspace_trash_dir
                    destination_root = self.config.workspace_root
                self._move_owned_directory(
                    current,
                    move.source,
                    source_root=source_root,
                    destination_root=destination_root,
                    inventory=expected_inventory,
                    context="trash transaction rollback",
                )
                if (
                    _deletion_inventory(move.source, context="trash rollback destination")
                    != expected_inventory
                ):
                    raise ConfigurationError(
                        f"trash transaction identity changed during rollback: {move.source}"
                    )
        for record_path in (
            transaction.state_temporary / "trash.json",
            transaction.state_destination / "trash.json",
        ):
            try:
                record_metadata = record_path.lstat()
            except FileNotFoundError:
                continue
            self._remove_owned_entry(
                record_path,
                root=self.trash_dir,
                metadata=record_metadata,
                directory=False,
                context="trash rollback record",
            )
        for path in (
            transaction.state_temporary / "jobs",
            transaction.state_temporary,
            transaction.state_destination / "jobs",
            transaction.state_destination,
            transaction.workspace_temporary,
            transaction.workspace_destination,
        ):
            try:
                directory_metadata = path.lstat()
            except FileNotFoundError:
                continue
            self._remove_owned_entry(
                path,
                root=(
                    self.workspace_trash_dir
                    if path in {transaction.workspace_temporary, transaction.workspace_destination}
                    else self.trash_dir
                ),
                metadata=directory_metadata,
                directory=True,
                context="trash rollback directory",
            )
        self._remove_trash_transaction(transaction)

    def _publish_trash_transaction(
        self,
        transaction: JobTrashTransaction,
    ) -> JobTrashRecord:
        for move in (*transaction.job_moves, *transaction.workspace_moves):
            current = self._transaction_move_location(move)
            if _deletion_inventory(
                current, context="trash transaction publication"
            ) != self._transaction_move_inventory(transaction, move):
                raise ConfigurationError(
                    f"trash transaction path changed before publication: {current}"
                )
        temporary_record = transaction.state_temporary / "trash.json"
        published_record = transaction.state_destination / "trash.json"
        if transaction.state_temporary.exists() and transaction.state_destination.exists():
            raise ConfigurationError("trash transaction has duplicate state batch directories")
        if published_record.is_file():
            reopened = _read_model(published_record, JobTrashRecord, require_canonical=True)
        elif temporary_record.is_file():
            reopened = _read_model(temporary_record, JobTrashRecord, require_canonical=True)
            if transaction.workspace_moves:
                if transaction.workspace_destination.exists():
                    if transaction.workspace_temporary.exists():
                        raise ConfigurationError(
                            "trash transaction has duplicate workspace batch directories"
                        )
                elif transaction.workspace_temporary.exists():
                    if self._lease is not None:
                        self._validate_owned_roots()
                    workspace_inventory = _deletion_inventory(
                        transaction.workspace_temporary,
                        context="trash workspace batch publication",
                    )
                    self._move_owned_directory(
                        transaction.workspace_temporary,
                        transaction.workspace_destination,
                        source_root=self.workspace_trash_dir,
                        destination_root=self.workspace_trash_dir,
                        inventory=workspace_inventory,
                        context="trash workspace batch publication",
                    )
                else:
                    raise ConfigurationError(
                        "trash transaction workspace batch is missing during publication"
                    )
            if self._lease is not None:
                self._validate_owned_roots()
            state_inventory = _deletion_inventory(
                transaction.state_temporary,
                context="trash state batch publication",
            )
            self._move_owned_directory(
                transaction.state_temporary,
                transaction.state_destination,
                source_root=self.trash_dir,
                destination_root=self.trash_dir,
                inventory=state_inventory,
                context="trash state batch publication",
            )
        else:
            raise ConfigurationError("trash transaction is not ready for publication")
        if reopened != transaction.trash_record:
            raise ConfigurationError("trash transaction record changed before publication")
        verified = self._verify_trash_record(
            reopened,
            expected_batch_id=transaction.batch_id,
            expected_path=published_record,
        )
        self._remove_trash_transaction(transaction)
        return verified

    def _recover_trash_transactions(self) -> None:
        self._validate_owned_roots()
        for path in sorted(self.trash_transactions_dir.glob("*/transaction.json")):
            if path.is_symlink() or path.parent.is_symlink():
                raise ConfigurationError("trash transaction path must not be a symlink")
            transaction = self._validate_trash_transaction(
                self._read_owned_model(
                    path,
                    JobTrashTransaction,
                    root=self.trash_transactions_dir,
                    context="trash apply transaction",
                )
            )
            if path != self._trash_transaction_path(transaction.batch_id):
                raise ConfigurationError("trash transaction is stored under the wrong batch id")
            if (transaction.state_temporary / "trash.json").is_file() or (
                transaction.state_destination / "trash.json"
            ).is_file():
                self._publish_trash_transaction(transaction)
            else:
                self._rollback_trash_transaction(transaction)

    def _recover_trash_action_transactions(self) -> None:
        self._validate_owned_roots()
        for path in sorted(self.trash_transactions_dir.glob("*/action.json")):
            _strict_child_path(
                self.trash_transactions_dir,
                path,
                context="trash action recovery",
                require_exists=True,
            )
            transaction = self._read_trash_action(path.parent.name)
            if path != self._trash_action_path(transaction.batch_id):
                raise ConfigurationError(
                    "trash action transaction is stored under the wrong batch id"
                )
            if transaction.action is JobTrashActionKind.RESTORE:
                self._finish_restore_action(transaction)
            else:
                self._finish_purge_action(transaction)

    def _record_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"

    def _request_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "request.json"

    def _result_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "result.json"

    def _launch_gate_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "launch-gate.json"

    def _worker_ready_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "worker-ready.json"

    def _event_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "events.jsonl"

    def _read_record(self, job_id: str) -> JobRecord:
        self._validate_if_owned()
        path = self._record_path(job_id)
        if not path.is_file():
            raise KeyError(job_id)
        if self._lease is None:
            return _read_model(path, JobRecord)
        return self._read_owned_model(
            path,
            JobRecord,
            root=self.jobs_dir,
            context="Web durable JobRecord",
        )

    def _write_record(
        self,
        record: JobRecord,
        *,
        message_key: str | None = None,
    ) -> JobRecord:
        self._validate_if_owned()
        updated = record.model_copy(update={"updated_at": utc_now()})
        path = self._record_path(updated.job_id)
        if self._lease is None:
            _atomic_write(path, updated)
        else:
            self._create_owned_directory(
                path.parent,
                root=self.jobs_dir,
                context="Web job directory",
                exist_ok=True,
            )
            self._write_owned_model(
                path,
                updated,
                root=self.jobs_dir,
                context="Web durable JobRecord",
            )
        if message_key is not None:
            self._append_event(updated, message_key)
        return updated

    def _append_event(self, record: JobRecord, message_key: str) -> JobEvent:
        self._validate_if_owned()
        events = self.read_events(record.job_id)
        event = JobEvent(
            sequence=(events[-1].sequence + 1 if events else 1),
            job_id=record.job_id,
            occurred_at=record.updated_at,
            state=record.state,
            progress_fraction=record.progress_fraction,
            current_stage=record.current_stage,
            ready_stages=record.ready_stages,
            message_key=message_key,
        )
        path = self._event_path(record.job_id)
        payload = b"".join(_canonical_bytes(item) for item in (*events, event))
        if len(payload) > 8 * 1024 * 1024:
            raise ConfigurationError("Web job event log exceeds the 8 MiB safety limit")
        if self._lease is None:
            _atomic_write_payload(path, payload, context="Web job event log")
        else:
            self._write_owned_payload(
                path,
                payload,
                root=self.jobs_dir,
                context="Web job event log",
            )
        return event

    def read_events(self, job_id: str, *, after: int = 0) -> tuple[JobEvent, ...]:
        """Return strictly parsed events after one sequence number."""
        self._validate_owned_roots()
        path = self._event_path(job_id)
        try:
            path.lstat()
        except FileNotFoundError:
            return ()
        events: list[JobEvent] = []
        try:
            payload = self._read_owned_payload(
                path,
                root=self.jobs_dir,
                context="Web job event log",
                max_bytes=8 * 1024 * 1024,
            )
            for line in payload.splitlines(keepends=True):
                event = JobEvent.model_validate_json(line)
                if line != _canonical_bytes(event):
                    raise ValueError("event is not canonically encoded")
                if event.job_id != job_id:
                    raise ValueError("event job identity does not match its log")
                events.append(event)
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"Web job event log is unreadable: {path}") from exc
        if (events and events[0].sequence != 1) or any(
            right.sequence != left.sequence + 1 for left, right in itertools.pairwise(events)
        ):
            raise ConfigurationError(f"Web job event sequence is not monotonic: {path}")
        return tuple(event for event in events if event.sequence > after)

    def submit(self, request: JobCreateRequest) -> JobRecord:
        """Persist and enqueue one complete validated workflow launch."""
        with self._lock:
            self._validate_owned_roots()
            normalized, _ = self.validate_request(request)
            request_payload = _canonical_bytes(normalized)
            request_sha256 = hashlib.sha256(request_payload).hexdigest()
            workspace = normalized.launch.workspace_dir
            job_id = uuid4().hex
            now = utc_now()
            record = JobRecord(
                job_id=job_id,
                created_at=now,
                updated_at=now,
                state=JobState.QUEUED,
                workspace_dir=workspace,
                expected_stages=expected_workflow_stages(normalized),
                progress_fraction=0.0,
                request_sha256=request_sha256,
            )
            self._validate_owned_roots()
            self._create_owned_directory(
                self._job_dir(job_id),
                root=self.jobs_dir,
                context="Web job directory",
            )
            self._write_owned_payload(
                self._request_path(job_id),
                request_payload,
                root=self.jobs_dir,
                context="Web worker request",
            )
            self._write_owned_model(
                self._record_path(job_id),
                record,
                root=self.jobs_dir,
                context="Web durable JobRecord",
            )
            self._append_event(record, "job.queued")
            self._start_queued_jobs()
            return self._read_record(job_id)

    def get(self, job_id: str, *, refresh: bool = True) -> JobRecord:
        """Return one job after optional process/status reconciliation."""
        with self._lock:
            self._validate_owned_roots()
            if refresh:
                self.refresh()
            return self._read_record(job_id)

    def list(self) -> tuple[JobRecord, ...]:
        """Return jobs newest first after reconciliation."""
        with self._lock:
            self._validate_owned_roots()
            self.refresh()
            records = [
                self._read_record(path.parent.name) for path in self.jobs_dir.glob("*/job.json")
            ]
            return tuple(sorted(records, key=lambda item: item.created_at, reverse=True))

    def _completed_record(self, job_id: str) -> JobRecord:
        record = self.get(job_id)
        if record.state is not JobState.COMPLETED or record.summary is None:
            raise ConfigurationError("workflow maintenance requires a completed job")
        return record

    @staticmethod
    def _validate_backup_id(backup_id: str) -> str:
        if len(backup_id) != 64 or any(
            character not in "0123456789abcdef" for character in backup_id
        ):
            raise KeyError(backup_id)
        return backup_id

    def open_backup_download(
        self,
        backup_id: str,
    ) -> tuple[VerifiedFileDownload, WorkflowBackupRecord]:
        """Verify one backup and retain that exact handle for streaming or restore."""
        validated = self._validate_backup_id(backup_id)
        self._validate_owned_roots()
        path = self.backups_dir / f"{validated}.zip"
        stream_context = open_owned_regular_binary(
            path,
            root=self.backups_dir,
            root_identity=self._owned_identity(self.backups_dir),
            context="workflow backup archive",
        )
        stack = contextlib.ExitStack()
        try:
            try:
                stream = stack.enter_context(stream_context)
            except FileNotFoundError as exc:
                raise KeyError(backup_id) from exc
            before = os.fstat(stream.fileno())
            manifest = verify_workflow_backup(stream, source_path=path)
            stream.seek(0)
            digest = hashlib.sha256()
            while block := stream.read(1024 * 1024):
                digest.update(block)
            finished = os.fstat(stream.fileno())
            if _stable_stat_fields(finished) != _stable_stat_fields(before):
                raise ConfigurationError("workflow backup changed while it was verified")
            archive_sha256 = digest.hexdigest()
            if manifest.backup_id != validated:
                raise ConfigurationError("workflow backup filename does not match its identity")
            stream.seek(0)
            record = WorkflowBackupRecord(
                backup_id=manifest.backup_id,
                workflow_id=manifest.workflow_id,
                original_workspace=manifest.original_workspace,
                archive_size_bytes=before.st_size,
                archive_sha256=archive_sha256,
                file_count=len(manifest.files),
                download_url=f"/api/v1/backups/{manifest.backup_id}",
                required_checks_passed=manifest.required_checks_passed,
            )
            return (
                VerifiedFileDownload(
                    stream=stream,
                    size_bytes=before.st_size,
                    sha256=archive_sha256,
                    _context=stack,
                ),
                record,
            )
        except BaseException:
            stack.close()
            raise

    def _backup_record(self, path: Path) -> WorkflowBackupRecord:
        backup_id = self._validate_backup_id(path.stem)
        download, record = self.open_backup_download(backup_id)
        download.close()
        return record

    def list_backups(self) -> tuple[WorkflowBackupRecord, ...]:
        """Strictly reopen adapter-owned backups and return stable records."""
        with self._lock:
            self._validate_owned_roots()
            try:
                with os.scandir(self.backups_dir) as entries:
                    names = tuple(sorted(entry.name for entry in entries))
            except OSError as exc:
                raise ConfigurationError("workflow backup directory is unreadable") from exc
            self._validate_owned_roots()
            records: list[WorkflowBackupRecord] = []
            for name in names:
                if name.startswith("."):
                    continue
                if not name.endswith(".zip"):
                    continue
                backup_id = name[:-4]
                self._validate_backup_id(backup_id)
                download, record = self.open_backup_download(backup_id)
                download.close()
                records.append(record)
            return tuple(records)

    def create_backup(self, job_id: str) -> WorkflowBackupRecord:
        """Create or reuse one deterministic verified backup for a completed job."""
        with self._lock:
            self._validate_owned_roots()
            record = self._completed_record(job_id)
            temporary = self.backups_dir / f".{job_id}.{uuid4().hex}.creating.zip"
            temporary_metadata: os.stat_result | None = None
            published = False
            try:
                with open_exclusive_owned_regular_binary(
                    temporary,
                    root=self.backups_dir,
                    root_identity=self._owned_identity(self.backups_dir),
                    context="workflow backup temporary",
                ) as stream:
                    temporary_metadata = os.fstat(stream.fileno())
                    result = create_workflow_backup(
                        record.workspace_dir,
                        stream,
                        source_path=temporary,
                    )
                    if os.fstat(stream.fileno()).st_ino != temporary_metadata.st_ino:
                        raise ConfigurationError("workflow backup temporary identity changed")
                destination = self.backups_dir / f"{result.manifest.backup_id}.zip"
                try:
                    move_owned_path(
                        temporary,
                        destination,
                        source_root=self.backups_dir,
                        source_root_identity=self._owned_identity(self.backups_dir),
                        destination_root=self.backups_dir,
                        destination_root_identity=self._owned_identity(self.backups_dir),
                        expected_identity=(
                            temporary_metadata.st_dev,
                            temporary_metadata.st_ino,
                        ),
                        directory=False,
                        context="workflow backup publication",
                    )
                except FileExistsError:
                    existing_download, existing = self.open_backup_download(
                        result.manifest.backup_id
                    )
                    existing_download.close()
                    if existing.archive_sha256 != result.archive_sha256:
                        raise ConfigurationError(
                            "workflow backup id collision has different archive bytes"
                        ) from None
                    return existing
                except (OSError, ValueError) as exc:
                    raise ConfigurationError(
                        "workflow backup could not be published safely"
                    ) from exc
                published = True
                download, created = self.open_backup_download(result.manifest.backup_id)
                download.close()
                return created
            finally:
                if not published and temporary_metadata is not None:
                    with contextlib.suppress(ConfigurationError):
                        self._remove_owned_entry(
                            temporary,
                            root=self.backups_dir,
                            metadata=temporary_metadata,
                            directory=False,
                            context="workflow backup temporary cleanup",
                            missing_ok=True,
                        )

    def backup_archive_path(
        self,
        backup_id: str,
    ) -> tuple[Path, WorkflowBackupRecord]:
        """Verify a backup through its pinned handle and return its lexical path."""
        validated = self._validate_backup_id(backup_id)
        download, record = self.open_backup_download(validated)
        download.close()
        return self.backups_dir / f"{validated}.zip", record

    def maintenance(self, job_id: str) -> JobMaintenanceOverview:
        """Return measured storage, cleanup, and backup state for one job."""
        self._validate_owned_roots()
        record = self._completed_record(job_id)
        summary = record.summary
        if summary is None:
            raise ConfigurationError("completed job summary is missing")
        launch = read_workflow_launch_config(record.workspace_dir / "workflow-launch.yaml")
        storage = estimate_workflow_storage(launch, summary=summary)
        cleanup = plan_workflow_cleanup(record.workspace_dir)
        backups = tuple(
            backup for backup in self.list_backups() if backup.workflow_id == summary.workflow_id
        )
        return JobMaintenanceOverview(
            job_id=record.job_id,
            storage=storage,
            cleanup=cleanup,
            backups=backups,
            required_checks_passed=(
                storage.sufficient_for_estimate
                and cleanup.required_checks_passed
                and all(item.required_checks_passed for item in backups)
            ),
        )

    def cleanup(
        self,
        job_id: str,
        *,
        confirm_workflow_id: str,
        confirm_plan_id: str,
    ) -> WorkflowCleanupResult:
        """Apply the core reviewed cleanup contract for one completed job."""
        with self._lock:
            self._validate_owned_roots()
            record = self._completed_record(job_id)
            if record.summary is None or confirm_workflow_id != record.summary.workflow_id:
                raise ConfigurationError(
                    "cleanup confirmation does not match the selected completed job"
                )
            return apply_workflow_cleanup(
                record.workspace_dir,
                confirm_workflow_id=confirm_workflow_id,
                confirm_plan_id=confirm_plan_id,
            )

    def restore_backup(
        self,
        backup_id: str,
        *,
        workspace_name: str | None = None,
    ) -> JobRecord:
        """Restore a verified backup below the workspace root and register it."""
        with self._lock:
            self._validate_owned_roots()
            download, backup = self.open_backup_download(backup_id)
            archive_path = self.backups_dir / f"{backup.backup_id}.zip"
            try:
                request = WorkflowRestoreRequest(workspace_name=workspace_name)
                if request.workspace_name is None:
                    raw_name = f"{backup.original_workspace.name}-restored-{backup.backup_id[:8]}"
                    name = "".join(
                        character if character.isalnum() or character in "._-" else "-"
                        for character in raw_name
                    ).strip(".-")
                else:
                    name = request.workspace_name
                if not name:
                    raise ConfigurationError("restored workspace name is empty")
                destination = self.workspace_relative_path(
                    self.config.workspace_root,
                    name,
                    context="restored workspace",
                )
                self._validate_owned_roots()
                result = restore_workflow_backup(
                    download.stream,
                    destination,
                    source_path=archive_path,
                    destination_root=self.config.workspace_root,
                    destination_root_identity=self._owned_identity(self.config.workspace_root),
                )
            finally:
                download.close()
            if not result.required_checks_passed:
                raise ConfigurationError("restored workflow did not pass strict checks")
            launch = read_workflow_launch_config(destination / "workflow-launch.yaml")
            create_request = JobCreateRequest(launch=launch)
            summary = inspect_workflow_workspace(destination)
            job_id = uuid4().hex
            now = utc_now()
            base_record = JobRecord(
                job_id=job_id,
                created_at=now,
                updated_at=now,
                state=JobState.COMPLETED,
                workspace_dir=destination,
                expected_stages=expected_workflow_stages(create_request),
                progress_fraction=1.0,
                current_stage=None,
                ready_stages=summary.ready_stages,
                pid=None,
                exit_code=0,
                summary=summary,
            )
            completed = base_record.model_copy(
                update={"artifacts": self._artifacts(base_record, summary.artifacts)}
            )
            self._create_owned_directory(
                self._job_dir(job_id),
                root=self.jobs_dir,
                context="restored Web job directory",
            )
            self._write_owned_model(
                self._request_path(job_id),
                create_request,
                root=self.jobs_dir,
                context="restored Web worker request",
            )
            self._write_owned_model(
                self._record_path(job_id),
                completed,
                root=self.jobs_dir,
                context="restored Web JobRecord",
            )
            self._append_event(completed, "job.restored")
            return self._read_record(job_id)

    def _all_records(self) -> tuple[JobRecord, ...]:
        return tuple(
            self._read_record(path.parent.name) for path in sorted(self.jobs_dir.glob("*/job.json"))
        )

    def _running_count(self) -> int:
        return sum(
            record.state in {JobState.STARTING, JobState.RUNNING, JobState.CANCELLING}
            for record in self._all_records()
        )

    def _start_queued_jobs(self) -> None:
        available = self.config.max_concurrent_jobs - self._running_count()
        if available <= 0:
            return
        queued = sorted(
            (record for record in self._all_records() if record.state is JobState.QUEUED),
            key=lambda item: item.created_at,
        )
        for record in queued[:available]:
            self._start_job(record)

    @staticmethod
    def _launch_gate_payload(record: JobRecord) -> dict[str, Any]:
        if (
            record.state is not JobState.RUNNING
            or record.launch_nonce is None
            or record.request_sha256 is None
            or record.worker_ready_sha256 is None
            or record.pid is None
            or record.process_identity is None
            or record.process_group_id is None
        ):
            raise ConfigurationError("worker launch gate requires a complete RUNNING record")
        return {
            "schema_version": _WORKER_GATE_SCHEMA_VERSION,
            "job_id": record.job_id,
            "launch_nonce": record.launch_nonce,
            "request_sha256": record.request_sha256,
            "worker_ready_sha256": record.worker_ready_sha256,
            "pid": record.pid,
            "process_identity": record.process_identity,
            "process_group_id": record.process_group_id,
        }

    def _read_worker_ready(self, record: JobRecord) -> tuple[WorkerReady, str]:
        """Read and bind one canonical ready record without trusting its path."""
        if record.launch_nonce is None or record.request_sha256 is None:
            raise ConfigurationError("STARTING record is missing its launch identity")
        path = self._worker_ready_path(record.job_id)
        try:
            payload = read_owned_regular_bytes(
                path,
                root=self.jobs_dir,
                root_identity=self._owned_identity(self.jobs_dir),
                context="worker containment-ready record",
                max_bytes=_MAX_WORKER_READY_BYTES,
            )
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as exc:
            raise ConfigurationError(
                f"worker containment-ready record is unsafe or unreadable: {path}"
            ) from exc
        try:
            ready = WorkerReady.model_validate_json(payload)
        except ValueError as exc:
            raise ConfigurationError("worker containment-ready record is invalid") from exc
        if payload != _canonical_bytes(ready):
            raise ConfigurationError("worker containment-ready record is not canonical")
        jobs_identity = self._owned_identity(self.jobs_dir)
        if (
            ready.job_id != record.job_id
            or ready.launch_nonce != record.launch_nonce
            or ready.request_sha256 != record.request_sha256
            or (ready.jobs_root_device, ready.jobs_root_inode) != jobs_identity
            or ready.process_group_id != ready.pid
            or (record.pid is not None and record.pid != ready.pid)
            or (
                record.process_identity is not None
                and record.process_identity != ready.process_identity
            )
            or (
                record.process_group_id is not None
                and record.process_group_id != ready.process_group_id
            )
        ):
            raise ConfigurationError(
                "worker containment-ready record does not match the durable launch identity"
            )
        digest = hashlib.sha256(payload).hexdigest()
        if record.worker_ready_sha256 is not None and record.worker_ready_sha256 != digest:
            raise ConfigurationError(
                "worker containment-ready record changed after durable publication"
            )
        return ready, digest

    @staticmethod
    def _verify_worker_ready_live(ready: WorkerReady) -> None:
        try:
            matches = process_containment_is_alive(
                ready.pid,
                ready.process_identity,
                ready.process_group_id,
            )
        except OSError as exc:
            raise ConfigurationError(
                "operating-system worker containment inspection is unavailable"
            ) from exc
        if not matches:
            raise ConfigurationError(
                "worker exited or changed identity/containment after publishing ready"
            )

    def _wait_for_worker_ready(
        self,
        record: JobRecord,
        process: subprocess.Popen[bytes],
    ) -> tuple[WorkerReady, str]:
        deadline = time.monotonic() + _WORKER_GATE_TIMEOUT_SECONDS
        while True:
            digest: str | None = None
            try:
                ready, digest = self._read_worker_ready(record)
            except FileNotFoundError:
                ready = None
            if ready is not None:
                if digest is None:
                    raise RuntimeError("worker containment-ready digest is unavailable")
                if ready.pid != process.pid:
                    raise RuntimeError(
                        "worker containment-ready PID does not match the spawned process"
                    )
                self._verify_worker_ready_live(ready)
                return ready, digest
            exit_code = process.poll()
            if exit_code is not None:
                raise RuntimeError(
                    "worker exited before publishing containment-ready evidence "
                    f"(exit code {exit_code})"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "worker did not publish containment-ready evidence before the deadline"
                )
            time.sleep(0.01)

    def _publish_launch_gate(self, record: JobRecord) -> None:
        gate_path = self._launch_gate_path(record.job_id)
        if record.launch_gate_deadline is not None and utc_now() > record.launch_gate_deadline:
            raise ConfigurationError("worker launch gate deadline expired before publication")
        expected = _canonical_bytes(self._launch_gate_payload(record))
        expected_sha256 = hashlib.sha256(expected).hexdigest()
        if record.launch_gate_sha256 != expected_sha256:
            raise ConfigurationError("RUNNING record does not bind its exact worker launch gate")
        durable_before = self._read_record(record.job_id)
        if durable_before != record:
            raise ConfigurationError(
                "durable RUNNING record changed before launch-gate publication"
            )
        try:
            write_exclusive_owned_regular_bytes(
                gate_path,
                expected,
                root=self.jobs_dir,
                root_identity=self._owned_identity(self.jobs_dir),
                context="worker launch gate",
            )
        except FileExistsError as exc:
            raise ConfigurationError(
                "worker launch gate already exists; refusing to overwrite prior authorization"
            ) from exc
        durable_after = self._read_record(record.job_id)
        if durable_after != record:
            raise ConfigurationError(
                "durable RUNNING record changed during launch-gate publication"
            )

    def _verify_launch_gate(self, record: JobRecord) -> None:
        gate_path = self._launch_gate_path(record.job_id)
        if (
            record.request_sha256 is None
            or record.worker_ready_sha256 is None
            or record.launch_gate_sha256 is None
        ):
            raise ConfigurationError("RUNNING record is missing request, ready, or gate identity")
        _, ready_sha256 = self._read_worker_ready(record)
        if ready_sha256 != record.worker_ready_sha256:
            raise ConfigurationError("RUNNING record no longer binds its worker-ready record")
        request_payload = self._read_owned_payload(
            self._request_path(record.job_id),
            root=self.jobs_dir,
            context="worker request",
            max_bytes=1024 * 1024,
        )
        if hashlib.sha256(request_payload).hexdigest() != record.request_sha256:
            raise ConfigurationError("worker request no longer matches the RUNNING record")
        expected = _canonical_bytes(self._launch_gate_payload(record))
        if hashlib.sha256(expected).hexdigest() != record.launch_gate_sha256:
            raise ConfigurationError("RUNNING record launch-gate hash is inconsistent")
        try:
            observed = self._read_owned_payload(
                gate_path,
                root=self.jobs_dir,
                context="worker launch gate",
                max_bytes=4096,
            )
        except ValueError as exc:
            raise ConfigurationError(
                "worker launch gate is missing, unsafe, or unstable; recovery will not republish it"
            ) from exc
        if observed != expected:
            raise ConfigurationError(
                "worker launch gate does not match the durable RUNNING identity"
            )

    def _start_job(self, record: JobRecord) -> None:
        if record.state is not JobState.QUEUED:
            raise ConfigurationError("only a queued job may enter the worker start protocol")
        if record.request_sha256 is None:
            legacy_payload = self._read_owned_payload(
                self._request_path(record.job_id),
                root=self.jobs_dir,
                context="legacy queued worker request",
                max_bytes=1024 * 1024,
            )
            try:
                legacy_request = JobCreateRequest.model_validate_json(legacy_payload)
            except ValueError as exc:
                raise ConfigurationError("legacy queued worker request is invalid") from exc
            canonical_request = _canonical_bytes(legacy_request)
            self._write_owned_payload(
                self._request_path(record.job_id),
                canonical_request,
                root=self.jobs_dir,
                context="Web worker request",
            )
            record = self._write_record(
                record.model_copy(
                    update={"request_sha256": hashlib.sha256(canonical_request).hexdigest()}
                )
            )
        launch_nonce = uuid4().hex
        parent_pid = os.getpid()
        parent_identity = inspect_process_identity(parent_pid)
        gate_deadline = utc_now() + timedelta(seconds=_WORKER_GATE_TIMEOUT_SECONDS)
        starting = self._write_record(
            record.model_copy(
                update={
                    "state": JobState.STARTING,
                    "launch_nonce": launch_nonce,
                    "worker_ready_sha256": None,
                    "launch_gate_sha256": None,
                    "launch_gate_deadline": gate_deadline,
                    "launch_parent_pid": parent_pid,
                    "launch_parent_identity": parent_identity,
                    "pid": None,
                    "process_identity": None,
                    "process_group_id": None,
                    "current_stage": None,
                    "error": None,
                }
            ),
            message_key="job.starting",
        )
        job_dir = self._job_dir(record.job_id)
        gate_path = self._launch_gate_path(record.job_id)
        ready_path = self._worker_ready_path(record.job_id)
        result_path = self._result_path(record.job_id)
        request_sha256 = starting.request_sha256
        stdout_path = job_dir / "stdout.log"
        stderr_path = job_dir / "stderr.log"
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        if self.config.bambu_studio_executable is not None:
            env["TOPOFORGE_BAMBU_STUDIO"] = str(self.config.bambu_studio_executable)
        jobs_identity = self._owned_identity(self.jobs_dir)
        command = [
            sys.executable,
            "-m",
            "topoforge.web.worker",
            "--request",
            str(self._request_path(record.job_id)),
            "--result",
            str(self._result_path(record.job_id)),
            "--gate",
            str(gate_path),
            "--ready",
            str(ready_path),
            "--jobs-root",
            str(self.jobs_dir),
            "--jobs-root-device",
            str(jobs_identity[0]),
            "--jobs-root-inode",
            str(jobs_identity[1]),
            "--launch-nonce",
            launch_nonce,
            "--request-sha256",
            request_sha256 or "",
            "--parent-pid",
            str(parent_pid),
            "--parent-identity",
            parent_identity or "",
            "--gate-timeout-seconds",
            str(_WORKER_GATE_TIMEOUT_SECONDS),
        ]
        process: subprocess.Popen[bytes] | None = None
        identity: str | None = None
        process_group: int | None = None
        try:
            if parent_identity is None:
                raise RuntimeError(
                    "the operating system did not expose a stable launch-parent identity"
                )
            if gate_path.exists() or gate_path.is_symlink():
                raise RuntimeError("a worker launch gate already exists before spawn")
            if ready_path.exists() or ready_path.is_symlink():
                raise RuntimeError("a worker containment-ready record already exists before spawn")
            if result_path.exists() or result_path.is_symlink():
                raise RuntimeError("a worker result already exists before spawn")
            if request_sha256 is None:
                raise RuntimeError("the durable job record is missing its request checksum")
            request_payload = self._read_owned_payload(
                self._request_path(record.job_id),
                root=self.jobs_dir,
                context="worker request",
                max_bytes=1024 * 1024,
            )
            if hashlib.sha256(request_payload).hexdigest() != request_sha256:
                raise RuntimeError("the worker request changed before spawn")
            with (
                open_exclusive_owned_regular_binary(
                    stdout_path,
                    root=self.jobs_dir,
                    root_identity=jobs_identity,
                    context="worker stdout log",
                ) as stdout,
                open_exclusive_owned_regular_binary(
                    stderr_path,
                    root=self.jobs_dir,
                    root_identity=jobs_identity,
                    context="worker stderr log",
                ) as stderr,
            ):
                process = cast(
                    "subprocess.Popen[bytes]",
                    subprocess.Popen(
                        command,
                        cwd=self.config.workspace_root,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                        **worker_process_options(),
                    ),
                )
            self._processes[record.job_id] = process
            ready, ready_sha256 = self._wait_for_worker_ready(starting, process)
            identity = ready.process_identity
            process_group = ready.process_group_id
            containment_ready = self._write_record(
                starting.model_copy(
                    update={
                        "worker_ready_sha256": ready_sha256,
                        "pid": ready.pid,
                        "process_identity": ready.process_identity,
                        "process_group_id": ready.process_group_id,
                    }
                )
            )
            self._verify_worker_ready_live(ready)
            running_candidate = containment_ready.model_copy(
                update={
                    "state": JobState.RUNNING,
                    "current_stage": record.expected_stages[0],
                }
            )
            gate_sha256 = hashlib.sha256(
                _canonical_bytes(self._launch_gate_payload(running_candidate))
            ).hexdigest()
            running = self._write_record(
                running_candidate.model_copy(update={"launch_gate_sha256": gate_sha256}),
                message_key="job.started",
            )
            self._publish_launch_gate(running)
        except Exception as exc:
            cleanup_failure: Exception | None = None
            if process is not None:
                try:
                    terminate_process_tree(
                        process.pid,
                        expected_identity=identity,
                        process_group=process_group,
                    )
                except Exception as cleanup_exc:
                    cleanup_failure = cleanup_exc
                    _LOGGER.exception(
                        "Could not clean up worker after job %s failed to start",
                        record.job_id,
                    )
                    if identity is None or process_group is None:
                        with contextlib.suppress(OSError):
                            process.kill()
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    process.wait(timeout=5)
                if process.poll() is not None:
                    cleanup_failure = None
            if cleanup_failure is not None and process is not None:
                self._processes[record.job_id] = process
                current = self._read_record(record.job_id)
                error = JobError(
                    code="worker-start-cleanup-failed",
                    message=(
                        "The worker start protocol failed, and the identity-verified worker "
                        f"could not be stopped safely: {cleanup_failure}"
                    ),
                    corrective_action=(
                        "Retry cancellation. TopoForge retains the PID, identity, process "
                        "group, and concurrency slot and will not launch a replacement."
                    ),
                    exception_type=cleanup_failure.__class__.__name__,
                )
                self._write_record(
                    current.model_copy(
                        update={
                            "state": JobState.CANCELLING,
                            "pid": current.pid or process.pid,
                            "process_identity": current.process_identity or identity,
                            "process_group_id": current.process_group_id or process_group,
                            "cancellation_requested": True,
                            "error": error,
                        }
                    ),
                    message_key="job.cancellation-failed",
                )
                return
            self._processes.pop(record.job_id, None)
            error = JobError(
                code="worker-start-failed",
                message=f"The isolated worker could not be started safely: {exc}",
                corrective_action=(
                    "Check operating-system process inspection permissions and temporary "
                    "storage, then retry the job."
                ),
                exception_type=exc.__class__.__name__,
            )
            current = self._read_record(record.job_id)
            self._write_record(
                current.model_copy(
                    update={
                        "state": JobState.FAILED,
                        "pid": None,
                        "process_identity": None,
                        "process_group_id": None,
                        "current_stage": None,
                        "error": error,
                    }
                ),
                message_key="job.failed",
            )

    def _block_or_finish_starting_protocol_failure(
        self,
        record: JobRecord,
        *,
        process: subprocess.Popen[bytes] | None,
        exc: Exception,
    ) -> JobRecord:
        """Retain a possibly live unreleased worker unless its containment is stopped."""
        exit_code = process.poll() if process is not None else None
        identity_complete = (
            record.pid is not None
            and record.process_identity is not None
            and record.process_group_id is not None
        )
        if (process is None or exit_code is not None) and identity_complete:
            assert record.pid is not None
            assert record.process_identity is not None
            assert record.process_group_id is not None
            try:
                still_live = process_containment_is_alive(
                    record.pid,
                    record.process_identity,
                    record.process_group_id,
                )
            except OSError as inspection_exc:
                error = self._worker_inspection_unavailable_error(
                    record.pid,
                    inspection_exc,
                )
                blocked = record.model_copy(update={"error": error})
                if blocked == record:
                    return record
                return self._write_record(blocked, message_key="job.recovery-blocked")
            if not still_live:
                self._processes.pop(record.job_id, None)
                return self._finish_reconciliation_failure(
                    record,
                    exit_code=exit_code,
                    exc=exc,
                )

        error = JobError(
            code="worker-start-recovery-blocked",
            message=(
                "The unreleased STARTING worker has unsafe, inconsistent, or unexpected "
                f"protocol evidence: {exc}. TopoForge cannot prove that its containment "
                "has stopped, so it will not clear the worker metadata, free the slot, "
                "publish another gate, or launch a replacement."
            ),
            corrective_action=(
                "Do not edit the retained request, ready, gate, or result files. Retry "
                "cancellation when a complete durable PID identity is available, or inspect "
                "the retained worker and evidence manually."
            ),
            exception_type=exc.__class__.__name__,
        )
        blocked = record.model_copy(update={"error": error})
        if blocked == record:
            return record
        return self._write_record(blocked, message_key="job.recovery-blocked")

    def _reconcile_starting(self, record: JobRecord) -> JobRecord:
        """Reconcile an unreleased worker without ever launching a replacement."""
        protocol_error: Exception | None = None
        try:
            gate_payload = self._read_optional_owned_payload(
                self._launch_gate_path(record.job_id),
                root=self.jobs_dir,
                context="STARTING worker launch gate",
                max_bytes=4096,
            )
        except ConfigurationError as exc:
            gate_payload = None
            protocol_error = exc
        if gate_payload is not None:
            protocol_error = ConfigurationError(
                "STARTING record unexpectedly has a published worker launch gate"
            )

        try:
            ready, ready_sha256 = self._read_worker_ready(record)
        except FileNotFoundError:
            ready = None
            ready_sha256 = None
        except ConfigurationError as exc:
            ready = None
            ready_sha256 = None
            if protocol_error is None:
                protocol_error = exc

        if ready is None and (
            record.worker_ready_sha256 is not None
            or record.pid is not None
            or record.process_identity is not None
            or record.process_group_id is not None
        ):
            protocol_error = protocol_error or ConfigurationError(
                "STARTING record retained worker identity but its containment-ready "
                "record is missing"
            )

        try:
            worker = self._read_optional_owned_model(
                self._result_path(record.job_id),
                WorkerResult,
                root=self.jobs_dir,
                context="STARTING worker result",
                max_bytes=8 * 1024 * 1024,
            )
        except ConfigurationError as exc:
            worker = None
            if protocol_error is None:
                protocol_error = exc

        process = self._processes.get(record.job_id)
        exit_code = process.poll() if process is not None else None
        effective_record = (
            record.model_copy(
                update={
                    "worker_ready_sha256": ready_sha256,
                    "pid": ready.pid,
                    "process_identity": ready.process_identity,
                    "process_group_id": ready.process_group_id,
                }
            )
            if ready is not None and ready_sha256 is not None
            else record
        )
        if process is not None and ready is not None and ready.pid != process.pid:
            protocol_error = protocol_error or ConfigurationError(
                "worker containment-ready PID does not match the spawned process"
            )
        if protocol_error is not None:
            return self._block_or_finish_starting_protocol_failure(
                effective_record,
                process=process,
                exc=protocol_error,
            )
        if worker is not None:
            if ready is None or ready_sha256 is None:
                return self._finish_reconciliation_failure(
                    record,
                    exit_code=exit_code,
                    exc=ConfigurationError(
                        "STARTING worker result has no verified containment-ready record"
                    ),
                )
            if process is not None and exit_code is None:
                return record
            if process is None:
                try:
                    still_live = process_containment_is_alive(
                        ready.pid,
                        ready.process_identity,
                        ready.process_group_id,
                    )
                except OSError as exc:
                    error = self._worker_inspection_unavailable_error(ready.pid, exc)
                    if record.error == error:
                        return record
                    return self._write_record(
                        record.model_copy(
                            update={
                                "worker_ready_sha256": ready_sha256,
                                "pid": ready.pid,
                                "process_identity": ready.process_identity,
                                "process_group_id": ready.process_group_id,
                                "error": error,
                            }
                        ),
                        message_key="job.recovery-blocked",
                    )
                if still_live:
                    return record
            candidate = record.model_copy(
                update={
                    "state": JobState.RUNNING,
                    "worker_ready_sha256": ready_sha256,
                    "pid": ready.pid,
                    "process_identity": ready.process_identity,
                    "process_group_id": ready.process_group_id,
                }
            )
            expected_gate_sha256 = hashlib.sha256(
                _canonical_bytes(self._launch_gate_payload(candidate))
            ).hexdigest()
            if (
                worker.ok
                or worker.job_id != record.job_id
                or worker.launch_nonce != record.launch_nonce
                or worker.request_sha256 != record.request_sha256
                or worker.worker_ready_sha256 != ready_sha256
                or worker.launch_gate_sha256 != expected_gate_sha256
            ):
                return self._finish_reconciliation_failure(
                    record,
                    exit_code=exit_code,
                    exc=ConfigurationError(
                        "STARTING worker result does not match its unreleased launch identity"
                    ),
                )
            self._processes.pop(record.job_id, None)
            try:
                return self._finish_job(
                    candidate.model_copy(update={"launch_gate_sha256": expected_gate_sha256}),
                    exit_code=exit_code,
                )
            except Exception as exc:
                return self._finish_reconciliation_failure(
                    record,
                    exit_code=exit_code,
                    exc=exc,
                )

        if ready is not None and ready_sha256 is not None:
            ready_record = record.model_copy(
                update={
                    "worker_ready_sha256": ready_sha256,
                    "pid": ready.pid,
                    "process_identity": ready.process_identity,
                    "process_group_id": ready.process_group_id,
                }
            )
            try:
                still_live = process_containment_is_alive(
                    ready.pid,
                    ready.process_identity,
                    ready.process_group_id,
                )
            except OSError as exc:
                error = self._worker_inspection_unavailable_error(ready.pid, exc)
                if record == ready_record and record.error == error:
                    return record
                return self._write_record(
                    ready_record.model_copy(update={"error": error}),
                    message_key="job.recovery-blocked",
                )
            if still_live:
                if ready_record.cancellation_requested:
                    cancelling = self._write_record(
                        ready_record.model_copy(
                            update={
                                "state": JobState.CANCELLING,
                                "error": None,
                            }
                        ),
                        message_key="job.cancelling",
                    )
                    threading.Thread(
                        target=self._terminate_process,
                        args=(
                            cancelling.job_id,
                            ready.pid,
                            ready.process_identity,
                            ready.process_group_id,
                        ),
                        name=f"topoforge-cancel-{record.job_id[:8]}",
                        daemon=True,
                    ).start()
                    return cancelling
                error = JobError(
                    code="worker-start-recovery-blocked",
                    message=(
                        "The worker established containment, but the manager stopped before "
                        "durably releasing its workflow. TopoForge will not publish a gate "
                        "or launch a replacement during recovery."
                    ),
                    corrective_action=(
                        "Cancel the identity-verified worker or wait for it to exit, then "
                        "submit the retained request again."
                    ),
                    exception_type="WorkerReadyUnreleased",
                )
                if record == ready_record and record.error == error:
                    return record
                return self._write_record(
                    ready_record.model_copy(update={"error": error}),
                    message_key="job.recovery-blocked",
                )
            self._processes.pop(record.job_id, None)
            error = JobError(
                code="worker-start-interrupted",
                message=(
                    "The containment-ready worker exited before a durable RUNNING record "
                    "and launch gate were published."
                ),
                corrective_action=(
                    "Inspect the retained request and logs, then submit a new job. The "
                    "workflow was not released."
                ),
                exception_type="WorkerReadyExited",
            )
            return self._write_record(
                ready_record.model_copy(
                    update={
                        "state": JobState.FAILED,
                        "current_stage": None,
                        "pid": None,
                        "process_identity": None,
                        "process_group_id": None,
                        "error": error,
                    }
                ),
                message_key="job.failed",
            )

        metadata_complete = (
            record.launch_nonce is not None
            and record.launch_gate_deadline is not None
            and record.launch_parent_pid is not None
            and record.launch_parent_identity is not None
        )
        parent_identity: str | None = None
        inspection_error: OSError | None = None
        if metadata_complete and record.launch_parent_pid is not None:
            try:
                parent_identity = inspect_process_identity(record.launch_parent_pid)
            except OSError as exc:
                inspection_error = exc
        before_deadline = (
            record.launch_gate_deadline is not None and utc_now() <= record.launch_gate_deadline
        )
        parent_status_unknown = inspection_error is not None and before_deadline
        parent_still_matches = (
            metadata_complete
            and parent_identity is not None
            and parent_identity == record.launch_parent_identity
            and before_deadline
        )
        if parent_status_unknown or parent_still_matches:
            detail = (
                f" Parent identity inspection failed: {inspection_error}."
                if inspection_error is not None
                else ""
            )
            error = JobError(
                code="worker-start-recovery-blocked",
                message=(
                    "A durable STARTING record has no safely attributable worker PID; "
                    "TopoForge will not launch a replacement before its gate deadline."
                    f"{detail}"
                ),
                corrective_action=(
                    "Wait for the gated worker to publish a terminal result or for the "
                    "recorded gate deadline to expire. The workflow has not been released."
                ),
                exception_type=(
                    inspection_error.__class__.__name__
                    if inspection_error is not None
                    else "WorkerLaunchPending"
                ),
            )
            if record.error == error:
                return record
            return self._write_record(
                record.model_copy(update={"error": error}),
                message_key="job.recovery-blocked",
            )

        self._processes.pop(record.job_id, None)
        error = JobError(
            code="worker-start-interrupted",
            message=(
                "The manager stopped during the durable STARTING window before a complete "
                "worker identity and launch gate were published."
            ),
            corrective_action=(
                "Inspect the retained request and logs, then submit a new job. TopoForge "
                "did not requeue or release this interrupted launch."
            ),
            exception_type="WorkerLaunchInterrupted",
        )
        return self._write_record(
            record.model_copy(
                update={
                    "state": JobState.FAILED,
                    "current_stage": None,
                    "pid": None,
                    "process_identity": None,
                    "process_group_id": None,
                    "error": error,
                }
            ),
            message_key="job.failed",
        )

    def _status_update(self, record: JobRecord) -> JobRecord:
        status_path = record.workspace_dir / "workflow-status.json"
        if not status_path.is_file():
            return record
        try:
            status = LocalWorkflowStatus.model_validate_json(
                status_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return record
        progress = _progress(record.expected_stages, status.ready_stages)
        if (
            status.current_stage == record.current_stage
            and status.ready_stages == record.ready_stages
            and progress == record.progress_fraction
        ):
            return record
        return self._write_record(
            record.model_copy(
                update={
                    "current_stage": status.current_stage,
                    "ready_stages": status.ready_stages,
                    "progress_fraction": progress,
                }
            ),
            message_key="job.progress",
        )

    def refresh(self) -> None:
        """Reconcile child processes, workflow status, terminal results, and queue."""
        with self._lock:
            self._validate_owned_roots()
            if not self.jobs_dir.exists():
                return
            for record in self._all_records():
                if (
                    record.state is JobState.COMPLETED
                    and record.summary is not None
                    and "project_manifest" in record.summary.artifacts
                    and not any(
                        artifact.artifact_id.startswith("bambu_project_3mf")
                        for artifact in record.artifacts
                    )
                ):
                    record = self._write_record(
                        record.model_copy(
                            update={
                                "artifacts": self._artifacts(
                                    record,
                                    record.summary.artifacts,
                                )
                            }
                        )
                    )
                if record.state is JobState.STARTING:
                    self._reconcile_starting(record)
                    continue
                if record.state not in {JobState.RUNNING, JobState.CANCELLING}:
                    continue
                record = self._status_update(record)
                process = self._processes.get(record.job_id)
                result_path = self._result_path(record.job_id)
                unidentified_pid = (
                    record.pid
                    if process is None
                    and (record.process_identity is None or record.process_group_id is None)
                    and (result_path.is_symlink() or not result_path.is_file())
                    else None
                )
                if unidentified_pid is not None:
                    inspection_error: OSError | None = None
                    try:
                        unidentified_worker_may_be_live = process_is_alive(unidentified_pid)
                    except OSError as exc:
                        inspection_error = exc
                        unidentified_worker_may_be_live = True
                    if unidentified_worker_may_be_live:
                        error = self._worker_identity_unavailable_error(
                            unidentified_pid, inspection_error=inspection_error
                        )
                        if record.error != error:
                            self._write_record(
                                record.model_copy(update={"error": error}),
                                message_key="job.recovery-blocked",
                            )
                        continue
                running = process is not None and process.poll() is None
                if not running and record.pid is not None:
                    try:
                        running = process_containment_is_alive(
                            record.pid,
                            record.process_identity,
                            record.process_group_id,
                        )
                    except OSError as exc:
                        error = self._worker_inspection_unavailable_error(record.pid, exc)
                        if record.error != error:
                            self._write_record(
                                record.model_copy(update={"error": error}),
                                message_key="job.recovery-blocked",
                            )
                        continue
                if running:
                    if record.state is JobState.RUNNING and record.launch_nonce is not None:
                        try:
                            self._verify_launch_gate(record)
                        except ConfigurationError as exc:
                            error = JobError(
                                code="worker-launch-gate-invalid",
                                message=f"The worker launch gate could not be verified: {exc}",
                                corrective_action=(
                                    "Do not replace or edit launch-gate.json. Cancel the "
                                    "identity-verified worker or wait for it to exit, then "
                                    "submit the retained request again."
                                ),
                                exception_type=exc.__class__.__name__,
                            )
                            if record.error != error:
                                self._write_record(
                                    record.model_copy(update={"error": error}),
                                    message_key="job.recovery-blocked",
                                )
                            continue
                    if record.error is not None and record.error.code in {
                        "worker-inspection-unavailable",
                        "worker-launch-gate-invalid",
                    }:
                        self._write_record(record.model_copy(update={"error": None}))
                    continue
                exit_code = process.returncode if process is not None else None
                self._processes.pop(record.job_id, None)
                try:
                    self._finish_job(record, exit_code=exit_code)
                except Exception as exc:
                    self._finish_reconciliation_failure(
                        record,
                        exit_code=exit_code,
                        exc=exc,
                    )
            self._start_queued_jobs()

    @staticmethod
    def _worker_identity_unavailable_error(
        pid: int,
        *,
        inspection_error: OSError | None = None,
    ) -> JobError:
        inspection_detail = (
            f" Operating-system liveness inspection also failed: {inspection_error}."
            if inspection_error is not None
            else ""
        )
        return JobError(
            code="worker-identity-unavailable",
            message=(
                f"Recovered worker PID {pid} is still live, but its durable record predates "
                "operating-system process identity and containment metadata."
                f"{inspection_detail}"
            ),
            corrective_action=(
                f"Inspect PID {pid} in the operating-system process viewer. If it is the "
                "retained TopoForge worker, wait for it to finish or stop it manually. "
                "TopoForge will not signal it, clear the PID, or start another queued job "
                "until that PID is gone or a complete atomic worker result appears."
            ),
            exception_type=(
                inspection_error.__class__.__name__
                if inspection_error is not None
                else "ProcessIdentityUnavailable"
            ),
        )

    @staticmethod
    def _worker_inspection_unavailable_error(pid: int, exc: OSError) -> JobError:
        return JobError(
            code="worker-inspection-unavailable",
            message=(
                f"Could not verify liveness, identity, or containment for worker PID {pid}: {exc}"
            ),
            corrective_action=(
                "Retry after operating-system process inspection is available; inspect "
                "the recorded PID, identity, and process group manually. TopoForge will "
                "not clear worker metadata or start another queued job while status is "
                "unknown."
            ),
            exception_type=exc.__class__.__name__,
        )

    def _finish_job(self, record: JobRecord, *, exit_code: int | None) -> JobRecord:
        if record.state is JobState.CANCELLING or record.cancellation_requested:
            return self._write_record(
                record.model_copy(
                    update={
                        "state": JobState.CANCELLED,
                        "exit_code": exit_code,
                        "current_stage": None,
                        "pid": None,
                        "process_identity": None,
                        "process_group_id": None,
                        "error": None,
                    }
                ),
                message_key="job.cancelled",
            )
        result_path = self._result_path(record.job_id)
        try:
            result_metadata = result_path.lstat()
        except FileNotFoundError:
            result_metadata = None
        if result_metadata is None:
            error = JobError(
                code="worker-result-missing",
                message="The isolated worker stopped before publishing a terminal result.",
                corrective_action=(
                    "Inspect the retained job stderr log and resume the saved launch."
                ),
            )
            return self._write_record(
                record.model_copy(
                    update={
                        "state": JobState.FAILED,
                        "exit_code": exit_code,
                        "current_stage": None,
                        "pid": None,
                        "process_identity": None,
                        "process_group_id": None,
                        "error": error,
                    }
                ),
                message_key="job.failed",
            )
        if (
            stat_result_is_link_like(result_metadata)
            or not stat.S_ISREG(result_metadata.st_mode)
            or result_metadata.st_nlink != 1
        ):
            raise ConfigurationError("worker result must be a real, non-hard-linked file")
        worker = self._read_owned_model(
            result_path,
            WorkerResult,
            root=self.jobs_dir,
            context="worker result",
        )
        if (
            record.launch_nonce is None
            or record.request_sha256 is None
            or record.worker_ready_sha256 is None
            or record.launch_gate_sha256 is None
            or worker.job_id != record.job_id
            or worker.launch_nonce != record.launch_nonce
            or worker.request_sha256 != record.request_sha256
            or worker.worker_ready_sha256 != record.worker_ready_sha256
            or worker.launch_gate_sha256 != record.launch_gate_sha256
        ):
            raise ConfigurationError("worker result does not match the durable job launch identity")
        if not worker.ok:
            return self._write_record(
                record.model_copy(
                    update={
                        "state": JobState.FAILED,
                        "exit_code": worker.exit_code,
                        "current_stage": None,
                        "pid": None,
                        "process_identity": None,
                        "process_group_id": None,
                        "error": worker.error,
                    }
                ),
                message_key="job.failed",
            )
        summary = inspect_workflow_workspace(record.workspace_dir)
        artifacts = self._artifacts(record, summary.artifacts)
        return self._write_record(
            record.model_copy(
                update={
                    "state": JobState.COMPLETED,
                    "progress_fraction": 1.0,
                    "current_stage": None,
                    "ready_stages": summary.ready_stages,
                    "pid": None,
                    "process_identity": None,
                    "process_group_id": None,
                    "exit_code": worker.exit_code,
                    "summary": summary,
                    "artifacts": artifacts,
                    "error": None,
                }
            ),
            message_key="job.completed",
        )

    def _finish_reconciliation_failure(
        self,
        record: JobRecord,
        *,
        exit_code: int | None,
        exc: Exception,
    ) -> JobRecord:
        _LOGGER.exception(
            "Could not reconcile terminal Web job %s",
            record.job_id,
            exc_info=exc,
        )
        error = JobError(
            code="worker-result-invalid",
            message=(
                "The isolated worker stopped, but its terminal result or workflow artifacts "
                f"could not be verified: {exc}"
            ),
            corrective_action=(
                "Inspect the retained result and stderr log, repair or remove corrupted "
                "workspace artifacts, then submit the saved launch again."
            ),
            exception_type=exc.__class__.__name__,
        )
        return self._write_record(
            record.model_copy(
                update={
                    "state": JobState.FAILED,
                    "exit_code": exit_code,
                    "current_stage": None,
                    "pid": None,
                    "process_identity": None,
                    "process_group_id": None,
                    "error": error,
                    "summary": None,
                    "artifacts": (),
                }
            ),
            message_key="job.failed",
        )

    def _artifacts(
        self,
        record: JobRecord,
        values: dict[str, str],
    ) -> tuple[JobArtifact, ...]:
        workspace = record.workspace_dir.resolve()
        expanded_values = dict(values)
        raw_manifest = expanded_values.get("project_manifest")
        if raw_manifest is not None:
            manifest_path = Path(raw_manifest).resolve()
            if not _within(workspace, manifest_path):
                raise ConfigurationError(f"workflow artifact escapes workspace: {manifest_path}")
            derived = _bambu_project_artifact_paths(workspace, manifest_path)
            duplicates = sorted(set(expanded_values).intersection(derived))
            if duplicates:
                raise ConfigurationError(
                    "workflow artifact roles collide with Bambu project roles: "
                    + ", ".join(duplicates)
                )
            expanded_values.update({role: str(path) for role, path in derived.items()})

        artifacts: list[JobArtifact] = []
        for role, raw_path in sorted(expanded_values.items()):
            path = Path(raw_path).resolve()
            if not _within(workspace, path):
                raise ConfigurationError(f"workflow artifact escapes workspace: {path}")
            is_file = path.is_file()
            relative = path.relative_to(workspace).as_posix() if path != workspace else "."
            artifacts.append(
                JobArtifact(
                    artifact_id=role,
                    relative_path=relative,
                    filename=path.name or workspace.name,
                    kind="file" if is_file else "directory",
                    media_type=mimetypes.guess_type(path.name)[0] if is_file else None,
                    size_bytes=path.stat().st_size if is_file else None,
                    sha256=sha256_file(path) if is_file else None,
                    download_url=(
                        f"/api/v1/jobs/{record.job_id}/artifacts/{role}" if is_file else None
                    ),
                )
            )
        return tuple(artifacts)

    def cancel(self, job_id: str) -> JobRecord:
        """Cancel an unreleased job or signal one identity-verified process group."""
        with self._lock:
            self._validate_owned_roots()
            record = self._read_record(job_id)
            if record.state is JobState.QUEUED:
                return self._write_record(
                    record.model_copy(
                        update={
                            "state": JobState.CANCELLED,
                            "cancellation_requested": True,
                            "error": None,
                        }
                    ),
                    message_key="job.cancelled",
                )
            if record.state is JobState.STARTING:
                record = self._reconcile_starting(record)
                if record.state is not JobState.STARTING:
                    return record
                if (
                    record.pid is not None
                    and record.process_identity is not None
                    and record.process_group_id is not None
                ):
                    updated = self._write_record(
                        record.model_copy(
                            update={
                                "state": JobState.CANCELLING,
                                "cancellation_requested": True,
                                "error": None,
                            }
                        ),
                        message_key="job.cancelling",
                    )
                    threading.Thread(
                        target=self._terminate_process,
                        args=(
                            updated.job_id,
                            updated.pid,
                            updated.process_identity,
                            updated.process_group_id,
                        ),
                        name=f"topoforge-cancel-{job_id[:8]}",
                        daemon=True,
                    ).start()
                    return updated
                error = JobError(
                    code="worker-start-cancellation-blocked",
                    message=(
                        "The STARTING worker has not published an identity-bound "
                        "containment-ready record, so TopoForge cannot signal a PID safely."
                    ),
                    corrective_action=(
                        "Wait for containment-ready evidence or the launch deadline. "
                        "TopoForge will keep the launch unreleased and will not start a "
                        "replacement meanwhile."
                    ),
                    exception_type="WorkerIdentityPending",
                )
                return self._write_record(
                    record.model_copy(
                        update={
                            "cancellation_requested": True,
                            "error": error,
                        }
                    ),
                    message_key="job.cancellation-blocked",
                )
            if record.state not in {JobState.RUNNING, JobState.CANCELLING}:
                return record
            if record.pid is not None and (
                record.process_identity is None or record.process_group_id is None
            ):
                error = self._worker_identity_unavailable_error(record.pid)
                return self._write_record(
                    record.model_copy(
                        update={
                            "state": JobState.CANCELLING,
                            "cancellation_requested": True,
                            "error": error,
                        }
                    ),
                    message_key="job.cancellation-blocked",
                )
            updated = self._write_record(
                record.model_copy(
                    update={
                        "state": JobState.CANCELLING,
                        "cancellation_requested": True,
                        "error": None,
                    }
                ),
                message_key="job.cancelling",
            )
            if updated.pid is not None and updated.process_identity is not None:
                threading.Thread(
                    target=self._terminate_process,
                    args=(
                        updated.job_id,
                        updated.pid,
                        updated.process_identity,
                        updated.process_group_id,
                    ),
                    name=f"topoforge-cancel-{job_id[:8]}",
                    daemon=True,
                ).start()
            return updated

    def plan_batch_delete(self, request: JobBatchDeletePlanRequest) -> JobBatchDeletePlan:
        """Measure one terminal-job batch without changing records or workspaces."""
        with self._lock:
            self._validate_owned_roots()
            self.refresh()
            job_ids = tuple(sorted(request.job_ids))
            records = {job_id: self._read_record(job_id) for job_id in job_ids}
            all_records = self._all_records()
            selected = set(job_ids)
            terminal_states = {
                JobState.CANCELLED,
                JobState.COMPLETED,
                JobState.FAILED,
            }
            backups_by_workflow: dict[str, list[str]] = {}
            for backup in self.list_backups():
                backups_by_workflow.setdefault(backup.workflow_id, []).append(backup.backup_id)

            selected_by_workspace: dict[Path, list[JobRecord]] = {}
            references_by_workspace: dict[Path, list[str]] = {}
            source_paths: dict[Path, Path] = {}
            for record in all_records:
                source_path = _lexical_absolute(record.workspace_dir)
                workspace = source_path
                references_by_workspace.setdefault(workspace, []).append(record.job_id)
                if record.job_id in selected:
                    selected_by_workspace.setdefault(workspace, []).append(record)
                    source_paths[workspace] = source_path

            workspace_blockers: dict[Path, tuple[str, ...]] = {}
            workspace_sizes: dict[Path, int] = {}
            backup_job_by_workspace: dict[Path, str] = {}
            workspace_inventories: dict[Path, JobDeletionInventory | None] = {}
            for workspace, workspace_records in selected_by_workspace.items():
                source_path = source_paths[workspace]
                blockers: list[str] = []
                exists = source_path.exists() or source_path.is_symlink()
                workspace_inventory: JobDeletionInventory | None = None
                workspace_sizes[workspace] = 0
                if request.mode is not JobBatchDeleteMode.RECORD_ONLY:
                    try:
                        safe_workspace = _strict_child_path(
                            self.config.workspace_root,
                            source_path,
                            context="batch workspace",
                            require_exists=exists,
                        )
                        if exists:
                            workspace_inventory = _deletion_inventory(
                                safe_workspace,
                                context="batch workspace",
                            )
                            workspace_sizes[workspace] = workspace_inventory.size_bytes
                    except ConfigurationError as exc:
                        blockers.append(str(exc))
                workspace_inventories[workspace] = workspace_inventory
                if request.mode is not JobBatchDeleteMode.RECORD_ONLY:
                    unselected = sorted(set(references_by_workspace.get(workspace, ())) - selected)
                    if unselected:
                        blockers.append(
                            "workspace is still referenced by unselected jobs: "
                            + ", ".join(unselected)
                        )
                    if request.mode is JobBatchDeleteMode.BACKUP_AND_QUARANTINE:
                        if not exists:
                            blockers.append(
                                "workspace is missing and cannot be verified for backup"
                            )
                        candidates = sorted(
                            record.job_id
                            for record in workspace_records
                            if record.state is JobState.COMPLETED and record.summary is not None
                        )
                        if candidates:
                            backup_job_by_workspace[workspace] = candidates[0]
                        else:
                            blockers.append(
                                "verified workflow backup requires a selected completed job"
                            )
                workspace_blockers[workspace] = tuple(blockers)

            items: list[JobBatchDeletePlanItem] = []
            aggregate_blockers: list[str] = []
            for job_id in job_ids:
                record = records[job_id]
                workspace = _lexical_absolute(record.workspace_dir)
                blockers = list(workspace_blockers[workspace])
                job_record_inventory: JobDeletionInventory | None = None
                try:
                    job_root = _strict_child_path(
                        self.jobs_dir,
                        self._job_dir(job_id),
                        context=f"batch job record {job_id}",
                        require_exists=True,
                    )
                    job_record_inventory = _deletion_inventory(
                        job_root,
                        context=f"batch job record {job_id}",
                    )
                except ConfigurationError as exc:
                    blockers.append(str(exc))
                if record.state not in terminal_states:
                    blockers.insert(0, "job is active; cancel it before batch removal")
                workflow_id = record.summary.workflow_id if record.summary is not None else None
                verified_backups = tuple(
                    sorted(backups_by_workflow.get(workflow_id, ()))
                    if workflow_id is not None
                    else ()
                )
                references = tuple(sorted(references_by_workspace.get(workspace, ())))
                unselected_references = tuple(sorted(set(references) - selected))
                item = JobBatchDeletePlanItem(
                    job_id=job_id,
                    state=record.state,
                    workspace=workspace,
                    workspace_existed=workspace.exists() or workspace.is_symlink(),
                    job_record_bytes=(
                        job_record_inventory.size_bytes if job_record_inventory is not None else 0
                    ),
                    workspace_bytes=(
                        0
                        if request.mode is JobBatchDeleteMode.RECORD_ONLY
                        else workspace_sizes[workspace]
                    ),
                    job_record_inventory=job_record_inventory,
                    workspace_inventory=workspace_inventories[workspace],
                    workspace_reference_job_ids=references,
                    unselected_reference_job_ids=unselected_references,
                    verified_backup_ids=verified_backups,
                    eligible=not blockers,
                    blockers=tuple(blockers),
                )
                items.append(item)
                aggregate_blockers.extend(f"{job_id}: {blocker}" for blocker in blockers)

            job_record_bytes = sum(item.job_record_bytes for item in items)
            workspace_bytes = (
                0
                if request.mode is JobBatchDeleteMode.RECORD_ONLY
                else sum(workspace_sizes.values())
            )
            provisional = JobBatchDeletePlan(
                plan_id="0" * 64,
                mode=request.mode,
                job_ids=job_ids,
                items=tuple(items),
                selected_job_count=len(job_ids),
                eligible_job_count=sum(item.eligible for item in items),
                unique_workspace_count=len(selected_by_workspace),
                job_record_bytes=job_record_bytes,
                workspace_bytes=workspace_bytes,
                total_target_bytes=job_record_bytes + workspace_bytes,
                backup_job_ids=tuple(sorted(backup_job_by_workspace.values())),
                blockers=tuple(aggregate_blockers),
                required_checks_passed=not aggregate_blockers,
            )
            identity = provisional.model_dump(mode="json", exclude={"plan_id"})
            plan_id = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
            return provisional.model_copy(update={"plan_id": plan_id})

    def _verify_trash_record(
        self,
        record: JobTrashRecord,
        *,
        expected_batch_id: str,
        expected_path: Path,
    ) -> JobTrashRecord:
        batch_id = self._validate_batch_id(expected_batch_id)
        if record.batch_id != batch_id:
            raise ConfigurationError("trash record batch identity does not match its directory")
        record_path = _lexical_absolute(expected_path)
        if record_path != _lexical_absolute(self._trash_record_path(batch_id)):
            raise ConfigurationError("trash record path does not match its batch identity")
        if record_path.is_symlink() or not record_path.is_file():
            raise ConfigurationError("trash record file is missing or link-like")
        if not record.required_checks_passed:
            raise ConfigurationError("job trash record did not pass its creation checks")
        if record.schema_version != "topoforge-web-job-trash-v2":
            raise ConfigurationError("only inventory-bound trash v2 may be reopened")

        batch_dir = _strict_child_path(
            self.trash_dir,
            self._trash_batch_dir(batch_id),
            context="trash batch",
            require_exists=True,
        )
        if tuple(sorted(path.name for path in batch_dir.iterdir())) != (
            "jobs",
            "trash.json",
        ):
            raise ConfigurationError("trash state batch contains unexpected entries")
        jobs_root = _strict_child_path(
            batch_dir,
            batch_dir / "jobs",
            context="quarantined jobs root",
            require_exists=True,
        )
        if tuple(sorted(path.name for path in jobs_root.iterdir())) != record.job_ids:
            raise ConfigurationError("trash jobs root does not exactly match its record")
        if len(set(record.job_ids)) != len(record.job_ids):
            raise ConfigurationError("trash record contains duplicate job identities")
        inventory_by_job = {item.job_id: item.inventory for item in record.job_inventories}
        for job_id in record.job_ids:
            self._job_dir(job_id)
            quarantined_job = _strict_child_path(
                batch_dir,
                batch_dir / "jobs" / job_id,
                context=f"quarantined job {job_id}",
                require_exists=True,
            )
            if not quarantined_job.is_dir() or path_is_link_like(quarantined_job):
                raise ConfigurationError(f"quarantined job record is missing or unsafe: {job_id}")
            if (
                _deletion_inventory(
                    quarantined_job,
                    context=f"quarantined job {job_id}",
                )
                != inventory_by_job[job_id]
            ):
                raise ConfigurationError(
                    f"quarantined job record changed after publication: {job_id}"
                )

        ordered_workspaces = tuple(
            sorted(
                record.workspaces, key=lambda item: str(_lexical_absolute(item.original_workspace))
            )
        )
        if ordered_workspaces != record.workspaces:
            raise ConfigurationError("trash workspaces are not in canonical path order")
        originals: set[Path] = set()
        quarantines: set[Path] = set()
        workspace_batch = _lexical_absolute(self._workspace_trash_batch_dir(batch_id))
        expected_workspace_names: list[str] = []
        for index, workspace in enumerate(record.workspaces):
            if record.mode is JobBatchDeleteMode.RECORD_ONLY:
                original = _lexical_absolute(workspace.original_workspace)
                workspace_root = _lexical_absolute(self.config.workspace_root)
                if original == workspace_root or workspace_root not in original.parents:
                    raise ConfigurationError(
                        f"trash original workspace is outside its configured root: {original}"
                    )
            else:
                original = _strict_child_path(
                    self.config.workspace_root,
                    workspace.original_workspace,
                    context="trash original workspace",
                    require_exists=False,
                )
            if original in originals:
                raise ConfigurationError("trash record contains duplicate original workspaces")
            originals.add(original)
            expected_quarantine = workspace_batch / (f"{index:04d}-{original.name or 'workspace'}")
            quarantined = workspace.quarantined_workspace
            should_be_quarantined = (
                record.mode is not JobBatchDeleteMode.RECORD_ONLY and workspace.workspace_existed
            )
            if not should_be_quarantined:
                if quarantined is not None:
                    raise ConfigurationError(
                        "trash record has a quarantine path for a retained or missing workspace"
                    )
                if (
                    workspace.workspace_existed
                    and record.mode is not JobBatchDeleteMode.RECORD_ONLY
                ):
                    retained = _strict_child_path(
                        self.config.workspace_root,
                        original,
                        context="retained workspace",
                        require_exists=True,
                    )
                    if not retained.is_dir():
                        raise ConfigurationError(
                            f"retained workspace is not a directory: {retained}"
                        )
                continue
            if quarantined is None:
                raise ConfigurationError("trash record is missing a required quarantine path")
            actual_quarantine = _lexical_absolute(quarantined)
            if actual_quarantine != expected_quarantine:
                raise ConfigurationError(
                    "quarantined workspace path does not match its exact batch-derived path"
                )
            if actual_quarantine in quarantines:
                raise ConfigurationError("trash record contains duplicate quarantine workspaces")
            quarantines.add(actual_quarantine)
            verified_quarantine = _strict_child_path(
                workspace_batch,
                actual_quarantine,
                context="quarantined workspace",
                require_exists=True,
            )
            if not verified_quarantine.is_dir() or path_is_link_like(verified_quarantine):
                raise ConfigurationError(
                    f"quarantined workspace is missing or unsafe: {verified_quarantine}"
                )
            expected_workspace_names.append(verified_quarantine.name)
            if workspace.inventory is None or (
                _deletion_inventory(
                    verified_quarantine,
                    context=f"quarantined workspace {verified_quarantine}",
                )
                != workspace.inventory
            ):
                raise ConfigurationError(
                    f"quarantined workspace changed after publication: {verified_quarantine}"
                )
            if original.exists() or original.is_symlink():
                raise ConfigurationError(
                    f"original workspace still exists beside its quarantine: {original}"
                )

        if expected_workspace_names:
            verified_workspace_batch = _strict_child_path(
                self.workspace_trash_dir,
                workspace_batch,
                context="workspace trash batch",
                require_exists=True,
            )
            if tuple(sorted(path.name for path in verified_workspace_batch.iterdir())) != tuple(
                expected_workspace_names
            ):
                raise ConfigurationError("workspace trash batch does not exactly match its record")
        elif workspace_batch.exists() or workspace_batch.is_symlink():
            raise ConfigurationError("unexpected empty workspace trash batch exists")

        for backup_id in record.backup_ids:
            self.backup_archive_path(backup_id)
        return record

    def list_trash(self) -> tuple[JobTrashRecord, ...]:
        """Strictly reopen recoverable batches newest first."""
        with self._lock:
            self._validate_owned_roots()
            if not self.trash_dir.exists():
                return ()
            records = [
                self._read_trash_record(path.parent.name, expected_path=path)
                for path in self.trash_dir.glob("*/trash.json")
            ]
            return tuple(sorted(records, key=lambda item: item.created_at, reverse=True))

    def apply_batch_delete(self, request: JobBatchDeleteApplyRequest) -> JobTrashRecord:
        """Apply one unchanged reviewed plan by moving records and workspaces to trash."""
        with self._lock:
            self._validate_owned_roots()
            plan = self.plan_batch_delete(
                JobBatchDeletePlanRequest(job_ids=request.job_ids, mode=request.mode)
            )
            if request.confirm_plan_id != plan.plan_id:
                raise ConfigurationError(
                    "batch deletion plan changed; review the new measured plan before applying"
                )
            if not plan.required_checks_passed:
                raise ConfigurationError(
                    "batch deletion plan has blockers: " + "; ".join(plan.blockers)
                )

            backup_ids = tuple(
                sorted({self.create_backup(job_id).backup_id for job_id in plan.backup_job_ids})
            )
            batch_id = uuid4().hex
            state_temporary = self.trash_dir / f".{batch_id}.creating"
            state_destination = self._trash_batch_dir(batch_id)
            workspace_temporary = self.workspace_trash_dir / f".{batch_id}.creating"
            workspace_destination = self._workspace_trash_batch_dir(batch_id)
            transaction_dir = self._trash_transaction_dir(batch_id)
            if any(
                path.exists() or path.is_symlink()
                for path in (
                    state_temporary,
                    state_destination,
                    workspace_temporary,
                    workspace_destination,
                    transaction_dir,
                )
            ):
                raise ConfigurationError("generated trash batch destination already exists")

            workspace_items: dict[Path, JobBatchDeletePlanItem] = {}
            job_items = {item.job_id: item for item in plan.items}
            for item in plan.items:
                workspace_items.setdefault(item.workspace, item)
            workspace_moves: list[JobTrashTransactionMove] = []
            trash_workspaces: list[JobTrashWorkspace] = []
            for index, (workspace, item) in enumerate(sorted(workspace_items.items())):
                existed = item.workspace_existed
                size_bytes = item.workspace_bytes
                quarantined: Path | None = None
                if request.mode is not JobBatchDeleteMode.RECORD_ONLY and existed:
                    name = f"{index:04d}-{workspace.name or 'workspace'}"
                    temporary = workspace_temporary / name
                    quarantined = workspace_destination / name
                    workspace_moves.append(
                        JobTrashTransactionMove(
                            source=workspace,
                            temporary=temporary,
                            destination=quarantined,
                        )
                    )
                trash_workspaces.append(
                    JobTrashWorkspace(
                        original_workspace=workspace,
                        quarantined_workspace=quarantined,
                        workspace_existed=item.workspace_existed,
                        size_bytes=size_bytes,
                        inventory=(item.workspace_inventory if quarantined is not None else None),
                    )
                )

            job_moves = tuple(
                JobTrashTransactionMove(
                    source=self._job_dir(job_id),
                    temporary=state_temporary / "jobs" / job_id,
                    destination=state_destination / "jobs" / job_id,
                )
                for job_id in plan.job_ids
            )
            created_at = utc_now()
            trash_record = JobTrashRecord(
                batch_id=batch_id,
                plan_id=plan.plan_id,
                mode=plan.mode,
                created_at=created_at,
                purge_after=created_at + timedelta(days=7),
                job_ids=plan.job_ids,
                job_record_bytes=plan.job_record_bytes,
                job_inventories=tuple(
                    JobTrashJobInventory(
                        job_id=job_id,
                        inventory=cast(
                            JobDeletionInventory,
                            job_items[job_id].job_record_inventory,
                        ),
                    )
                    for job_id in plan.job_ids
                ),
                workspaces=tuple(trash_workspaces),
                backup_ids=backup_ids,
                total_quarantined_bytes=plan.total_target_bytes,
                backups_preserved=True,
                required_checks_passed=True,
            )
            transaction = self._validate_trash_transaction(
                JobTrashTransaction(
                    batch_id=batch_id,
                    state_temporary=state_temporary,
                    state_destination=state_destination,
                    workspace_temporary=workspace_temporary,
                    workspace_destination=workspace_destination,
                    job_moves=job_moves,
                    workspace_moves=tuple(workspace_moves),
                    trash_record=trash_record,
                )
            )

            self._create_owned_directory(
                transaction_dir,
                root=self.trash_transactions_dir,
                context="trash apply transaction directory",
            )
            self._write_owned_model(
                self._trash_transaction_path(batch_id),
                transaction,
                root=self.trash_transactions_dir,
                context="trash apply transaction",
            )
            self._create_owned_directory(
                state_temporary,
                root=self.trash_dir,
                context="trash state staging directory",
            )
            self._create_owned_directory(
                state_temporary / "jobs",
                root=self.trash_dir,
                context="trash jobs staging directory",
            )
            if workspace_moves:
                self._create_owned_directory(
                    workspace_temporary,
                    root=self.workspace_trash_dir,
                    context="trash workspace staging directory",
                )

            try:
                for move in transaction.workspace_moves:
                    item = workspace_items[move.source]
                    expected_inventory = item.workspace_inventory
                    if expected_inventory is None:
                        raise ConfigurationError(
                            f"workspace inventory is missing during apply: {move.source}"
                        )
                    current_inventory = _deletion_inventory(
                        _strict_child_path(
                            self.config.workspace_root,
                            move.source,
                            context="batch workspace move",
                            require_exists=True,
                        ),
                        context="batch workspace move",
                    )
                    if current_inventory != expected_inventory:
                        raise ConfigurationError(
                            f"workspace changed during batch apply: {move.source}"
                        )
                    self._validate_owned_roots()
                    self._move_owned_directory(
                        move.source,
                        move.temporary,
                        source_root=self.config.workspace_root,
                        destination_root=self.workspace_trash_dir,
                        inventory=expected_inventory,
                        context="batch workspace quarantine",
                    )
                    if (
                        _deletion_inventory(
                            move.temporary,
                            context="quarantined workspace move",
                        )
                        != expected_inventory
                    ):
                        raise ConfigurationError(
                            f"workspace identity changed while quarantining: {move.source}"
                        )
                for move in transaction.job_moves:
                    item = job_items[move.source.name]
                    expected_inventory = item.job_record_inventory
                    if expected_inventory is None:
                        raise ConfigurationError(
                            f"job inventory is missing during apply: {move.source.name}"
                        )
                    current_inventory = _deletion_inventory(
                        _strict_child_path(
                            self.jobs_dir,
                            move.source,
                            context=f"batch job move {move.source.name}",
                            require_exists=True,
                        ),
                        context=f"batch job move {move.source.name}",
                    )
                    if current_inventory != expected_inventory:
                        raise ConfigurationError(
                            f"job record changed during batch apply: {move.source.name}"
                        )
                    self._validate_owned_roots()
                    self._move_owned_directory(
                        move.source,
                        move.temporary,
                        source_root=self.jobs_dir,
                        destination_root=self.trash_dir,
                        inventory=expected_inventory,
                        context=f"batch job quarantine {move.source.name}",
                    )
                    if (
                        _deletion_inventory(
                            move.temporary,
                            context=f"quarantined job move {move.source.name}",
                        )
                        != expected_inventory
                    ):
                        raise ConfigurationError(
                            f"job identity changed while quarantining: {move.source.name}"
                        )
                self._write_owned_model(
                    state_temporary / "trash.json",
                    trash_record,
                    root=self.trash_dir,
                    context="trash record",
                )
                published = self._publish_trash_transaction(transaction)
            except BaseException:
                if self._trash_transaction_path(batch_id).is_file():
                    self._rollback_trash_transaction(transaction)
                raise
            for job_id in plan.job_ids:
                self._processes.pop(job_id, None)
            return published

    def _begin_trash_action(
        self,
        record: JobTrashRecord,
        action: JobTrashActionKind,
    ) -> JobTrashActionTransaction:
        self._validate_owned_roots()
        batch_id = record.batch_id
        action_path = self._trash_action_path(batch_id)
        transaction_dir = action_path.parent
        if action_path.exists() or action_path.is_symlink():
            existing = self._read_trash_action(batch_id)
            if existing.action is not action:
                raise ConfigurationError("a different durable action already owns this trash batch")
            return existing
        if (
            self._trash_transaction_path(batch_id).exists()
            or self._trash_transaction_path(batch_id).is_symlink()
        ):
            raise ConfigurationError("trash apply transaction has not finished")
        transaction_dir_exists = transaction_dir.exists() or transaction_dir.is_symlink()
        if transaction_dir_exists:
            _strict_child_path(
                self.trash_transactions_dir,
                transaction_dir,
                context="trash action directory",
                require_exists=True,
            )
            if any(transaction_dir.iterdir()):
                raise ConfigurationError("trash action directory contains unexpected durable state")
        state_batch = self._trash_batch_dir(batch_id)
        workspace_batch = self._workspace_trash_batch_dir(batch_id)
        state_inventory = _deletion_inventory(
            state_batch,
            context="trash action state batch",
        )
        has_workspace_batch = any(
            workspace.quarantined_workspace is not None for workspace in record.workspaces
        )
        workspace_inventory = (
            _deletion_inventory(
                workspace_batch,
                context="trash action workspace batch",
            )
            if has_workspace_batch
            else None
        )
        purge_entries: tuple[JobTrashPurgeEntry, ...] = ()
        if action is JobTrashActionKind.PURGE:
            workspace_entries = (
                _purge_manifest(
                    workspace_batch,
                    root_name="workspace",
                    context="workspace trash purge manifest",
                )
                if workspace_inventory is not None
                else ()
            )
            state_entries = _purge_manifest(
                state_batch,
                root_name="state",
                context="state trash purge manifest",
            )
            if _deletion_inventory(
                state_batch,
                context="state trash purge manifest",
            ) != state_inventory or (
                workspace_inventory is not None
                and _deletion_inventory(
                    workspace_batch,
                    context="workspace trash purge manifest",
                )
                != workspace_inventory
            ):
                raise ConfigurationError("trash batch changed while purge intent was built")
            purge_entries = (*workspace_entries, *state_entries)
            if len(purge_entries) > _MAX_PURGE_MANIFEST_ENTRIES:
                raise ConfigurationError(
                    "trash purge manifest exceeds the 20,000-entry safety limit; "
                    "restore the batch and purge smaller batches, or inspect and remove "
                    "the retained quarantine manually"
                )
        if not has_workspace_batch and (workspace_batch.exists() or workspace_batch.is_symlink()):
            raise ConfigurationError("unexpected workspace batch blocks trash action")
        label = "restored" if action is JobTrashActionKind.RESTORE else "purged"
        created_at = utc_now()
        affected_bytes = record.total_quarantined_bytes
        audit_payload = self._trash_audit_payload(
            record,
            action=label,
            affected_bytes=affected_bytes,
            occurred_at=created_at.isoformat(),
        )
        state_identity = self._owned_root_identities[self.config.state_dir]
        workspace_identity = self._owned_root_identities[self.config.workspace_root]
        transaction = self._validate_trash_action(
            JobTrashActionTransaction(
                batch_id=batch_id,
                action=action,
                created_at=created_at,
                affected_bytes=affected_bytes,
                record_sha256=hashlib.sha256(_canonical_bytes(record)).hexdigest(),
                audit_sha256=hashlib.sha256(_canonical_bytes(audit_payload)).hexdigest(),
                state_root_device=state_identity[0],
                state_root_inode=state_identity[1],
                workspace_root_device=workspace_identity[0],
                workspace_root_inode=workspace_identity[1],
                state_batch=state_batch,
                workspace_batch=workspace_batch,
                state_purging=self._state_purging_path(batch_id),
                workspace_purging=self._workspace_purging_path(batch_id),
                state_batch_inventory=state_inventory,
                workspace_batch_inventory=workspace_inventory,
                purge_entries=purge_entries,
                audit_path=self.deletion_audit_dir / f"{batch_id}-{label}.json",
                trash_record=record,
            )
        )
        worst_case = transaction.model_copy(
            update={
                "phase": JobTrashActionPhase.FINALIZING,
                "purge_index": len(transaction.purge_entries),
            }
        )
        self._check_trash_action_size(worst_case)
        self._check_trash_action_size(transaction)
        self._validate_owned_roots()
        if not transaction_dir_exists:
            self._create_owned_directory(
                transaction_dir,
                root=self.trash_transactions_dir,
                context="trash action directory",
            )
        else:
            _strict_child_path(
                self.trash_transactions_dir,
                transaction_dir,
                context="trash action directory",
                require_exists=True,
            )
        try:
            write_exclusive_owned_regular_bytes(
                action_path,
                _canonical_bytes(transaction),
                root=self.trash_transactions_dir,
                root_identity=self._owned_identity(self.trash_transactions_dir),
                context="trash action transaction",
            )
        except FileExistsError:
            return self._read_trash_action(batch_id)
        return self._read_trash_action(batch_id)

    def _set_trash_action_phase(
        self,
        transaction: JobTrashActionTransaction,
        phase: JobTrashActionPhase,
    ) -> JobTrashActionTransaction:
        order = {
            JobTrashActionPhase.PREPARED: 0,
            JobTrashActionPhase.MOVING: 1,
            JobTrashActionPhase.FINALIZING: 2,
        }
        if order[phase] < order[transaction.phase]:
            raise ConfigurationError("trash action phase cannot move backwards")
        if phase is transaction.phase:
            return transaction
        updated = transaction.model_copy(update={"phase": phase})
        self._check_trash_action_size(updated)
        self._write_owned_model(
            self._trash_action_path(transaction.batch_id),
            updated,
            root=self.trash_transactions_dir,
            context="trash action transaction",
        )
        return self._read_trash_action(transaction.batch_id)

    def _write_trash_audit(
        self,
        record: JobTrashRecord,
        *,
        action: str,
        affected_bytes: int,
        occurred_at: str | None = None,
    ) -> None:
        self._validate_owned_roots()
        path = self.deletion_audit_dir / f"{record.batch_id}-{action}.json"
        payload = _canonical_bytes(
            self._trash_audit_payload(
                record,
                action=action,
                affected_bytes=affected_bytes,
                occurred_at=occurred_at or utc_now().isoformat(),
            )
        )
        try:
            write_exclusive_owned_regular_bytes(
                path,
                payload,
                root=self.deletion_audit_dir,
                root_identity=self._owned_identity(self.deletion_audit_dir),
                context="trash terminal audit",
            )
        except FileExistsError:
            try:
                observed = self._read_owned_payload(
                    path,
                    root=self.deletion_audit_dir,
                    context="trash terminal audit",
                    max_bytes=1024 * 1024,
                )
            except (ValueError, ConfigurationError) as exc:
                raise ConfigurationError("existing trash audit is unsafe") from exc
            if observed != payload:
                raise ConfigurationError(
                    "existing trash audit does not match the durable action"
                ) from None

    def _restore_inventory_move(
        self,
        *,
        source: Path,
        destination: Path,
        destination_root: Path,
        inventory: JobDeletionInventory,
        context: str,
    ) -> None:
        locations: list[Path] = []
        for path in (source, destination):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if stat_result_is_link_like(metadata):
                raise ConfigurationError(f"{context} path is link-like: {path}")
            locations.append(path)
        if len(locations) != 1:
            raise ConfigurationError(f"{context} requires exactly one source or destination copy")
        current = locations[0]
        if _deletion_inventory(current, context=context) != inventory:
            raise ConfigurationError(f"{context} content identity changed: {current}")
        if current == source:
            self._validate_owned_roots()
            _strict_child_path(
                destination_root,
                destination,
                context=context,
                require_exists=False,
            )
            self._move_owned_directory(
                source,
                destination,
                source_root=(
                    self.trash_dir
                    if destination_root == self.jobs_dir
                    else self.workspace_trash_dir
                ),
                destination_root=destination_root,
                inventory=inventory,
                context=context,
            )
            if _deletion_inventory(destination, context=context) != inventory:
                raise ConfigurationError(
                    f"{context} identity changed during restore: {destination}"
                )

    def _finish_restore_action(
        self,
        transaction: JobTrashActionTransaction,
    ) -> JobTrashActionResult:
        transaction = self._validate_trash_action(transaction)
        if transaction.action is not JobTrashActionKind.RESTORE:
            raise ConfigurationError("durable trash action is not a restore")
        if transaction.phase is JobTrashActionPhase.PREPARED:
            transaction = self._set_trash_action_phase(
                transaction,
                JobTrashActionPhase.MOVING,
            )
        record = transaction.trash_record
        for workspace in record.workspaces:
            if workspace.inventory is None:
                if workspace.workspace_existed:
                    retained = _strict_child_path(
                        self.config.workspace_root,
                        workspace.original_workspace,
                        context="retained workspace during trash restore",
                        require_exists=True,
                    )
                    if not retained.is_dir():
                        raise ConfigurationError(
                            f"retained workspace is not a directory: {retained}"
                        )
                continue
            quarantined = workspace.quarantined_workspace
            if quarantined is None:
                raise ConfigurationError("trash restore workspace source is missing")
            self._restore_inventory_move(
                source=quarantined,
                destination=workspace.original_workspace,
                destination_root=self.config.workspace_root,
                inventory=workspace.inventory,
                context="trash workspace restore",
            )
        job_inventories = {item.job_id: item.inventory for item in record.job_inventories}
        for job_id in record.job_ids:
            self._restore_inventory_move(
                source=transaction.state_batch / "jobs" / job_id,
                destination=self._job_dir(job_id),
                destination_root=self.jobs_dir,
                inventory=job_inventories[job_id],
                context=f"trash job restore {job_id}",
            )
        transaction = self._set_trash_action_phase(
            transaction,
            JobTrashActionPhase.FINALIZING,
        )
        self._validate_owned_roots()
        workspace_batch = transaction.workspace_batch
        try:
            workspace_metadata = workspace_batch.lstat()
        except FileNotFoundError:
            workspace_metadata = None
        if workspace_metadata is not None:
            if stat_result_is_link_like(workspace_metadata) or not stat.S_ISDIR(
                workspace_metadata.st_mode
            ):
                raise ConfigurationError("workspace trash batch became unsafe during restore")
            if any(workspace_batch.iterdir()):
                raise ConfigurationError(
                    "workspace trash batch is not empty after complete restore"
                )
            self._remove_owned_entry(
                workspace_batch,
                root=self.workspace_trash_dir,
                metadata=workspace_metadata,
                directory=True,
                context="workspace trash batch finalization",
            )
        state_batch = transaction.state_batch
        try:
            state_metadata = state_batch.lstat()
        except FileNotFoundError:
            state_metadata = None
        if state_metadata is not None:
            if stat_result_is_link_like(state_metadata) or not stat.S_ISDIR(state_metadata.st_mode):
                raise ConfigurationError("state trash batch became unsafe during restore")
            record_path = state_batch / "trash.json"
            try:
                record_metadata = record_path.lstat()
            except FileNotFoundError:
                record_metadata = None
            if record_metadata is not None:
                if stat_result_is_link_like(record_metadata) or not stat.S_ISREG(
                    record_metadata.st_mode
                ):
                    raise ConfigurationError("trash restore record became unsafe")
                observed = self._read_owned_payload(
                    record_path,
                    root=self.trash_dir,
                    context="trash restore record",
                    max_bytes=8 * 1024 * 1024,
                )
                if observed != _canonical_bytes(record):
                    raise ConfigurationError("trash record changed before restore finalization")
                self._remove_owned_entry(
                    record_path,
                    root=self.trash_dir,
                    metadata=record_metadata,
                    directory=False,
                    context="trash restore record",
                )
            jobs_root = state_batch / "jobs"
            try:
                jobs_metadata = jobs_root.lstat()
            except FileNotFoundError:
                jobs_metadata = None
            if jobs_metadata is not None:
                if stat_result_is_link_like(jobs_metadata) or not stat.S_ISDIR(
                    jobs_metadata.st_mode
                ):
                    raise ConfigurationError("trash jobs root became unsafe during restore")
                if any(jobs_root.iterdir()):
                    raise ConfigurationError("trash jobs root is not empty after complete restore")
                self._remove_owned_entry(
                    jobs_root,
                    root=self.trash_dir,
                    metadata=jobs_metadata,
                    directory=True,
                    context="trash jobs root finalization",
                )
            if any(state_batch.iterdir()):
                raise ConfigurationError("trash state batch has unexpected finalization entries")
            self._remove_owned_entry(
                state_batch,
                root=self.trash_dir,
                metadata=state_metadata,
                directory=True,
                context="trash state batch finalization",
            )
        self._write_trash_audit(
            record,
            action="restored",
            affected_bytes=transaction.affected_bytes,
            occurred_at=transaction.created_at.isoformat(),
        )
        action_path = self._trash_action_path(record.batch_id)
        action_metadata = action_path.lstat()
        self._remove_owned_entry(
            action_path,
            root=self.trash_transactions_dir,
            metadata=action_metadata,
            directory=False,
            context="trash restore action",
        )
        action_parent_metadata = action_path.parent.lstat()
        self._remove_owned_entry(
            action_path.parent,
            root=self.trash_transactions_dir,
            metadata=action_parent_metadata,
            directory=True,
            context="trash restore action directory",
        )
        return JobTrashActionResult(
            batch_id=record.batch_id,
            action="restored",
            job_ids=record.job_ids,
            workspace_count=len(record.workspaces),
            affected_bytes=transaction.affected_bytes,
            backups_preserved=True,
            required_checks_passed=True,
        )

    def _prepare_purge_batch(
        self,
        *,
        source: Path,
        destination: Path,
        inventory: JobDeletionInventory | None,
        context: str,
    ) -> None:
        source_exists = source.exists() or source.is_symlink()
        destination_exists = destination.exists() or destination.is_symlink()
        if inventory is None:
            if source_exists or destination_exists:
                raise ConfigurationError(f"{context} unexpectedly exists")
            return
        if source_exists == destination_exists:
            raise ConfigurationError(f"{context} requires exactly one published or purging batch")
        current = source if source_exists else destination
        if _deletion_inventory(current, context=context) != inventory:
            raise ConfigurationError(f"{context} changed before purge")
        if current == source:
            self._validate_owned_roots()
            owned_root = (
                self.workspace_trash_dir
                if source.parent == self.workspace_trash_dir
                else self.trash_dir
            )
            self._move_owned_directory(
                source,
                destination,
                source_root=owned_root,
                destination_root=owned_root,
                inventory=inventory,
                context=context,
            )
            if _deletion_inventory(destination, context=context) != inventory:
                raise ConfigurationError(f"{context} changed while entering purge")

    def _set_purge_index(
        self,
        transaction: JobTrashActionTransaction,
        index: int,
    ) -> JobTrashActionTransaction:
        if index < transaction.purge_index or index > len(transaction.purge_entries):
            raise ConfigurationError("trash purge cursor cannot move backwards or out of bounds")
        if index == transaction.purge_index:
            return transaction
        updated = transaction.model_copy(update={"purge_index": index})
        self._check_trash_action_size(updated)
        self._write_owned_model(
            self._trash_action_path(transaction.batch_id),
            updated,
            root=self.trash_transactions_dir,
            context="trash action transaction",
        )
        return self._read_trash_action(transaction.batch_id)

    def _remove_purge_entry(
        self,
        transaction: JobTrashActionTransaction,
        entry: JobTrashPurgeEntry,
    ) -> None:
        root = transaction.state_purging if entry.root == "state" else transaction.workspace_purging
        path = (
            root
            if entry.relative_path == "."
            else root.joinpath(*PurePosixPath(entry.relative_path).parts)
        )
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if (
            stat_result_is_link_like(metadata)
            or metadata.st_dev != entry.device
            or metadata.st_ino != entry.inode
        ):
            raise ConfigurationError(f"trash purge entry identity changed: {path}")
        self._validate_owned_roots()
        if entry.kind == "file":
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != entry.link_count
                or metadata.st_size != entry.size_bytes
                or metadata.st_mtime_ns != entry.modified_time_ns
            ):
                raise ConfigurationError(f"trash purge file metadata changed: {path}")
            owned_root = self.trash_dir if entry.root == "state" else self.workspace_trash_dir
            payload = self._read_owned_payload(
                path,
                root=owned_root,
                context="trash purge file",
                max_bytes=max(entry.size_bytes, 1),
            )
            if hashlib.sha256(payload).hexdigest() != entry.sha256:
                raise ConfigurationError(f"trash purge file content changed: {path}")
            final_metadata = path.lstat()
            if final_metadata.st_dev != entry.device or final_metadata.st_ino != entry.inode:
                raise ConfigurationError(f"trash purge file changed before unlink: {path}")
            self._remove_owned_entry(
                path,
                root=owned_root,
                metadata=final_metadata,
                directory=False,
                context="trash purge file",
            )
            return
        if not stat.S_ISDIR(metadata.st_mode):
            raise ConfigurationError(f"trash purge directory became unsafe: {path}")
        try:
            self._remove_owned_entry(
                path,
                root=(self.trash_dir if entry.root == "state" else self.workspace_trash_dir),
                metadata=metadata,
                directory=True,
                context="trash purge directory",
            )
        except (OSError, ConfigurationError) as exc:
            raise ConfigurationError(
                f"trash purge directory contains an unexpected remaining entry: {path}"
            ) from exc

    def _finish_purge_action(
        self,
        transaction: JobTrashActionTransaction,
    ) -> JobTrashActionResult:
        transaction = self._validate_trash_action(transaction)
        if transaction.action is not JobTrashActionKind.PURGE:
            raise ConfigurationError("durable trash action is not a purge")
        if transaction.phase is JobTrashActionPhase.PREPARED:
            transaction = self._set_trash_action_phase(
                transaction,
                JobTrashActionPhase.MOVING,
            )
        if transaction.phase is JobTrashActionPhase.MOVING:
            self._prepare_purge_batch(
                source=transaction.workspace_batch,
                destination=transaction.workspace_purging,
                inventory=transaction.workspace_batch_inventory,
                context="workspace trash purge",
            )
            self._prepare_purge_batch(
                source=transaction.state_batch,
                destination=transaction.state_purging,
                inventory=transaction.state_batch_inventory,
                context="state trash purge",
            )
            transaction = self._set_trash_action_phase(
                transaction,
                JobTrashActionPhase.FINALIZING,
            )
        for path in (transaction.state_batch, transaction.workspace_batch):
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            raise ConfigurationError("published trash batch remained after purge rename")
        for index in range(transaction.purge_index, len(transaction.purge_entries)):
            entry = transaction.purge_entries[index]
            self._remove_purge_entry(transaction, entry)
            transaction = self._set_purge_index(transaction, index + 1)
        for path in (transaction.state_purging, transaction.workspace_purging):
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            raise ConfigurationError("trash purge manifest did not remove every batch root")
        self._write_trash_audit(
            transaction.trash_record,
            action="purged",
            affected_bytes=transaction.affected_bytes,
            occurred_at=transaction.created_at.isoformat(),
        )
        action_path = self._trash_action_path(transaction.batch_id)
        action_metadata = action_path.lstat()
        self._remove_owned_entry(
            action_path,
            root=self.trash_transactions_dir,
            metadata=action_metadata,
            directory=False,
            context="trash purge action",
        )
        action_parent_metadata = action_path.parent.lstat()
        self._remove_owned_entry(
            action_path.parent,
            root=self.trash_transactions_dir,
            metadata=action_parent_metadata,
            directory=True,
            context="trash purge action directory",
        )
        record = transaction.trash_record
        return JobTrashActionResult(
            batch_id=record.batch_id,
            action="purged",
            job_ids=record.job_ids,
            workspace_count=len(record.workspaces),
            affected_bytes=transaction.affected_bytes,
            backups_preserved=True,
            required_checks_passed=True,
        )

    def restore_trash(
        self,
        batch_id: str,
        request: JobTrashActionRequest,
    ) -> JobTrashActionResult:
        """Restore every job record and quarantined workspace in one batch."""
        with self._lock:
            self._validate_owned_roots()
            if request.confirm_batch_id != batch_id:
                raise ConfigurationError(
                    "trash restore confirmation does not match the selected batch"
                )
            if (
                self._trash_action_path(batch_id).exists()
                or self._trash_action_path(batch_id).is_symlink()
            ):
                transaction = self._read_trash_action(batch_id)
            else:
                record = self._read_trash_record(
                    batch_id,
                    expected_path=self._trash_record_path(batch_id),
                )
                transaction = self._begin_trash_action(
                    record,
                    JobTrashActionKind.RESTORE,
                )
            return self._finish_restore_action(transaction)

    def purge_trash(
        self,
        batch_id: str,
        request: JobTrashActionRequest,
    ) -> JobTrashActionResult:
        """Permanently remove one reviewed trash batch while preserving backups."""
        with self._lock:
            self._validate_owned_roots()
            if request.confirm_batch_id != batch_id:
                raise ConfigurationError(
                    "trash purge confirmation does not match the selected batch"
                )
            if (
                self._trash_action_path(batch_id).exists()
                or self._trash_action_path(batch_id).is_symlink()
            ):
                transaction = self._read_trash_action(batch_id)
            else:
                record = self._read_trash_record(
                    batch_id,
                    expected_path=self._trash_record_path(batch_id),
                )
                transaction = self._begin_trash_action(
                    record,
                    JobTrashActionKind.PURGE,
                )
            return self._finish_purge_action(transaction)

    def delete(
        self,
        job_id: str,
        request: JobDeleteRequest,
    ) -> JobDeleteResult:
        """Remove one terminal job record and optionally its unshared workspace."""
        with self._lock:
            self.refresh()
            record = self._read_record(job_id)
            if request.confirm_job_id != job_id:
                raise ConfigurationError(
                    "job deletion confirmation does not match the selected job"
                )
            terminal_states = {
                JobState.CANCELLED,
                JobState.COMPLETED,
                JobState.FAILED,
            }
            if record.state not in terminal_states:
                raise ConfigurationError(
                    "only cancelled, failed, or completed jobs can be deleted; "
                    "cancel the selected job first"
                )

            self._validate_owned_roots()
            mode = (
                JobBatchDeleteMode.QUARANTINE_WORKSPACE
                if request.delete_workspace
                else JobBatchDeleteMode.RECORD_ONLY
            )
            plan_request = JobBatchDeletePlanRequest(job_ids=(job_id,), mode=mode)
            plan = self.plan_batch_delete(plan_request)
            if not plan.required_checks_passed:
                blocker_text = "; ".join(plan.blockers)
                if "referenced by unselected jobs" in blocker_text:
                    raise ConfigurationError(
                        "job workspace is still referenced by other jobs: "
                        + blocker_text.rsplit(": ", 1)[-1]
                    )
                if "outside its configured root" in blocker_text:
                    raise ConfigurationError(
                        "job workspace is outside the configured workspace root"
                    )
                if "link-like" in blocker_text:
                    raise ConfigurationError(
                        "job workspace is a symlink and will not be recursively deleted"
                    )
                raise ConfigurationError("job deletion plan has blockers: " + blocker_text)
            item = plan.items[0]
            trash = self.apply_batch_delete(
                JobBatchDeleteApplyRequest(
                    job_ids=plan.job_ids,
                    mode=plan.mode,
                    confirm_plan_id=plan.plan_id,
                )
            )
            self.purge_trash(
                trash.batch_id,
                JobTrashActionRequest(confirm_batch_id=trash.batch_id),
            )
            workspace = _lexical_absolute(record.workspace_dir)
            workspace_retained = workspace.exists() or workspace.is_symlink()
            workspace_removed = (
                request.delete_workspace and item.workspace_existed and not workspace_retained
            )
            deleted_workspace_bytes = item.workspace_bytes if workspace_removed else 0
            return JobDeleteResult(
                job_id=job_id,
                previous_state=record.state,
                workspace=workspace,
                workspace_existed=item.workspace_existed,
                workspace_removed=workspace_removed,
                workspace_retained=workspace_retained,
                deleted_job_record_bytes=item.job_record_bytes,
                deleted_workspace_bytes=deleted_workspace_bytes,
                reclaimed_bytes=item.job_record_bytes + deleted_workspace_bytes,
                backups_preserved=True,
                required_checks_passed=True,
            )

    def _terminate_process(
        self,
        job_id: str,
        pid: int,
        identity: str,
        process_group: int | None,
    ) -> None:
        with self._lock:
            try:
                self._validate_owned_roots()
            except ConfigurationError:
                return
            try:
                terminate_process_tree(
                    pid,
                    expected_identity=identity,
                    process_group=process_group,
                )
            except Exception as exc:
                _LOGGER.exception("Could not terminate Web job %s", job_id, exc_info=exc)
                error = JobError(
                    code="worker-termination-failed",
                    message=f"The isolated worker could not be terminated safely: {exc}",
                    corrective_action=(
                        "Retry cancellation. If the same verified worker remains, close "
                        "TopoForge and stop that process from the operating-system task manager."
                    ),
                    exception_type=exc.__class__.__name__,
                )
                try:
                    record = self._read_record(job_id)
                except KeyError:
                    return
                if (
                    record.state is JobState.CANCELLING
                    and record.pid == pid
                    and record.process_identity == identity
                ):
                    self._write_record(
                        record.model_copy(update={"error": error}),
                        message_key="job.cancellation-failed",
                    )

    def _artifact_record(
        self,
        job_id: str,
        artifact_id: str,
        *,
        kind: str,
    ) -> tuple[JobRecord, JobArtifact]:
        record = self.get(job_id)
        artifact = next(
            (item for item in record.artifacts if item.artifact_id == artifact_id),
            None,
        )
        if artifact is None or artifact.kind != kind:
            raise KeyError(artifact_id)
        return record, artifact

    def workspace_relative_path(
        self,
        base: Path,
        relative: str,
        *,
        context: str,
    ) -> Path:
        """Return a lexical workspace child after validating its owned directory base."""
        workspace_root = self.config.workspace_root
        base_path = _lexical_absolute(base)
        if base_path != workspace_root and workspace_root not in base_path.parents:
            raise ConfigurationError(f"{context} base escapes the configured workspace root")
        try:
            owned_directory_identity(
                base_path,
                root=workspace_root,
                root_identity=self._owned_identity(workspace_root),
                context=f"{context} base",
            )
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"{context} base is missing or unsafe") from exc
        pure = PurePosixPath(relative)
        if (
            not relative
            or "\\" in relative
            or ":" in relative
            or pure.is_absolute()
            or ".." in pure.parts
        ):
            raise ConfigurationError(f"{context} path is not a safe relative path: {relative}")
        parts = tuple(part for part in pure.parts if part != ".")
        candidate = _lexical_absolute(base_path.joinpath(*parts))
        if candidate != base_path and base_path not in candidate.parents:
            raise ConfigurationError(f"{context} path escapes its workspace base")
        return candidate

    def _artifact_path(
        self,
        record: JobRecord,
        artifact: JobArtifact,
    ) -> Path:
        return self.workspace_relative_path(
            record.workspace_dir,
            artifact.relative_path,
            context=f"job artifact {artifact.artifact_id}",
        )

    def open_artifact_download(
        self,
        job_id: str,
        artifact_id: str,
    ) -> tuple[VerifiedFileDownload, JobArtifact]:
        """Return one artifact with its verified handle retained for streaming."""
        record, artifact = self._artifact_record(job_id, artifact_id, kind="file")
        if artifact.sha256 is None:
            raise KeyError(artifact_id)
        download = self.open_owned_download(
            self._artifact_path(record, artifact),
            root=self.config.workspace_root,
            expected_sha256=artifact.sha256,
            expected_size=artifact.size_bytes,
            context=f"job artifact {artifact_id}",
        )
        return download, artifact

    def artifact_path(self, job_id: str, artifact_id: str) -> tuple[Path, JobArtifact]:
        """Verify one file artifact through a pinned handle and return its lexical path."""
        record, artifact = self._artifact_record(job_id, artifact_id, kind="file")
        path = self._artifact_path(record, artifact)
        download, _ = self.open_artifact_download(job_id, artifact_id)
        download.close()
        return path, artifact

    def directory_artifact_path(self, job_id: str, artifact_id: str) -> tuple[Path, JobArtifact]:
        """Strictly resolve one published workspace-contained directory artifact."""
        record, artifact = self._artifact_record(job_id, artifact_id, kind="directory")
        path = self._artifact_path(record, artifact)
        try:
            owned_directory_identity(
                path,
                root=self.config.workspace_root,
                root_identity=self._owned_identity(self.config.workspace_root),
                context=f"job directory artifact {artifact_id}",
            )
        except (OSError, ValueError) as exc:
            raise ConfigurationError(
                "job directory artifact is missing or escapes its workspace"
            ) from exc
        return path, artifact

    def _resolve_input(self, raw_path: Path) -> Path:
        path = raw_path.expanduser().resolve()
        if not any(_within(root, path) for root in self.config.input_roots):
            raise ConfigurationError("requested input path is outside configured input roots")
        return path

    def list_files(self, path: Path | None = None) -> FileListing:
        """List non-hidden local inputs while enforcing configured root containment."""
        roots = tuple(str(root) for root in self.config.input_roots)
        self._validate_owned_roots()
        if path is None:
            root_entries = tuple(
                FileEntry(
                    name=root.name or str(root),
                    path=str(root),
                    kind="directory",
                    selectable=False,
                )
                for root in self.config.input_roots
                if root.is_dir()
            )
            return FileListing(path=None, parent=None, roots=roots, entries=root_entries)
        directory = self._resolve_input(path)
        if not directory.is_dir():
            raise ConfigurationError(f"input browser path is not a directory: {directory}")
        parent = directory.parent.resolve()
        safe_parent = (
            str(parent) if any(_within(root, parent) for root in self.config.input_roots) else None
        )
        entries: list[FileEntry] = []
        children = sorted(
            directory.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.casefold()),
        )
        for child in children:
            if child.name.startswith("."):
                continue
            resolved = child.resolve()
            if not any(_within(root, resolved) for root in self.config.input_roots):
                continue
            if child.is_dir():
                entries.append(
                    FileEntry(
                        name=child.name,
                        path=str(resolved),
                        kind="directory",
                        selectable=False,
                    )
                )
            elif child.is_file() and child.suffix.lower() in _SELECTABLE_SUFFIXES:
                entries.append(
                    FileEntry(
                        name=child.name,
                        path=str(resolved),
                        kind="file",
                        size_bytes=child.stat().st_size,
                        selectable=True,
                    )
                )
        return FileListing(
            path=str(directory),
            parent=safe_parent,
            roots=roots,
            entries=tuple(entries),
        )

    def iter_events(self, job_id: str, *, after: int = 0) -> Iterator[JobEvent]:
        """Yield current events for adapter-neutral streaming tests."""
        yield from self.read_events(job_id, after=after)
