#!/usr/bin/env python3
"""Parse, normalize, and verify relocatable Mach-O dependency closures."""

from __future__ import annotations

import hashlib
import json
import posixpath
import struct
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

APPLE_SYSTEM_PREFIXES = ("/System/Library/", "/usr/lib/")
PYTHON_FRAMEWORK_INSTALL_PREFIX = "/Library/Frameworks/Python.framework/"
PYTHON_FRAMEWORK_BUNDLE_PREFIX = "Contents/Frameworks/Python.framework"

_THIN_MAGICS = {
    b"\xcf\xfa\xed\xfe": "<",
    b"\xfe\xed\xfa\xcf": ">",
}
_FAT_MAGICS = {
    b"\xca\xfe\xba\xbe": (">", False),
    b"\xbe\xba\xfe\xca": ("<", False),
    b"\xca\xfe\xba\xbf": (">", True),
    b"\xbf\xba\xfe\xca": ("<", True),
}
_CPU_ARCHITECTURES = {
    0x01000007: "x86_64",
    0x0100000C: "arm64",
}
_DYLIB_COMMANDS = {
    0x0C: "LC_LOAD_DYLIB",
    0x0D: "LC_ID_DYLIB",
    0x80000018: "LC_LOAD_WEAK_DYLIB",
    0x8000001F: "LC_REEXPORT_DYLIB",
    0x20: "LC_LAZY_LOAD_DYLIB",
    0x80000023: "LC_LOAD_UPWARD_DYLIB",
}
_LOAD_COMMAND_NAMES = frozenset(
    {
        "LC_LOAD_DYLIB",
        "LC_LOAD_WEAK_DYLIB",
        "LC_REEXPORT_DYLIB",
        "LC_LAZY_LOAD_DYLIB",
        "LC_LOAD_UPWARD_DYLIB",
    }
)
_MAX_LOAD_COMMANDS = 16_384
_LC_RPATH = 0x8000001C
_LC_DYLD_ENVIRONMENT = 0x27


def _slice_ranges(data: bytes, *, label: str) -> list[tuple[int, int]]:
    if len(data) < 4:
        raise ValueError(f"Mach-O file is truncated: {label}")
    magic = data[:4]
    if magic in _THIN_MAGICS:
        return [(0, len(data))]
    fat = _FAT_MAGICS.get(magic)
    if fat is None:
        raise ValueError(f"file is not a supported Mach-O binary: {label}")
    byte_order, is_64_bit = fat
    count = struct.unpack_from(f"{byte_order}I", data, 4)[0]
    entry_size = 32 if is_64_bit else 20
    if count < 1 or count > 16 or 8 + count * entry_size > len(data):
        raise ValueError(f"Mach-O fat header has an invalid architecture count: {label}")
    ranges: list[tuple[int, int]] = []
    for index in range(count):
        entry_offset = 8 + index * entry_size
        if is_64_bit:
            _cpu, _subtype, offset, size, _align, _reserved = struct.unpack_from(
                f"{byte_order}IIQQII", data, entry_offset
            )
        else:
            _cpu, _subtype, offset, size, _align = struct.unpack_from(
                f"{byte_order}5I", data, entry_offset
            )
        if offset < 0 or size < 32 or offset + size > len(data):
            raise ValueError(f"Mach-O fat slice is outside the file: {label}")
        if data[offset : offset + 4] not in _THIN_MAGICS:
            raise ValueError(f"Mach-O fat slice is not a 64-bit Mach-O image: {label}")
        ranges.append((offset, size))
    return ranges


def is_macho_bytes(data: bytes) -> bool:
    """Return whether bytes contain supported 64-bit thin or fat Mach-O images."""
    return data[:4] in {*_THIN_MAGICS, *_FAT_MAGICS}


def _command_string(
    data: bytes,
    *,
    command_offset: int,
    command_size: int,
    string_offset: int,
    label: str,
) -> str:
    start = command_offset + string_offset
    end = command_offset + command_size
    if string_offset < 8 or start >= end:
        raise ValueError(f"Mach-O load-command string offset is invalid: {label}")
    terminator = data.find(b"\0", start, end)
    if terminator < 0:
        raise ValueError(f"Mach-O load-command string is unterminated: {label}")
    try:
        value = data[start:terminator].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Mach-O load-command string is not UTF-8: {label}") from exc
    if not value:
        raise ValueError(f"Mach-O load-command string is empty: {label}")
    return value


def macho_dynamic_slices_bytes(data: bytes, *, label: str) -> list[dict[str, Any]]:
    """Return dependency-relevant load commands from every immutable Mach-O slice."""
    slices: list[dict[str, Any]] = []
    for slice_offset, slice_size in _slice_ranges(data, label=label):
        magic = data[slice_offset : slice_offset + 4]
        if slice_size < 32:
            raise ValueError(f"Mach-O header is truncated: {label}")
        byte_order = _THIN_MAGICS[magic]
        header = struct.unpack_from(f"{byte_order}8I", data, slice_offset)
        command_offset = slice_offset + 32
        command_limit = command_offset + header[5]
        if command_limit > slice_offset + slice_size:
            raise ValueError(f"Mach-O load commands exceed their slice: {label}")
        dylib_id: str | None = None
        if header[4] > _MAX_LOAD_COMMANDS or header[4] > header[5] // 8:
            raise ValueError(
                f"Mach-O load-command count exceeds its bounded command region: {label}"
            )
        dependencies: list[dict[str, str]] = []
        rpaths: list[str] = []
        for index in range(header[4]):
            if command_offset + 8 > command_limit:
                raise ValueError(f"Mach-O load-command header is truncated: {label}")
            command, command_size = struct.unpack_from(f"{byte_order}2I", data, command_offset)
            if (
                command_size < 8
                or command_size % 8 != 0
                or command_offset + command_size > command_limit
            ):
                raise ValueError(f"Mach-O load command is invalid: {label} command {index}")
            command_name = _DYLIB_COMMANDS.get(command)
            if command_name is not None:
                if command_size < 24:
                    raise ValueError(f"Mach-O dylib command is truncated: {label}")
                string_offset = struct.unpack_from(f"{byte_order}I", data, command_offset + 8)[0]
                if string_offset < 24:
                    raise ValueError(f"Mach-O dylib string offset is invalid: {label}")
                value = _command_string(
                    data,
                    command_offset=command_offset,
                    command_size=command_size,
                    string_offset=string_offset,
                    label=label,
                )
                if command_name == "LC_ID_DYLIB":
                    if dylib_id is not None:
                        raise ValueError(f"Mach-O contains multiple LC_ID_DYLIB commands: {label}")
                    dylib_id = value
                else:
                    dependencies.append({"command": command_name, "install_name": value})
            elif command == _LC_RPATH:
                if command_size < 12:
                    raise ValueError(f"Mach-O LC_RPATH is truncated: {label}")
                string_offset = struct.unpack_from(f"{byte_order}I", data, command_offset + 8)[0]
                if string_offset < 12:
                    raise ValueError(f"Mach-O LC_RPATH string offset is invalid: {label}")
                rpaths.append(
                    _command_string(
                        data,
                        command_offset=command_offset,
                        command_size=command_size,
                        string_offset=string_offset,
                        label=label,
                    )
                )
            elif command == _LC_DYLD_ENVIRONMENT:
                raise ValueError(f"embedded Mach-O contains LC_DYLD_ENVIRONMENT: {label}")
            command_offset += command_size
        if command_offset != command_limit:
            raise ValueError(f"Mach-O load-command byte count is inconsistent: {label}")
        slices.append(
            {
                "architecture": _CPU_ARCHITECTURES.get(header[1], f"cpu-{header[1]:08x}"),
                "file_type": header[3],
                "dylib_id": dylib_id,
                "rpaths": rpaths,
                "dependencies": dependencies,
            }
        )
    return slices


def _safe_bundle_path(value: str, *, label: str) -> PurePosixPath:
    if not value or "\0" in value or "\\" in value:
        raise ValueError(f"{label} is not a canonical bundle path: {value!r}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{label} is not a canonical bundle path: {value!r}")
    return path


def _join_contained(base: PurePosixPath, suffix: str, *, label: str) -> PurePosixPath:
    if not suffix or "\0" in suffix or "\\" in suffix or suffix.startswith("/"):
        raise ValueError(f"{label} is invalid: {suffix!r}")
    parts = list(base.parts)
    for part in PurePosixPath(suffix).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ValueError(f"{label} escapes TopoForge.app: {suffix!r}")
            parts.pop()
            continue
        if part.startswith("@") or ":" in part:
            raise ValueError(f"{label} is invalid: {suffix!r}")
        parts.append(part)
    if not parts:
        raise ValueError(f"{label} resolves outside TopoForge.app: {suffix!r}")
    return PurePosixPath(*parts)


def _is_apple_system_path(value: str) -> bool:
    if not any(
        value == prefix.rstrip("/") or value.startswith(prefix) for prefix in APPLE_SYSTEM_PREFIXES
    ):
        return False
    path = PurePosixPath(value)
    return path.is_absolute() and path.as_posix() == value and ".." not in path.parts


def _resolve_rpath(
    value: str,
    *,
    loader: PurePosixPath,
    executable: PurePosixPath,
    directories: set[str],
) -> tuple[str, str]:
    if value == "@loader_path":
        candidate = loader.parent
    elif value.startswith("@loader_path/"):
        candidate = _join_contained(
            loader.parent,
            value[len("@loader_path/") :],
            label="LC_RPATH @loader_path",
        )
    elif value == "@executable_path":
        candidate = executable.parent
    elif value.startswith("@executable_path/"):
        candidate = _join_contained(
            executable.parent,
            value[len("@executable_path/") :],
            label="LC_RPATH @executable_path",
        )
    elif value.startswith("/"):
        if not _is_apple_system_path(value):
            raise ValueError(f"Mach-O LC_RPATH references an external build path: {value}")
        return "apple-system", value
    else:
        raise ValueError(f"Mach-O LC_RPATH uses an unsupported token: {value}")
    resolved = candidate.as_posix()
    if resolved not in directories:
        raise ValueError(f"Mach-O LC_RPATH does not resolve to an app directory: {value}")
    return "app-bundle", resolved


def _resolve_install_name(
    value: str,
    *,
    loader: PurePosixPath,
    executable: PurePosixPath,
    rpaths: Sequence[str],
    macho_paths: set[str],
    directories: set[str],
    absolute_rewrites: Mapping[str, str] | None,
) -> tuple[str, str | None]:
    if value.startswith("/"):
        if _is_apple_system_path(value):
            return "apple-system", value
        absolute = PurePosixPath(value)
        if "\0" in value or "\\" in value or absolute.as_posix() != value or ".." in absolute.parts:
            raise ValueError(f"Mach-O references a noncanonical absolute path: {value}")
        if absolute_rewrites is None:
            raise ValueError(f"Mach-O references a non-system absolute path: {value}")
        candidate: PurePosixPath | None = None
        for source_prefix, bundle_prefix in sorted(
            absolute_rewrites.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if value.startswith(source_prefix):
                candidate = _join_contained(
                    _safe_bundle_path(bundle_prefix, label="absolute rewrite destination"),
                    value[len(source_prefix) :],
                    label="absolute Mach-O dependency",
                )
                break
        if candidate is None:
            raise ValueError(f"Mach-O references a non-system absolute path: {value}")
    elif value.startswith("@loader_path/"):
        candidate = _join_contained(
            loader.parent,
            value[len("@loader_path/") :],
            label="Mach-O @loader_path dependency",
        )
    elif value.startswith("@executable_path/"):
        candidate = _join_contained(
            executable.parent,
            value[len("@executable_path/") :],
            label="Mach-O @executable_path dependency",
        )
    elif value.startswith("@rpath/"):
        suffix = value[len("@rpath/") :]
        candidates: set[tuple[str, str | None]] = set()
        for rpath in rpaths:
            try:
                scope, directory = _resolve_rpath(
                    rpath,
                    loader=loader,
                    executable=executable,
                    directories=directories,
                )
            except ValueError:
                continue
            if scope == "apple-system":
                system_candidate = posixpath.normpath(f"{directory}/{suffix}")
                if _is_apple_system_path(system_candidate):
                    candidates.add(("apple-system", system_candidate))
                continue
            internal = _join_contained(
                _safe_bundle_path(directory, label="resolved LC_RPATH"),
                suffix,
                label="Mach-O @rpath dependency",
            ).as_posix()
            if internal in macho_paths:
                candidates.add(("app-bundle", internal))
        if len(candidates) != 1:
            raise ValueError(
                f"Mach-O @rpath dependency is unresolved or ambiguous: {loader} -> {value}"
            )
        return next(iter(candidates))
    else:
        raise ValueError(f"Mach-O dependency uses an unsupported install name: {value}")
    resolved = candidate.as_posix()
    if resolved not in macho_paths:
        raise ValueError(f"Mach-O dependency is missing from TopoForge.app: {loader} -> {value}")
    return "app-bundle", resolved


def _directories(paths: Sequence[str]) -> set[str]:
    directories: set[str] = set()
    for value in paths:
        path = _safe_bundle_path(value, label="bundle member")
        for index in range(1, len(path.parts)):
            directories.add(PurePosixPath(*path.parts[:index]).as_posix())
    return directories


def macho_closure_records(
    payloads: Mapping[str, bytes],
    *,
    executable_path: str,
    bundle_directories: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Verify every final dynamic dependency and return canonical per-file closure records."""
    executable = _safe_bundle_path(executable_path, label="Mach-O executable path")
    parsed: dict[str, dict[str, Any]] = {}
    for path_value, payload in sorted(payloads.items()):
        path = _safe_bundle_path(path_value, label="Mach-O path")
        slices = macho_dynamic_slices_bytes(payload, label=path_value)
        if len(slices) != 1 or slices[0]["architecture"] != "arm64":
            raise ValueError(f"Mach-O closure requires one arm64 slice: {path_value}")
        parsed[path.as_posix()] = slices[0]
    macho_paths = set(parsed)
    directories = _directories(list(macho_paths))
    if bundle_directories is not None:
        directories.update(bundle_directories)

    records: list[dict[str, Any]] = []
    for path_value, dynamic in sorted(parsed.items()):
        loader = PurePosixPath(path_value)
        rpaths = list(dynamic["rpaths"])
        for rpath in rpaths:
            _resolve_rpath(
                rpath,
                loader=loader,
                executable=executable,
                directories=directories,
            )
        dependencies: list[dict[str, Any]] = []
        for dependency in dynamic["dependencies"]:
            scope, resolved = _resolve_install_name(
                dependency["install_name"],
                loader=loader,
                executable=executable,
                rpaths=rpaths,
                macho_paths=macho_paths,
                directories=directories,
                absolute_rewrites=None,
            )
            dependencies.append(
                {
                    "command": dependency["command"],
                    "install_name": dependency["install_name"],
                    "scope": scope,
                    "resolved_path": resolved,
                }
            )
        dylib_id = dynamic["dylib_id"]
        if dylib_id is not None:
            scope, resolved = _resolve_install_name(
                dylib_id,
                loader=loader,
                executable=executable,
                rpaths=rpaths,
                macho_paths=macho_paths,
                directories=directories,
                absolute_rewrites=None,
            )
            if scope != "app-bundle" or resolved != path_value:
                raise ValueError(f"Mach-O LC_ID_DYLIB does not resolve to itself: {path_value}")
        records.append(
            {
                "path": path_value,
                "file_type": dynamic["file_type"],
                "dylib_id": dylib_id,
                "rpaths": rpaths,
                "dependencies": dependencies,
            }
        )
    return records


def _canonical_loader_name(loader: PurePosixPath, target: str) -> str:
    relative = posixpath.relpath(target, start=loader.parent.as_posix())
    if relative.startswith("/"):
        raise ValueError(f"canonical Mach-O dependency escaped the app: {target}")
    return f"@loader_path/{relative}"


def macho_rewrite_plans(
    payloads: Mapping[str, bytes],
    *,
    executable_path: str,
    absolute_rewrites: Mapping[str, str],
    bundle_directories: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Plan deterministic install-name normalization without consulting the host filesystem."""
    executable = _safe_bundle_path(executable_path, label="Mach-O executable path")
    parsed: dict[str, dict[str, Any]] = {}
    for path_value, payload in sorted(payloads.items()):
        path = _safe_bundle_path(path_value, label="Mach-O path")
        slices = macho_dynamic_slices_bytes(payload, label=path_value)
        if len(slices) != 1 or slices[0]["architecture"] != "arm64":
            raise ValueError(f"Mach-O rewrite requires one arm64 slice: {path_value}")
        parsed[path.as_posix()] = slices[0]
    macho_paths = set(parsed)
    directories = _directories(list(macho_paths))
    if bundle_directories is not None:
        directories.update(bundle_directories)

    plans: list[dict[str, Any]] = []
    for path_value, dynamic in sorted(parsed.items()):
        loader = PurePosixPath(path_value)
        changes_by_old: dict[str, str] = {}
        for dependency in dynamic["dependencies"]:
            scope, resolved = _resolve_install_name(
                dependency["install_name"],
                loader=loader,
                executable=executable,
                rpaths=dynamic["rpaths"],
                macho_paths=macho_paths,
                directories=directories,
                absolute_rewrites=absolute_rewrites,
            )
            if scope == "apple-system":
                if resolved is None:
                    raise AssertionError("Apple system dependency must have a resolved path")
                if dependency["install_name"] != resolved:
                    changes_by_old[dependency["install_name"]] = resolved
                continue
            if resolved is None:
                raise AssertionError("app-bundle dependency must have a resolved path")
            replacement = _canonical_loader_name(loader, resolved)
            previous = changes_by_old.setdefault(dependency["install_name"], replacement)
            if previous != replacement:
                raise ValueError(f"one install name resolves to multiple app files: {path_value}")
        dylib_id = dynamic["dylib_id"]
        canonical_id = None if dylib_id is None else f"@loader_path/{loader.name}"
        plans.append(
            {
                "path": path_value,
                "changes": [
                    {"old": old, "new": new}
                    for old, new in sorted(changes_by_old.items())
                    if old != new
                ],
                "dylib_id": canonical_id if dylib_id != canonical_id else None,
                "delete_rpaths": list(dynamic["rpaths"]),
            }
        )
    return plans


def macho_closure_summary(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Return a stable aggregate identity for canonical per-file closure records."""
    dependencies = [item for record in records for item in record["dependencies"]]
    closure_payload = [
        {
            "path": record["path"],
            "file_type": record["file_type"],
            "dylib_id": record["dylib_id"],
            "rpaths": record["rpaths"],
            "dependencies": record["dependencies"],
        }
        for record in records
    ]
    digest = hashlib.sha256(
        json.dumps(closure_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "dependency_count": len(dependencies),
        "bundled_dependency_count": sum(item["scope"] == "app-bundle" for item in dependencies),
        "apple_system_dependency_count": sum(
            item["scope"] == "apple-system" for item in dependencies
        ),
        "dylib_id_count": sum(record["dylib_id"] is not None for record in records),
        "rpath_count": sum(len(record["rpaths"]) for record in records),
        "closure_sha256": digest,
        "allowed_external_prefixes": list(APPLE_SYSTEM_PREFIXES),
        "required_checks_passed": True,
    }
