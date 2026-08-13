#!/usr/bin/env python3
"""Generate runtime evidence for a non-destructive TopoForge release rollback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import unicodedata
import zipfile
from collections.abc import Iterator
from configparser import ConfigParser
from configparser import Error as ConfigParserError
from contextlib import contextmanager
from datetime import datetime
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from topoforge.util import preflight_zip_central_directory

if __package__:
    from scripts.windows_acceptance import EvidencePublicationError, write_canonical_json
else:
    from windows_acceptance import EvidencePublicationError, write_canonical_json

SCHEMA_VERSION = "topoforge-rollback-runtime-verification-v4"
VERSION_PATTERN = re.compile(r"0[.]11[.]([0-9]+)")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
CHECKSUM_LINE_PATTERN = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)")
GITHUB_PUBLISHED_AT_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
WHEEL_MAX_BYTES = 64 * 1024 * 1024
WHEEL_MAX_MEMBERS = 20_000
WHEEL_MAX_MEMBER_BYTES = 32 * 1024 * 1024
WHEEL_MAX_EXPANDED_BYTES = 256 * 1024 * 1024
WHEEL_MAX_COMPRESSION_RATIO = 1000.0
WHEEL_MAX_METADATA_BYTES = 1024 * 1024
WHEEL_MAX_MEMBER_NAME_BYTES = 1024
WHEEL_MAX_CENTRAL_DIRECTORY_BYTES = 16 * 1024 * 1024


class _ExactEntryPointConfigParser(ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def rollback_previous_version(version: str) -> str:
    """Return the only release version accepted as the rollback target."""
    match = VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise ValueError(f"rollback evidence does not support version {version}")
    patch = int(match.group(1))
    return "0.10.3" if patch == 0 else f"0.11.{patch - 1}"


def canonical_rollback_script(version: str, previous_version: str) -> bytes:
    """Return the exact non-destructive source rollback script for a release."""
    if rollback_previous_version(version) != previous_version:
        raise ValueError("previous_version is not the canonical rollback target")
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        'if [[ "${1:-}" != "--confirm-rollback" ]]; then\n'
        '  echo "usage: $0 --confirm-rollback" >&2\n'
        "  exit 2\n"
        "fi\n\n"
        f'release_tag="v{version}"\n'
        f'previous_tag="v{previous_version}"\n'
        'release_commit="$(git rev-parse "$release_tag^{commit}")"\n'
        'current_commit="$(git rev-parse HEAD)"\n'
        'if [[ "$current_commit" != "$release_commit" ]]; then\n'
        '  echo "rollback requires HEAD to be exactly $release_tag" >&2\n'
        "  exit 2\n"
        "fi\n\n"
        'git rev-parse --verify "$previous_tag^{commit}" >/dev/null\n'
        f'rollback_dir="${{TOPOFORGE_ROLLBACK_DIR:-../TopoForge-{previous_version}}}"\n'
        'if [[ -e "$rollback_dir" ]]; then\n'
        '  echo "rollback destination already exists: $rollback_dir" >&2\n'
        "  exit 2\n"
        "fi\n\n"
        'git worktree add --detach "$rollback_dir" "$previous_tag"\n'
        'test "$(git -C "$rollback_dir" rev-parse HEAD)" = '
        '"$(git rev-parse "$previous_tag^{commit}")"\n\n'
        "cat <<EOF\n"
        f"TopoForge {previous_version} source rollback worktree created at $rollback_dir.\n"
        f"The {version} checkout and retained DEMs, caches, outputs, backups, and Web "
        "workspaces were not changed.\n"
        "EOF\n"
    ).encode()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _absolute_lexical_path(path: Path) -> Path:
    """Return an absolute path without following any filesystem object."""
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return Path(os.path.abspath(os.fspath(candidate)))


def _link_like(information: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(information.st_mode) or bool(
        getattr(information, "st_file_attributes", 0) & reparse_flag
    )


def _object_identity(information: os.stat_result) -> tuple[int, int, int]:
    return (
        information.st_dev,
        information.st_ino,
        stat.S_IFMT(information.st_mode),
    )


def _file_identity(information: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        information.st_dev,
        information.st_ino,
        stat.S_IFMT(information.st_mode),
        information.st_nlink,
        information.st_size,
        information.st_mtime_ns,
        information.st_ctime_ns,
    )


def _checked_path_chain(path: Path, *, label: str) -> tuple[os.stat_result, ...]:
    """Inspect every lexical component without accepting links or reparse points."""
    absolute = _absolute_lexical_path(path)
    parts = absolute.parts
    current = Path(absolute.anchor)
    information: list[os.stat_result] = []
    for index, part in enumerate(parts):
        if index:
            current /= part
        try:
            current_information = current.lstat()
        except OSError as exc:
            raise FileNotFoundError(f"{label} path component is unavailable: {current}") from exc
        if _link_like(current_information):
            raise ValueError(f"{label} path contains a link or reparse point: {current}")
        if index < len(parts) - 1 and not stat.S_ISDIR(current_information.st_mode):
            raise ValueError(f"{label} path component is not a directory: {current}")
        information.append(current_information)
    return tuple(information)


def _same_path_objects(
    before: tuple[os.stat_result, ...],
    after: tuple[os.stat_result, ...],
) -> bool:
    return len(before) == len(after) and all(
        _object_identity(left) == _object_identity(right)
        for left, right in zip(before, after, strict=True)
    )


def _unlink_if_same_object(
    path: Path,
    expected_identity: tuple[int, int, int] | None,
) -> OSError | None:
    """Remove a failed private artifact only while its name still identifies it."""
    if expected_identity is None:
        return None
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return exc
    if (
        _link_like(observed)
        or not stat.S_ISREG(observed.st_mode)
        or _object_identity(observed) != expected_identity
    ):
        return None
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return exc
    return None


def _create_plain_directory_tree(
    path: Path,
    *,
    label: str,
    require_new: bool = False,
) -> tuple[os.stat_result, ...]:
    """Create a missing lexical directory tree while retaining each parent identity."""
    absolute = _absolute_lexical_path(path)
    missing: list[Path] = []
    cursor = absolute
    while True:
        try:
            cursor.lstat()
        except FileNotFoundError as exc:
            if cursor == Path(cursor.anchor):
                raise FileNotFoundError(
                    f"{label} filesystem anchor is unavailable: {cursor}"
                ) from exc
            missing.append(cursor)
            cursor = cursor.parent
            continue
        break
    existing_chain = _checked_path_chain(cursor, label=label)
    if not stat.S_ISDIR(existing_chain[-1].st_mode):
        raise ValueError(f"{label} existing ancestor is not a directory: {cursor}")
    if require_new and not missing:
        raise FileExistsError(f"{label} already exists: {absolute}")
    for directory in reversed(missing):
        parent_chain = _checked_path_chain(directory.parent, label=label)
        directory.mkdir()
        if not _same_path_objects(
            parent_chain,
            _checked_path_chain(directory.parent, label=label),
        ):
            raise ValueError(f"{label} parent changed while its directory was created")
        created = directory.lstat()
        if _link_like(created) or not stat.S_ISDIR(created.st_mode):
            raise ValueError(f"{label} created path is not a real directory: {directory}")
    return _checked_path_chain(absolute, label=label)


@contextmanager
def _open_pinned_regular_file(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    """Open one bounded single-link regular file and retain its exact object."""
    path = _absolute_lexical_path(path)
    chain_before = _checked_path_chain(path, label=label)
    before = chain_before[-1]
    if (
        _link_like(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size <= 0
        or before.st_size > maximum_bytes
    ):
        raise ValueError(f"{label} must be a bounded real single-link file: {path}")
    descriptor = -1
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} cannot be opened without following links: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if _file_identity(opened) != _file_identity(before):
            raise ValueError(f"{label} changed while it was opened: {path}")
        chain_opened = _checked_path_chain(path, label=label)
        if not _same_path_objects(chain_before, chain_opened):
            raise ValueError(f"{label} path changed while it was opened: {path}")
        if _file_identity(chain_opened[-1]) != _file_identity(opened):
            raise ValueError(f"{label} path no longer names its opened file: {path}")
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "rb") as handle:
        descriptor = -1
        try:
            yield handle, opened
        except BaseException as exc:
            try:
                if _file_identity(os.fstat(handle.fileno())) != _file_identity(opened):
                    raise ValueError(f"{label} changed while it was being read: {path}")
                chain_after_error = _checked_path_chain(path, label=label)
                if not _same_path_objects(chain_before, chain_after_error) or _file_identity(
                    chain_after_error[-1]
                ) != _file_identity(opened):
                    raise ValueError(f"{label} path changed while it was being read: {path}")
            except BaseException as integrity_error:
                exc.add_note(f"additional {label} integrity failure: {integrity_error}")
            raise
        if _file_identity(os.fstat(handle.fileno())) != _file_identity(opened):
            raise ValueError(f"{label} changed while it was being read: {path}")
        chain_after = _checked_path_chain(path, label=label)
        if not _same_path_objects(chain_before, chain_after) or _file_identity(
            chain_after[-1]
        ) != _file_identity(opened):
            raise ValueError(f"{label} path changed while it was being read: {path}")


def _read_pinned_bytes(path: Path, *, label: str, maximum_bytes: int) -> bytes:
    with _open_pinned_regular_file(
        path,
        label=label,
        maximum_bytes=maximum_bytes,
    ) as (handle, information):
        payload = handle.read(maximum_bytes + 1)
        if len(payload) != information.st_size:
            raise ValueError(f"{label} size changed while it was read: {path}")
        return payload


def _sha256_stream(stream: BinaryIO) -> str:
    stream.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _bounded_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    maximum_bytes: int,
) -> bytes:
    if info.file_size > maximum_bytes:
        raise ValueError(f"release wheel metadata exceeds its byte bound: {info.filename}")
    with archive.open(info) as source:
        payload = source.read(maximum_bytes + 1)
        trailing = source.read(1)
    if len(payload) != info.file_size or trailing:
        raise ValueError(f"release wheel member size changed while reading: {info.filename}")
    return payload


def _validated_wheel_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if not infos or len(infos) > WHEEL_MAX_MEMBERS:
        raise ValueError("release wheel member count is outside the accepted bound")
    members: dict[str, zipfile.ZipInfo] = {}
    aliases: set[str] = set()
    file_paths: set[tuple[str, ...]] = set()
    directory_paths: set[tuple[str, ...]] = set()
    expanded_bytes = 0
    for info in infos:
        raw_name = info.filename
        if (
            not raw_name
            or info.orig_filename != raw_name
            or len(raw_name.encode("utf-8")) > WHEEL_MAX_MEMBER_NAME_BYTES
        ):
            raise ValueError("release wheel contains an unsafe raw member name")
        member = PurePosixPath(raw_name[:-1] if info.is_dir() else raw_name)
        canonical_name = member.as_posix() + ("/" if info.is_dir() else "")
        mode = info.external_attr >> 16
        if (
            "\\" in raw_name
            or raw_name != canonical_name
            or member.is_absolute()
            or "." in member.parts
            or ".." in member.parts
            or info.flag_bits & 0x1
            or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            or (mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR})
            or info.header_offset < 0
            or info.file_size < 0
            or info.compress_size < 0
            or info.file_size > WHEEL_MAX_MEMBER_BYTES
        ):
            raise ValueError(f"release wheel contains an unsafe member: {raw_name!r}")
        alias_parts = tuple(unicodedata.normalize("NFC", part).casefold() for part in member.parts)
        alias = "/".join(alias_parts)
        if alias in aliases or alias_parts in file_paths or alias_parts in directory_paths:
            raise ValueError(f"release wheel contains duplicate or aliased members: {raw_name}")
        for index in range(1, len(alias_parts)):
            prefix = alias_parts[:index]
            if prefix in file_paths:
                raise ValueError(f"release wheel contains a file/directory conflict: {raw_name}")
            directory_paths.add(prefix)
        aliases.add(alias)
        if info.is_dir():
            directory_paths.add(alias_parts)
        else:
            file_paths.add(alias_parts)
            expanded_bytes += info.file_size
            if expanded_bytes > WHEEL_MAX_EXPANDED_BYTES:
                raise ValueError("release wheel exceeds its total expansion bound")
            if info.file_size:
                if info.compress_size <= 0:
                    raise ValueError(f"release wheel member has invalid compression: {raw_name}")
                if info.file_size / info.compress_size > WHEEL_MAX_COMPRESSION_RATIO:
                    raise ValueError(
                        f"release wheel member exceeds its compression ratio bound: {raw_name}"
                    )
        members[raw_name] = info
    return members


def _expected_wheel_filename(version: str) -> str:
    return f"topoforge-{version}-py3-none-any.whl"


def _positive_github_id(value: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive GitHub numeric ID")
    return value


def _github_published_at(value: str) -> str:
    if not isinstance(value, str) or GITHUB_PUBLISHED_AT_PATTERN.fullmatch(value) is None:
        raise ValueError("previous release published_at must be a canonical GitHub UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(
            "previous release published_at must be a valid GitHub UTC timestamp"
        ) from exc
    return value


def _validate_release_wheel(
    path: Path,
    *,
    version: str,
    expected_sha256: str,
) -> dict[str, Any]:
    if SHA256_PATTERN.fullmatch(expected_sha256) is None:
        raise ValueError("expected release wheel SHA-256 must be lowercase hexadecimal")
    expected_filename = _expected_wheel_filename(version)
    if path.name != expected_filename:
        raise ValueError(f"release wheel filename must be {expected_filename}")
    dist_info = f"topoforge-{version}.dist-info"
    metadata_name = f"{dist_info}/METADATA"
    entry_points_name = f"{dist_info}/entry_points.txt"
    required = {
        "topoforge/__init__.py",
        "topoforge/cli/app.py",
        metadata_name,
        entry_points_name,
    }
    with _open_pinned_regular_file(
        path,
        label="release wheel",
        maximum_bytes=WHEEL_MAX_BYTES,
    ) as (handle, information):
        observed_sha256 = _sha256_stream(handle)
        if observed_sha256 != expected_sha256:
            raise ValueError("release wheel SHA-256 differs from its tracked expected hash")
        central_directory = preflight_zip_central_directory(
            handle,
            maximum_entries=WHEEL_MAX_MEMBERS,
            maximum_central_directory_bytes=WHEEL_MAX_CENTRAL_DIRECTORY_BYTES,
            maximum_comment_bytes=0,
            label="release wheel",
        )
        if central_directory.file_size != information.st_size:
            raise ValueError("release wheel size changed before ZIP parsing")
        handle.seek(0)
        with zipfile.ZipFile(handle) as archive:
            members = _validated_wheel_members(archive)
            missing = sorted(required - members.keys())
            if missing:
                raise ValueError(f"release wheel is missing required members: {missing}")
            try:
                metadata = Parser().parsestr(
                    _bounded_zip_member(
                        archive,
                        members[metadata_name],
                        maximum_bytes=WHEEL_MAX_METADATA_BYTES,
                    ).decode("utf-8")
                )
                entry_points = _bounded_zip_member(
                    archive,
                    members[entry_points_name],
                    maximum_bytes=WHEEL_MAX_METADATA_BYTES,
                ).decode("utf-8")
            except (UnicodeDecodeError, KeyError) as exc:
                raise ValueError("release wheel metadata is unreadable") from exc
        size = information.st_size
    if metadata.get("Name") != "topoforge" or metadata.get("Version") != version:
        raise ValueError("release wheel METADATA name/version differs from the expected release")
    entry_point_config = _ExactEntryPointConfigParser(interpolation=None, strict=True)
    try:
        entry_point_config.read_string(entry_points)
    except ConfigParserError as exc:
        raise ValueError("release wheel console entry points are not valid INI") from exc
    if (
        entry_point_config.defaults()
        or entry_point_config.sections() != ["console_scripts"]
        or dict(entry_point_config.items("console_scripts", raw=True))
        != {"topoforge": "topoforge.cli.app:app"}
    ):
        raise ValueError("release wheel console entry point is missing or incorrect")
    return {
        "filename": expected_filename,
        "sha256": observed_sha256,
        "bytes": size,
        "metadata_name": metadata.get("Name"),
        "metadata_version": metadata.get("Version"),
        "console_entry_point": "topoforge = topoforge.cli.app:app",
        "required_checks_passed": True,
    }


def _snapshot_release_wheel(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
) -> None:
    """Copy one expected wheel from a pinned handle into an exclusive private path."""
    destination = _absolute_lexical_path(destination)
    try:
        destination.parent.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(
            f"private wheel snapshot directory already exists: {destination.parent}"
        )
    _create_plain_directory_tree(
        destination.parent,
        label="private wheel snapshot directory",
        require_new=True,
    )
    target_identity: tuple[int, int, int] | None = None
    descriptor = -1
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        target_opened = os.fstat(descriptor)
        if (
            _link_like(target_opened)
            or not stat.S_ISREG(target_opened.st_mode)
            or target_opened.st_nlink != 1
            or target_opened.st_size != 0
        ):
            raise ValueError("private wheel snapshot is not a new single-link regular file")
        target_identity = _object_identity(target_opened)
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            with _open_pinned_regular_file(
                source,
                label="release wheel",
                maximum_bytes=WHEEL_MAX_BYTES,
            ) as (source_handle, source_information):
                source_handle.seek(0)
                digest = hashlib.sha256()
                copied = 0
                for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                    copied += len(chunk)
                    if copied > WHEEL_MAX_BYTES:
                        raise ValueError("release wheel expanded beyond its input byte bound")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
                target_written = os.fstat(target.fileno())
                if (
                    _object_identity(target_written) != target_identity
                    or target_written.st_nlink != 1
                    or target_written.st_size != copied
                ):
                    raise ValueError("private wheel snapshot changed while it was written")
        if copied != source_information.st_size or digest.hexdigest() != expected_sha256:
            raise ValueError("release wheel changed while creating its private install snapshot")
        destination_information = destination.lstat()
        if (
            _link_like(destination_information)
            or not stat.S_ISREG(destination_information.st_mode)
            or destination_information.st_nlink != 1
            or _object_identity(destination_information) != target_identity
            or destination_information.st_size != copied
        ):
            raise ValueError("private wheel snapshot path changed after it was written")
        _checked_path_chain(destination, label="private wheel snapshot")
    except BaseException as exc:
        cleanup_error = _unlink_if_same_object(destination, target_identity)
        if cleanup_error is not None:
            exc.add_note(f"private wheel snapshot cleanup also failed: {cleanup_error}")
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_previous_checksums(
    path: Path,
    *,
    expected_sha256: str,
    wheel_filename: str,
    wheel_sha256: str,
) -> dict[str, Any]:
    if path.name != "SHA256SUMS":
        raise FileNotFoundError("previous release SHA256SUMS must use its canonical filename")
    if SHA256_PATTERN.fullmatch(expected_sha256) is None:
        raise ValueError("expected SHA256SUMS hash must be lowercase hexadecimal")
    payload = _read_pinned_bytes(
        path,
        label="previous release SHA256SUMS",
        maximum_bytes=1024 * 1024,
    )
    if _sha256_bytes(payload) != expected_sha256:
        raise ValueError("previous release SHA256SUMS differs from its tracked expected hash")
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("previous release SHA256SUMS is not ASCII") from exc
    records: dict[str, str] = {}
    for line in lines:
        match = CHECKSUM_LINE_PATTERN.fullmatch(line)
        if match is None or match.group(2) in records:
            raise ValueError("previous release SHA256SUMS is not canonical")
        records[match.group(2)] = match.group(1)
    if records.get(wheel_filename) != wheel_sha256:
        raise ValueError("previous release wheel is not bound by SHA256SUMS")
    return {
        "filename": "SHA256SUMS",
        "sha256": expected_sha256,
        "wheel_entry": f"{wheel_sha256}  {wheel_filename}",
        "required_checks_passed": True,
    }


def _run(
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
    standard_input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        input=standard_input,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        command = " ".join(arguments[:3])
        raise RuntimeError(f"rollback evidence command failed ({command})")
    return completed


def _git(repository: Path, *arguments: str) -> str:
    return _run(["git", *arguments], cwd=repository).stdout.strip()


def _inventory(root: Path) -> dict[str, Any]:
    root_chain = _checked_path_chain(root, label="retained evidence root")
    if not stat.S_ISDIR(root_chain[-1].st_mode):
        raise FileNotFoundError(f"retained evidence root is not a directory: {root}")
    records: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"retained evidence contains a symlink: {path}")
        if not path.is_file():
            continue
        size = path.stat().st_size
        total_bytes += size
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256_file(path),
                "size_bytes": size,
            }
        )
    if not records:
        raise ValueError("retained evidence inventory must not be empty")
    manifest = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return {
        "file_count": len(records),
        "total_bytes": total_bytes,
        "manifest_sha256": _sha256_bytes(manifest),
    }


def _environment_python(environment_root: Path) -> Path:
    windows = environment_root / "Scripts" / "python.exe"
    return windows if windows.is_file() else environment_root / "bin" / "python"


def _environment_console_launcher(environment_root: Path) -> Path:
    candidates = (
        (
            environment_root / "Scripts" / "topoforge.exe",
            environment_root / "Scripts" / "topoforge.cmd",
        )
        if os.name == "nt"
        else (environment_root / "bin" / "topoforge",)
    )
    launchers = [path for path in candidates if path.is_file() and not path.is_symlink()]
    if len(launchers) != 1:
        raise ValueError(
            "installed environment must contain exactly one regular TopoForge console launcher"
        )
    return launchers[0]


def _console_launcher_command(launcher: Path, *arguments: str) -> list[str]:
    if launcher.suffix.casefold() in {".cmd", ".bat"}:
        return ["cmd.exe", "/d", "/s", "/c", str(launcher), *arguments]
    return [str(launcher), *arguments]


def _build_and_verify_install(
    source_root: Path,
    *,
    wheel: Path,
    expected_wheel_sha256: str,
    version: str,
    work_root: Path,
    label: str,
) -> tuple[dict[str, Any], Path]:
    install_wheel = _absolute_lexical_path(work_root / f"{label}-release-artifact" / wheel.name)
    _snapshot_release_wheel(
        wheel,
        install_wheel,
        expected_sha256=expected_wheel_sha256,
    )
    wheel_record = _validate_release_wheel(
        install_wheel,
        version=version,
        expected_sha256=expected_wheel_sha256,
    )
    lock_file = source_root / "uv.lock"
    if lock_file.is_symlink() or not lock_file.is_file():
        raise FileNotFoundError(f"{label} source checkout does not contain a regular uv.lock")
    locked_requirements = work_root / f"{label}-locked-runtime-requirements.txt"
    _run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-annotate",
            "--no-header",
            "--no-sources",
            "--quiet",
            "--output-file",
            str(locked_requirements),
        ],
        cwd=source_root,
    )
    if locked_requirements.is_symlink() or not locked_requirements.is_file():
        raise FileNotFoundError(f"{label} locked dependency export was not created")
    locked_requirements_bytes = locked_requirements.read_bytes()
    if b"--hash=sha256:" not in locked_requirements_bytes:
        raise ValueError(f"{label} locked dependency export does not contain SHA-256 hashes")
    environment_root = work_root / f"{label}-environment"
    _run(["uv", "venv", "--python", "3.12", str(environment_root)], cwd=source_root)
    python = _environment_python(environment_root)
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--requirements",
            str(locked_requirements),
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--no-deps",
            "--strict",
            "--quiet",
        ],
        cwd=source_root,
    )
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--requirements",
            "-",
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--no-deps",
            "--no-index",
            "--strict",
            "--quiet",
        ],
        cwd=source_root,
        standard_input=(
            f"topoforge @ {install_wheel.as_uri()} --hash=sha256:{expected_wheel_sha256}\n"
        ),
    )
    if (
        _validate_release_wheel(
            install_wheel,
            version=version,
            expected_sha256=expected_wheel_sha256,
        )
        != wheel_record
    ):
        raise ValueError(f"{label} release wheel changed during installation")
    launcher = _environment_console_launcher(environment_root)
    doctor = _run(_console_launcher_command(launcher, "doctor"), cwd=source_root)
    try:
        doctor_report = json.loads(doctor.stdout)
    except ValueError as exc:
        raise ValueError(f"{label} installed doctor output is not JSON") from exc
    if not isinstance(doctor_report, dict) or doctor_report.get("topoforge") != version:
        raise ValueError(f"{label} installed environment reported the wrong version")
    return (
        {
            "version": version,
            "wheel_filename": wheel_record["filename"],
            "wheel_sha256": wheel_record["sha256"],
            "launcher_relative_path": launcher.relative_to(environment_root).as_posix(),
            "launcher_sha256": _sha256_file(launcher),
            "doctor_output_sha256": _sha256_bytes(doctor.stdout.encode("utf-8")),
            "doctor_exit_code": doctor.returncode,
            "dependency_install_mode": ("uv-lock-hashed-dependencies-plus-project-wheel-no-deps"),
            "uv_lock_sha256": _sha256_file(lock_file),
            "locked_requirements_sha256": _sha256_bytes(locked_requirements_bytes),
            "required_checks_passed": True,
        },
        launcher,
    )


def _doctor_through_active_entrypoint(entrypoint: Path, *, version: str) -> dict[str, Any]:
    completed = _run(
        _console_launcher_command(entrypoint, "doctor"),
        cwd=entrypoint.parent,
    )
    try:
        report = json.loads(completed.stdout)
    except ValueError as exc:
        raise ValueError("active rollback entrypoint doctor output is not JSON") from exc
    if not isinstance(report, dict) or report.get("topoforge") != version:
        raise ValueError("active rollback entrypoint reported the wrong version")
    return {
        "version": version,
        "output_sha256": _sha256_bytes(completed.stdout.encode("utf-8")),
        "exit_code": completed.returncode,
    }


def _verify_installed_switch(
    *,
    work_root: Path,
    current_launcher: Path,
    current_version: str,
    previous_launcher: Path,
    previous_version: str,
) -> dict[str, Any]:
    if current_launcher.suffix.casefold() != previous_launcher.suffix.casefold():
        raise ValueError("installed rollback launchers use different executable suffixes")
    active_root = work_root / "active-installation"
    active_root.mkdir()
    active_entrypoint = active_root / f"topoforge{current_launcher.suffix}"
    active_entrypoint.symlink_to(current_launcher)
    if active_entrypoint.resolve() != current_launcher.resolve():
        raise ValueError("installed rollback active pointer did not start at the current release")
    before = _doctor_through_active_entrypoint(active_entrypoint, version=current_version)
    replacement = active_root / f".topoforge-{secrets.token_hex(12)}.tmp"
    try:
        replacement.symlink_to(previous_launcher)
        os.replace(replacement, active_entrypoint)
    finally:
        replacement.unlink(missing_ok=True)
    if active_entrypoint.resolve() != previous_launcher.resolve():
        raise ValueError("installed rollback active pointer did not switch to the previous release")
    after = _doctor_through_active_entrypoint(active_entrypoint, version=previous_version)
    return {
        "entrypoint": "active-installation/topoforge",
        "before_target": "current",
        "before_launcher_target": current_launcher.relative_to(work_root).as_posix(),
        "before_launcher_sha256": _sha256_file(current_launcher),
        "before_version": before["version"],
        "before_output_sha256": before["output_sha256"],
        "before_exit_code": before["exit_code"],
        "after_target": "previous",
        "after_launcher_target": previous_launcher.relative_to(work_root).as_posix(),
        "after_launcher_sha256": _sha256_file(previous_launcher),
        "after_version": after["version"],
        "after_output_sha256": after["output_sha256"],
        "after_exit_code": after["exit_code"],
        "atomic_pointer_switch": True,
        "required_checks_passed": True,
    }


def generate_runtime_report(
    *,
    repository_root: Path,
    version: str,
    source_commit: str,
    script_path: Path,
    current_wheel: Path,
    current_wheel_sha256: str,
    previous_wheel: Path,
    previous_wheel_sha256: str,
    previous_checksums: Path,
    previous_checksums_sha256: str,
    previous_release_id: int,
    previous_release_published_at: str,
    previous_wheel_asset_id: int,
    previous_checksums_asset_id: int,
    retained_evidence_root: Path,
    work_root: Path,
) -> dict[str, Any]:
    """Execute both rollback paths and return a path-free, hash-bound report."""
    repository_root = _absolute_lexical_path(repository_root)
    script_path = _absolute_lexical_path(script_path)
    current_wheel = _absolute_lexical_path(current_wheel)
    previous_wheel = _absolute_lexical_path(previous_wheel)
    previous_checksums = _absolute_lexical_path(previous_checksums)
    retained_evidence_root = _absolute_lexical_path(retained_evidence_root)
    work_root = _absolute_lexical_path(work_root)
    if COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise ValueError("source_commit must be a full lowercase Git commit")
    repository_chain = _checked_path_chain(repository_root, label="source repository")
    if not stat.S_ISDIR(repository_chain[-1].st_mode):
        raise FileNotFoundError(f"source repository is not a directory: {repository_root}")
    previous_release_id = _positive_github_id(previous_release_id, "previous release ID")
    previous_wheel_asset_id = _positive_github_id(
        previous_wheel_asset_id, "previous wheel asset ID"
    )
    previous_checksums_asset_id = _positive_github_id(
        previous_checksums_asset_id, "previous SHA256SUMS asset ID"
    )
    if previous_wheel_asset_id == previous_checksums_asset_id:
        raise ValueError("previous release wheel and SHA256SUMS asset IDs must differ")
    previous_release_published_at = _github_published_at(previous_release_published_at)
    previous_version = rollback_previous_version(version)
    current_artifact = _validate_release_wheel(
        current_wheel,
        version=version,
        expected_sha256=current_wheel_sha256,
    )
    previous_artifact = _validate_release_wheel(
        previous_wheel,
        version=previous_version,
        expected_sha256=previous_wheel_sha256,
    )
    previous_checksum_record = _validate_previous_checksums(
        previous_checksums,
        expected_sha256=previous_checksums_sha256,
        wheel_filename=str(previous_artifact["filename"]),
        wheel_sha256=str(previous_artifact["sha256"]),
    )
    expected_script = canonical_rollback_script(version, previous_version)
    if (
        _read_pinned_bytes(
            script_path,
            label="rollback script",
            maximum_bytes=1024 * 1024,
        )
        != expected_script
    ):
        raise ValueError("rollback script differs from the canonical generated script")
    try:
        work_root.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"rollback work root already exists: {work_root}")
    _create_plain_directory_tree(work_root, label="rollback work root", require_new=True)
    release_commit = _git(repository_root, "rev-parse", f"v{version}^{{commit}}")
    if _git(repository_root, "rev-parse", "HEAD") != release_commit:
        raise ValueError("rollback producer requires HEAD at the release tag commit")
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", source_commit, release_commit],
        cwd=repository_root,
        check=False,
        capture_output=True,
    )
    if ancestry.returncode != 0:
        raise ValueError("rollback producer source_commit is not an ancestor of the release tag")
    previous_commit = _git(repository_root, "rev-parse", f"v{previous_version}^{{commit}}")
    before = _inventory(retained_evidence_root)

    checkout = work_root / "release-source"
    _run(["git", "clone", "--no-hardlinks", str(repository_root), str(checkout)], cwd=work_root)
    _git(checkout, "checkout", "--detach", release_commit)
    rollback_checkout = work_root / "rollback-source"
    environment = os.environ.copy()
    environment["TOPOFORGE_ROLLBACK_DIR"] = str(rollback_checkout)
    completed = _run(
        ["bash", str(checkout / script_path.relative_to(repository_root)), "--confirm-rollback"],
        cwd=checkout,
        environment=environment,
    )
    rollback_commit = _git(rollback_checkout, "rev-parse", "HEAD")
    if rollback_commit != previous_commit or _git(rollback_checkout, "status", "--porcelain"):
        raise ValueError("source rollback worktree is not the clean previous release tag")

    current_install, current_launcher = _build_and_verify_install(
        checkout,
        wheel=current_wheel,
        expected_wheel_sha256=current_wheel_sha256,
        version=version,
        work_root=work_root,
        label="current",
    )
    previous_install, previous_launcher = _build_and_verify_install(
        rollback_checkout,
        wheel=previous_wheel,
        expected_wheel_sha256=previous_wheel_sha256,
        version=previous_version,
        work_root=work_root,
        label="previous",
    )
    if current_install["wheel_sha256"] == previous_install["wheel_sha256"]:
        raise ValueError("rollback installations did not distinguish release artifacts")
    activation = _verify_installed_switch(
        work_root=work_root,
        current_launcher=current_launcher,
        current_version=version,
        previous_launcher=previous_launcher,
        previous_version=previous_version,
    )
    if (
        activation["before_launcher_sha256"] != current_install["launcher_sha256"]
        or activation["after_launcher_sha256"] != previous_install["launcher_sha256"]
        or activation["before_output_sha256"] != current_install["doctor_output_sha256"]
        or activation["after_output_sha256"] != previous_install["doctor_output_sha256"]
        or activation["before_output_sha256"] == activation["after_output_sha256"]
    ):
        raise ValueError("active rollback launcher evidence differs from isolated installations")
    after = _inventory(retained_evidence_root)
    if before != after:
        raise ValueError("retained release evidence changed during rollback verification")
    if (
        _validate_previous_checksums(
            previous_checksums,
            expected_sha256=previous_checksums_sha256,
            wheel_filename=str(previous_artifact["filename"]),
            wheel_sha256=str(previous_artifact["sha256"]),
        )
        != previous_checksum_record
    ):
        raise ValueError("previous release SHA256SUMS changed during rollback verification")
    producer_path = Path(__file__).resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "topoforge_version": version,
        "source_commit": source_commit,
        "release_commit": release_commit,
        "producer_sha256": _sha256_file(producer_path),
        "script_sha256": _sha256_bytes(expected_script),
        "previous_version": previous_version,
        "release_artifacts": {
            "current": {
                "role": "formal-current-release-primary-wheel",
                **current_artifact,
            },
            "previous": {
                "role": "verified-previous-public-release-wheel",
                "release_tag": f"v{previous_version}",
                "release_id": previous_release_id,
                "published_at": previous_release_published_at,
                "wheel_asset_id": previous_wheel_asset_id,
                **previous_artifact,
                "checksums": {
                    "asset_id": previous_checksums_asset_id,
                    **previous_checksum_record,
                },
            },
            "required_checks_passed": True,
        },
        "installed_environment": {
            "strategy": "parallel-isolated-environments-atomic-pointer-switch",
            "current": current_install,
            "previous": previous_install,
            "activation": activation,
            "required_checks_passed": True,
        },
        "source_checkout": {
            "release_tag": f"v{version}",
            "release_commit": release_commit,
            "previous_tag": f"v{previous_version}",
            "previous_commit": previous_commit,
            "script_exit_code": completed.returncode,
            "rollback_worktree_commit": rollback_commit,
            "rollback_worktree_clean": True,
            "required_checks_passed": True,
        },
        "retained_evidence": {
            "before_rollback": before,
            "after_rollback": after,
            "required_checks_passed": True,
        },
        "required_checks_passed": True,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    try:
        write_canonical_json(path, report)
    except EvidencePublicationError as exc:
        if exc.committed is True:
            exc.add_note(
                "the exact rollback report replaced its destination, but directory durability "
                "was not confirmed; preserve the reported destination for reconciliation"
            )
        raise


def main() -> int:
    """Generate the release-workflow-owned rollback runtime report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--current-wheel", type=Path, required=True)
    parser.add_argument("--current-wheel-sha256", required=True)
    parser.add_argument("--previous-wheel", type=Path, required=True)
    parser.add_argument("--previous-wheel-sha256", required=True)
    parser.add_argument("--previous-checksums", type=Path, required=True)
    parser.add_argument("--previous-checksums-sha256", required=True)
    parser.add_argument("--previous-release-id", type=int, required=True)
    parser.add_argument("--previous-release-published-at", required=True)
    parser.add_argument("--previous-wheel-asset-id", type=int, required=True)
    parser.add_argument("--previous-checksums-asset-id", type=int, required=True)
    parser.add_argument("--retained-evidence-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    repository_root = _absolute_lexical_path(args.repository_root)
    report = generate_runtime_report(
        repository_root=repository_root,
        version=args.version,
        source_commit=args.source_commit,
        script_path=_absolute_lexical_path(args.script),
        current_wheel=_absolute_lexical_path(args.current_wheel),
        current_wheel_sha256=args.current_wheel_sha256,
        previous_wheel=_absolute_lexical_path(args.previous_wheel),
        previous_wheel_sha256=args.previous_wheel_sha256,
        previous_checksums=_absolute_lexical_path(args.previous_checksums),
        previous_checksums_sha256=args.previous_checksums_sha256,
        previous_release_id=args.previous_release_id,
        previous_release_published_at=args.previous_release_published_at,
        previous_wheel_asset_id=args.previous_wheel_asset_id,
        previous_checksums_asset_id=args.previous_checksums_asset_id,
        retained_evidence_root=_absolute_lexical_path(args.retained_evidence_root),
        work_root=_absolute_lexical_path(args.work_root),
    )
    _write_report(_absolute_lexical_path(args.report), report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
