"""Export, reopen, and verify per-tile Bambu Studio project 3MF evidence."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import io
import json
import math
import os
import platform
import re
import secrets
import shutil
import stat
import struct
import sys
import tempfile
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from pydantic import BaseModel, ConfigDict, ValidationError

from topoforge.exporters.three_mf import inspect_3mf
from topoforge.validation.manufacturing import evaluate_bambu_p2s_release_gate
from topoforge.validation.slicers import SliceStatus, parse_gcode_generator, parse_gcode_metrics
from topoforge.validation.slicers.bambu import parse_bambu_studio_version
from topoforge.validation.slicers.base import CommandExecution, run_command

SCHEMA_VERSION = "topoforge-bambu-tile-project-assembly-v1"
TILE_SCHEMA_VERSION = "topoforge-bambu-tile-project-v1"
_MAX_JSON_BYTES = 16 * 1024 * 1024
_PROJECT_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_PROJECT_MAX_MEMBERS = 20_000
_PROJECT_MAX_CENTRAL_DIRECTORY_BYTES = 64 * 1024 * 1024
_PROJECT_MAX_MEMBER_BYTES = 1024 * 1024 * 1024
_PROJECT_MAX_TOTAL_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_PROJECT_MAX_COMPRESSION_RATIO = 1000.0
_PROJECT_MAX_RELATIONSHIP_BYTES = 8 * 1024 * 1024
# A 64 MiB model keeps production terrain capacity while the streaming parser below
# clears XML nodes eagerly instead of retaining a second full ElementTree.
_PROJECT_MAX_MODEL_XML_BYTES = 64 * 1024 * 1024
# Component graphs can encode exponentially many mesh instances in a tiny XML
# document. These limits are checked with a memoized, saturating graph summary
# before any transformed vertex traversal begins.
_PROJECT_MAX_EXPANDED_INSTANCES = 100_000
_PROJECT_MAX_EXPANDED_VERTICES = 10_000_000
_PROJECT_MAX_EXPANDED_TRIANGLES = 20_000_000
_PROJECT_MAX_COMPONENT_DEPTH = 128
# Real Bambu G-code can be much larger than its metadata. The reader hashes all
# 256 MiB but retains at most 32 MiB of comment lines consumed by the shared parser.
_PROJECT_MAX_GCODE_TEXT_BYTES = 256 * 1024 * 1024
_PROJECT_MAX_GCODE_SEMANTIC_BYTES = 32 * 1024 * 1024
_PROJECT_MAX_GCODE_LINE_BYTES = 1024 * 1024
_PROJECT_MAX_MEMBER_NAME_BYTES = 1024
_PROJECT_ALLOWED_FLAG_BITS = 0x080E
_DESCRIPTOR_RELATIVE_SUPPORTED = (
    hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd
)
_PROJECT_REQUIRED_MEMBERS = frozenset(
    {
        "[Content_Types].xml",
        "_rels/.rels",
        "3D/3dmodel.model",
        "Metadata/plate_1.gcode",
        "Metadata/plate_1.gcode.md5",
        "Metadata/project_settings.config",
    }
)
_PROJECT_FILE_ROLES = frozenset(
    {
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
)
_ROOT_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "layout_id",
        "source_print_manifest_sha256",
        "source_slice_manifest_sha256",
        "bambu_studio_path",
        "bambu_studio_sha256",
        "bambu_studio_version",
        "bambu_studio_probe",
        "printer_profile_id",
        "profile_files",
        "tile_grid_shape",
        "tile_count",
        "all_projects_reopened",
        "all_release_gates_passed",
        "claim_boundary",
        "required_checks_passed",
        "tiles",
    }
)
_ROOT_TILE_FIELDS = frozenset(
    {
        "tile_id",
        "row",
        "column",
        "source_print_tile_manifest_sha256",
        "source_slice_report_sha256",
        "validation_path",
        "validation_sha256",
        "files",
        "sha256",
        "required_checks_passed",
    }
)
_VALIDATION_FIELDS = frozenset(
    {
        "schema_version",
        "tile_id",
        "source_print_local_3mf_path",
        "source_print_local_3mf_sha256",
        "source_slice_report_sha256",
        "source_dimensions_mm",
        "source_triangle_count",
        "build_execution",
        "reopen_execution",
        "build_result",
        "reopen_result",
        "build_object",
        "reopen_object",
        "dimensions_match",
        "triangle_counts_match",
        "project_archive",
        "primary_metrics",
        "reopen_metrics",
        "primary_release_gate",
        "reopen_release_gate",
        "expected_bambu_studio_version",
        "primary_bambu_studio_version",
        "reopen_bambu_studio_version",
        "bambu_studio_versions_match",
        "external_profiles_loaded_on_reopen",
        "required_checks_passed",
    }
)


@dataclass(frozen=True, slots=True)
class _EvidenceArgs:
    print_set: Path
    slice_set: Path
    bambu_studio: Path
    output: Path
    timeout: float


@dataclass(frozen=True, slots=True)
class _SourceTileEvidence:
    tile_id: str
    row: int
    column: int
    print_record: Any
    print_artifact: Any
    slice_record: Any
    slice_report: Any
    source_3mf: Path
    source_3mf_sha256: str
    source_3mf_size: int
    verified_source_3mf: Path
    source_3mf_inspection: Any
    source_slice_gcode: Path
    source_slice_gcode_sha256: str
    source_slice_gcode_size: int


@dataclass(frozen=True, slots=True)
class _SourceEvidence:
    print_manifest: Any
    print_manifest_sha256: str
    slice_manifest: Any
    slice_manifest_sha256: str
    expected_version: str
    executable_sha256: str
    profiles: tuple[tuple[Any, Path, _BinarySnapshot], ...]
    tiles: tuple[_SourceTileEvidence, ...]


@dataclass(frozen=True, slots=True)
class _PinnedRegularFile:
    path: Path
    handle: BinaryIO
    information: os.stat_result


@dataclass(frozen=True, slots=True)
class _ExecutableIdentity:
    path: Path
    pinned: _PinnedRegularFile
    sha256: str


@dataclass(frozen=True, slots=True)
class _BinarySnapshot:
    path: Path
    payload: bytes
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _TextSnapshot:
    path: Path
    text: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _ArchiveInspection:
    evidence: dict[str, Any]
    project_sha256: str
    primary_gcode: _TextSnapshot


class BambuProjectEvidenceResult(BaseModel):
    """Published Bambu project evidence paths and strict verification summary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_dir: Path
    manifest_path: Path
    verification: dict[str, Any]


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_exists_no_follow(path: Path) -> bool:
    try:
        os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError(
            f"path cannot be inspected without following links: {path}: {exc}"
        ) from exc
    return True


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _descriptor_relative_supported() -> bool:
    return _DESCRIPTOR_RELATIVE_SUPPORTED


def _is_link_or_reparse(information: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(information.st_mode) or bool(
        getattr(information, "st_file_attributes", 0) & reparse_flag
    )


def _is_single_link_regular(information: os.stat_result) -> bool:
    return stat.S_ISREG(information.st_mode) and information.st_nlink == 1


def _checked_path_chain(path: Path, *, label: str) -> tuple[os.stat_result, ...]:
    absolute = _absolute_path(path)
    current = Path(absolute.anchor)
    information: list[os.stat_result] = []
    for part in absolute.parts[1:]:
        current /= part
        try:
            item = os.stat(current, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(f"{label} path component is unavailable: {current}: {exc}") from exc
        if _is_link_or_reparse(item):
            raise RuntimeError(f"{label} path contains a symbolic link or reparse point: {current}")
        information.append(item)
    return tuple(information)


def _ensure_directory_tree(path: Path, *, label: str) -> Path:
    """Create a directory tree one component at a time without traversing links."""
    absolute = _absolute_path(path)
    if _descriptor_relative_supported() and os.mkdir in os.supports_dir_fd:
        descriptor = -1
        try:
            descriptor = os.open(absolute.anchor, _directory_flags())
            for part in absolute.parts[1:]:
                try:
                    next_descriptor = os.open(part, _directory_flags(), dir_fd=descriptor)
                except FileNotFoundError:
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                    next_descriptor = os.open(part, _directory_flags(), dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            information = os.fstat(descriptor)
            if not _directory_path_still_names(absolute, information):
                raise RuntimeError(
                    f"{label} directory path changed while it was created: {absolute}"
                )
        except OSError as exc:
            raise RuntimeError(
                f"{label} directory is unavailable without following links: {absolute}: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return absolute

    current = Path(absolute.anchor)
    try:
        current_information = os.stat(current, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError(f"{label} directory anchor is unavailable: {current}: {exc}") from exc
    if not stat.S_ISDIR(current_information.st_mode) or _is_link_or_reparse(current_information):
        raise RuntimeError(f"{label} directory anchor is not a plain directory: {current}")
    for part in absolute.parts[1:]:
        candidate = current / part
        parent_information = os.stat(current, follow_symlinks=False)
        try:
            information = os.stat(candidate, follow_symlinks=False)
        except FileNotFoundError:
            with suppress(FileExistsError):
                os.mkdir(candidate, 0o700)
            information = os.stat(candidate, follow_symlinks=False)
            parent_after = os.stat(current, follow_symlinks=False)
            if _object_identity(parent_after) != _object_identity(parent_information):
                raise RuntimeError(
                    f"{label} directory parent changed while creating: {candidate}"
                ) from None
        if not stat.S_ISDIR(information.st_mode) or _is_link_or_reparse(information):
            raise RuntimeError(
                f"{label} path contains a symbolic link, reparse point, or non-directory: "
                f"{candidate}"
            )
        current = candidate
    return absolute


def _make_private_staging_directory(parent: Path, *, prefix: str) -> Path:
    absolute_parent = _ensure_directory_tree(parent, label="Bambu staging parent")
    if _descriptor_relative_supported() and os.mkdir in os.supports_dir_fd:
        with _open_pinned_directory(absolute_parent, label="Bambu staging parent") as descriptor:
            parent_information = os.fstat(descriptor)
            for _attempt in range(128):
                name = f"{prefix}{secrets.token_hex(16)}"
                try:
                    os.mkdir(name, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    continue
                if not _directory_path_still_names(absolute_parent, parent_information):
                    with suppress(OSError):
                        os.rmdir(name, dir_fd=descriptor)
                    raise RuntimeError("Bambu staging parent changed while creating the stage")
                return absolute_parent / name
        raise RuntimeError("unable to allocate a unique Bambu staging directory")

    before = _checked_path_chain(absolute_parent, label="Bambu staging parent")
    staging = Path(tempfile.mkdtemp(prefix=prefix, dir=absolute_parent))
    after = _checked_path_chain(absolute_parent, label="Bambu staging parent")
    stage_information = os.stat(staging, follow_symlinks=False)
    if (
        tuple(_object_identity(item) for item in before)
        != tuple(_object_identity(item) for item in after)
        or not stat.S_ISDIR(stage_information.st_mode)
        or _is_link_or_reparse(stage_information)
    ):
        with suppress(OSError):
            os.rmdir(staging)
        raise RuntimeError("Bambu staging parent changed while creating the stage")
    return staging


@contextmanager
def _open_pinned_directory(path: Path, *, label: str) -> Iterator[int]:
    absolute = _absolute_path(path)
    if not _descriptor_relative_supported():
        raise RuntimeError(
            f"{label} descriptor-relative directory access is unavailable on this platform"
        )
    descriptor = -1
    try:
        if absolute.is_absolute():
            descriptor = os.open(absolute.anchor, _directory_flags())
            for part in absolute.parts[1:]:
                next_descriptor = os.open(part, _directory_flags(), dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
        else:
            descriptor = os.open(absolute, _directory_flags())
        information = os.fstat(descriptor)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise RuntimeError(
            f"{label} directory is unavailable without following links: {path}: {exc}"
        ) from exc
    if not stat.S_ISDIR(information.st_mode):
        os.close(descriptor)
        raise RuntimeError(f"{label} parent must be a directory: {path}")
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_pinned_regular_file(path: Path, *, label: str) -> Iterator[_PinnedRegularFile]:
    absolute = _absolute_path(path)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        if _descriptor_relative_supported():
            with _open_pinned_directory(absolute.parent, label=label) as parent_descriptor:
                descriptor = os.open(absolute.name, flags, dir_fd=parent_descriptor)
                information = os.fstat(descriptor)
                if not _is_single_link_regular(information):
                    raise RuntimeError(
                        f"{label} must be a regular non-link file with exactly one hard link: "
                        f"{path}"
                    )
                with os.fdopen(descriptor, "rb") as handle:
                    descriptor = -1
                    yield _PinnedRegularFile(
                        path=absolute,
                        handle=handle,
                        information=information,
                    )
            return

        before = _checked_path_chain(absolute, label=label)
        descriptor = os.open(absolute, flags)
        information = os.fstat(descriptor)
        after = _checked_path_chain(absolute, label=label)
        if (
            not _is_single_link_regular(information)
            or not after
            or _stat_identity(after[-1]) != _stat_identity(information)
            or tuple(_object_identity(item) for item in before)
            != tuple(_object_identity(item) for item in after)
        ):
            raise RuntimeError(
                f"{label} path changed while opening or is not a single-link regular file: {path}"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            yield _PinnedRegularFile(
                path=absolute,
                handle=handle,
                information=information,
            )
    except OSError as exc:
        raise RuntimeError(
            f"{label} must be a regular non-link file with exactly one hard link and is "
            f"unavailable: {path}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _stat_identity(information: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        information.st_dev,
        information.st_ino,
        stat.S_IFMT(information.st_mode),
        information.st_size,
        information.st_mtime_ns,
        information.st_ctime_ns,
        information.st_nlink,
    )


def _object_identity(information: os.stat_result) -> tuple[int, int, int]:
    return (
        information.st_dev,
        information.st_ino,
        stat.S_IFMT(information.st_mode),
    )


def _require_pinned_unchanged(pinned: _PinnedRegularFile, *, label: str) -> None:
    if _stat_identity(os.fstat(pinned.handle.fileno())) != _stat_identity(pinned.information):
        raise RuntimeError(f"{label} changed while it was being read: {pinned.path}")


def _hash_pinned(pinned: _PinnedRegularFile, *, label: str) -> str:
    pinned.handle.seek(0)
    digest = hashlib.sha256()
    size = 0
    while True:
        block = pinned.handle.read(1024 * 1024)
        if not block:
            break
        size += len(block)
        digest.update(block)
    if size != pinned.information.st_size:
        raise RuntimeError(f"{label} size changed while it was being hashed: {pinned.path}")
    _require_pinned_unchanged(pinned, label=label)
    return digest.hexdigest()


def _read_binary_snapshot(
    path: Path,
    *,
    label: str,
    minimum_bytes: int,
    maximum_bytes: int,
) -> _BinarySnapshot:
    with _open_pinned_regular_file(path, label=label) as pinned:
        size = pinned.information.st_size
        if size < minimum_bytes or size > maximum_bytes:
            raise RuntimeError(
                f"{label} size {size} is outside the supported "
                f"{minimum_bytes}..{maximum_bytes} byte range: {path}"
            )
        payload = pinned.handle.read(maximum_bytes + 1)
        if len(payload) != size:
            raise RuntimeError(f"{label} size changed while it was being read: {path}")
        _require_pinned_unchanged(pinned, label=label)
    return _BinarySnapshot(
        path=_absolute_path(path),
        payload=payload,
        size=size,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _copy_pinned_regular_file_snapshot(
    pinned: _PinnedRegularFile,
    destination: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> tuple[str, int]:
    _ensure_directory_tree(destination.parent, label=f"{label} snapshot destination")
    absolute_destination = _absolute_path(destination)
    size = pinned.information.st_size
    if size <= 0 or size > maximum_bytes:
        raise RuntimeError(
            f"{label} size {size} is outside the supported "
            f"1..{maximum_bytes} byte range: {pinned.path}"
        )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    digest = hashlib.sha256()
    consumed = 0
    pinned.handle.seek(0)

    def transfer(descriptor: int) -> os.stat_result:
        nonlocal consumed
        with os.fdopen(descriptor, "wb") as target:
            while True:
                block = pinned.handle.read(1024 * 1024)
                if not block:
                    break
                consumed += len(block)
                if consumed > maximum_bytes:
                    raise RuntimeError(f"{label} expanded beyond the {maximum_bytes} byte limit")
                digest.update(block)
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
            target_information = os.fstat(target.fileno())
        if consumed != size:
            raise RuntimeError(f"{label} size changed while snapshotting: {pinned.path}")
        _require_pinned_unchanged(pinned, label=label)
        return target_information

    if _descriptor_relative_supported():
        with _open_pinned_directory(
            absolute_destination.parent,
            label=f"{label} snapshot destination",
        ) as parent:
            parent_information = os.fstat(parent)
            descriptor = os.open(
                absolute_destination.name,
                flags,
                0o600,
                dir_fd=parent,
            )
            try:
                target_information = transfer(descriptor)
                destination_information = os.stat(
                    absolute_destination.name,
                    dir_fd=parent,
                    follow_symlinks=False,
                )
                if _stat_identity(destination_information) != _stat_identity(
                    target_information
                ) or not _directory_path_still_names(
                    absolute_destination.parent,
                    parent_information,
                ):
                    raise RuntimeError(
                        f"{label} snapshot path or parent changed: {absolute_destination}"
                    )
            except BaseException:
                with suppress(FileNotFoundError):
                    os.unlink(absolute_destination.name, dir_fd=parent)
                raise
    else:
        parent_before = _checked_path_chain(
            absolute_destination.parent,
            label=f"{label} snapshot destination",
        )
        if not parent_before or not stat.S_ISDIR(parent_before[-1].st_mode):
            raise RuntimeError(
                f"{label} snapshot parent is not a directory: {absolute_destination.parent}"
            )
        descriptor = os.open(absolute_destination, flags, 0o600)
        try:
            target_information = transfer(descriptor)
            parent_after = _checked_path_chain(
                absolute_destination.parent,
                label=f"{label} snapshot destination",
            )
            destination_after = _checked_path_chain(
                absolute_destination,
                label=f"{label} snapshot destination",
            )
            if (
                tuple(_object_identity(item) for item in parent_before)
                != tuple(_object_identity(item) for item in parent_after)
                or not destination_after
                or _stat_identity(destination_after[-1]) != _stat_identity(target_information)
            ):
                raise RuntimeError(f"{label} snapshot path changed during publication")
        except BaseException:
            absolute_destination.unlink(missing_ok=True)
            raise
    return digest.hexdigest(), consumed


def _copy_regular_file_snapshot(
    source: Path,
    destination: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> tuple[str, int]:
    with _open_pinned_regular_file(source, label=label) as pinned:
        return _copy_pinned_regular_file_snapshot(
            pinned,
            destination,
            label=label,
            maximum_bytes=maximum_bytes,
        )


def sha256(path: Path) -> str:
    with _open_pinned_regular_file(path, label="SHA-256 input") as pinned:
        return _hash_pinned(pinned, label="SHA-256 input")


def _require_executable_identity(identity: _ExecutableIdentity, *, phase: str) -> None:
    _require_pinned_unchanged(identity.pinned, label="Bambu Studio executable")
    with _open_pinned_regular_file(
        identity.path,
        label="Bambu Studio executable",
    ) as current:
        if _stat_identity(current.information) != _stat_identity(identity.pinned.information):
            raise RuntimeError(f"Bambu Studio executable identity changed {phase}: {identity.path}")
        if _hash_pinned(current, label="Bambu Studio executable") != identity.sha256:
            raise RuntimeError(f"Bambu Studio executable content changed {phase}: {identity.path}")


@contextmanager
def _lock_executable(path: Path) -> Iterator[_ExecutableIdentity]:
    absolute = _absolute_path(path)
    if not os.access(absolute, os.X_OK):
        raise RuntimeError(f"Bambu Studio executable is not executable: {absolute}")
    with _open_pinned_regular_file(absolute, label="Bambu Studio executable") as pinned:
        if os.name != "nt" and pinned.information.st_mode & 0o111 == 0:
            raise RuntimeError(f"Bambu Studio executable has no execute permission: {absolute}")
        identity = _ExecutableIdentity(
            path=absolute,
            pinned=pinned,
            sha256=_hash_pinned(pinned, label="Bambu Studio executable"),
        )
        _require_executable_identity(identity, phase="before evidence generation")
        yield identity


def _run_with_executable_identity(
    identity: _ExecutableIdentity,
    command: Sequence[str],
    *,
    timeout_seconds: float,
    env: Mapping[str, str] | None,
) -> CommandExecution:
    if not command or command[0] != str(identity.path):
        raise RuntimeError("Bambu Studio command does not use the locked executable path")
    _require_executable_identity(identity, phase="immediately before execution")
    try:
        return run_command(
            command,
            timeout_seconds=timeout_seconds,
            env=env,
        )
    finally:
        _require_executable_identity(identity, phase="immediately after execution")


def sha256_text(value: str) -> str:
    """Return the SHA-256 of a UTF-8 diagnostic stream."""
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _directory_path_still_names(
    path: Path,
    information: os.stat_result,
) -> bool:
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(current.st_mode) and (
        current.st_dev,
        current.st_ino,
    ) == (
        information.st_dev,
        information.st_ino,
    )


def _write_atomic_bytes(path: Path, payload: bytes) -> Path:
    _ensure_directory_tree(path.parent, label="atomic destination")
    absolute = _absolute_path(path)
    if _descriptor_relative_supported() and os.rename in os.supports_dir_fd:
        with _open_pinned_directory(absolute.parent, label="canonical destination") as parent:
            parent_information = os.fstat(parent)
            temporary_name = f".{absolute.name}.{secrets.token_hex(16)}.tmp"
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                if not _directory_path_still_names(absolute.parent, parent_information):
                    raise RuntimeError(
                        "canonical destination parent changed before publication: "
                        f"{absolute.parent}"
                    )
                os.replace(
                    temporary_name,
                    absolute.name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                )
                os.fsync(parent)
                if not _directory_path_still_names(absolute.parent, parent_information):
                    raise RuntimeError(
                        "canonical destination parent changed during publication: "
                        f"{absolute.parent}"
                    )
            except BaseException:
                if descriptor >= 0:
                    os.close(descriptor)
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=parent)
                raise
        return path

    parent_before = _checked_path_chain(
        absolute.parent,
        label="canonical destination",
    )
    if not parent_before or not stat.S_ISDIR(parent_before[-1].st_mode):
        raise RuntimeError(f"canonical destination parent is not a directory: {absolute.parent}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{absolute.name}.",
        suffix=".tmp",
        dir=absolute.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_information = os.fstat(handle.fileno())
        parent_during = _checked_path_chain(
            absolute.parent,
            label="canonical destination",
        )
        if tuple(_object_identity(item) for item in parent_before) != tuple(
            _object_identity(item) for item in parent_during
        ):
            raise RuntimeError(
                f"canonical destination parent changed before publication: {absolute.parent}"
            )
        os.replace(temporary, absolute)
        parent_after = _checked_path_chain(
            absolute.parent,
            label="canonical destination",
        )
        destination_after = _checked_path_chain(
            absolute,
            label="canonical destination",
        )
        if (
            tuple(_object_identity(item) for item in parent_before)
            != tuple(_object_identity(item) for item in parent_after)
            or not destination_after
            or _object_identity(destination_after[-1]) != _object_identity(temporary_information)
            or destination_after[-1].st_size != temporary_information.st_size
        ):
            raise RuntimeError(f"canonical destination path changed during publication: {absolute}")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def write_canonical(path: Path, value: dict[str, Any]) -> Path:
    return _write_atomic_bytes(path, canonical_bytes(value))


def _write_text_from_memory(path: Path, value: str) -> str:
    payload = value.encode("utf-8", errors="replace")
    _write_atomic_bytes(path, payload)
    return hashlib.sha256(payload).hexdigest()


def _raise_publish_error(error_code: int, *, output: Path) -> None:
    if error_code in {errno.EEXIST, errno.ENOTEMPTY, 80, 183}:
        raise RuntimeError(f"Bambu tile project destination already exists: {output}")
    message = os.strerror(error_code) if error_code > 0 else "unknown native error"
    raise RuntimeError(f"atomic no-clobber Bambu publication failed: {output}: {message}")


def _native_publish_no_replace(
    staging: Path,
    output: Path,
    *,
    parent_descriptor: int | None,
) -> None:
    if sys.platform.startswith("linux"):
        library: Any = ctypes.CDLL(None, use_errno=True)
        renameat2: Any = getattr(library, "renameat2", None)
        if renameat2 is None:
            raise RuntimeError("atomic no-clobber publication requires Linux renameat2")
        directory = parent_descriptor if parent_descriptor is not None else -100
        source = staging.name if parent_descriptor is not None else os.fspath(staging)
        destination = output.name if parent_descriptor is not None else os.fspath(output)
        result = renameat2(
            directory,
            ctypes.c_char_p(os.fsencode(source)),
            directory,
            ctypes.c_char_p(os.fsencode(destination)),
            1,
        )
        if result != 0:
            _raise_publish_error(ctypes.get_errno(), output=output)
        return

    if sys.platform == "darwin":
        library = ctypes.CDLL(None, use_errno=True)
        renameatx: Any = getattr(library, "renameatx_np", None)
        if parent_descriptor is not None and renameatx is not None:
            result = renameatx(
                parent_descriptor,
                ctypes.c_char_p(os.fsencode(staging.name)),
                parent_descriptor,
                ctypes.c_char_p(os.fsencode(output.name)),
                0x00000004,
            )
        else:
            renamex: Any = getattr(library, "renamex_np", None)
            if renamex is None:
                raise RuntimeError("atomic no-clobber publication requires macOS renamex_np")
            result = renamex(
                ctypes.c_char_p(os.fsencode(staging)),
                ctypes.c_char_p(os.fsencode(output)),
                0x00000004,
            )
        if result != 0:
            _raise_publish_error(ctypes.get_errno(), output=output)
        return

    if os.name == "nt":
        win_dll: Any = getattr(ctypes, "WinDLL")  # noqa: B009 -- Windows-only API
        kernel32: Any = win_dll("kernel32", use_last_error=True)
        move_file_ex: Any = kernel32.MoveFileExW
        move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move_file_ex.restype = ctypes.c_int
        if not move_file_ex(os.fspath(staging), os.fspath(output), 0x00000008):
            get_last_error: Any = getattr(  # noqa: B009 -- Windows-only API
                ctypes,
                "get_last_error",
            )
            _raise_publish_error(int(get_last_error()), output=output)
        return

    raise RuntimeError(
        f"atomic no-clobber Bambu publication is unsupported on platform {sys.platform!r}"
    )


@dataclass(frozen=True, slots=True)
class _PublicationReconciliation:
    committed: bool
    output_state: str
    staging_state: str
    parent_state: str


def _publication_directory_state(
    path: Path,
    *,
    parent_descriptor: int | None,
) -> tuple[tuple[int, int, int] | None, str]:
    try:
        information = (
            os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
            if parent_descriptor is not None
            else os.stat(path, follow_symlinks=False)
        )
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:
        return None, f"unavailable ({type(exc).__name__}: {exc})"
    identity = _object_identity(information)
    if not stat.S_ISDIR(information.st_mode) or _is_link_or_reparse(information):
        return identity, f"not a plain directory (identity={identity!r})"
    return identity, f"plain directory (identity={identity!r})"


def _reconcile_directory_publication(
    *,
    staging: Path,
    output: Path,
    stage_information: os.stat_result,
    parent: Path,
    parent_information: os.stat_result | None,
    parent_descriptor: int | None,
    parent_before: tuple[os.stat_result, ...] | None,
) -> _PublicationReconciliation:
    output_identity, output_state = _publication_directory_state(
        output,
        parent_descriptor=parent_descriptor,
    )
    staging_identity, staging_state = _publication_directory_state(
        staging,
        parent_descriptor=parent_descriptor,
    )
    if parent_descriptor is not None and parent_information is not None:
        parent_matches = _directory_path_still_names(parent, parent_information)
        parent_state = "unchanged" if parent_matches else "changed or unavailable"
    elif parent_before is not None:
        try:
            parent_after = _checked_path_chain(parent, label="Bambu publication parent")
        except RuntimeError as exc:
            parent_matches = False
            parent_state = f"unavailable ({exc})"
        else:
            parent_matches = tuple(_object_identity(item) for item in parent_before) == tuple(
                _object_identity(item) for item in parent_after
            )
            parent_state = "unchanged" if parent_matches else "changed"
    else:
        parent_matches = False
        parent_state = "not checked"
    committed = bool(
        parent_matches
        and output_identity == _object_identity(stage_information)
        and staging_identity is None
        and staging_state == "missing"
    )
    return _PublicationReconciliation(
        committed=committed,
        output_state=output_state,
        staging_state=staging_state,
        parent_state=parent_state,
    )


def _handle_post_rename_publication_error(
    *,
    error: Exception,
    staging: Path,
    output: Path,
    stage_information: os.stat_result,
    parent: Path,
    parent_information: os.stat_result | None,
    parent_descriptor: int | None,
    parent_before: tuple[os.stat_result, ...] | None,
    durability_error: bool,
) -> None:
    reconciliation = _reconcile_directory_publication(
        staging=staging,
        output=output,
        stage_information=stage_information,
        parent=parent,
        parent_information=parent_information,
        parent_descriptor=parent_descriptor,
        parent_before=parent_before,
    )
    locations = (
        f"output={output} [{reconciliation.output_state}]; "
        f"staging={staging} [{reconciliation.staging_state}]; "
        f"parent={parent} [{reconciliation.parent_state}]"
    )
    if reconciliation.committed and not durability_error:
        return
    if reconciliation.committed:
        raise RuntimeError(
            "Bambu publication committed, but directory durability could not be confirmed; "
            f"the verified output is retained for explicit inspection: {locations}; "
            f"fsync error: {error}"
        ) from error
    raise RuntimeError(
        "Bambu publication state is uncertain after the native no-clobber rename; "
        f"inspect both recorded paths before retrying: {locations}; post-rename error: {error}"
    ) from error


def _publish_directory_no_replace(staging: Path, output: Path) -> None:
    absolute_staging = _absolute_path(staging)
    absolute_output = _absolute_path(output)
    if absolute_staging.parent != absolute_output.parent:
        raise RuntimeError("Bambu staging and destination directories must share one parent")
    parent = absolute_output.parent
    if _descriptor_relative_supported():
        with _open_pinned_directory(parent, label="Bambu publication parent") as descriptor:
            parent_information = os.fstat(descriptor)
            stage_information = os.stat(
                absolute_staging.name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(stage_information.st_mode) or _is_link_or_reparse(
                stage_information
            ):
                raise RuntimeError("Bambu staging path is not a plain directory")
            _native_publish_no_replace(
                absolute_staging,
                absolute_output,
                parent_descriptor=descriptor,
            )
            try:
                published_information = os.stat(
                    absolute_output.name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if _object_identity(published_information) != _object_identity(
                    stage_information
                ) or not _directory_path_still_names(parent, parent_information):
                    raise RuntimeError("Bambu publication parent or directory identity changed")
            except Exception as exc:
                _handle_post_rename_publication_error(
                    error=exc,
                    staging=absolute_staging,
                    output=absolute_output,
                    stage_information=stage_information,
                    parent=parent,
                    parent_information=parent_information,
                    parent_descriptor=descriptor,
                    parent_before=None,
                    durability_error=False,
                )
            try:
                os.fsync(descriptor)
            except OSError as exc:
                _handle_post_rename_publication_error(
                    error=exc,
                    staging=absolute_staging,
                    output=absolute_output,
                    stage_information=stage_information,
                    parent=parent,
                    parent_information=parent_information,
                    parent_descriptor=descriptor,
                    parent_before=None,
                    durability_error=True,
                )
        return

    parent_before = _checked_path_chain(parent, label="Bambu publication parent")
    stage_information = os.stat(absolute_staging, follow_symlinks=False)
    if not stat.S_ISDIR(stage_information.st_mode) or _is_link_or_reparse(stage_information):
        raise RuntimeError("Bambu staging path is not a plain directory")
    _native_publish_no_replace(
        absolute_staging,
        absolute_output,
        parent_descriptor=None,
    )
    try:
        parent_after = _checked_path_chain(parent, label="Bambu publication parent")
        published_information = os.stat(absolute_output, follow_symlinks=False)
        if tuple(_object_identity(item) for item in parent_before) != tuple(
            _object_identity(item) for item in parent_after
        ) or _object_identity(published_information) != _object_identity(stage_information):
            raise RuntimeError("Bambu publication parent or directory identity changed")
    except Exception as exc:
        _handle_post_rename_publication_error(
            error=exc,
            staging=absolute_staging,
            output=absolute_output,
            stage_information=stage_information,
            parent=parent,
            parent_information=None,
            parent_descriptor=None,
            parent_before=parent_before,
            durability_error=False,
        )


def _relative_parts(relative: str, *, label: str) -> tuple[str, ...]:
    if not relative or "\x00" in relative or "\\" in relative or relative.startswith("/"):
        raise RuntimeError(f"{label} is not a canonical relative POSIX path: {relative!r}")
    parts = tuple(relative.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise RuntimeError(f"{label} is not a safe relative path: {relative!r}")
    return parts


def resolve_relative(root: Path, relative: str) -> Path:
    parts = _relative_parts(relative, label="evidence path")
    lexical = root.joinpath(*parts)
    current = lexical
    while current != root:
        if current.is_symlink():
            raise RuntimeError(f"evidence path contains a symbolic link: {relative}")
        current = current.parent
    candidate = lexical.resolve()
    if candidate == root or root not in candidate.parents:
        raise RuntimeError(f"path escapes evidence directory: {relative}")
    return candidate


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RuntimeError(f"JSON contains a duplicate object key: {key}")
        value[key] = item
    return value


def _read_json_snapshot(path: Path) -> tuple[Any, _BinarySnapshot]:
    snapshot = _read_binary_snapshot(
        path,
        label="JSON file",
        minimum_bytes=1,
        maximum_bytes=_MAX_JSON_BYTES,
    )
    try:
        value = json.loads(
            snapshot.payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"JSON is unreadable: {path}: {exc}") from exc
    return value, snapshot


def _snapshot_generated_json(
    source: Path,
    destination: Path,
    *,
    label: str,
) -> dict[str, Any]:
    value, snapshot = _read_json_snapshot(source)
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} root is not an object: {source}")
    _write_atomic_bytes(destination, snapshot.payload)
    return value


def _read_json_value(path: Path) -> Any:
    value, _snapshot = _read_json_snapshot(path)
    return value


def load_json(path: Path) -> dict[str, Any]:
    value = _read_json_value(path)
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON root is not an object: {path}")
    return value


def _load_canonical_model_snapshot(
    path: Path,
    model: type[BaseModel],
) -> tuple[BaseModel, _BinarySnapshot]:
    value, snapshot = _read_json_snapshot(path)
    try:
        parsed = model.model_validate(value)
    except ValidationError as exc:
        raise RuntimeError(f"JSON does not match {model.__name__}: {path}: {exc}") from exc
    if snapshot.payload != canonical_bytes(parsed.model_dump(mode="json")):
        raise RuntimeError(f"JSON is not canonical: {path}")
    return parsed, snapshot


def _load_canonical_model(path: Path, model: type[BaseModel]) -> BaseModel:
    parsed, _snapshot = _load_canonical_model_snapshot(path, model)
    return parsed


def _load_canonical_json(path: Path) -> tuple[dict[str, Any], _BinarySnapshot]:
    value, snapshot = _read_json_snapshot(path)
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON root is not an object: {path}")
    if snapshot.payload != canonical_bytes(value):
        raise RuntimeError(f"JSON is not canonical: {path}")
    return value, snapshot


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


def _read_gcode_from_pinned(
    pinned: _PinnedRegularFile,
    *,
    label: str,
) -> _TextSnapshot:
    size = pinned.information.st_size
    if size <= 0 or size > _PROJECT_MAX_GCODE_TEXT_BYTES:
        raise RuntimeError(
            f"{label} size {size} is outside the supported "
            f"1..{_PROJECT_MAX_GCODE_TEXT_BYTES} byte range: {pinned.path}"
        )
    pinned.handle.seek(0)
    digest = hashlib.sha256()
    semantic = io.StringIO()
    semantic_size = 0
    consumed = 0
    while True:
        raw_line = pinned.handle.readline(_PROJECT_MAX_GCODE_LINE_BYTES + 1)
        if not raw_line:
            break
        consumed += len(raw_line)
        digest.update(raw_line)
        if len(raw_line) > _PROJECT_MAX_GCODE_LINE_BYTES:
            raise RuntimeError(
                f"{label} contains a line exceeding the "
                f"{_PROJECT_MAX_GCODE_LINE_BYTES} byte limit: {pinned.path}"
            )
        if raw_line.lstrip().startswith(b";"):
            semantic_size += len(raw_line)
            if semantic_size > _PROJECT_MAX_GCODE_SEMANTIC_BYTES:
                raise RuntimeError(
                    f"{label} semantic comments exceed the "
                    f"{_PROJECT_MAX_GCODE_SEMANTIC_BYTES} byte limit: {pinned.path}"
                )
            semantic.write(raw_line.decode("utf-8", errors="replace"))
    if consumed != size:
        raise RuntimeError(f"{label} size changed while it was being read: {pinned.path}")
    _require_pinned_unchanged(pinned, label=label)
    return _TextSnapshot(
        path=pinned.path,
        text=semantic.getvalue(),
        size=size,
        sha256=digest.hexdigest(),
    )


def _read_gcode_snapshot(path: Path, *, label: str) -> _TextSnapshot:
    with _open_pinned_regular_file(path, label=label) as pinned:
        return _read_gcode_from_pinned(pinned, label=label)


def _read_bounded_text(path: Path, *, label: str) -> str:
    return _read_gcode_snapshot(path, label=label).text


def _gcode_bambu_version_text(gcode_text: str, *, path: Path) -> str:
    tokens = re.findall(
        r"^;\s*(?:generated\s+by\s+)?BambuStudio\s+([^\s;]+)",
        gcode_text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if not tokens:
        raise RuntimeError(f"G-code does not identify Bambu Studio as its generator: {path}")
    versions: set[str] = set()
    for token in tokens:
        parsed = parse_bambu_studio_version(f"BambuStudio-{token}:")
        if parsed is None or parsed != token:
            raise RuntimeError(f"G-code contains a malformed Bambu Studio version: {path}")
        versions.add(parsed)
    if len(versions) != 1:
        raise RuntimeError(f"G-code contains conflicting Bambu Studio versions: {path}")
    generator = parse_gcode_generator(gcode_text)
    version = next(iter(versions))
    if (
        generator is None
        or generator[0].casefold().replace(" ", "") != "bambustudio"
        or generator[1] != version
    ):
        raise RuntimeError(f"G-code Bambu Studio generator identity is ambiguous: {path}")
    return version


def gcode_bambu_version(gcode: Path) -> str:
    """Parse and validate one independently generated Bambu G-code version."""
    return _gcode_bambu_version_text(
        _read_bounded_text(gcode, label="Bambu G-code"),
        path=gcode,
    )


def release_gate(
    gcode: Path,
    *,
    expected_version: str,
    stdout: str,
    stderr: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    snapshot = _read_gcode_snapshot(gcode, label="Bambu G-code")
    return _release_gate_snapshot(
        snapshot,
        expected_version=expected_version,
        stdout=stdout,
        stderr=stderr,
    )


def _release_gate_snapshot(
    snapshot: _TextSnapshot,
    *,
    expected_version: str,
    stdout: str,
    stderr: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    actual_version = _gcode_bambu_version_text(snapshot.text, path=snapshot.path)
    if actual_version != expected_version:
        raise RuntimeError(
            f"Bambu Studio G-code version {actual_version!r} does not match the frozen "
            f"source-slice version {expected_version!r}: {snapshot.path}"
        )
    metrics = parse_gcode_metrics(
        snapshot.text,
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


def _windows_archive_alias(name: str) -> str:
    if name.endswith("//"):
        raise RuntimeError(f"Bambu project archive member has an unsafe name: {name!r}")
    canonical_name = name[:-1] if name.endswith("/") else name
    parts = _relative_parts(canonical_name, label="Bambu project archive member")
    aliases: list[str] = []
    for part in parts:
        normalized = unicodedata.normalize("NFC", part)
        if (
            normalized != part
            or normalized.endswith((" ", "."))
            or ":" in normalized
            or any(ord(character) < 32 for character in normalized)
        ):
            raise RuntimeError(f"Bambu project archive member has an unsafe name: {name!r}")
        alias = normalized.casefold().rstrip(" .")
        stem = alias.split(".", 1)[0]
        if (
            not alias
            or stem in {"con", "prn", "aux", "nul"}
            or re.fullmatch(r"(?:com|lpt)[1-9]", stem) is not None
        ):
            raise RuntimeError(f"Bambu project archive member has a Windows-unsafe name: {name!r}")
        aliases.append(alias)
    return "/".join(aliases)


def _archive_member_is_regular(info: ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if info.is_dir():
        return file_type in {0, stat.S_IFDIR}
    return file_type in {0, stat.S_IFREG}


def _read_pinned_range(
    pinned: _PinnedRegularFile,
    offset: int,
    length: int,
    *,
    label: str,
) -> bytes:
    if offset < 0 or length < 0 or offset + length > pinned.information.st_size:
        raise RuntimeError(f"{label} points outside the pinned file")
    pinned.handle.seek(offset)
    payload = pinned.handle.read(length)
    if len(payload) != length:
        raise RuntimeError(f"{label} could not be read completely")
    return payload


def _preflight_project_central_directory(project_file: _PinnedRegularFile) -> None:
    size = project_file.information.st_size
    eocd_size = 22
    maximum_comment = 65_535
    if size < eocd_size:
        raise RuntimeError("Bambu project archive has no ZIP end-of-central-directory record")
    tail_size = min(size, eocd_size + maximum_comment)
    tail_offset = size - tail_size
    tail = _read_pinned_range(
        project_file,
        tail_offset,
        tail_size,
        label="Bambu project ZIP tail",
    )
    signature = b"PK\x05\x06"
    position = tail.rfind(signature)
    eocd: tuple[bytes, int, int, int, int, int, int, int] | None = None
    while position >= 0:
        if position + eocd_size <= len(tail):
            candidate = struct.unpack_from("<4sHHHHIIH", tail, position)
            if position + eocd_size + candidate[-1] == len(tail):
                eocd = candidate
                break
        position = tail.rfind(signature, 0, position)
    if eocd is None:
        raise RuntimeError("Bambu project archive has an invalid ZIP end record")
    (
        _signature,
        disk_number,
        directory_disk,
        entries_on_disk,
        entry_count,
        directory_size,
        directory_offset,
        _comment_size,
    ) = eocd
    legacy_entries_on_disk = entries_on_disk
    legacy_entry_count = entry_count
    legacy_directory_size = directory_size
    legacy_directory_offset = directory_offset
    if disk_number != 0 or directory_disk != 0 or entries_on_disk != entry_count:
        raise RuntimeError("Bambu project archive must not use a multi-disk ZIP layout")

    eocd_offset = tail_offset + position
    locator_offset = eocd_offset - 20
    zip64_offset: int | None = None
    if locator_offset >= 0:
        locator = _read_pinned_range(
            project_file,
            locator_offset,
            20,
            label="Bambu project ZIP64 locator",
        )
        if locator[:4] == b"PK\x06\x07":
            _locator_signature, zip64_disk, parsed_zip64_offset, disk_count = struct.unpack(
                "<4sIQI",
                locator,
            )
            zip64_offset = int(parsed_zip64_offset)
            if zip64_disk != 0 or disk_count != 1:
                raise RuntimeError("Bambu project archive must not use multi-disk ZIP64")
            fixed = _read_pinned_range(
                project_file,
                zip64_offset,
                56,
                label="Bambu project ZIP64 end record",
            )
            (
                zip64_signature,
                zip64_record_size,
                _version_made,
                _version_needed,
                zip64_disk_number,
                zip64_directory_disk,
                zip64_entries_on_disk,
                zip64_entry_count,
                zip64_directory_size,
                zip64_directory_offset,
            ) = struct.unpack("<4sQHHIIQQQQ", fixed)
            if zip64_signature != b"PK\x06\x06" or zip64_record_size < 44:
                raise RuntimeError("Bambu project archive has an invalid ZIP64 end record")
            if zip64_offset + 12 + zip64_record_size != locator_offset:
                raise RuntimeError("Bambu project archive has an inconsistent ZIP64 end record")
            if (
                zip64_disk_number != 0
                or zip64_directory_disk != 0
                or zip64_entries_on_disk != zip64_entry_count
            ):
                raise RuntimeError("Bambu project archive must not use multi-disk ZIP64")
            for legacy, sentinel, extended, field_name in (
                (legacy_entry_count, 0xFFFF, zip64_entry_count, "entry count"),
                (
                    legacy_entries_on_disk,
                    0xFFFF,
                    zip64_entries_on_disk,
                    "disk entry count",
                ),
                (
                    legacy_directory_size,
                    0xFFFFFFFF,
                    zip64_directory_size,
                    "central-directory size",
                ),
                (
                    legacy_directory_offset,
                    0xFFFFFFFF,
                    zip64_directory_offset,
                    "central-directory offset",
                ),
            ):
                if legacy != sentinel and legacy != extended:
                    raise RuntimeError(f"Bambu project ZIP64 {field_name} disagrees with its EOCD")
            entry_count = zip64_entry_count
            directory_size = zip64_directory_size
            directory_offset = zip64_directory_offset

    uses_sentinel = (
        legacy_entry_count == 0xFFFF
        or legacy_entries_on_disk == 0xFFFF
        or legacy_directory_size == 0xFFFFFFFF
        or legacy_directory_offset == 0xFFFFFFFF
    )
    if uses_sentinel and zip64_offset is None:
        raise RuntimeError("Bambu project archive has ZIP64 sentinels without ZIP64 metadata")
    if entry_count > _PROJECT_MAX_MEMBERS:
        raise RuntimeError(
            f"Bambu project archive member count {entry_count} exceeds {_PROJECT_MAX_MEMBERS}"
        )
    if directory_size > _PROJECT_MAX_CENTRAL_DIRECTORY_BYTES:
        raise RuntimeError(
            f"Bambu project central directory size {directory_size} exceeds "
            f"{_PROJECT_MAX_CENTRAL_DIRECTORY_BYTES} bytes"
        )
    if entry_count and directory_size < entry_count * 46:
        raise RuntimeError("Bambu project central directory is too small for its entry count")
    directory_boundary = eocd_offset if zip64_offset is None else zip64_offset
    if directory_offset > directory_boundary or directory_size != (
        directory_boundary - directory_offset
    ):
        raise RuntimeError(
            "Bambu project central directory does not end immediately before its ZIP trailer"
        )
    position = directory_offset
    directory_end = directory_offset + directory_size
    actual_entries = 0
    while position < directory_end:
        header = _read_pinned_range(
            project_file,
            position,
            46,
            label="Bambu project central directory header",
        )
        if header[:4] != b"PK\x01\x02":
            raise RuntimeError("Bambu project central directory has an invalid entry header")
        name_size, extra_size, comment_size = struct.unpack_from("<HHH", header, 28)
        record_size = 46 + name_size + extra_size + comment_size
        if record_size > directory_end - position:
            raise RuntimeError("Bambu project central directory entry exceeds its declared size")
        actual_entries += 1
        if actual_entries > _PROJECT_MAX_MEMBERS:
            raise RuntimeError(
                f"Bambu project central directory has more than {_PROJECT_MAX_MEMBERS} entries"
            )
        position += record_size
    if position != directory_end or actual_entries != entry_count:
        raise RuntimeError(
            "Bambu project central directory entry count does not match its end record"
        )
    _require_pinned_unchanged(project_file, label="Bambu project archive")


def _validated_project_members(package: ZipFile, *, project: Path) -> dict[str, ZipInfo]:
    infos = package.infolist()
    if not infos or len(infos) > _PROJECT_MAX_MEMBERS:
        raise RuntimeError(
            f"Bambu project archive member count {len(infos)} is outside the supported "
            f"1..{_PROJECT_MAX_MEMBERS} range: {project}"
        )
    by_name: dict[str, ZipInfo] = {}
    aliases: dict[str, str] = {}
    total_uncompressed = 0
    for info in infos:
        name = info.filename
        original_name = info.orig_filename
        if (
            not name
            or original_name != name
            or "\x00" in original_name
            or len(original_name.encode("utf-8", errors="surrogatepass"))
            > _PROJECT_MAX_MEMBER_NAME_BYTES
        ):
            raise RuntimeError(
                f"Bambu project archive member raw name is invalid: {original_name!r}"
            )
        if name in by_name:
            raise RuntimeError(f"Bambu project archive contains a duplicate member: {name}")
        alias = _windows_archive_alias(name)
        previous = aliases.get(alias)
        if previous is not None:
            raise RuntimeError(
                "Bambu project archive contains a Unicode/case/Windows alias collision: "
                f"{previous!r} and {name!r}"
            )
        aliases[alias] = name
        by_name[name] = info
        if info.flag_bits & 0x1:
            raise RuntimeError(f"Bambu project archive contains an encrypted member: {name}")
        if info.flag_bits & ~_PROJECT_ALLOWED_FLAG_BITS:
            raise RuntimeError(
                f"Bambu project archive member has unsupported flags 0x{info.flag_bits:04x}: {name}"
            )
        if info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
            raise RuntimeError(
                f"Bambu project archive uses unsupported compression {info.compress_type}: {name}"
            )
        if not _archive_member_is_regular(info):
            raise RuntimeError(
                f"Bambu project archive member is not a regular file/directory: {name}"
            )
        if info.file_size < 0 or info.file_size > _PROJECT_MAX_MEMBER_BYTES:
            raise RuntimeError(
                f"Bambu project archive member exceeds the {_PROJECT_MAX_MEMBER_BYTES} "
                f"byte limit: {name}"
            )
        total_uncompressed += info.file_size
        if total_uncompressed > _PROJECT_MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise RuntimeError(
                "Bambu project archive exceeds the total uncompressed byte limit: "
                f"{_PROJECT_MAX_TOTAL_UNCOMPRESSED_BYTES}"
            )
        if info.file_size:
            if info.compress_size <= 0:
                raise RuntimeError(
                    f"Bambu project archive member has an invalid compressed size: {name}"
                )
            ratio = info.file_size / info.compress_size
            if ratio > _PROJECT_MAX_COMPRESSION_RATIO:
                raise RuntimeError(
                    f"Bambu project archive member compression ratio {ratio:.3f} exceeds "
                    f"{_PROJECT_MAX_COMPRESSION_RATIO:g}: {name}"
                )
    missing = _PROJECT_REQUIRED_MEMBERS - by_name.keys()
    if missing:
        raise RuntimeError(
            f"Bambu project archive is missing required members: {', '.join(sorted(missing))}"
        )
    if any(by_name[name].is_dir() for name in _PROJECT_REQUIRED_MEMBERS):
        raise RuntimeError("Bambu project archive stores a required member as a directory")
    return by_name


def _reject_external_relationships(payload: bytes, *, name: str) -> None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RuntimeError(f"Bambu project relationship XML is invalid: {name}: {exc}") from exc
    for relationship in root.iter():
        if relationship.attrib.get("TargetMode", "Internal").casefold() == "external":
            raise RuntimeError(f"Bambu project archive contains an external relationship: {name}")


def _project_model_measurement(payload: bytes | bytearray) -> dict[str, Any]:
    core_namespace = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    core_prefix = f"{{{core_namespace}}}"

    identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)

    def transform(value: str | None) -> tuple[float, ...]:
        if value is None:
            return identity
        try:
            parsed = tuple(float(item) for item in value.split())
        except ValueError as exc:
            raise RuntimeError("Bambu project model has an invalid transform") from exc
        if len(parsed) != 12 or not all(math.isfinite(item) for item in parsed):
            raise RuntimeError("Bambu project model has an invalid transform")
        return parsed

    def apply_transform(
        point: tuple[float, float, float], matrix: tuple[float, ...]
    ) -> tuple[float, float, float]:
        x, y, z = point
        return (
            x * matrix[0] + y * matrix[3] + z * matrix[6] + matrix[9],
            x * matrix[1] + y * matrix[4] + z * matrix[7] + matrix[10],
            x * matrix[2] + y * matrix[5] + z * matrix[8] + matrix[11],
        )

    objects: dict[int, dict[str, Any]] = {}
    build_items: list[tuple[int, tuple[float, ...]]] = []
    resources_count = 0
    build_count = 0
    element_stack: list[ET.Element] = []
    tag_stack: list[str] = []
    current_object: dict[str, Any] | None = None
    try:
        events = ET.iterparse(io.BytesIO(payload), events=("start", "end"))
        for event, element in events:
            if event == "start":
                parent_tag = tag_stack[-1] if tag_stack else None
                grandparent_tag = tag_stack[-2] if len(tag_stack) > 1 else None
                element_stack.append(element)
                tag_stack.append(element.tag)
                if len(tag_stack) == 1:
                    if element.tag != f"{core_prefix}model":
                        raise RuntimeError("Bambu project model has no core model root")
                    if element.attrib.get("unit", "millimeter").casefold() not in {
                        "millimeter",
                        "millimetre",
                        "mm",
                    }:
                        raise RuntimeError("Bambu project model must use millimetres")
                elif len(tag_stack) == 2 and element.tag == f"{core_prefix}resources":
                    resources_count += 1
                elif len(tag_stack) == 2 and element.tag == f"{core_prefix}build":
                    build_count += 1
                elif (
                    element.tag == f"{core_prefix}object"
                    and parent_tag == f"{core_prefix}resources"
                    and grandparent_tag == f"{core_prefix}model"
                ):
                    try:
                        object_id = int(element.attrib["id"])
                    except (KeyError, ValueError) as exc:
                        raise RuntimeError("Bambu project model has an invalid object id") from exc
                    if object_id <= 0 or object_id in objects:
                        raise RuntimeError(
                            "Bambu project model object ids must be unique and positive"
                        )
                    current_object = {
                        "id": object_id,
                        "mesh_count": 0,
                        "components_group_count": 0,
                        "vertices_group_count": 0,
                        "triangles_group_count": 0,
                        "vertices": [],
                        "triangle_count": 0,
                        "maximum_triangle_index": -1,
                        "components": [],
                    }
                elif current_object is not None:
                    if element.tag == f"{core_prefix}mesh" and parent_tag == f"{core_prefix}object":
                        current_object["mesh_count"] += 1
                    elif (
                        element.tag == f"{core_prefix}components"
                        and parent_tag == f"{core_prefix}object"
                    ):
                        current_object["components_group_count"] += 1
                    elif (
                        element.tag == f"{core_prefix}vertices"
                        and parent_tag == f"{core_prefix}mesh"
                    ):
                        current_object["vertices_group_count"] += 1
                    elif (
                        element.tag == f"{core_prefix}triangles"
                        and parent_tag == f"{core_prefix}mesh"
                    ):
                        current_object["triangles_group_count"] += 1
                continue

            parent_tag = tag_stack[-2] if len(tag_stack) > 1 else None
            grandparent_tag = tag_stack[-3] if len(tag_stack) > 2 else None
            if current_object is not None and (
                element.tag == f"{core_prefix}vertex"
                and parent_tag == f"{core_prefix}vertices"
                and grandparent_tag == f"{core_prefix}mesh"
            ):
                try:
                    coordinates = tuple(float(element.attrib[axis]) for axis in ("x", "y", "z"))
                except (KeyError, ValueError) as exc:
                    raise RuntimeError("Bambu project model has an invalid vertex") from exc
                if len(coordinates) != 3 or not all(math.isfinite(value) for value in coordinates):
                    raise RuntimeError("Bambu project model has a non-finite vertex")
                current_object["vertices"].append(coordinates)
            elif current_object is not None and (
                element.tag == f"{core_prefix}triangle"
                and parent_tag == f"{core_prefix}triangles"
                and grandparent_tag == f"{core_prefix}mesh"
            ):
                try:
                    indices = tuple(int(element.attrib[axis]) for axis in ("v1", "v2", "v3"))
                except (KeyError, ValueError) as exc:
                    raise RuntimeError("Bambu project model has an invalid triangle") from exc
                if len(set(indices)) != 3 or min(indices) < 0:
                    raise RuntimeError("Bambu project model triangle indices are invalid")
                current_object["triangle_count"] += 1
                current_object["maximum_triangle_index"] = max(
                    current_object["maximum_triangle_index"],
                    *indices,
                )
            elif current_object is not None and (
                element.tag == f"{core_prefix}component"
                and parent_tag == f"{core_prefix}components"
                and grandparent_tag == f"{core_prefix}object"
            ):
                try:
                    referenced_id = int(element.attrib["objectid"])
                except (KeyError, ValueError) as exc:
                    raise RuntimeError("Bambu project component has an invalid object id") from exc
                current_object["components"].append(
                    (referenced_id, transform(element.attrib.get("transform")))
                )
            elif (
                element.tag == f"{core_prefix}item"
                and parent_tag == f"{core_prefix}build"
                and grandparent_tag == f"{core_prefix}model"
            ):
                try:
                    build_object_id = int(element.attrib["objectid"])
                except (KeyError, ValueError) as exc:
                    raise RuntimeError("Bambu project build item has an invalid object id") from exc
                build_items.append((build_object_id, transform(element.attrib.get("transform"))))
            elif current_object is not None and (
                element.tag == f"{core_prefix}object" and parent_tag == f"{core_prefix}resources"
            ):
                mesh_count = int(current_object["mesh_count"])
                component_group_count = int(current_object["components_group_count"])
                if (mesh_count, component_group_count) not in {(1, 0), (0, 1)}:
                    raise RuntimeError(
                        "Bambu project object must contain one mesh or components group"
                    )
                vertices = current_object["vertices"]
                triangle_count = int(current_object["triangle_count"])
                components = current_object["components"]
                if mesh_count:
                    if (
                        current_object["vertices_group_count"] != 1
                        or current_object["triangles_group_count"] != 1
                        or not vertices
                        or triangle_count <= 0
                    ):
                        raise RuntimeError("Bambu project model has an empty triangle mesh")
                    if current_object["maximum_triangle_index"] >= len(vertices):
                        raise RuntimeError("Bambu project model triangle indices are invalid")
                    objects[int(current_object["id"])] = {
                        "vertices": tuple(vertices),
                        "triangle_count": triangle_count,
                        "components": (),
                    }
                else:
                    if not components:
                        raise RuntimeError("Bambu project components group is empty")
                    objects[int(current_object["id"])] = {
                        "vertices": (),
                        "triangle_count": 0,
                        "components": tuple(components),
                    }
                current_object = None

            tag_stack.pop()
            element_stack.pop()
            element.clear()
            if element_stack:
                with suppress(ValueError):
                    element_stack[-1].remove(element)
    except ET.ParseError as exc:
        raise RuntimeError(f"Bambu project model XML is invalid: {exc}") from exc

    if resources_count != 1:
        raise RuntimeError("Bambu project model must contain one core resources element")
    if build_count != 1:
        raise RuntimeError("Bambu project model must contain one core build element")
    if len(build_items) != 1:
        raise RuntimeError("Bambu project model must contain exactly one build item")
    build_object_id, build_transform = build_items[0]

    def saturated_add(left: int, right: int, limit: int) -> int:
        if left > limit or right > limit or right > limit - left:
            return limit + 1
        return left + right

    summaries: dict[int, tuple[int, int, int, int]] = {}

    def summarize(
        object_id: int,
        active: frozenset[int],
    ) -> tuple[int, int, int, int]:
        if object_id in active:
            raise RuntimeError("Bambu project component graph contains a cycle")
        cached = summaries.get(object_id)
        if cached is not None:
            return cached
        if len(active) >= _PROJECT_MAX_COMPONENT_DEPTH:
            raise RuntimeError(
                "Bambu project component graph exceeds the expanded depth limit "
                f"{_PROJECT_MAX_COMPONENT_DEPTH}"
            )
        model_object = objects.get(object_id)
        if model_object is None:
            raise RuntimeError("Bambu project references a missing object")
        vertices = model_object["vertices"]
        if vertices:
            summary = (
                1,
                len(vertices),
                int(model_object["triangle_count"]),
                1,
            )
        else:
            instances = 1
            vertex_visits = 0
            triangle_visits = 0
            depth = 1
            descendants = active | {object_id}
            for referenced_id, _component_transform in model_object["components"]:
                child_instances, child_vertices, child_triangles, child_depth = summarize(
                    referenced_id,
                    descendants,
                )
                instances = saturated_add(
                    instances,
                    child_instances,
                    _PROJECT_MAX_EXPANDED_INSTANCES,
                )
                vertex_visits = saturated_add(
                    vertex_visits,
                    child_vertices,
                    _PROJECT_MAX_EXPANDED_VERTICES,
                )
                triangle_visits = saturated_add(
                    triangle_visits,
                    child_triangles,
                    _PROJECT_MAX_EXPANDED_TRIANGLES,
                )
                depth = max(depth, child_depth + 1)
            summary = (instances, vertex_visits, triangle_visits, depth)
        instances, vertex_visits, triangle_visits, depth = summary
        if depth > _PROJECT_MAX_COMPONENT_DEPTH:
            raise RuntimeError(
                "Bambu project component graph exceeds the expanded depth limit "
                f"{_PROJECT_MAX_COMPONENT_DEPTH}"
            )
        if instances > _PROJECT_MAX_EXPANDED_INSTANCES:
            raise RuntimeError(
                "Bambu project component graph exceeds the expanded object instance limit "
                f"{_PROJECT_MAX_EXPANDED_INSTANCES}"
            )
        if vertex_visits > _PROJECT_MAX_EXPANDED_VERTICES:
            raise RuntimeError(
                "Bambu project component graph exceeds the expanded vertex limit "
                f"{_PROJECT_MAX_EXPANDED_VERTICES}"
            )
        if triangle_visits > _PROJECT_MAX_EXPANDED_TRIANGLES:
            raise RuntimeError(
                "Bambu project component graph exceeds the expanded triangle limit "
                f"{_PROJECT_MAX_EXPANDED_TRIANGLES}"
            )
        summaries[object_id] = summary
        return summary

    (
        _expanded_instances,
        _expanded_vertices,
        expanded_triangle_count,
        _expanded_depth,
    ) = summarize(build_object_id, frozenset())
    minimum = [math.inf, math.inf, math.inf]
    maximum = [-math.inf, -math.inf, -math.inf]
    triangle_count = 0

    def visit(
        object_id: int,
        transforms: tuple[tuple[float, ...], ...],
        active: frozenset[int],
    ) -> None:
        nonlocal triangle_count
        if object_id in active:
            raise RuntimeError("Bambu project component graph contains a cycle")
        model_object = objects.get(object_id)
        if model_object is None:
            raise RuntimeError("Bambu project references a missing object")
        vertices = model_object["vertices"]
        if vertices:
            for vertex in vertices:
                transformed = vertex
                for matrix in transforms:
                    transformed = apply_transform(transformed, matrix)
                for axis, coordinate in enumerate(transformed):
                    minimum[axis] = min(minimum[axis], coordinate)
                    maximum[axis] = max(maximum[axis], coordinate)
            triangle_count += int(model_object["triangle_count"])
            return
        descendants = active | {object_id}
        for referenced_id, component_transform in model_object["components"]:
            visit(referenced_id, (component_transform, *transforms), descendants)

    visit(build_object_id, (build_transform,), frozenset())
    if (
        triangle_count <= 0
        or triangle_count != expanded_triangle_count
        or not all(math.isfinite(value) for value in (*minimum, *maximum))
    ):
        raise RuntimeError("Bambu project build item has no finite triangle mesh")
    return {
        "dimensions_mm": [maximum[axis] - minimum[axis] for axis in range(3)],
        "triangle_count": triangle_count,
    }


def _inspect_archive_pinned(
    project_file: _PinnedRegularFile,
    primary_file: _PinnedRegularFile,
) -> _ArchiveInspection:
    project = project_file.path
    actual_md5 = hashlib.md5(usedforsecurity=False)
    recorded_md5_bytes: bytes | None = None
    model_xml_bytes: bytearray | None = None
    embedded_matches_primary = True
    try:
        project_size = project_file.information.st_size
        if project_size <= 0 or project_size > _PROJECT_MAX_ARCHIVE_BYTES:
            raise RuntimeError(
                f"Bambu project archive size {project_size} is outside the "
                f"supported 1..{_PROJECT_MAX_ARCHIVE_BYTES} byte range: {project}"
            )
        _preflight_project_central_directory(project_file)
        project_hash = _hash_pinned(project_file, label="Bambu project archive")
        primary_snapshot = _read_gcode_from_pinned(
            primary_file,
            label="primary Bambu G-code",
        )
        project_file.handle.seek(0)
        primary_file.handle.seek(0)
        with ZipFile(project_file.handle, "r") as package:
            members = _validated_project_members(package, project=project)
            embedded_info = members["Metadata/plate_1.gcode"]
            if embedded_info.file_size != primary_snapshot.size:
                embedded_matches_primary = False
            for info in package.infolist():
                if info.is_dir():
                    continue
                capture_relationship = info.filename.endswith(".rels")
                capture_md5 = info.filename == "Metadata/plate_1.gcode.md5"
                capture_model = info.filename == "3D/3dmodel.model"
                captured = bytearray()
                count = 0
                with package.open(info, "r") as member:
                    while True:
                        block = member.read(1024 * 1024)
                        if not block:
                            break
                        count += len(block)
                        if count > info.file_size or count > _PROJECT_MAX_MEMBER_BYTES:
                            raise RuntimeError(
                                "Bambu project archive member expanded beyond its "
                                f"validated size: {info.filename}"
                            )
                        if capture_relationship:
                            if count > _PROJECT_MAX_RELATIONSHIP_BYTES:
                                raise RuntimeError(
                                    "Bambu project relationship member exceeds the "
                                    f"{_PROJECT_MAX_RELATIONSHIP_BYTES} byte limit: "
                                    f"{info.filename}"
                                )
                            captured.extend(block)
                        elif capture_md5:
                            if count > 128:
                                raise RuntimeError(
                                    "Bambu project embedded G-code MD5 record is oversized"
                                )
                            captured.extend(block)
                        elif capture_model:
                            if count > _PROJECT_MAX_MODEL_XML_BYTES:
                                raise RuntimeError(
                                    "Bambu project model XML exceeds the "
                                    f"{_PROJECT_MAX_MODEL_XML_BYTES} byte limit"
                                )
                            captured.extend(block)
                        elif info.filename == "Metadata/plate_1.gcode":
                            actual_md5.update(block)
                            if primary_file.handle.read(len(block)) != block:
                                embedded_matches_primary = False
                if count != info.file_size:
                    raise RuntimeError(
                        f"Bambu project archive member size changed while reading: {info.filename}"
                    )
                if capture_relationship:
                    _reject_external_relationships(bytes(captured), name=info.filename)
                elif capture_md5:
                    recorded_md5_bytes = bytes(captured)
                elif capture_model:
                    model_xml_bytes = captured
            if primary_file.handle.read(1):
                embedded_matches_primary = False
        _require_pinned_unchanged(project_file, label="Bambu project archive")
        _require_pinned_unchanged(primary_file, label="primary Bambu G-code")
    except (BadZipFile, OSError) as exc:
        raise RuntimeError(f"Bambu project archive is invalid: {project}: {exc}") from exc

    if recorded_md5_bytes is None:
        raise RuntimeError("Bambu project archive has no embedded G-code MD5 record")
    if model_xml_bytes is None:
        raise RuntimeError("Bambu project archive has no model XML")
    try:
        recorded_md5 = recorded_md5_bytes.decode("ascii").strip().upper()
    except UnicodeDecodeError as exc:
        raise RuntimeError("Bambu project embedded G-code MD5 is not ASCII") from exc
    if re.fullmatch(r"[0-9A-F]{32}", recorded_md5) is None:
        raise RuntimeError("Bambu project embedded G-code MD5 is malformed")
    actual_md5_value = actual_md5.hexdigest().upper()
    model_measurement = _project_model_measurement(model_xml_bytes)
    evidence = {
        "archive_test_passed": True,
        "embedded_gcode_md5": recorded_md5,
        "embedded_gcode_md5_actual": actual_md5_value,
        "embedded_gcode_md5_verified": recorded_md5 == actual_md5_value,
        "embedded_gcode_matches_primary": embedded_matches_primary,
        "project_model_dimensions_mm": model_measurement["dimensions_mm"],
        "project_model_triangle_count": model_measurement["triangle_count"],
    }
    return _ArchiveInspection(
        evidence=evidence,
        project_sha256=project_hash,
        primary_gcode=primary_snapshot,
    )


def _inspect_archive(project: Path, primary_gcode: Path) -> _ArchiveInspection:
    with (
        _open_pinned_regular_file(project, label="Bambu project archive") as project_file,
        _open_pinned_regular_file(
            primary_gcode,
            label="primary Bambu G-code",
        ) as primary_file,
    ):
        return _inspect_archive_pinned(project_file, primary_file)


def archive_evidence(project: Path, primary_gcode: Path) -> dict[str, Any]:
    return _inspect_archive(project, primary_gcode).evidence


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
            _ensure_directory_tree(path, label="isolated Bambu runtime")
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
        _ensure_directory_tree(path, label="isolated Bambu runtime")
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
    _identity: _ExecutableIdentity | None = None,
) -> dict[str, Any]:
    """Probe the exact executable in isolation and retain version-bearing hashes."""
    if _identity is None:
        with _lock_executable(executable) as locked:
            return probe_bambu_studio(
                locked.path,
                runtime=runtime,
                timeout_seconds=timeout_seconds,
                evidence_root=evidence_root,
                _identity=locked,
            )
    if _absolute_path(executable) != _identity.path:
        raise RuntimeError("Bambu Studio probe path differs from the locked executable")
    command = [str(_identity.path), "--help"]
    execution = _run_with_executable_identity(
        _identity,
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
        _ensure_directory_tree(evidence_root, label="Bambu probe evidence")
        stdout_path = evidence_root / "bambu-studio-probe.stdout.log"
        stderr_path = evidence_root / "bambu-studio-probe.stderr.log"
        stdout_hash = _write_text_from_memory(stdout_path, execution.stdout)
        stderr_hash = _write_text_from_memory(stderr_path, execution.stderr)
        record.update(
            {
                "stdout_path": stdout_path.name,
                "stderr_path": stderr_path.name,
                "stdout_sha256": stdout_hash,
                "stderr_sha256": stderr_hash,
            }
        )
    return record


def run_checked(
    command: list[str],
    *,
    runtime: Path,
    timeout_seconds: float,
    executable_identity: _ExecutableIdentity,
) -> CommandExecution:
    execution = _run_with_executable_identity(
        executable_identity,
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


def dimensions_match(first: Sequence[Any], second: Sequence[float]) -> bool:
    try:
        return all(
            abs(float(actual) - expected) <= 0.001
            for actual, expected in zip(first, second, strict=True)
        )
    except (TypeError, ValueError):
        return False


def _expected_tile_id(row: int, column: int) -> str:
    return f"tile-r{row:04d}-c{column:04d}"


def _verified_source_profiles(
    slice_root: Path,
    manifest: Any,
) -> tuple[tuple[Any, Path, _BinarySnapshot], ...]:
    records = tuple(manifest.profile_files)
    identities = tuple((record.role, record.index) for record in records)
    expected = (("settings", 0), ("settings", 1), ("filament", 0))
    if identities != expected:
        raise RuntimeError(
            "official slice profiles must be exactly machine/process/filament in stable order"
        )
    paths: set[Path] = set()
    aliases: set[str] = set()
    verified: list[tuple[Any, Path, _BinarySnapshot]] = []
    for record in records:
        if PurePosixPath(record.path).parent != PurePosixPath("profiles") or ";" in record.path:
            raise RuntimeError("official slice profile path is not CLI-safe")
        path = resolve_relative(slice_root, record.path)
        alias = unicodedata.normalize("NFC", record.path).casefold()
        if path in paths or alias in aliases:
            raise RuntimeError("official slice profile paths are duplicated or aliased")
        snapshot = _read_binary_snapshot(
            path,
            label="official slice profile",
            minimum_bytes=1,
            maximum_bytes=_MAX_JSON_BYTES,
        )
        if snapshot.sha256 != record.sha256:
            raise RuntimeError(f"official slice profile checksum mismatch: {path}")
        paths.add(path)
        aliases.add(alias)
        verified.append((record, path, snapshot))
    profile_root = slice_root / "profiles"
    if (
        not profile_root.is_dir()
        or profile_root.is_symlink()
        or {path.resolve() for path in profile_root.iterdir()} != paths
        or any(not path.is_file() or path.is_symlink() for path in profile_root.iterdir())
    ):
        raise RuntimeError("official slice profile inventory has missing or extra files")
    return tuple(verified)


def _source_print_tile(
    *,
    print_root: Path,
    print_manifest: Any,
    print_record: Any,
    snapshot_root: Path,
) -> tuple[Any, Path, str, int, Path, Any]:
    from topoforge.tiling.connectors import PrintTileArtifactManifest

    expected_id = _expected_tile_id(print_record.row, print_record.column)
    expected_directory = f"tiles/{expected_id}"
    expected_manifest_path = f"{expected_directory}/print_tile_manifest.json"
    if (
        print_record.tile_id != expected_id
        or print_record.directory != expected_directory
        or print_record.tile_manifest != expected_manifest_path
    ):
        raise RuntimeError(
            f"source print tile id/row/column/path binding changed: {print_record.tile_id}"
        )
    artifact_path = resolve_relative(print_root, print_record.tile_manifest)
    artifact_value, artifact_snapshot = _load_canonical_model_snapshot(
        artifact_path,
        PrintTileArtifactManifest,
    )
    if artifact_snapshot.sha256 != print_record.tile_manifest_sha256:
        raise RuntimeError(f"source print tile manifest checksum mismatch: {expected_id}")
    if not isinstance(artifact_value, PrintTileArtifactManifest):
        raise AssertionError("unexpected source print tile model")
    artifact = artifact_value
    expected_files = {role: f"{expected_directory}/{name}" for role, name in artifact.files.items()}
    if (
        artifact.schema_version != "topoforge-print-tile-artifact-v1"
        or artifact.tile_id != expected_id
        or artifact.layout_id != print_manifest.layout_id
        or (artifact.row, artifact.column) != (print_record.row, print_record.column)
        or artifact.tile_key != print_record.tile_key
        or artifact.source_tile_mesh_manifest_sha256
        != print_record.source_tile_mesh_manifest_sha256
        or artifact.connector_plan_sha256 != print_manifest.connector_plan_sha256
        or artifact.sha256 != print_record.sha256
        or expected_files != print_record.files
    ):
        raise RuntimeError(f"source print tile artifact identity mismatch: {expected_id}")
    tile_directory = print_root.joinpath(*_relative_parts(expected_directory, label="tile"))
    expected_inventory = {*artifact.files.values(), "print_tile_manifest.json"}
    if (
        not tile_directory.is_dir()
        or tile_directory.is_symlink()
        or {path.name for path in tile_directory.iterdir()} != expected_inventory
        or any(not path.is_file() or path.is_symlink() for path in tile_directory.iterdir())
    ):
        raise RuntimeError(f"source print tile inventory changed: {expected_id}")
    verified_files: dict[str, Path] = {}
    source_3mf_hash: str | None = None
    source_3mf_size: int | None = None
    verified_source_3mf: Path | None = None
    for role, relative in print_record.files.items():
        path = resolve_relative(print_root, relative)
        if role == "print_local_3mf":
            verified_source_3mf = snapshot_root / f"{expected_id}.print-local.3mf"
            actual_hash, source_3mf_size = _copy_regular_file_snapshot(
                path,
                verified_source_3mf,
                label=f"source print-local 3MF for {expected_id}",
                maximum_bytes=_PROJECT_MAX_ARCHIVE_BYTES,
            )
            source_3mf_hash = actual_hash
        else:
            actual_hash = sha256(path)
        if actual_hash != print_record.sha256[role] or actual_hash != artifact.sha256[role]:
            raise RuntimeError(f"source print artifact checksum mismatch: {expected_id}: {role}")
        verified_files[role] = path
    source_3mf = verified_files["print_local_3mf"]
    if source_3mf_hash is None or source_3mf_size is None or verified_source_3mf is None:
        raise RuntimeError(f"source print tile has no print-local 3MF: {expected_id}")
    inspection = inspect_3mf(verified_source_3mf)
    bounds = print_record.print_local_bounds_mm
    bounds_dimensions = (
        bounds[3] - bounds[0],
        bounds[4] - bounds[1],
        bounds[5] - bounds[2],
    )
    validation = artifact.validation
    if (
        validation.schema_version != "topoforge-print-tile-artifact-v1"
        or validation.tile_id != expected_id
        or validation.male_connector_ids != print_record.male_connector_ids
        or validation.female_connector_ids != print_record.female_connector_ids
        or validation.expected_print_local_bounds_mm != print_record.print_local_bounds_mm
        or validation.global_to_print_local_translation_mm
        != print_record.global_to_print_local_translation_mm
        or inspection.strict_warning_count != 0
        or validation.strict_3mf_warning_count.get("print_local") != 0
        or inspection.triangle_count != print_record.triangle_count
        or inspection.triangle_count != validation.local_geometry.triangle_count
        or inspection.triangle_count != validation.local_format_triangle_counts.get("3mf")
        or not dimensions_match(
            list(inspection.dimensions_mm),
            tuple(validation.local_geometry.dimensions_mm),
        )
        or not dimensions_match(list(inspection.dimensions_mm), bounds_dimensions)
        or validation.required_checks_passed is not True
        or validation.triangle_counts_match is not True
        or validation.bounds_match is not True
        or validation.orientation_consistent is not True
    ):
        raise RuntimeError(
            f"source print-local 3MF measurements do not match source evidence: {expected_id}"
        )
    return (
        artifact,
        source_3mf,
        source_3mf_hash,
        source_3mf_size,
        verified_source_3mf,
        inspection,
    )


def _source_slice_tile(
    *,
    slice_root: Path,
    slice_manifest: Any,
    slice_record: Any,
    print_record: Any,
    source_3mf: Path,
    source_3mf_sha256: str,
    source_inspection: Any,
    expected_version: str,
) -> tuple[Any, Path, _TextSnapshot]:
    from topoforge.tiling.slicing import PrintTileSliceReport

    tile_id = print_record.tile_id
    expected_directory = f"tiles/{tile_id}"
    if (
        slice_record.tile_id != tile_id
        or (slice_record.row, slice_record.column) != (print_record.row, print_record.column)
        or slice_record.directory != expected_directory
        or slice_record.report_path != f"{expected_directory}/slice_report.json"
        or slice_record.gcode_path != f"{expected_directory}/model.gcode"
        or slice_record.source_print_tile_manifest_sha256 != print_record.tile_manifest_sha256
        or slice_record.source_print_local_3mf_sha256 != source_3mf_sha256
    ):
        raise RuntimeError(f"source print/slice tile binding mismatch: {tile_id}")
    tile_directory = slice_root.joinpath(
        *_relative_parts(expected_directory, label="source slice tile")
    )
    if (
        not tile_directory.is_dir()
        or tile_directory.is_symlink()
        or {path.name for path in tile_directory.iterdir()} != {"slice_report.json", "model.gcode"}
        or any(not path.is_file() or path.is_symlink() for path in tile_directory.iterdir())
    ):
        raise RuntimeError(f"source slice tile inventory changed: {tile_id}")
    report_path = resolve_relative(slice_root, slice_record.report_path)
    source_gcode = resolve_relative(slice_root, slice_record.gcode_path)
    report_value, report_snapshot = _load_canonical_model_snapshot(
        report_path,
        PrintTileSliceReport,
    )
    if report_snapshot.sha256 != slice_record.report_sha256:
        raise RuntimeError(f"source slice report checksum mismatch: {tile_id}")
    gcode_snapshot = _read_gcode_snapshot(
        source_gcode,
        label="source slice G-code",
    )
    if gcode_snapshot.sha256 != slice_record.gcode_sha256:
        raise RuntimeError(f"source slice G-code checksum mismatch: {tile_id}")
    if not isinstance(report_value, PrintTileSliceReport):
        raise AssertionError("unexpected source slice report model")
    report = report_value
    if (
        report.schema_version != "topoforge-print-tile-slice-v1"
        or report.tile_id != tile_id
        or report.source_print_tile_manifest_sha256 != print_record.tile_manifest_sha256
        or report.source_print_local_3mf_path != print_record.files["print_local_3mf"]
        or report.source_print_local_3mf_sha256 != source_3mf_sha256
        or report.gcode_path != slice_record.gcode_path
        or report.gcode_sha256 != slice_record.gcode_sha256
        or report.gcode_size_bytes != gcode_snapshot.size
        or report.input_strict_3mf_warning_count != source_inspection.strict_warning_count
        or report.slicer_result.input_model != Path(report.source_print_local_3mf_path)
        or report.slicer_result.output_gcode != Path(report.gcode_path)
        or report.slicer_result.profile != slice_manifest.profile_name
        or report.slicer_result.slicer.name != slice_manifest.slicer.name
        or report.slicer_result.slicer.version != slice_manifest.slicer.version
        or report.slicer_result.slicer.status != slice_manifest.slicer.status
    ):
        raise RuntimeError(f"source slice report identity mismatch: {tile_id}")

    actual_version = _gcode_bambu_version_text(gcode_snapshot.text, path=source_gcode)
    reopened_metrics = parse_gcode_metrics(
        gcode_snapshot.text,
        diagnostics="\n".join((report.slicer_result.stdout, report.slicer_result.stderr)),
    )
    expected_gate = evaluate_bambu_p2s_release_gate(
        report.slicer_result.model_dump(mode="json"),
        printer_profile_id=slice_manifest.printer_profile_id,
    )
    release_passed = expected_gate.get("release_gate_passed") is True
    parameter_checks_passed = expected_gate.get("parameter_checks_passed") is True
    exit_code_zero = report.slicer_result.exit_code == 0
    gcode_generated = bool(
        report.slicer_result.gcode_generated
        and report.slicer_result.gcode_size_bytes == gcode_snapshot.size
    )
    layer_count_positive = bool(
        reopened_metrics.layer_count is not None and reopened_metrics.layer_count > 0
    )
    required = bool(
        report.slicer_result.status is SliceStatus.SUCCEEDED
        and exit_code_zero
        and gcode_generated
        and source_inspection.strict_warning_count == 0
        and reopened_metrics == report.slicer_result.metrics
        and layer_count_positive
        and not reopened_metrics.out_of_bed
        and not reopened_metrics.empty_layer_warning
        and not reopened_metrics.floating_region_warning
        and reopened_metrics.support_material is False
        and release_passed
        and parameter_checks_passed
        and actual_version == expected_version
    )
    if (
        reopened_metrics != report.reopened_metrics
        or report.manufacturing_release_gate != expected_gate
        or report.release_role != "official-p2s-release"
        or report.official_p2s_release_gate_passed is not release_passed
        or report.exit_code_zero is not exit_code_zero
        or report.gcode_generated is not gcode_generated
        or report.metrics_reopen_match is not (reopened_metrics == report.slicer_result.metrics)
        or report.layer_count_positive is not layer_count_positive
        or report.out_of_bed is not reopened_metrics.out_of_bed
        or report.empty_layer_warning is not reopened_metrics.empty_layer_warning
        or report.floating_region_warning is not reopened_metrics.floating_region_warning
        or report.support_material is not reopened_metrics.support_material
        or report.required_checks_passed is not required
        or slice_record.layer_count != reopened_metrics.layer_count
        or slice_record.estimated_time_seconds != reopened_metrics.estimated_time_seconds
        or slice_record.filament_used_mm != reopened_metrics.filament_used_mm
        or slice_record.filament_used_cm3 != reopened_metrics.filament_used_cm3
        or slice_record.filament_used_g != reopened_metrics.filament_used_g
        or slice_record.required_checks_passed is not required
        or not required
    ):
        raise RuntimeError(f"source slice metrics/release gate changed: {tile_id}")
    return report, source_gcode, gcode_snapshot


def _optional_total(values: list[float | int | None]) -> float | int | None:
    present = [value for value in values if value is not None]
    return None if not present else sum(present)


def _verify_source_evidence_at(
    *,
    print_root: Path,
    slice_root: Path,
    executable: Path,
    snapshot_root: Path,
) -> _SourceEvidence:
    from topoforge.tiling.connectors import PrintTileAssemblyManifest
    from topoforge.tiling.slicing import PrintTileSliceManifest

    print_path = print_root / "print-tile-assembly-manifest.json"
    slice_path = slice_root / "tile-slice-manifest.json"
    print_value, print_snapshot = _load_canonical_model_snapshot(
        print_path,
        PrintTileAssemblyManifest,
    )
    slice_value, slice_snapshot = _load_canonical_model_snapshot(
        slice_path,
        PrintTileSliceManifest,
    )
    if not isinstance(print_value, PrintTileAssemblyManifest):
        raise AssertionError("unexpected source print assembly model")
    if not isinstance(slice_value, PrintTileSliceManifest):
        raise AssertionError("unexpected source slice assembly model")
    print_manifest = print_value
    slice_manifest = slice_value
    expected_version = frozen_source_bambu_version(slice_manifest.model_dump(mode="python"))
    executable_hash = sha256(executable)
    if (
        print_manifest.schema_version != "topoforge-print-tile-assembly-v1"
        or slice_manifest.schema_version != "topoforge-print-tile-slice-assembly-v1"
        or slice_manifest.layout_id != print_manifest.layout_id
        or slice_manifest.source_print_tile_assembly_sha256 != print_snapshot.sha256
        or slice_manifest.source_connector_plan_sha256 != print_manifest.connector_plan_sha256
        or slice_manifest.tile_grid_shape != print_manifest.tile_grid_shape
        or slice_manifest.tile_count != print_manifest.tile_count
        or slice_manifest.printer_profile_id != "bambu-p2s-0.4"
        or slice_manifest.release_role != "official-p2s-release"
        or slice_manifest.slicer.name != "BambuStudio"
        or slice_manifest.slicer.status.value != "available"
        or slice_manifest.slicer.version != expected_version
        or slice_manifest.slicer_executable_sha256 != executable_hash
        or parse_bambu_studio_version(f"BambuStudio-{expected_version}:") != expected_version
    ):
        raise RuntimeError("source print/slice/Bambu identities do not match")
    profiles = _verified_source_profiles(slice_root, slice_manifest)

    print_records = tuple(print_manifest.tiles)
    slice_records = tuple(slice_manifest.tiles)
    print_identity = tuple((record.tile_id, record.row, record.column) for record in print_records)
    slice_identity = tuple((record.tile_id, record.row, record.column) for record in slice_records)
    if print_identity != slice_identity:
        raise RuntimeError("source print and slice manifests have different tile sets/order")

    tiles: list[_SourceTileEvidence] = []
    reports: list[Any] = []
    total_gcode_size = 0
    for print_record, slice_record in zip(print_records, slice_records, strict=True):
        (
            artifact,
            source_3mf,
            source_3mf_hash,
            source_3mf_size,
            verified_source_3mf,
            inspection,
        ) = _source_print_tile(
            print_root=print_root,
            print_manifest=print_manifest,
            print_record=print_record,
            snapshot_root=snapshot_root,
        )
        report, source_gcode, gcode_snapshot = _source_slice_tile(
            slice_root=slice_root,
            slice_manifest=slice_manifest,
            slice_record=slice_record,
            print_record=print_record,
            source_3mf=source_3mf,
            source_3mf_sha256=source_3mf_hash,
            source_inspection=inspection,
            expected_version=expected_version,
        )
        total_gcode_size += gcode_snapshot.size
        reports.append(report)
        tiles.append(
            _SourceTileEvidence(
                tile_id=print_record.tile_id,
                row=print_record.row,
                column=print_record.column,
                print_record=print_record,
                print_artifact=artifact,
                slice_record=slice_record,
                slice_report=report,
                source_3mf=source_3mf,
                source_3mf_sha256=source_3mf_hash,
                source_3mf_size=source_3mf_size,
                verified_source_3mf=verified_source_3mf,
                source_3mf_inspection=inspection,
                source_slice_gcode=source_gcode,
                source_slice_gcode_sha256=gcode_snapshot.sha256,
                source_slice_gcode_size=gcode_snapshot.size,
            )
        )

    time_sum = _optional_total([record.estimated_time_seconds for record in slice_records])
    filament_mm_sum = _optional_total([record.filament_used_mm for record in slice_records])
    filament_cm3_sum = _optional_total([record.filament_used_cm3 for record in slice_records])
    filament_g_sum = _optional_total([record.filament_used_g for record in slice_records])
    aggregate_required = bool(
        tiles
        and all(report.required_checks_passed for report in reports)
        and all(
            report.manufacturing_release_gate is not None
            and report.manufacturing_release_gate.get("release_gate_passed") is True
            for report in reports
        )
    )
    parameter_checks = bool(
        tiles
        and all(
            report.manufacturing_release_gate is not None
            and report.manufacturing_release_gate.get("parameter_checks_passed") is True
            for report in reports
        )
    )
    if (
        total_gcode_size != slice_manifest.total_gcode_size_bytes
        or slice_manifest.total_estimated_time_seconds
        != (None if time_sum is None else int(time_sum))
        or slice_manifest.total_filament_used_mm
        != (None if filament_mm_sum is None else float(filament_mm_sum))
        or slice_manifest.total_filament_used_cm3
        != (None if filament_cm3_sum is None else float(filament_cm3_sum))
        or slice_manifest.total_filament_used_g
        != (None if filament_g_sum is None else float(filament_g_sum))
        or slice_manifest.maximum_layer_count != max(record.layer_count for record in slice_records)
        or slice_manifest.official_p2s_release_gate_passed is not aggregate_required
        or slice_manifest.all_parameter_checks_passed is not parameter_checks
        or slice_manifest.all_exit_codes_zero is not True
        or slice_manifest.no_out_of_bed is not True
        or slice_manifest.no_empty_layers is not True
        or slice_manifest.no_floating_regions is not True
        or slice_manifest.no_support_material is not True
        or slice_manifest.required_checks_passed is not aggregate_required
        or not aggregate_required
    ):
        raise RuntimeError("source slice aggregate evidence does not recompute")
    return _SourceEvidence(
        print_manifest=print_manifest,
        print_manifest_sha256=print_snapshot.sha256,
        slice_manifest=slice_manifest,
        slice_manifest_sha256=slice_snapshot.sha256,
        expected_version=expected_version,
        executable_sha256=executable_hash,
        profiles=profiles,
        tiles=tuple(tiles),
    )


def _verify_source_evidence(
    *,
    print_root: Path,
    slice_root: Path,
    executable: Path,
    snapshot_root: Path | None = None,
) -> _SourceEvidence:
    if snapshot_root is not None:
        _ensure_directory_tree(snapshot_root, label="verified Bambu source snapshots")
        return _verify_source_evidence_at(
            print_root=print_root,
            slice_root=slice_root,
            executable=executable,
            snapshot_root=snapshot_root,
        )
    with tempfile.TemporaryDirectory(prefix="topoforge-bambu-source-") as temporary:
        return _verify_source_evidence_at(
            print_root=print_root,
            slice_root=slice_root,
            executable=executable,
            snapshot_root=Path(temporary),
        )


def copy_profiles(
    staging: Path,
    profiles: tuple[tuple[Any, Path, _BinarySnapshot], ...],
) -> tuple[list[Path], list[Path], list[dict[str, Any]]]:
    destination = staging / "profiles"
    _ensure_directory_tree(destination, label="Bambu profile snapshots")
    copied_settings: list[Path] = []
    copied_filaments: list[Path] = []
    records: list[dict[str, Any]] = []
    for record, source, snapshot in profiles:
        target = copied_settings if record.role == "settings" else copied_filaments
        name = f"{record.role}-{record.index:02d}-{source.name}"
        output = destination / name
        _write_atomic_bytes(output, snapshot.payload)
        target.append(output)
        records.append(
            {
                "role": record.role,
                "index": record.index,
                "path": f"profiles/{name}",
                "sha256": snapshot.sha256,
            }
        )
    return copied_settings, copied_filaments, records


def _diagnostic_snapshot(path: Path, *, label: str) -> _TextSnapshot:
    snapshot = _read_binary_snapshot(
        path,
        label=label,
        minimum_bytes=0,
        maximum_bytes=_MAX_JSON_BYTES,
    )
    return _TextSnapshot(
        path=snapshot.path,
        text=snapshot.payload.decode("utf-8", errors="replace"),
        size=snapshot.size,
        sha256=snapshot.sha256,
    )


def _diagnostic_text(path: Path, *, label: str) -> str:
    return _diagnostic_snapshot(path, label=label).text


def _historical_stage_root(output_directory: str, *, tile_id: str, mode: str) -> Path:
    output = Path(output_directory)
    if (
        not output.is_absolute()
        or output.name != mode
        or output.parent.name != tile_id
        or output.parent.parent.name != ".runtime"
    ):
        raise RuntimeError(f"Bambu {mode} output directory is not stage/tile bound")
    return output.parent.parent.parent


def _verify_execution_record(
    value: Any,
    *,
    mode: str,
    executable_path: str,
    source: _SourceTileEvidence,
    settings_names: tuple[str, ...],
    filament_names: tuple[str, ...],
) -> Path:
    if not isinstance(value, dict) or set(value) != {
        "command",
        "process_exit_code",
        "duration_seconds",
    }:
        raise RuntimeError(f"Bambu {mode} execution record has an invalid field set")
    command = value.get("command")
    duration = value.get("duration_seconds")
    if (
        not isinstance(command, list)
        or any(not isinstance(item, str) or not item for item in command)
        or value.get("process_exit_code") != 0
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or float(duration) < 0
    ):
        raise RuntimeError(f"Bambu {mode} execution record is invalid")
    if mode == "build":
        if (
            len(command) != 21
            or command[0:4] != [executable_path, "--debug", "2", "--load-settings"]
            or command[5] != "--load-filaments"
            or command[7:13]
            != [
                "--load-defaultfila",
                "--curr-bed-type",
                "Textured PEI Plate",
                "--normative-check",
                "--ensure-on-bed",
                "--arrange",
            ]
            or command[13:19]
            != [
                "1",
                "--slice",
                "0",
                "--export-3mf",
                f"{source.tile_id}.bambu-p2s.3mf",
                "--outputdir",
            ]
        ):
            raise RuntimeError("Bambu build command does not match the exact normative grammar")
        stage = _historical_stage_root(command[19], tile_id=source.tile_id, mode="build")
        expected_settings = tuple(str(stage / "profiles" / name) for name in settings_names)
        expected_filaments = tuple(str(stage / "profiles" / name) for name in filament_names)
        expected_source = (
            stage / ".runtime" / "verified-source" / f"{source.tile_id}.print-local.3mf"
        )
        if (
            tuple(command[4].split(";")) != expected_settings
            or tuple(command[6].split(";")) != expected_filaments
            or command[20] != str(expected_source)
        ):
            raise RuntimeError("Bambu build command changed its input profiles or model")
        return stage
    if mode == "reopen":
        if len(command) != 9 or command[0:7] != [
            executable_path,
            "--debug",
            "2",
            "--normative-check",
            "--slice",
            "0",
            "--outputdir",
        ]:
            raise RuntimeError("Bambu reopen command does not match the exact normative grammar")
        stage = _historical_stage_root(command[7], tile_id=source.tile_id, mode="reopen")
        expected_project = stage / "tiles" / source.tile_id / "model.bambu-p2s.3mf"
        if command[8] != str(expected_project):
            raise RuntimeError("Bambu reopen command changed its project input")
        return stage
    raise AssertionError(f"unknown Bambu execution mode: {mode}")


def _verify_project_profiles(
    root: Path,
    reported: Any,
    source_profiles: tuple[tuple[Any, Path, _BinarySnapshot], ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(reported, list) or len(reported) != len(source_profiles):
        raise RuntimeError("Bambu project profile set does not match source profiles")
    settings: list[str] = []
    filaments: list[str] = []
    expected_paths: set[Path] = set()
    for item, (source_record, _source_path, _source_snapshot) in zip(
        reported,
        source_profiles,
        strict=True,
    ):
        if (
            not isinstance(item, dict)
            or set(item) != {"role", "index", "path", "sha256"}
            or item.get("role") != source_record.role
            or item.get("index") != source_record.index
            or item.get("sha256") != source_record.sha256
            or not isinstance(item.get("path"), str)
        ):
            raise RuntimeError("Bambu project profile identity differs from source profiles")
        path = resolve_relative(root, item["path"])
        if path in expected_paths or sha256(path) != source_record.sha256:
            raise RuntimeError(f"Bambu project profile substitution detected: {path}")
        expected_paths.add(path)
        target = settings if source_record.role == "settings" else filaments
        target.append(path.name)
    profile_root = root / "profiles"
    if (
        not profile_root.is_dir()
        or profile_root.is_symlink()
        or {path.resolve() for path in profile_root.iterdir()} != expected_paths
    ):
        raise RuntimeError("Bambu project profile directory has missing or extra files")
    return tuple(settings), tuple(filaments)


def verify_output(
    root: Path,
    *,
    print_root: Path,
    slice_root: Path,
    executable: Path,
) -> dict[str, Any]:
    source = _verify_source_evidence(
        print_root=print_root,
        slice_root=slice_root,
        executable=executable,
    )
    manifest_path = root / "bambu-tile-project-manifest.json"
    manifest, _manifest_snapshot = _load_canonical_json(manifest_path)
    executable_path = manifest.get("bambu_studio_path")
    probe = manifest.get("bambu_studio_probe")
    claim_boundary = (
        "official Bambu Studio software export/reopen/reslice evidence; "
        "no physical print or vendor certification claim"
    )
    if (
        set(manifest) != _ROOT_MANIFEST_FIELDS
        or manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("layout_id") != source.print_manifest.layout_id
        or manifest.get("source_print_manifest_sha256") != source.print_manifest_sha256
        or manifest.get("source_slice_manifest_sha256") != source.slice_manifest_sha256
        or manifest.get("bambu_studio_sha256") != source.executable_sha256
        or manifest.get("bambu_studio_version") != source.expected_version
        or manifest.get("printer_profile_id") != "bambu-p2s-0.4"
        or manifest.get("tile_grid_shape") != list(source.print_manifest.tile_grid_shape)
        or manifest.get("tile_count") != len(source.tiles)
        or manifest.get("claim_boundary") != claim_boundary
        or not isinstance(executable_path, str)
        or not executable_path
        or not isinstance(probe, dict)
        or set(probe)
        != {
            "command",
            "process_exit_code",
            "duration_seconds",
            "version",
            "stdout_sha256",
            "stderr_sha256",
            "stdout_path",
            "stderr_path",
        }
    ):
        raise RuntimeError("Bambu tile project root identities changed")

    probe_stdout_relative = probe.get("stdout_path")
    probe_stderr_relative = probe.get("stderr_path")
    if not isinstance(probe_stdout_relative, str) or not isinstance(probe_stderr_relative, str):
        raise RuntimeError("Bambu Studio probe logs are not bound to the evidence root")
    probe_stdout = resolve_relative(root, probe_stdout_relative)
    probe_stderr = resolve_relative(root, probe_stderr_relative)
    probe_stdout_snapshot = _diagnostic_snapshot(
        probe_stdout,
        label="Bambu probe stdout",
    )
    probe_stderr_snapshot = _diagnostic_snapshot(
        probe_stderr,
        label="Bambu probe stderr",
    )
    probe_duration = probe.get("duration_seconds")
    probe_output_version = parse_bambu_studio_version(
        "\n".join(
            (
                probe_stdout_snapshot.text,
                probe_stderr_snapshot.text,
            )
        )
    )
    if (
        probe.get("command") != [executable_path, "--help"]
        or probe.get("process_exit_code") != 0
        or probe.get("version") != source.expected_version
        or isinstance(probe_duration, bool)
        or not isinstance(probe_duration, (int, float))
        or not math.isfinite(float(probe_duration))
        or float(probe_duration) < 0
        or probe_stdout_snapshot.sha256 != probe.get("stdout_sha256")
        or probe_stderr_snapshot.sha256 != probe.get("stderr_sha256")
        or probe_output_version != source.expected_version
    ):
        raise RuntimeError("Bambu Studio probe evidence changed")

    settings_names, filament_names = _verify_project_profiles(
        root,
        manifest.get("profile_files"),
        source.profiles,
    )
    records = manifest.get("tiles")
    if not isinstance(records, list):
        raise RuntimeError("Bambu tile project records are not a list")
    reported_identity = tuple(
        (
            record.get("tile_id"),
            record.get("row"),
            record.get("column"),
        )
        for record in records
        if isinstance(record, dict)
    )
    source_identity = tuple((tile.tile_id, tile.row, tile.column) for tile in source.tiles)
    if len(reported_identity) != len(records) or reported_identity != source_identity:
        raise RuntimeError("Bambu project has a missing, duplicate, extra, or reordered tile")
    tiles_root = root / "tiles"
    if (
        not tiles_root.is_dir()
        or tiles_root.is_symlink()
        or {path.name for path in tiles_root.iterdir()} != {tile.tile_id for tile in source.tiles}
        or any(not path.is_dir() or path.is_symlink() for path in tiles_root.iterdir())
    ):
        raise RuntimeError("Bambu project tile directory set differs from source tile set")

    recomputed_tiles: list[bool] = []
    expected_names = {
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
    for record, source_tile in zip(records, source.tiles, strict=True):
        if not isinstance(record, dict) or set(record) != _ROOT_TILE_FIELDS:
            raise RuntimeError("Bambu tile project record has an invalid field set")
        tile_id = source_tile.tile_id
        relative_dir = f"tiles/{tile_id}"
        expected_files = {role: f"{relative_dir}/{name}" for role, name in expected_names.items()}
        files = record.get("files")
        hashes = record.get("sha256")
        if (
            record.get("source_print_tile_manifest_sha256")
            != source_tile.print_record.tile_manifest_sha256
            or record.get("source_slice_report_sha256") != source_tile.slice_record.report_sha256
            or record.get("validation_path") != f"{relative_dir}/project_validation.json"
            or record.get("required_checks_passed") is not True
            or not isinstance(files, dict)
            or files != expected_files
            or not isinstance(hashes, dict)
            or set(hashes) != _PROJECT_FILE_ROLES
        ):
            raise RuntimeError(f"Bambu project tile source/role binding changed: {tile_id}")
        validation_path = resolve_relative(root, record["validation_path"])
        validation, validation_snapshot = _load_canonical_json(validation_path)
        if set(validation) != _VALIDATION_FIELDS or validation_snapshot.sha256 != record.get(
            "validation_sha256"
        ):
            raise RuntimeError(f"Bambu tile validation identity changed: {tile_id}")
        resolved_files: dict[str, Path] = {}
        for role in sorted(_PROJECT_FILE_ROLES):
            relative = files[role]
            path = resolve_relative(root, relative)
            resolved_files[role] = path
        tile_directory = root / relative_dir
        expected_inventory = {
            *expected_names.values(),
            "project_validation.json",
        }
        if (
            not tile_directory.is_dir()
            or tile_directory.is_symlink()
            or {path.name for path in tile_directory.iterdir()} != expected_inventory
            or any(not path.is_file() or path.is_symlink() for path in tile_directory.iterdir())
        ):
            raise RuntimeError(f"Bambu project tile file inventory changed: {tile_id}")

        project = resolved_files["bambu_project_3mf"]
        primary = resolved_files["primary_gcode"]
        reopened = resolved_files["reopen_gcode"]
        archive_inspection = _inspect_archive(project, primary)
        archive = archive_inspection.evidence
        reopened_snapshot = _read_gcode_snapshot(
            reopened,
            label="reopened Bambu G-code",
        )
        build_result_value, build_result_snapshot = _read_json_snapshot(
            resolved_files["build_result"]
        )
        reopen_result_value, reopen_result_snapshot = _read_json_snapshot(
            resolved_files["reopen_result"]
        )
        if not isinstance(build_result_value, dict) or not isinstance(reopen_result_value, dict):
            raise RuntimeError(f"Bambu result.json root is not an object: {tile_id}")
        build_result = build_result_value
        reopen_result = reopen_result_value
        build_stdout_snapshot = _diagnostic_snapshot(
            resolved_files["build_stdout"],
            label="Bambu build stdout",
        )
        build_stderr_snapshot = _diagnostic_snapshot(
            resolved_files["build_stderr"],
            label="Bambu build stderr",
        )
        reopen_stdout_snapshot = _diagnostic_snapshot(
            resolved_files["reopen_stdout"],
            label="Bambu reopen stdout",
        )
        reopen_stderr_snapshot = _diagnostic_snapshot(
            resolved_files["reopen_stderr"],
            label="Bambu reopen stderr",
        )
        actual_hashes = {
            "bambu_project_3mf": archive_inspection.project_sha256,
            "primary_gcode": archive_inspection.primary_gcode.sha256,
            "reopen_gcode": reopened_snapshot.sha256,
            "build_result": build_result_snapshot.sha256,
            "reopen_result": reopen_result_snapshot.sha256,
            "build_stdout": build_stdout_snapshot.sha256,
            "build_stderr": build_stderr_snapshot.sha256,
            "reopen_stdout": reopen_stdout_snapshot.sha256,
            "reopen_stderr": reopen_stderr_snapshot.sha256,
        }
        mismatched_roles = sorted(
            role for role, actual_hash in actual_hashes.items() if hashes.get(role) != actual_hash
        )
        if mismatched_roles:
            raise RuntimeError(
                f"Bambu tile project checksum mismatch: {tile_id}: {', '.join(mismatched_roles)}"
            )
        if not result_passed(build_result) or not result_passed(reopen_result):
            raise RuntimeError(f"Bambu result.json failed on reopen: {tile_id}")
        build_object = object_measurement(build_result)
        reopen_object = object_measurement(reopen_result)
        source_dimensions = tuple(source_tile.source_3mf_inspection.dimensions_mm)
        source_triangles = int(source_tile.source_3mf_inspection.triangle_count)
        dimensions_ok = bool(
            dimensions_match(build_object["dimensions_mm"], source_dimensions)
            and dimensions_match(reopen_object["dimensions_mm"], source_dimensions)
            and dimensions_match(archive["project_model_dimensions_mm"], source_dimensions)
        )
        triangles_ok = bool(
            build_object["triangle_count"] == source_triangles
            and reopen_object["triangle_count"] == source_triangles
            and archive["project_model_triangle_count"] == source_triangles
        )
        build_metrics, build_gate, build_version = _release_gate_snapshot(
            archive_inspection.primary_gcode,
            expected_version=source.expected_version,
            stdout=build_stdout_snapshot.text,
            stderr=build_stderr_snapshot.text,
        )
        reopen_metrics, reopen_gate, reopen_version = _release_gate_snapshot(
            reopened_snapshot,
            expected_version=source.expected_version,
            stdout=reopen_stdout_snapshot.text,
            stderr=reopen_stderr_snapshot.text,
        )
        build_stage = _verify_execution_record(
            validation.get("build_execution"),
            mode="build",
            executable_path=executable_path,
            source=source_tile,
            settings_names=settings_names,
            filament_names=filament_names,
        )
        reopen_stage = _verify_execution_record(
            validation.get("reopen_execution"),
            mode="reopen",
            executable_path=executable_path,
            source=source_tile,
            settings_names=settings_names,
            filament_names=filament_names,
        )
        if build_stage != reopen_stage:
            raise RuntimeError("Bambu build and reopen commands use different staging roots")
        required = bool(
            archive["archive_test_passed"]
            and archive["embedded_gcode_md5_verified"]
            and archive["embedded_gcode_matches_primary"]
            and build_gate.get("release_gate_passed") is True
            and reopen_gate.get("release_gate_passed") is True
            and build_version == reopen_version == source.expected_version
            and dimensions_ok
            and triangles_ok
        )
        if (
            validation.get("schema_version") != TILE_SCHEMA_VERSION
            or validation.get("tile_id") != tile_id
            or validation.get("source_print_local_3mf_path")
            != source_tile.print_record.files["print_local_3mf"]
            or validation.get("source_print_local_3mf_sha256") != source_tile.source_3mf_sha256
            or validation.get("source_slice_report_sha256")
            != source_tile.slice_record.report_sha256
            or validation.get("source_dimensions_mm") != list(source_dimensions)
            or validation.get("source_triangle_count") != source_triangles
            or validation.get("build_result") != build_result
            or validation.get("reopen_result") != reopen_result
            or validation.get("build_object") != build_object
            or validation.get("reopen_object") != reopen_object
            or validation.get("dimensions_match") is not dimensions_ok
            or validation.get("triangle_counts_match") is not triangles_ok
            or validation.get("project_archive") != archive
            or validation.get("primary_metrics") != build_metrics
            or validation.get("reopen_metrics") != reopen_metrics
            or validation.get("primary_release_gate") != build_gate
            or validation.get("reopen_release_gate") != reopen_gate
            or validation.get("expected_bambu_studio_version") != source.expected_version
            or validation.get("primary_bambu_studio_version") != build_version
            or validation.get("reopen_bambu_studio_version") != reopen_version
            or validation.get("bambu_studio_versions_match")
            is not (build_version == reopen_version == source.expected_version)
            or validation.get("external_profiles_loaded_on_reopen") is not False
            or validation.get("required_checks_passed") is not required
            or not required
        ):
            raise RuntimeError(f"Bambu project semantic validation changed: {tile_id}")
        recomputed_tiles.append(required)

    aggregate = bool(recomputed_tiles and all(recomputed_tiles))
    if (
        manifest.get("all_projects_reopened") is not aggregate
        or manifest.get("all_release_gates_passed") is not aggregate
        or manifest.get("required_checks_passed") is not aggregate
        or not aggregate
    ):
        raise RuntimeError("Bambu tile project aggregate gate changed")
    return {
        "status": "verified",
        "tile_count": len(source.tiles),
        "all_projects_reopened": True,
        "all_release_gates_passed": True,
        "required_checks_passed": True,
    }


def _build_evidence_locked(
    args: _EvidenceArgs,
    executable_identity: _ExecutableIdentity,
) -> dict[str, Any]:
    print_root = _absolute_path(args.print_set.expanduser())
    slice_root = _absolute_path(args.slice_set.expanduser())
    executable = executable_identity.path
    output = _absolute_path(args.output.expanduser())
    if _path_exists_no_follow(output):
        raise RuntimeError(f"Bambu tile project destination already exists: {output}")
    _ensure_directory_tree(output.parent, label="Bambu tile project destination parent")
    staging = _make_private_staging_directory(
        output.parent,
        prefix=f".{output.name}.topoforge-stage-",
    )
    runtime_root = staging / ".runtime"
    try:
        _require_executable_identity(executable_identity, phase="before source verification")
        source = _verify_source_evidence(
            print_root=print_root,
            slice_root=slice_root,
            executable=executable,
            snapshot_root=runtime_root / "verified-source",
        )
        _require_executable_identity(executable_identity, phase="after source verification")
        if source.executable_sha256 != executable_identity.sha256:
            raise RuntimeError("source evidence does not bind the locked Bambu Studio executable")
        print_manifest = source.print_manifest
        expected_version = source.expected_version
        probe_record = probe_bambu_studio(
            executable,
            runtime=runtime_root / "probe",
            timeout_seconds=args.timeout,
            evidence_root=staging,
            _identity=executable_identity,
        )
        if probe_record["version"] != expected_version:
            raise RuntimeError(
                f"Bambu Studio probe version {probe_record['version']!r} does not match "
                f"the frozen source-slice version {expected_version!r}"
            )
        settings, filaments, profile_records = copy_profiles(staging, source.profiles)
        records: list[dict[str, Any]] = []
        for source_tile in source.tiles:
            tile_id = source_tile.tile_id
            source_record = source_tile.print_record
            source_slice = source_tile.slice_record
            input_relative = source_record.files["print_local_3mf"]
            input_path = source_tile.verified_source_3mf
            input_inspection = source_tile.source_3mf_inspection
            tile_dir = staging / "tiles" / tile_id
            _ensure_directory_tree(tile_dir, label="Bambu tile evidence")
            build_runtime = runtime_root / tile_id / "build"
            reopen_runtime = runtime_root / tile_id / "reopen"
            _ensure_directory_tree(build_runtime, label="Bambu build runtime")
            _ensure_directory_tree(reopen_runtime, label="Bambu reopen runtime")
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
                build_command,
                runtime=build_runtime / "environment",
                timeout_seconds=args.timeout,
                executable_identity=executable_identity,
            )
            build_result_path = build_runtime / "result.json"
            build_project_path = build_runtime / project_name
            build_gcode_path = build_runtime / "plate_1.gcode"
            build_result = _snapshot_generated_json(
                build_result_path,
                tile_dir / "build_result.json",
                label="Bambu build result",
            )
            if not result_passed(build_result):
                raise RuntimeError(f"Bambu project build failed for {tile_id}")
            project_path = tile_dir / "model.bambu-p2s.3mf"
            primary_gcode = tile_dir / "primary.gcode"
            with (
                _open_pinned_regular_file(
                    build_project_path,
                    label="generated Bambu project archive",
                ) as build_project_file,
                _open_pinned_regular_file(
                    build_gcode_path,
                    label="generated primary Bambu G-code",
                ) as build_gcode_file,
            ):
                archive_inspection = _inspect_archive_pinned(
                    build_project_file,
                    build_gcode_file,
                )
                copied_project_hash, _project_size = _copy_pinned_regular_file_snapshot(
                    build_project_file,
                    project_path,
                    label="generated Bambu project archive",
                    maximum_bytes=_PROJECT_MAX_ARCHIVE_BYTES,
                )
                copied_primary_hash, _primary_size = _copy_pinned_regular_file_snapshot(
                    build_gcode_file,
                    primary_gcode,
                    label="generated primary Bambu G-code",
                    maximum_bytes=_PROJECT_MAX_GCODE_TEXT_BYTES,
                )
            if (
                copied_project_hash != archive_inspection.project_sha256
                or copied_primary_hash != archive_inspection.primary_gcode.sha256
            ):
                raise RuntimeError("Bambu build outputs changed between inspection and snapshot")
            archive = archive_inspection.evidence
            _write_text_from_memory(tile_dir / "build.stdout.log", build_execution.stdout)
            _write_text_from_memory(tile_dir / "build.stderr.log", build_execution.stderr)
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
                reopen_command,
                runtime=reopen_runtime / "environment",
                timeout_seconds=args.timeout,
                executable_identity=executable_identity,
            )
            reopen_result_path = reopen_runtime / "result.json"
            reopen_gcode_path = reopen_runtime / "plate_1.gcode"
            reopen_result = _snapshot_generated_json(
                reopen_result_path,
                tile_dir / "reopen_result.json",
                label="Bambu reopen result",
            )
            if not result_passed(reopen_result):
                raise RuntimeError(f"Bambu project reopen failed for {tile_id}")
            reopen_gcode = tile_dir / "reopen.gcode"
            with _open_pinned_regular_file(
                reopen_gcode_path,
                label="generated reopened Bambu G-code",
            ) as reopen_gcode_file:
                reopened_snapshot = _read_gcode_from_pinned(
                    reopen_gcode_file,
                    label="generated reopened Bambu G-code",
                )
                copied_reopen_hash, _reopen_size = _copy_pinned_regular_file_snapshot(
                    reopen_gcode_file,
                    reopen_gcode,
                    label="generated reopened Bambu G-code",
                    maximum_bytes=_PROJECT_MAX_GCODE_TEXT_BYTES,
                )
            if copied_reopen_hash != reopened_snapshot.sha256:
                raise RuntimeError("Bambu reopen G-code changed between inspection and snapshot")
            _write_text_from_memory(tile_dir / "reopen.stdout.log", reopen_execution.stdout)
            _write_text_from_memory(tile_dir / "reopen.stderr.log", reopen_execution.stderr)
            primary_metrics, primary_gate, primary_version = _release_gate_snapshot(
                archive_inspection.primary_gcode,
                expected_version=expected_version,
                stdout=build_execution.stdout,
                stderr=build_execution.stderr,
            )
            reopen_metrics, reopen_gate, reopen_version = _release_gate_snapshot(
                reopened_snapshot,
                expected_version=expected_version,
                stdout=reopen_execution.stdout,
                stderr=reopen_execution.stderr,
            )
            build_object = object_measurement(build_result)
            reopen_object = object_measurement(reopen_result)
            dimensions_ok = bool(
                dimensions_match(build_object["dimensions_mm"], input_inspection.dimensions_mm)
                and dimensions_match(reopen_object["dimensions_mm"], input_inspection.dimensions_mm)
                and dimensions_match(
                    archive["project_model_dimensions_mm"],
                    input_inspection.dimensions_mm,
                )
            )
            triangles_ok = bool(
                build_object["triangle_count"] == input_inspection.triangle_count
                and reopen_object["triangle_count"] == input_inspection.triangle_count
                and archive["project_model_triangle_count"] == input_inspection.triangle_count
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
                "source_print_local_3mf_sha256": source_tile.source_3mf_sha256,
                "source_slice_report_sha256": source_slice.report_sha256,
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
                    "row": source_record.row,
                    "column": source_record.column,
                    "source_print_tile_manifest_sha256": source_record.tile_manifest_sha256,
                    "source_slice_report_sha256": source_slice.report_sha256,
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
            "layout_id": print_manifest.layout_id,
            "source_print_manifest_sha256": source.print_manifest_sha256,
            "source_slice_manifest_sha256": source.slice_manifest_sha256,
            "bambu_studio_path": str(executable),
            "bambu_studio_sha256": source.executable_sha256,
            "bambu_studio_version": expected_version,
            "bambu_studio_probe": probe_record,
            "printer_profile_id": "bambu-p2s-0.4",
            "profile_files": profile_records,
            "tile_grid_shape": list(print_manifest.tile_grid_shape),
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
        _require_executable_identity(executable_identity, phase="before final verification")
        verification = verify_output(
            staging, print_root=print_root, slice_root=slice_root, executable=executable
        )
        _require_executable_identity(executable_identity, phase="before atomic publication")
        _publish_directory_no_replace(staging, output)
        return {
            "status": "published",
            "output": str(output),
            "manifest": str(output / "bambu-tile-project-manifest.json"),
            "verification": verification,
        }
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _build_evidence(args: _EvidenceArgs) -> dict[str, Any]:
    executable = _absolute_path(args.bambu_studio.expanduser())
    with _lock_executable(executable) as executable_identity:
        return _build_evidence_locked(args, executable_identity)


def verify_bambu_project_evidence(
    output_dir: Path,
    *,
    print_set_dir: Path,
    slice_set_dir: Path,
    bambu_studio: Path,
) -> dict[str, Any]:
    """Strictly reopen project archives, G-code, source bindings, and release gates."""
    root = _absolute_path(output_dir.expanduser())
    print_root = _absolute_path(print_set_dir.expanduser())
    slice_root = _absolute_path(slice_set_dir.expanduser())
    executable = _absolute_path(bambu_studio.expanduser())
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
