#!/usr/bin/env python3
"""Shared deterministic archive and identity helpers for the macOS app candidate."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import stat
import struct
import time
import unicodedata
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import IO, Any

CONFIG_SCHEMA_VERSION = "topoforge-macos-arm64-runtime-v1"
MANIFEST_SCHEMA_VERSION = "topoforge-macos-app-manifest-v1"
BUILD_SCHEMA_VERSION = "topoforge-macos-app-build-v1"
VERIFICATION_SCHEMA_VERSION = "topoforge-macos-app-verification-v1"
SYSTEM_SCHEMA_VERSION = "topoforge-macos-system-acceptance-v1"
DEFAULT_CONFIG = Path("packaging/macos-arm64-runtime.json")
APP_ROOT = "TopoForge.app"
MANIFEST_PATH = "Contents/Resources/manifest.json"
INFO_PLIST_PATH = "Contents/Info.plist"
CLI_LAUNCHER_PATH = "Contents/Resources/bin/topoforge"
WEB_LAUNCHER_PATH = "Contents/MacOS/TopoForge"
PYTHON_PATH = "Contents/Frameworks/Python.framework/Versions/3.12/bin/python3.12"

_CPU_ARCHITECTURES = {
    0x01000007: "x86_64",
    0x0100000C: "arm64",
}
_LC_VERSION_MIN_MACOSX = 0x24
_LC_BUILD_VERSION = 0x32
_MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}
_PINNED_RUNTIME_IDENTITY = {
    "implementation": "CPython",
    "version": "3.12.10",
    "abi": "cp312",
    "provider": "Python Software Foundation",
    "release_page_url": "https://www.python.org/downloads/release/python-31210/",
    "archive_name": "python-3.12.10-macos11.pkg",
    "url": "https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg",
    "sigstore_url": (
        "https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg.sigstore"
    ),
    "sha256": "8373e58da4ea146b3eb1c1f9834f19a319440b6b679b06050b1f9ee3237aa8e4",
    "bytes": 45720356,
    "archive_kind": "python.org macOS 64-bit universal2 installer",
    "source_architecture": "universal2",
    "embedded_architecture": "arm64",
    "upstream_minimum_macos": "10.13",
    "framework_version": "3.12",
}
_PINNED_SOURCE_PRIMARY_MACHO = {
    "architectures": ["x86_64", "arm64"],
    "minimum_macos": {"x86_64": "10.13", "arm64": "11.0"},
}
_PINNED_EMBEDDED_PRIMARY_MACHO = {
    "architecture": "arm64",
    "minimum_macos": "11.0",
}
_PINNED_SOURCE_ONLY_PATHS = [
    "Versions/3.12/bin/python3-intel64",
    "Versions/3.12/bin/python3.12-intel64",
    "Versions/3.12/lib/itcl4.3.2/libitclstub4.3.2.a",
    "Versions/3.12/lib/libtclstub8.6.a",
    "Versions/3.12/lib/libtkstub8.6.a",
    "Versions/3.12/lib/python3.12/config-3.12-darwin/python.o",
    "Versions/3.12/lib/tdbc1.1.10/libtdbcstub1.1.10.a",
]


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 of one regular file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """Render stable UTF-8 JSON with a terminating newline."""
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_json_with_sha256(path: Path, value: Any) -> str:
    """Write canonical JSON and a detached SHA-256 sidecar."""
    payload = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    path.with_name(f"{path.name}.sha256").write_text(
        f"{digest}  {path.name}\n",
        encoding="utf-8",
    )
    return digest


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _sha256_value(value: Any, label: str) -> str:
    digest = _string(value, label)
    if len(digest) != 64:
        raise ValueError(f"{label} must contain 64 hexadecimal characters")
    try:
        int(digest, 16)
    except ValueError as exc:
        raise ValueError(f"{label} is not hexadecimal") from exc
    return digest


def load_config(path: Path) -> dict[str, Any]:
    """Load and validate the immutable macOS arm64 runtime contract."""
    try:
        config = _json_object(json.loads(path.read_text(encoding="utf-8")), "config")
    except (OSError, ValueError) as exc:
        raise ValueError(f"macOS runtime config is unreadable: {path}") from exc
    if config.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ValueError("macOS runtime config schema is unsupported")

    target = _json_object(config.get("target"), "target")
    if target.get("os") != "macOS" or target.get("architecture") != "arm64":
        raise ValueError("macOS app target must be native macOS arm64")
    if target.get("deployment_target") != "15.0":
        raise ValueError("macOS app deployment target must remain 15.0")
    if target.get("candidate_major_versions") != [15, 26]:
        raise ValueError("macOS app candidate majors must be exactly 15 and 26")
    if target.get("unsupported_0_12_x") != [
        "macOS 14",
        "Intel x86_64",
        "preview macOS releases",
    ]:
        raise ValueError("macOS 0.12.x exclusions must remain explicit and frozen")

    runtime = _json_object(config.get("python_runtime"), "python_runtime")
    for field in (
        "implementation",
        "version",
        "abi",
        "provider",
        "release_page_url",
        "archive_name",
        "url",
        "sigstore_url",
        "sha256",
        "archive_kind",
        "source_architecture",
        "embedded_architecture",
        "upstream_minimum_macos",
        "framework_version",
    ):
        _string(runtime.get(field), f"python_runtime.{field}")
    _sha256_value(runtime["sha256"], "python_runtime.sha256")
    _positive_int(runtime.get("bytes"), "python_runtime.bytes")
    for field, expected in _PINNED_RUNTIME_IDENTITY.items():
        if runtime.get(field) != expected:
            raise ValueError(f"pinned CPython runtime identity changed: {field}")
    if runtime.get("source_primary_macho") != _PINNED_SOURCE_PRIMARY_MACHO:
        raise ValueError("official CPython universal2 Mach-O identity changed")
    if runtime.get("embedded_primary_macho") != _PINNED_EMBEDDED_PRIMARY_MACHO:
        raise ValueError("embedded CPython arm64 Mach-O identity changed")
    if runtime.get("source_only_paths") != _PINNED_SOURCE_ONLY_PATHS:
        raise ValueError("official CPython source-only payload identity changed")

    if PurePosixPath(runtime["archive_name"]).name != runtime["archive_name"]:
        raise ValueError("python_runtime.archive_name must be a filename")

    bounds = _json_object(config.get("bounds"), "bounds")
    for field in (
        "runtime_archive_max_bytes",
        "bundle_member_max_bytes",
        "bundle_member_count_max",
        "bundle_uncompressed_max_bytes",
        "archive_max_bytes",
        "manifest_max_bytes",
        "evidence_max_bytes",
    ):
        _positive_int(bounds.get(field), f"bounds.{field}")
    epoch = _positive_int(config.get("source_date_epoch"), "source_date_epoch")
    if not 315532800 <= epoch <= 4354819199:
        raise ValueError("source_date_epoch must fit the ZIP timestamp range")
    return config


def safe_relative_path(name: str) -> PurePosixPath:
    """Return a canonical portable POSIX path or reject it."""
    if not name or "\x00" in name or "\\" in name:
        raise ValueError(f"path is not a canonical relative path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or ".." in path.parts:
        raise ValueError(f"path is not a canonical relative path: {name!r}")
    if any(part in {"", ".", ".."} or ":" in part for part in path.parts):
        raise ValueError(f"path is unsafe in a macOS app bundle: {name!r}")
    return path


def register_macos_path(path: PurePosixPath, seen: dict[str, tuple[str, str]], kind: str) -> None:
    """Reject case/normalization and file/directory collisions on common macOS volumes."""
    for length in range(1, len(path.parts) + 1):
        prefix = PurePosixPath(*path.parts[:length]).as_posix()
        folded = unicodedata.normalize("NFD", prefix).casefold()
        current_kind = "directory" if length < len(path.parts) else kind
        previous = seen.get(folded)
        if previous is None:
            seen[folded] = (prefix, current_kind)
            continue
        previous_path, previous_kind = previous
        if previous_path != prefix:
            raise ValueError(f"paths collide on macOS: {previous_path!r} and {prefix!r}")
        if previous_kind != current_kind:
            raise ValueError(f"path is both a file and directory on macOS: {prefix!r}")


def _lexical_symlink_destination(link_path: PurePosixPath, target: str) -> PurePosixPath:
    if not target or "\x00" in target or "\\" in target:
        raise ValueError(f"app symlink target is invalid: {target!r}")
    raw = PurePosixPath(target)
    if raw.is_absolute():
        raise ValueError(f"app symlink target is absolute: {target!r}")
    parts: list[str] = list(link_path.parent.parts)
    for part in raw.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ValueError(f"app symlink escapes the bundle: {link_path} -> {target}")
            parts.pop()
        else:
            if ":" in part:
                raise ValueError(f"app symlink target is unsafe: {target!r}")
            parts.append(part)
    if not parts or parts[0] != APP_ROOT:
        raise ValueError(f"app symlink escapes the bundle: {link_path} -> {target}")
    return PurePosixPath(*parts)


def _resolved_bundle_destination(
    link_path: PurePosixPath,
    target: str,
    *,
    object_kinds: dict[str, str],
    symlink_targets: dict[str, str],
) -> PurePosixPath:
    """Resolve an internal bundle link through link-bearing path components."""
    destination = _lexical_symlink_destination(link_path, target)
    visited: set[str] = set()
    for _attempt in range(len(symlink_targets) + 1):
        replaced = False
        for length in range(1, len(destination.parts) + 1):
            prefix = PurePosixPath(*destination.parts[:length])
            prefix_name = prefix.as_posix()
            kind = object_kinds.get(prefix_name)
            if kind == "file" and length < len(destination.parts):
                raise ValueError(
                    f"bundle symlink traverses a regular file: {link_path} -> {target}"
                )
            if kind != "symlink":
                continue
            if prefix_name in visited:
                raise ValueError(f"bundle contains a symlink cycle: {link_path}")
            visited.add(prefix_name)
            nested_target = symlink_targets.get(prefix_name)
            if nested_target is None:
                raise ValueError(f"bundle symlink target metadata is missing: {prefix}")
            resolved_prefix = _lexical_symlink_destination(prefix, nested_target)
            destination = resolved_prefix.joinpath(*destination.parts[length:])
            replaced = True
            break
        if not replaced:
            if destination.as_posix() not in object_kinds:
                raise ValueError(f"bundle symlink target is missing: {link_path} -> {target}")
            return destination
    raise ValueError(f"bundle contains a symlink cycle: {link_path}")


def bundle_entries(app_root: Path, *, bounds: dict[str, Any]) -> list[dict[str, Any]]:
    """Enumerate an app without following links and return its exact payload projection."""
    if app_root.name != APP_ROOT or not app_root.is_dir() or app_root.is_symlink():
        raise ValueError(f"bundle root must be a real {APP_ROOT} directory")
    records: list[dict[str, Any]] = []
    seen: dict[str, tuple[str, str]] = {}
    objects: dict[str, str] = {}
    symlink_targets: dict[str, str] = {}
    total_bytes = 0
    stack = [app_root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
        for child in children:
            relative = PurePosixPath(APP_ROOT) / child.path[len(str(app_root)) + 1 :]
            relative = safe_relative_path(relative.as_posix())
            result = child.stat(follow_symlinks=False)
            if child.is_dir(follow_symlinks=False):
                register_macos_path(relative, seen, "directory")
                objects[relative.as_posix()] = "directory"
                stack.append(Path(child.path))
                continue
            if child.is_symlink():
                register_macos_path(relative, seen, "symlink")
                target = os.readlink(child.path)
                _lexical_symlink_destination(relative, target)
                payload = target.encode("utf-8")
                record = {
                    "path": relative.as_posix()[len(APP_ROOT) + 1 :],
                    "kind": "symlink",
                    "target": target,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                objects[relative.as_posix()] = "symlink"
                symlink_targets[relative.as_posix()] = target
                records.append(record)
                continue
            if not stat.S_ISREG(result.st_mode):
                raise ValueError(f"bundle contains a special object: {relative}")
            if result.st_nlink != 1:
                raise ValueError(f"bundle contains a hard-linked regular file: {relative}")
            register_macos_path(relative, seen, "file")
            if result.st_size > bounds["bundle_member_max_bytes"]:
                raise ValueError(f"bundle member exceeds its size bound: {relative}")
            total_bytes += result.st_size
            if total_bytes > bounds["bundle_uncompressed_max_bytes"]:
                raise ValueError("bundle exceeds its expansion bound")
            path = Path(child.path)
            records.append(
                {
                    "path": relative.as_posix()[len(APP_ROOT) + 1 :],
                    "kind": "file",
                    "mode": stat.S_IMODE(result.st_mode),
                    "bytes": result.st_size,
                    "sha256": sha256_file(path),
                }
            )
            objects[relative.as_posix()] = "file"
            if len(records) > bounds["bundle_member_count_max"]:
                raise ValueError("bundle exceeds its member-count bound")

    for record in records:
        if record["kind"] != "symlink":
            continue
        link = PurePosixPath(APP_ROOT) / record["path"]
        _resolved_bundle_destination(
            link,
            record["target"],
            object_kinds=objects,
            symlink_targets=symlink_targets,
        )
    return sorted(records, key=lambda item: item["path"])


def payload_sha256(records: list[dict[str, Any]]) -> str:
    """Hash the canonical exact bundle projection."""
    payload = json.dumps(
        records,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def zip_timestamp(source_date_epoch: int) -> tuple[int, int, int, int, int, int]:
    """Return the reproducible, two-second-granularity ZIP timestamp."""
    value = time.gmtime(source_date_epoch)
    return (
        value.tm_year,
        value.tm_mon,
        value.tm_mday,
        value.tm_hour,
        value.tm_min,
        value.tm_sec // 2 * 2,
    )


def write_reproducible_zip(
    app_root: Path,
    destination: Path,
    *,
    source_date_epoch: int,
    bounds: dict[str, Any],
    overwrite: bool,
) -> None:
    """Create one deterministic ZIP containing only the closed app payload."""
    if destination.exists() and not overwrite:
        raise FileExistsError(f"app archive already exists: {destination}")
    records = bundle_entries(app_root, bounds=bounds)
    temporary = destination.with_name(f".{destination.name}.part")
    temporary.unlink(missing_ok=True)
    timestamp = zip_timestamp(source_date_epoch)
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            for record in records:
                member = f"{APP_ROOT}/{record['path']}"
                info = zipfile.ZipInfo(member, date_time=timestamp)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.flag_bits |= 0x800
                if record["kind"] == "symlink":
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, record["target"].encode("utf-8"))
                else:
                    info.external_attr = (stat.S_IFREG | record["mode"]) << 16
                    source_path = app_root / Path(*PurePosixPath(record["path"]).parts)
                    with source_path.open("rb") as source, archive.open(info, "w") as output:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            output.write(chunk)
        if temporary.stat().st_size > bounds["archive_max_bytes"]:
            raise ValueError("app archive exceeds its configured size bound")
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _read_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo, maximum: int) -> bytes:
    if info.file_size > maximum:
        raise ValueError(f"archive member exceeds its read bound: {info.filename}")
    with archive.open(info) as source:
        payload = source.read(maximum + 1)
    if len(payload) != info.file_size or len(payload) > maximum:
        raise ValueError(f"archive member byte count changed: {info.filename}")
    return payload


def inspect_archive(
    archive_path: Path,
    *,
    config: dict[str, Any],
    expected_source_commit: str | None = None,
) -> dict[str, Any]:
    """Validate exact archive closure and all static manifest bindings."""
    bounds = _json_object(config["bounds"], "bounds")
    if not archive_path.is_file() or archive_path.is_symlink():
        raise FileNotFoundError(f"macOS app archive is missing: {archive_path}")
    archive_bytes = archive_path.stat().st_size
    if archive_bytes > bounds["archive_max_bytes"]:
        raise ValueError("app archive exceeds its configured size bound")
    expected_timestamp = zip_timestamp(config["source_date_epoch"])
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        filenames = [info.filename for info in infos]
        if filenames != sorted(filenames):
            raise ValueError("app archive members are not in canonical order")
        if not infos or len(infos) > bounds["bundle_member_count_max"]:
            raise ValueError("app archive member count is invalid")
        if archive.comment:
            raise ValueError("app archive must not contain a ZIP comment")
        members: dict[str, tuple[zipfile.ZipInfo, str]] = {}
        seen: dict[str, tuple[str, str]] = {}
        object_kinds: dict[str, str] = {APP_ROOT: "directory"}
        uncompressed = 0
        for info in infos:
            path = safe_relative_path(info.filename)
            if len(path.parts) < 2 or path.parts[0] != APP_ROOT or info.is_dir():
                raise ValueError(
                    f"archive member is outside the canonical app closure: {info.filename}"
                )
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if file_type == stat.S_IFLNK:
                kind = "symlink"
            elif file_type in {0, stat.S_IFREG}:
                kind = "file"
            else:
                raise ValueError(f"archive contains a special object: {info.filename}")
            register_macos_path(path, seen, kind)
            for length in range(2, len(path.parts)):
                parent = PurePosixPath(*path.parts[:length]).as_posix()
                previous = object_kinds.get(parent)
                if previous not in {None, "directory"}:
                    raise ValueError(f"archive member traverses a non-directory: {info.filename}")
                object_kinds[parent] = "directory"
            object_kinds[path.as_posix()] = kind
            if relative in members:
                raise ValueError(f"archive has a duplicate member: {relative}")
            if info.flag_bits & 0x1 or info.extra or info.comment:
                raise ValueError(f"archive member has non-canonical metadata: {relative}")
            if info.create_system != 3 or info.compress_type != zipfile.ZIP_DEFLATED:
                raise ValueError(f"archive member has non-canonical metadata: {relative}")
            if info.date_time != expected_timestamp:
                raise ValueError(f"archive member timestamp is not reproducible: {relative}")
            if info.file_size > bounds["bundle_member_max_bytes"]:
                raise ValueError(f"archive member exceeds its size bound: {relative}")
            uncompressed += info.file_size
            if uncompressed > bounds["bundle_uncompressed_max_bytes"]:
                raise ValueError("app archive exceeds its expansion bound")
            members[relative] = (info, kind)

        manifest_info = members.get(MANIFEST_PATH)
        if manifest_info is None or manifest_info[1] != "file":
            raise ValueError("app manifest is missing")
        manifest = _json_object(
            json.loads(
                _read_bounded(archive, manifest_info[0], bounds["manifest_max_bytes"]).decode(
                    "utf-8"
                )
            ),
            "manifest",
        )
        if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise ValueError("app manifest schema is unsupported")
        if manifest.get("package_role") != "phase13a-macos-arm64-unsigned-candidate":
            raise ValueError("app manifest package role changed")
        source = _json_object(manifest.get("source"), "manifest.source")
        if expected_source_commit is not None and source.get("commit") != expected_source_commit:
            raise ValueError("app archive source commit differs from the expected commit")
        if source.get("tracked_dirty") is not False:
            raise ValueError("app archive does not identify a clean source tree")
        source_commit = _string(source.get("commit"), "manifest.source.commit")
        if len(source_commit) != 40:
            raise ValueError("app archive source commit must contain 40 hexadecimal characters")
        try:
            int(source_commit, 16)
        except ValueError as exc:
            raise ValueError("app archive source commit is not hexadecimal") from exc
        runtime = _json_object(manifest.get("python_runtime"), "manifest.python_runtime")
        build_identity = _json_object(manifest.get("build_identity"), "manifest.build_identity")
        locked_dependencies = _json_object(
            manifest.get("locked_dependencies"), "manifest.locked_dependencies"
        )
        configured_runtime = _json_object(config["python_runtime"], "python_runtime")
        for field in (
            "config_sha256",
            "uv_lock_sha256",
            "pyproject_sha256",
            "build_constraints_sha256",
        ):
            _sha256_value(build_identity.get(field), f"manifest.build_identity.{field}")
        verifier_hashes = _json_object(
            build_identity.get("verifier_sha256"),
            "manifest.build_identity.verifier_sha256",
        )
        for role in ("builder", "shared", "archive", "system"):
            _sha256_value(verifier_hashes.get(role), f"manifest verifier {role}")
        if locked_dependencies.get("uv_lock_sha256") != build_identity["uv_lock_sha256"]:
            raise ValueError("locked dependency uv.lock identity differs from the build identity")
        _sha256_value(
            locked_dependencies.get("requirements_sha256"),
            "manifest.locked_dependencies.requirements_sha256",
        )
        packages = locked_dependencies.get("packages")
        if not isinstance(packages, list) or locked_dependencies.get("count") != len(packages):
            raise ValueError("locked dependency package inventory count changed")
        package_names: set[str] = set()
        for index, package_value in enumerate(packages):
            package = _json_object(package_value, f"locked dependency package {index}")
            name = _string(package.get("canonical_name"), "locked dependency canonical name")
            _string(package.get("name"), "locked dependency name")
            _string(package.get("version"), "locked dependency version")
            if name in package_names:
                raise ValueError(f"locked dependency package is duplicated: {name}")
            package_names.add(name)
        for field in ("version", "url", "sha256", "bytes", "embedded_architecture"):
            if runtime.get(field) != configured_runtime[field]:
                raise ValueError(f"app runtime binding changed: {field}")
        if runtime.get("macho_architectures") != ["arm64"]:
            raise ValueError("embedded runtime is not arm64-only")

        contents = _json_object(manifest.get("contents"), "manifest.contents")
        raw_records = contents.get("files")
        if not isinstance(raw_records, list):
            raise ValueError("app manifest contents.files must be a list")
        expected_records: list[dict[str, Any]] = []
        expected_paths: list[str] = []
        symlink_targets: dict[str, str] = {}
        observed_macho: list[dict[str, Any]] = []
        for index, value in enumerate(raw_records):
            record = _json_object(value, f"manifest.contents.files[{index}]")
            relative = _string(record.get("path"), "manifest file path")
            safe_relative_path(relative)
            if relative == MANIFEST_PATH:
                raise ValueError("app manifest must not include itself in its payload projection")
            member = members.get(relative)
            if member is None:
                raise ValueError(f"app manifest references a missing member: {relative}")
            info, kind = member
            if record.get("kind") != kind or record.get("bytes") != info.file_size:
                raise ValueError(f"app manifest member identity changed: {relative}")
            payload = _read_bounded(archive, info, bounds["bundle_member_max_bytes"])
            if record.get("sha256") != hashlib.sha256(payload).hexdigest():
                raise ValueError(f"app manifest member SHA-256 changed: {relative}")
            if kind == "symlink":
                target = payload.decode("utf-8")
                if record.get("target") != target:
                    raise ValueError(f"app symlink target changed: {relative}")
                _lexical_symlink_destination(PurePosixPath(APP_ROOT) / relative, target)
                symlink_targets[f"{APP_ROOT}/{relative}"] = target
            else:
                archived_mode = stat.S_IMODE(info.external_attr >> 16)
                if record.get("mode") != archived_mode:
                    raise ValueError(f"app manifest member mode changed: {relative}")
                if payload[:4] in _MACHO_MAGICS:
                    slices = macho_slices_bytes(payload, label=relative)
                    if len(slices) != 1 or slices[0]["architecture"] != "arm64":
                        raise ValueError(f"archived Mach-O is not arm64-only: {relative}")
                    minimum = slices[0]["minimum_macos"]
                    if _version_tuple(minimum) > _version_tuple(
                        config["target"]["deployment_target"]
                    ):
                        raise ValueError(
                            f"archived Mach-O requires macOS {minimum}, above app target: "
                            f"{relative}"
                        )
                    observed_macho.append(
                        {
                            "path": relative,
                            "architecture": "arm64",
                            "minimum_macos": minimum,
                            "bytes": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                    )
            expected_records.append(record)
            expected_paths.append(relative)
        if expected_paths != sorted(expected_paths) or len(expected_paths) != len(
            set(expected_paths)
        ):
            raise ValueError("app manifest file list is not sorted and unique")
        actual_paths = sorted(set(members) - {MANIFEST_PATH})
        if expected_paths != actual_paths:
            raise ValueError("app manifest does not cover the archive exactly")
        if contents.get("file_count") != len(expected_records):
            raise ValueError("app manifest file count changed")
        if contents.get("payload_sha256") != payload_sha256(expected_records):
            raise ValueError("app payload projection SHA-256 changed")

        for relative, target in symlink_targets.items():
            _resolved_bundle_destination(
                PurePosixPath(relative),
                target,
                object_kinds=object_kinds,
                symlink_targets=symlink_targets,
            )

        observed_macho.sort(key=lambda item: item["path"])
        declared_macho = runtime.get("macho_files")
        if declared_macho != observed_macho:
            raise ValueError("archived Mach-O identity differs from the app manifest")
        if runtime.get("macho_file_count") != len(observed_macho):
            raise ValueError("archived Mach-O file count differs from the app manifest")
        if not observed_macho or not any(item["path"] == PYTHON_PATH for item in observed_macho):
            raise ValueError("embedded CPython Mach-O is missing from the archive")

        plist_info = members.get(INFO_PLIST_PATH)
        if plist_info is None or plist_info[1] != "file":
            raise ValueError("Info.plist is missing")
        plist = plistlib.loads(_read_bounded(archive, plist_info[0], bounds["manifest_max_bytes"]))
        expected_plist = {
            "CFBundleExecutable": "TopoForge",
            "CFBundleIdentifier": "org.topoforge.app",
            "CFBundleName": "TopoForge",
            "CFBundlePackageType": "APPL",
            "LSMinimumSystemVersion": "15.0",
        }
        if any(plist.get(key) != value for key, value in expected_plist.items()):
            raise ValueError("Info.plist identity or deployment target changed")
        for required in (PYTHON_PATH, CLI_LAUNCHER_PATH, WEB_LAUNCHER_PATH):
            if required not in members or members[required][1] != "file":
                raise ValueError(f"required app executable is missing: {required}")
        web_assets = [
            path
            for path in members
            if path.startswith(
                "Contents/Frameworks/Python.framework/Versions/3.12/lib/python3.12/site-packages/topoforge/web/static/"
            )
        ]
        if not web_assets:
            raise ValueError("production Web assets are missing from the app")

    return {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "archive": {
            "filename": archive_path.name,
            "sha256": sha256_file(archive_path),
            "bytes": archive_bytes,
            "member_count": len(infos),
            "uncompressed_bytes": uncompressed,
        },
        "source": source,
        "build_identity": build_identity,
        "python_runtime": runtime,
        "locked_dependencies": locked_dependencies,
        "contents": {
            "file_count": len(expected_records),
            "payload_sha256": contents["payload_sha256"],
        },
        "macho": {
            "file_count": len(observed_macho),
            "architecture": "arm64",
            "minimum_versions": sorted({item["minimum_macos"] for item in observed_macho}),
            "required_checks_passed": True,
        },
        "info_plist": expected_plist,
        "static_checks_passed": True,
        "native_execution": {
            "status": "not-run",
            "corrective_action": (
                "Run scripts/verify_macos_app.py --execute on native arm64 macOS 15 or 26."
            ),
        },
        "clean_system_evidence": False,
        "gatekeeper_evidence": False,
        "signed": False,
        "bambu_phase13b_evidence": False,
        "required_checks_passed": True,
    }


def extract_archive(
    archive_path: Path,
    destination: Path,
    *,
    config: dict[str, Any],
) -> Path:
    """Extract a statically verified app without allowing path or link escape."""
    inspect_archive(archive_path, config=config)
    if destination.exists():
        raise FileExistsError(f"app extraction destination already exists: {destination}")
    destination.mkdir(parents=True)
    bounds = _json_object(config["bounds"], "bounds")
    pending_links: list[tuple[Path, PurePosixPath, str]] = []
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            relative = safe_relative_path(info.filename)
            output = destination.joinpath(*relative.parts)
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) == stat.S_IFLNK:
                target = _read_bounded(archive, info, bounds["bundle_member_max_bytes"]).decode(
                    "utf-8"
                )
                _lexical_symlink_destination(relative, target)
                pending_links.append((output, relative, target))
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, output.open("xb") as target_file:
                _copy_exact(source, target_file, info.file_size)
            os.chmod(output, stat.S_IMODE(mode) or 0o644)
    for output, relative, target in pending_links:
        output.parent.mkdir(parents=True, exist_ok=True)
        _lexical_symlink_destination(relative, target)
        output.symlink_to(target)
    return destination / APP_ROOT


def _copy_exact(source: IO[bytes], destination: IO[bytes], expected_bytes: int) -> None:
    written = 0
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        written += len(chunk)
        if written > expected_bytes:
            raise ValueError("archive member exceeds its declared size")
        destination.write(chunk)
    if written != expected_bytes:
        raise ValueError("archive member byte count changed while extracting")


def _version_string(encoded: int) -> str:
    parts = ((encoded >> 16) & 0xFFFF, (encoded >> 8) & 0xFF, encoded & 0xFF)
    while len(parts) > 2 and parts[-1] == 0:
        parts = parts[:-1]
    return ".".join(str(item) for item in parts)


def _thin_macho_slice(data: bytes, offset: int, size: int) -> dict[str, Any]:
    if offset < 0 or size < 32 or offset + size > len(data):
        raise ValueError("Mach-O slice is outside the file")
    magic = data[offset : offset + 4]
    if magic == b"\xcf\xfa\xed\xfe":
        byte_order = "<"
    elif magic == b"\xfe\xed\xfa\xcf":
        byte_order = ">"
    else:
        raise ValueError("unsupported non-64-bit Mach-O slice")
    header = struct.unpack_from(f"{byte_order}8I", data, offset)
    architecture = _CPU_ARCHITECTURES.get(header[1], f"cpu-{header[1]:08x}")
    command_offset = offset + 32
    command_limit = command_offset + header[5]
    if command_limit > offset + size:
        raise ValueError("Mach-O load commands exceed their slice")
    minimum_versions: list[str] = []
    for _index in range(header[4]):
        if command_offset + 8 > command_limit:
            raise ValueError("Mach-O load command header is truncated")
        command, command_size = struct.unpack_from(f"{byte_order}2I", data, command_offset)
        if command_size < 8 or command_offset + command_size > command_limit:
            raise ValueError("Mach-O load command is invalid")
        if command == _LC_BUILD_VERSION:
            if command_size < 24:
                raise ValueError("Mach-O LC_BUILD_VERSION is truncated")
            _platform, minimum, _sdk, _tools = struct.unpack_from(
                f"{byte_order}4I", data, command_offset + 8
            )
            minimum_versions.append(_version_string(minimum))
        elif command == _LC_VERSION_MIN_MACOSX:
            if command_size < 16:
                raise ValueError("Mach-O LC_VERSION_MIN_MACOSX is truncated")
            minimum, _sdk = struct.unpack_from(f"{byte_order}2I", data, command_offset + 8)
            minimum_versions.append(_version_string(minimum))
        command_offset += command_size
    if len(minimum_versions) != 1:
        raise ValueError("Mach-O slice must declare exactly one macOS minimum version")
    return {
        "architecture": architecture,
        "minimum_macos": minimum_versions[0],
        "offset": offset,
        "bytes": size,
    }


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        parts = tuple(int(item) for item in value.split("."))
    except ValueError as exc:
        raise ValueError(f"macOS version is not numeric: {value!r}") from exc
    if not parts:
        raise ValueError("macOS version is empty")
    return parts


def macho_slices_bytes(data: bytes, *, label: str = "Mach-O payload") -> list[dict[str, Any]]:
    """Parse 64-bit thin/fat Mach-O metadata from immutable bytes."""
    if len(data) < 4:
        raise ValueError(f"Mach-O file is truncated: {label}")
    magic = data[:4]
    if magic in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"}:
        return [_thin_macho_slice(data, 0, len(data))]
    if magic == b"\xca\xfe\xba\xbe":
        byte_order = ">"
        is_64_bit_fat = False
    elif magic == b"\xbe\xba\xfe\xca":
        byte_order = "<"
        is_64_bit_fat = False
    elif magic == b"\xca\xfe\xba\xbf":
        byte_order = ">"
        is_64_bit_fat = True
    elif magic == b"\xbf\xba\xfe\xca":
        byte_order = "<"
        is_64_bit_fat = True
    else:
        raise ValueError(f"file is not a supported Mach-O binary: {label}")
    count = struct.unpack_from(f"{byte_order}I", data, 4)[0]
    entry_size = 32 if is_64_bit_fat else 20
    if count < 1 or count > 16 or 8 + count * entry_size > len(data):
        raise ValueError("Mach-O fat header has an invalid architecture count")
    slices: list[dict[str, Any]] = []
    for index in range(count):
        entry_offset = 8 + index * entry_size
        if is_64_bit_fat:
            _cpu, _subtype, offset, size, _align, _reserved = struct.unpack_from(
                f"{byte_order}IIQQII", data, entry_offset
            )
        else:
            _cpu, _subtype, offset, size, _align = struct.unpack_from(
                f"{byte_order}5I", data, entry_offset
            )
        slices.append(_thin_macho_slice(data, offset, size))
    architectures = [item["architecture"] for item in slices]
    if len(architectures) != len(set(architectures)):
        raise ValueError("Mach-O fat binary has duplicate architectures")
    return slices


def macho_slices(path: Path) -> list[dict[str, Any]]:
    """Parse 64-bit thin/fat Mach-O architecture and deployment metadata."""
    return macho_slices_bytes(path.read_bytes(), label=str(path))


def archive_sidecar(path: Path) -> Path:
    """Write and return the detached archive SHA-256 sidecar."""
    sidecar = path.with_name(f"{path.name}.sha256")
    sidecar.write_text(f"{sha256_file(path)}  {path.name}\n", encoding="utf-8")
    return sidecar


def ensure_expected_files(paths: Iterable[Path]) -> None:
    """Require regular, non-link files used as immutable build inputs."""
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"required regular build input is missing: {path}")
