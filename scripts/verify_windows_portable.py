#!/usr/bin/env python3
"""Inspect and optionally execute a TopoForge Windows x64 portable archive."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import platform
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import IO, Any

if __package__:
    from scripts.build_windows_portable import (
        CLI_LAUNCHER,
        DEPENDENCY_RECORD_SCHEMA_VERSION,
        MANIFEST_SCHEMA_VERSION,
        RUNTIME_SITE_PACKAGES_SCHEMA_VERSION,
        WEB_LAUNCHER,
        _canonical_distribution_name,
        _load_config,
        _metadata_values,
        _projection_sha256,
        _register_windows_path,
        _safe_relative_path,
        _sha256,
    )
    from scripts.verify_platform_core import verify_platform_core
else:
    from build_windows_portable import (  # type: ignore[import-not-found]
        CLI_LAUNCHER,
        DEPENDENCY_RECORD_SCHEMA_VERSION,
        MANIFEST_SCHEMA_VERSION,
        RUNTIME_SITE_PACKAGES_SCHEMA_VERSION,
        WEB_LAUNCHER,
        _canonical_distribution_name,
        _load_config,
        _metadata_values,
        _projection_sha256,
        _register_windows_path,
        _safe_relative_path,
        _sha256,
    )
    from verify_platform_core import verify_platform_core  # type: ignore[import-not-found]

_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(_SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIRECTORY))
import windows_acceptance as _windows_evidence  # noqa: E402

WINDOWS_TARGETS = _windows_evidence.WINDOWS_TARGETS
WINDOWS_TARGET_IDS = _windows_evidence.WINDOWS_TARGET_IDS
evidence_sha256_file = _windows_evidence.sha256_file
source_repository_record = _windows_evidence.source_repository_record
windows_host_record = _windows_evidence.windows_host_record
windows_target_record = _windows_evidence.windows_target_record
write_canonical_json = _windows_evidence.write_canonical_json

VERIFICATION_SCHEMA_VERSION = "topoforge-windows-portable-verification-v2"
DEFAULT_CONFIG = Path("packaging/windows-x64-runtime.json")
PUBLIC_EVIDENCE_SCHEMA_VERSION = "topoforge-windows-public-evidence-v1"
PUBLIC_PRIVATE_FIELDS = frozenset(
    {
        "command",
        "commands",
        "cwd",
        "repository_root",
        "stderr",
        "stdout",
    }
)


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _read_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    maximum_bytes: int,
) -> bytes:
    if info.file_size > maximum_bytes:
        raise ValueError(f"archive member exceeds its read bound: {info.filename}")
    with archive.open(info) as source:
        payload = source.read(maximum_bytes + 1)
    if len(payload) != info.file_size or len(payload) > maximum_bytes:
        raise ValueError(f"archive member byte count changed: {info.filename}")
    return payload


def _member_sha256(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    digest = hashlib.sha256()
    received = 0
    with archive.open(info) as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            received += len(chunk)
            if received > info.file_size:
                raise ValueError(f"archive member exceeds its declared size: {info.filename}")
            digest.update(chunk)
    if received != info.file_size:
        raise ValueError(f"archive member size changed while hashing: {info.filename}")
    return digest.hexdigest()


def _stream_sha256(
    source: IO[bytes],
    *,
    expected_bytes: int,
    maximum_bytes: int,
    label: str,
) -> str:
    if expected_bytes > maximum_bytes:
        raise ValueError(f"{label} exceeds its expansion bound")
    digest = hashlib.sha256()
    received = 0
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        received += len(chunk)
        if received > expected_bytes or received > maximum_bytes:
            raise ValueError(f"{label} exceeds its declared size")
        digest.update(chunk)
    if received != expected_bytes:
        raise ValueError(f"{label} size changed while hashing")
    return digest.hexdigest()


def _file_handle_sha256(source: IO[bytes]) -> str:
    source.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
    source.seek(0)
    return digest.hexdigest()


def _expected_zip_timestamp(source_date_epoch: int) -> tuple[int, int, int, int, int, int]:
    value = time.gmtime(source_date_epoch)
    second = value.tm_sec - (value.tm_sec % 2)
    return (value.tm_year, value.tm_mon, value.tm_mday, value.tm_hour, value.tm_min, second)


def _validate_archive_members(
    archive: zipfile.ZipFile,
    config: dict[str, Any],
) -> tuple[dict[str, zipfile.ZipInfo], int]:
    bounds = _json_object(config["bounds"], "bounds")
    package_root = config["package_root"]
    infos = archive.infolist()
    if not infos:
        raise ValueError("portable archive is empty")
    if len(infos) > bounds["portable_member_count_max"]:
        raise ValueError("portable archive exceeds its member-count bound")
    if archive.comment:
        raise ValueError("portable archive must not contain a ZIP comment")

    expected_timestamp = _expected_zip_timestamp(config["source_date_epoch"])
    relative_infos: dict[str, zipfile.ZipInfo] = {}
    windows_paths: dict[str, tuple[str, str]] = {}
    uncompressed_bytes = 0
    for info in infos:
        if info.is_dir():
            raise ValueError(
                f"portable archive contains a non-canonical directory entry: {info.filename}"
            )
        path = _safe_relative_path(info.filename)
        if len(path.parts) < 2 or path.parts[0] != package_root:
            raise ValueError(f"portable archive member is outside {package_root}/: {info.filename}")
        relative = PurePosixPath(*path.parts[1:]).as_posix()
        relative_path = _safe_relative_path(relative)
        _register_windows_path(relative_path, windows_paths)
        if relative in relative_infos:
            raise ValueError(f"portable archive has a duplicate Windows path: {relative}")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK or info.flag_bits & 0x1:
            raise ValueError(f"portable archive contains an unsafe member: {info.filename}")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ValueError(f"portable archive uses unsupported compression: {info.filename}")
        if info.file_size > bounds["portable_member_max_bytes"]:
            raise ValueError(f"portable archive member exceeds its size bound: {relative}")
        if info.date_time != expected_timestamp:
            raise ValueError(f"portable archive timestamp is not reproducible: {relative}")
        if info.extra or info.comment:
            raise ValueError(f"portable archive member has non-canonical metadata: {relative}")
        uncompressed_bytes += info.file_size
        if uncompressed_bytes > bounds["portable_uncompressed_max_bytes"]:
            raise ValueError("portable archive exceeds its expansion bound")
        relative_infos[relative] = info
    return relative_infos, uncompressed_bytes


def _validate_manifest_files(
    archive: zipfile.ZipFile,
    relative_infos: dict[str, zipfile.ZipInfo],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    contents = _json_object(manifest.get("contents"), "manifest.contents")
    raw_files = contents.get("files")
    if not isinstance(raw_files, list):
        raise ValueError("portable manifest contents.files must be a list")
    listed_paths: list[str] = []
    listed_bytes = 0
    for index, raw in enumerate(raw_files):
        entry = _json_object(raw, f"manifest.contents.files[{index}]")
        path = entry.get("path")
        size = entry.get("bytes")
        digest = entry.get("sha256")
        if not isinstance(path, str):
            raise ValueError("portable manifest file path must be a string")
        _safe_relative_path(path)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError(f"portable manifest file size is invalid: {path}")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"portable manifest file digest is invalid: {path}")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError(f"portable manifest file digest is not hexadecimal: {path}") from exc
        info = relative_infos.get(path)
        if info is None:
            raise ValueError(f"portable manifest references a missing file: {path}")
        if info.file_size != size:
            raise ValueError(f"portable manifest byte count changed: {path}")
        if _member_sha256(archive, info) != digest:
            raise ValueError(f"portable manifest SHA-256 changed: {path}")
        listed_paths.append(path)
        listed_bytes += size

    if listed_paths != sorted(listed_paths) or len(set(listed_paths)) != len(listed_paths):
        raise ValueError("portable manifest file list is not sorted and unique")
    actual_paths = sorted(set(relative_infos) - {"manifest.json"})
    if listed_paths != actual_paths:
        missing = sorted(set(actual_paths) - set(listed_paths))
        extra = sorted(set(listed_paths) - set(actual_paths))
        raise ValueError(
            f"portable manifest does not cover the archive exactly; "
            f"missing={missing[:20]}, extra={extra[:20]}"
        )
    if contents.get("file_count") != len(listed_paths):
        raise ValueError("portable manifest file count changed")
    if contents.get("uncompressed_bytes") != listed_bytes:
        raise ValueError("portable manifest uncompressed byte count changed")
    return {
        "file_count": len(listed_paths),
        "payload_uncompressed_bytes": listed_bytes,
    }


def _validate_project_wheel_projection(
    archive: zipfile.ZipFile,
    relative_infos: dict[str, zipfile.ZipInfo],
    manifest: dict[str, Any],
    *,
    version: str,
    maximum_bytes: int,
) -> tuple[dict[str, Any], set[str]]:
    project = _json_object(manifest.get("project_wheel"), "manifest.project_wheel")
    wheel_path = project.get("path")
    if not isinstance(wheel_path, str):
        raise ValueError("portable manifest project wheel has no path")
    _safe_relative_path(wheel_path)
    wheel_info = relative_infos.get(wheel_path)
    if wheel_info is None:
        raise ValueError("portable project provenance wheel is missing")
    if project.get("bytes") != wheel_info.file_size:
        raise ValueError("portable project wheel byte count changed")
    if project.get("sha256") != _member_sha256(archive, wheel_info):
        raise ValueError("portable project wheel SHA-256 changed")
    wheel_payload = _read_member(archive, wheel_info, maximum_bytes=maximum_bytes)

    projected: set[str] = set()
    wheel_names: set[str] = set()
    windows_paths: dict[str, tuple[str, str]] = {}
    expanded_bytes = 0
    with zipfile.ZipFile(io.BytesIO(wheel_payload)) as wheel:
        if wheel.comment:
            raise ValueError("portable project wheel must not contain a ZIP comment")
        if len(wheel.infolist()) > 10_000:
            raise ValueError("portable project wheel exceeds its member-count bound")
        for info in wheel.infolist():
            raw_name = info.filename[:-1] if info.is_dir() else info.filename
            if not raw_name:
                raise ValueError("portable project wheel contains an empty member path")
            relative = _safe_relative_path(raw_name)
            _register_windows_path(relative, windows_paths, is_directory=info.is_dir())
            if info.is_dir():
                continue
            if info.filename in wheel_names:
                raise ValueError("portable project wheel has a duplicate member name")
            wheel_names.add(info.filename)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK or info.flag_bits & 0x1:
                raise ValueError(
                    f"portable project wheel contains an unsafe member: {info.filename}"
                )
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ValueError(
                    f"portable project wheel uses unsupported compression: {info.filename}"
                )
            expanded_bytes += info.file_size
            if expanded_bytes > maximum_bytes:
                raise ValueError("portable project wheel exceeds its expansion bound")
            destination = f"runtime/Lib/site-packages/{relative.as_posix()}"
            outer_info = relative_infos.get(destination)
            if outer_info is None:
                raise ValueError(
                    f"portable project wheel member was not installed: {info.filename}"
                )
            if outer_info.file_size != info.file_size:
                raise ValueError(f"portable installed project member size changed: {info.filename}")
            with wheel.open(info) as source:
                nested_digest = _stream_sha256(
                    source,
                    expected_bytes=info.file_size,
                    maximum_bytes=maximum_bytes,
                    label=f"portable project wheel member {info.filename}",
                )
            if _member_sha256(archive, outer_info) != nested_digest:
                raise ValueError(f"portable installed project member hash changed: {info.filename}")
            projected.add(destination)

    if project.get("extracted_member_count") != len(projected):
        raise ValueError("portable project wheel extracted member count changed")
    dist_info_prefix = f"runtime/Lib/site-packages/topoforge-{version}.dist-info/"
    installed_project = {
        path
        for path in relative_infos
        if path.startswith("runtime/Lib/site-packages/topoforge/")
        or path.startswith(dist_info_prefix)
    }
    if installed_project != projected:
        extra = sorted(installed_project - projected)
        missing = sorted(projected - installed_project)
        raise ValueError(
            f"portable installed project differs from its wheel; "
            f"extra={extra[:20]}, missing={missing[:20]}"
        )

    metadata_path = f"{dist_info_prefix}METADATA"
    metadata_info = relative_infos.get(metadata_path)
    if metadata_info is None:
        raise ValueError("portable installed project metadata is missing")
    expected_metadata = {
        "Name": "topoforge",
        "Version": version,
        "Requires-Python": "<3.15,>=3.11",
        "License-Expression": "Apache-2.0",
    }
    metadata = _metadata_values(
        _read_member(archive, metadata_info, maximum_bytes=1024 * 1024),
        context="portable installed project",
        required_fields=("Metadata-Version", *expected_metadata),
    )
    for field, expected in expected_metadata.items():
        if metadata[field] != expected:
            raise ValueError(
                f"portable installed metadata {field} is {metadata[field]!r}, expected {expected!r}"
            )
    return (
        {
            "path": wheel_path,
            "sha256": project["sha256"],
            "bytes": project["bytes"],
            "extracted_member_count": len(projected),
            "metadata": expected_metadata,
        },
        projected,
    )


def _validate_build_provenance(
    manifest: dict[str, Any],
    *,
    config_path: Path,
    build_constraints_path: Path,
) -> dict[str, Any]:
    """Verify the source and verifier identity embedded by the archive builder."""
    embedded = _json_object(manifest.get("build_provenance"), "manifest.build_provenance")
    source_commit = embedded.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit)
        or embedded.get("source_tracked_dirty") is not False
        or embedded.get("required_checks_passed") is not True
    ):
        raise ValueError("portable build provenance does not identify one clean source commit")

    repository_root = Path(__file__).resolve().parents[1]
    verifier_paths = {
        "builder": repository_root / "scripts" / "build_windows_portable.py",
        "portable": Path(__file__).resolve(),
        "system": repository_root / "scripts" / "verify_windows_system.py",
        "bambu": repository_root / "scripts" / "verify_windows_bambu.py",
        "helper": repository_root / "scripts" / "windows_acceptance.py",
    }
    verifier_hashes = _json_object(
        embedded.get("verifier_sha256"),
        "manifest.build_provenance.verifier_sha256",
    )
    if set(verifier_hashes) != set(verifier_paths):
        raise ValueError("portable build provenance verifier role set changed")
    for role, verifier_path in verifier_paths.items():
        expected_digest = verifier_hashes.get(role)
        if (
            not isinstance(expected_digest, str)
            or len(expected_digest) != 64
            or any(character not in "0123456789abcdef" for character in expected_digest)
            or evidence_sha256_file(verifier_path) != expected_digest
        ):
            raise ValueError(f"portable embedded {role} verifier SHA-256 changed")

    config_sha256 = embedded.get("config_sha256")
    build_constraints_sha256 = embedded.get("build_constraints_sha256")
    if config_sha256 != evidence_sha256_file(config_path):
        raise ValueError("portable embedded config SHA-256 changed")
    if build_constraints_sha256 != evidence_sha256_file(build_constraints_path):
        raise ValueError("portable embedded build constraints SHA-256 changed")
    return {
        "source_commit": source_commit,
        "source_dirty": False,
        "source_tracked_dirty": False,
        "config_sha256": config_sha256,
        "build_constraints_sha256": build_constraints_sha256,
        "verifier_sha256": verifier_hashes,
        "binding_scope": (
            "archive-embedded clean source, config, build-constraints, and five-script "
            "identity; execution additionally binds the archive SHA-256"
        ),
    }


def _sha256_value(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} is not a lowercase SHA-256 digest")
    return value


def _nonnegative_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} is not a non-negative integer")
    return value


def _locked_requirement_pins(requirements: bytes) -> dict[str, set[str]]:
    try:
        text = requirements.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("portable locked requirements are not UTF-8") from exc
    logical_lines: list[str] = []
    current = ""
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        current = f"{current} {stripped}".strip()
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        logical_lines.append(current)
        current = ""
    if current:
        raise ValueError("portable locked requirements end with an incomplete continuation")

    pins: dict[str, set[str]] = {}
    for line in logical_lines:
        if line.startswith("-"):
            continue
        first = line.split(maxsplit=1)[0]
        if "==" not in first:
            raise ValueError(f"portable locked requirement is not exactly pinned: {first}")
        name, version = first.split("==", 1)
        if not name or not version:
            raise ValueError(f"portable locked requirement is invalid: {first}")
        hashes = line.split("--hash=sha256:")[1:]
        if not hashes:
            raise ValueError(f"portable locked requirement has no SHA-256 hashes: {first}")
        for suffix in hashes:
            digest = suffix.split(maxsplit=1)[0].rstrip("\\")
            _sha256_value(digest, f"locked requirement {first} hash")
        canonical_name = _canonical_distribution_name(name)
        pins.setdefault(canonical_name, set()).add(version)
    if not pins:
        raise ValueError("portable locked requirements contain no exact package pins")
    return pins


def _validate_runtime_site_packages_projection(
    archive: zipfile.ZipFile,
    relative_infos: dict[str, zipfile.ZipInfo],
    baseline: dict[str, Any],
    *,
    maximum_files: int,
) -> tuple[set[str], list[dict[str, Any]]]:
    if baseline.get("schema_version") != RUNTIME_SITE_PACKAGES_SCHEMA_VERSION:
        raise ValueError("portable runtime site-packages projection schema is unsupported")
    raw_files = baseline.get("files")
    if not isinstance(raw_files, list) or len(raw_files) > maximum_files:
        raise ValueError("portable runtime site-packages projection exceeds its file-count bound")
    entries: list[dict[str, Any]] = []
    paths: set[str] = set()
    windows_paths: dict[str, tuple[str, str]] = {}
    for index, raw in enumerate(raw_files):
        entry = _json_object(raw, f"runtime_site_packages.files[{index}]")
        if set(entry) != {"path", "bytes", "sha256"}:
            raise ValueError("portable runtime site-packages entry fields changed")
        raw_path = entry.get("path")
        if not isinstance(raw_path, str):
            raise ValueError("portable runtime site-packages path is invalid")
        relative = _safe_relative_path(raw_path)
        _register_windows_path(relative, windows_paths)
        path = relative.as_posix()
        if path in paths:
            raise ValueError(f"portable runtime site-packages path is duplicated: {path}")
        size = _nonnegative_integer(
            entry.get("bytes"),
            f"runtime site-packages {path} bytes",
        )
        digest = _sha256_value(
            entry.get("sha256"),
            f"runtime site-packages {path} SHA-256",
        )
        outer_path = f"runtime/Lib/site-packages/{path}"
        info = relative_infos.get(outer_path)
        if info is None or info.file_size != size:
            raise ValueError(f"portable runtime site-packages file size changed: {path}")
        if _member_sha256(archive, info) != digest:
            raise ValueError(f"portable runtime site-packages file SHA-256 changed: {path}")
        paths.add(path)
        entries.append({"path": path, "bytes": size, "sha256": digest})
    if entries != sorted(entries, key=lambda item: (item["path"].casefold(), item["path"])):
        raise ValueError("portable runtime site-packages projection is not sorted")
    if baseline.get("file_count") != len(entries):
        raise ValueError("portable runtime site-packages projection count changed")
    if baseline.get("files_sha256") != _projection_sha256(entries):
        raise ValueError("portable runtime site-packages projection digest changed")
    return paths, entries


def _decode_record_sha256(value: str, path: str) -> str:
    prefix = "sha256="
    if not value.startswith(prefix):
        raise ValueError(f"portable dependency RECORD hash algorithm changed: {path}")
    encoded = value[len(prefix) :]
    try:
        payload = encoded.encode("ascii")
        padding = b"=" * ((4 - len(payload) % 4) % 4)
        digest = base64.b64decode(payload + padding, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError(f"portable dependency RECORD hash is invalid: {path}") from exc
    if len(digest) != 32:
        raise ValueError(f"portable dependency RECORD hash length changed: {path}")
    canonical = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    if encoded != canonical:
        raise ValueError(f"portable dependency RECORD hash is not canonical: {path}")
    return digest.hex()


def _validate_dependency_record(
    archive: zipfile.ZipFile,
    relative_infos: dict[str, zipfile.ZipInfo],
    package: dict[str, Any],
    *,
    maximum_record_bytes: int,
    maximum_files: int,
) -> tuple[dict[str, Any], set[str], list[dict[str, Any]]]:
    expected_fields = {
        "name",
        "version",
        "dist_info",
        "record_path",
        "record_sha256",
        "installed_file_count",
        "installed_bytes",
        "installed_files_sha256",
    }
    if set(package) != expected_fields:
        raise ValueError("portable dependency package projection fields changed")
    name = package.get("name")
    version = package.get("version")
    dist_info = package.get("dist_info")
    record_path = package.get("record_path")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(version, str)
        or not version
        or not isinstance(dist_info, str)
        or not dist_info.endswith(".dist-info")
        or not isinstance(record_path, str)
    ):
        raise ValueError("portable dependency package projection identity is invalid")
    dist_info_path = _safe_relative_path(dist_info)
    if len(dist_info_path.parts) != 1:
        raise ValueError("portable dependency dist-info path is not a directory name")
    expected_record_path = f"{dist_info}/RECORD"
    if record_path != expected_record_path:
        raise ValueError("portable dependency RECORD path differs from its dist-info")
    record_outer_path = f"runtime/Lib/site-packages/{record_path}"
    record_info = relative_infos.get(record_outer_path)
    if record_info is None:
        raise ValueError(f"portable dependency RECORD is missing: {record_path}")
    record_payload = _read_member(
        archive,
        record_info,
        maximum_bytes=maximum_record_bytes,
    )
    record_sha256 = _sha256_value(
        package.get("record_sha256"),
        f"portable dependency {name} RECORD SHA-256",
    )
    if hashlib.sha256(record_payload).hexdigest() != record_sha256:
        raise ValueError(f"portable dependency RECORD SHA-256 changed: {record_path}")
    try:
        decoded = record_payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"portable dependency RECORD is not UTF-8: {record_path}") from exc

    rows: list[tuple[str, str, str]] = []
    reader = csv.reader(io.StringIO(decoded, newline=""))
    for row in reader:
        if len(rows) >= maximum_files:
            raise ValueError(f"portable dependency RECORD exceeds its row bound: {record_path}")
        if len(row) != 3 or not row[0] or len(row[0]) > 1024:
            raise ValueError(f"portable dependency RECORD row is invalid: {record_path}")
        rows.append((row[0], row[1], row[2]))
    if not rows:
        raise ValueError(f"portable dependency RECORD is empty: {record_path}")
    if rows != sorted(rows, key=lambda row: (row[0].casefold(), row[0])):
        raise ValueError(f"portable dependency RECORD is not sorted: {record_path}")

    entries: list[dict[str, Any]] = []
    paths: set[str] = set()
    windows_paths: dict[str, tuple[str, str]] = {}
    self_count = 0
    for raw_path, hash_spec, raw_size in rows:
        relative = _safe_relative_path(raw_path)
        _register_windows_path(relative, windows_paths)
        path = relative.as_posix()
        if path in paths:
            raise ValueError(f"portable dependency RECORD path is duplicated: {path}")
        paths.add(path)
        outer_path = f"runtime/Lib/site-packages/{path}"
        info = relative_infos.get(outer_path)
        if info is None:
            raise ValueError(f"portable dependency RECORD member is missing: {path}")
        if path == record_path:
            self_count += 1
            if hash_spec or raw_size:
                raise ValueError(f"portable dependency RECORD self row changed: {record_path}")
            digest = _member_sha256(archive, info)
            size = info.file_size
        else:
            digest = _decode_record_sha256(hash_spec, path)
            if not raw_size.isdecimal():
                raise ValueError(f"portable dependency RECORD size is invalid: {path}")
            size = int(raw_size)
            if info.file_size != size:
                raise ValueError(f"portable dependency RECORD member size changed: {path}")
            if _member_sha256(archive, info) != digest:
                raise ValueError(f"portable dependency RECORD member SHA-256 changed: {path}")
        entries.append({"path": path, "bytes": size, "sha256": digest})
    if self_count != 1:
        raise ValueError(f"portable dependency RECORD must have one self row: {record_path}")
    metadata_path = f"{dist_info}/METADATA"
    if metadata_path not in paths:
        raise ValueError(f"portable dependency RECORD does not bind METADATA: {record_path}")
    metadata_info = relative_infos[f"runtime/Lib/site-packages/{metadata_path}"]
    metadata = _metadata_values(
        _read_member(archive, metadata_info, maximum_bytes=1024 * 1024),
        context=f"portable dependency {metadata_path}",
        required_fields=("Metadata-Version", "Name", "Version"),
    )
    if metadata["Name"] != name or metadata["Version"] != version:
        raise ValueError(f"portable dependency METADATA identity changed: {metadata_path}")

    entries.sort(key=lambda entry: (entry["path"].casefold(), entry["path"]))
    if package.get("installed_file_count") != len(entries):
        raise ValueError(f"portable dependency installed file count changed: {name}")
    if package.get("installed_bytes") != sum(int(entry["bytes"]) for entry in entries):
        raise ValueError(f"portable dependency installed byte count changed: {name}")
    if package.get("installed_files_sha256") != _projection_sha256(entries):
        raise ValueError(f"portable dependency installed projection changed: {name}")
    return (
        {
            "name": name,
            "version": version,
            "record_path": record_path,
            "record_sha256": record_sha256,
            "installed_file_count": len(entries),
            "installed_files_sha256": package["installed_files_sha256"],
        },
        paths,
        entries,
    )


def _validate_dependency_install_projection(
    archive: zipfile.ZipFile,
    relative_infos: dict[str, zipfile.ZipInfo],
    dependencies: dict[str, Any],
    requirements: bytes,
    *,
    project_paths: set[str],
    maximum_files: int,
    maximum_record_bytes: int,
) -> dict[str, Any]:
    raw_packages = dependencies.get("packages")
    if (
        not isinstance(raw_packages, list)
        or dependencies.get("count") != len(raw_packages)
        or len(raw_packages) > maximum_files
    ):
        raise ValueError("portable dependency inventory count changed")
    pins = _locked_requirement_pins(requirements)
    baseline = _json_object(
        dependencies.get("runtime_site_packages"),
        "manifest.locked_dependencies.runtime_site_packages",
    )
    baseline_paths, _baseline_entries = _validate_runtime_site_packages_projection(
        archive,
        relative_infos,
        baseline,
        maximum_files=maximum_files,
    )

    package_reports: list[dict[str, Any]] = []
    package_order: list[str] = []
    dependency_paths: set[str] = set()
    dependency_entries: list[dict[str, Any]] = []
    for raw in raw_packages:
        package = _json_object(raw, "manifest.locked_dependencies.packages entry")
        report, paths, entries = _validate_dependency_record(
            archive,
            relative_infos,
            package,
            maximum_record_bytes=maximum_record_bytes,
            maximum_files=maximum_files,
        )
        canonical_name = _canonical_distribution_name(str(report["name"]))
        if str(report["version"]) not in pins.get(canonical_name, set()):
            raise ValueError(
                f"portable installed dependency is absent from locked requirements: "
                f"{report['name']}=={report['version']}"
            )
        if canonical_name in package_order:
            raise ValueError(f"portable dependency inventory has a duplicate: {report['name']}")
        overlap = dependency_paths & paths
        if overlap:
            raise ValueError(
                f"portable dependency RECORD projections overlap: {sorted(overlap)[:20]}"
            )
        package_order.append(canonical_name)
        package_reports.append(report)
        dependency_paths.update(paths)
        dependency_entries.extend(entries)
        if len(dependency_paths) > maximum_files:
            raise ValueError("portable dependency projection exceeds its file-count bound")
    if package_order != sorted(package_order):
        raise ValueError("portable dependency inventory is not sorted by canonical name")

    dependency_entries.sort(key=lambda entry: (entry["path"].casefold(), entry["path"]))
    record_projection = _json_object(
        dependencies.get("record_projection"),
        "manifest.locked_dependencies.record_projection",
    )
    if record_projection.get("schema_version") != DEPENDENCY_RECORD_SCHEMA_VERSION:
        raise ValueError("portable dependency RECORD projection schema is unsupported")
    if record_projection.get("installed_file_count") != len(dependency_entries):
        raise ValueError("portable dependency RECORD projection file count changed")
    if record_projection.get("installed_bytes") != sum(
        int(entry["bytes"]) for entry in dependency_entries
    ):
        raise ValueError("portable dependency RECORD projection byte count changed")
    if record_projection.get("installed_files_sha256") != _projection_sha256(dependency_entries):
        raise ValueError("portable dependency RECORD projection digest changed")

    prefixed_baseline = {f"runtime/Lib/site-packages/{path}" for path in baseline_paths}
    prefixed_dependencies = {f"runtime/Lib/site-packages/{path}" for path in dependency_paths}
    if (
        prefixed_baseline & prefixed_dependencies
        or prefixed_baseline & project_paths
        or prefixed_dependencies & project_paths
    ):
        raise ValueError("portable site-packages projections overlap")
    expected_paths = prefixed_baseline | prefixed_dependencies | project_paths
    actual_paths = {
        path for path in relative_infos if path.startswith("runtime/Lib/site-packages/")
    }
    if actual_paths != expected_paths:
        extra = sorted(actual_paths - expected_paths)
        missing = sorted(expected_paths - actual_paths)
        raise ValueError(
            "portable site-packages differs from runtime, locked wheels, and project wheel; "
            f"extra={extra[:20]}, missing={missing[:20]}"
        )
    return {
        "dependency_count": len(package_reports),
        "runtime_baseline_file_count": len(baseline_paths),
        "installed_file_count": len(dependency_entries),
        "installed_files_sha256": record_projection["installed_files_sha256"],
        "packages": package_reports,
        "site_packages_exactly_covered": True,
    }


def _validate_provenance(
    archive: zipfile.ZipFile,
    relative_infos: dict[str, zipfile.ZipInfo],
    manifest: dict[str, Any],
    config_path: Path,
    *,
    project_paths: set[str],
) -> dict[str, Any]:
    dependencies = _json_object(manifest.get("locked_dependencies"), "manifest.locked_dependencies")
    requirements_path = dependencies.get("requirements_path")
    uv_lock_path = dependencies.get("uv_lock_path")
    build_constraints_path = dependencies.get("build_constraints_path")
    if (
        not isinstance(requirements_path, str)
        or not isinstance(uv_lock_path, str)
        or not isinstance(build_constraints_path, str)
    ):
        raise ValueError("portable dependency/build provenance paths are missing")
    config = _load_config(config_path)
    runtime_config = _json_object(config["python_runtime"], "python_runtime")
    bounds = _json_object(config["bounds"], "bounds")
    provider_license_path = f"provenance/{runtime_config['provider_license_file']}"
    requirements_info = relative_infos.get(requirements_path)
    uv_lock_info = relative_infos.get(uv_lock_path)
    build_constraints_info = relative_infos.get(build_constraints_path)
    config_relative = f"provenance/{config_path.name}"
    config_info = relative_infos.get(config_relative)
    provider_license_info = relative_infos.get(provider_license_path)
    if any(
        info is None
        for info in (
            requirements_info,
            uv_lock_info,
            build_constraints_info,
            config_info,
            provider_license_info,
        )
    ):
        raise ValueError("portable dependency/runtime provenance files are incomplete")
    assert requirements_info is not None
    assert uv_lock_info is not None
    assert build_constraints_info is not None
    assert config_info is not None
    assert provider_license_info is not None

    requirements = _read_member(archive, requirements_info, maximum_bytes=4 * 1024 * 1024)
    if b"--hash=sha256:" not in requirements:
        raise ValueError("portable locked requirements contain no package hashes")
    if hashlib.sha256(requirements).hexdigest() != dependencies.get("requirements_sha256"):
        raise ValueError("portable locked requirements SHA-256 changed")
    uv_lock = _read_member(archive, uv_lock_info, maximum_bytes=4 * 1024 * 1024)
    if hashlib.sha256(uv_lock).hexdigest() != dependencies.get("uv_lock_sha256"):
        raise ValueError("portable uv.lock SHA-256 changed")
    build_constraints = _read_member(
        archive,
        build_constraints_info,
        maximum_bytes=1024 * 1024,
    )
    if hashlib.sha256(build_constraints).hexdigest() != dependencies.get(
        "build_constraints_sha256"
    ):
        raise ValueError("portable build constraints SHA-256 changed")
    expected_build_constraints = (
        Path(__file__).resolve().parents[1] / "packaging" / "build-constraints.txt"
    )
    if not expected_build_constraints.is_file():
        raise ValueError("verifier build constraints are missing")
    if build_constraints != expected_build_constraints.read_bytes():
        raise ValueError("portable build constraints differ from the verifier source")
    build_provenance = _validate_build_provenance(
        manifest,
        config_path=config_path,
        build_constraints_path=expected_build_constraints,
    )
    if build_provenance["build_constraints_sha256"] != dependencies.get("build_constraints_sha256"):
        raise ValueError("portable build provenance and dependency constraints differ")
    bundled_config = _read_member(archive, config_info, maximum_bytes=4 * 1024 * 1024)
    if bundled_config != config_path.read_bytes():
        raise ValueError("portable bundled runtime config differs from the verifier config")
    provider_license = _read_member(archive, provider_license_info, maximum_bytes=1024 * 1024)
    expected_provider_license = (
        config_path.parent / runtime_config["provider_license_file"]
    ).read_bytes()
    if provider_license != expected_provider_license:
        raise ValueError("portable runtime provider license differs from the source license")

    dependency_projection = _validate_dependency_install_projection(
        archive,
        relative_infos,
        dependencies,
        requirements,
        project_paths=project_paths,
        maximum_files=bounds["portable_member_count_max"],
        maximum_record_bytes=bounds["manifest_max_bytes"],
    )

    extension_count = sum(
        1
        for path in relative_infos
        if path.startswith("runtime/Lib/site-packages/") and path.casefold().endswith(".pyd")
    )
    if dependencies.get("compiled_extension_count") != extension_count:
        raise ValueError("portable compiled extension count changed")
    forbidden = [
        path
        for path in relative_infos
        if path.startswith("runtime/Lib/site-packages/")
        and path.casefold().endswith((".so", ".dylib"))
    ]
    if forbidden:
        raise ValueError(f"portable archive contains non-Windows binaries: {forbidden[:20]}")
    return {
        **build_provenance,
        **dependency_projection,
        "compiled_extension_count": extension_count,
        "requirements_sha256": dependencies["requirements_sha256"],
        "uv_lock_sha256": dependencies["uv_lock_sha256"],
        "build_constraints_sha256": dependencies["build_constraints_sha256"],
    }


def inspect_windows_portable(
    archive_path: Path,
    *,
    config_path: Path,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Strictly inspect one portable archive without requiring Windows."""
    archive_file = archive_path.resolve()
    config_file = config_path.resolve()
    config = _load_config(config_file)
    bounds = _json_object(config["bounds"], "bounds")
    runtime_config = _json_object(config["python_runtime"], "python_runtime")
    if not archive_file.is_file():
        raise FileNotFoundError(f"portable archive does not exist: {archive_file}")
    archive_bytes = archive_file.stat().st_size
    if archive_bytes > bounds["portable_archive_max_bytes"]:
        raise ValueError("portable archive exceeds its configured size bound")

    required = {
        "DATA_LICENSES.md",
        "LICENSE",
        "README-Windows.txt",
        "THIRD_PARTY_NOTICES.md",
        "TopoForge-Web.cmd",
        "manifest.json",
        "provenance/build-constraints.txt",
        "provenance/locked-runtime-requirements.txt",
        f"provenance/{runtime_config['provider_license_file']}",
        f"provenance/{config_file.name}",
        "provenance/uv.lock",
        "runtime/LICENSE.txt",
        "runtime/python.exe",
        "runtime/python312.dll",
        "runtime/pythonw.exe",
        "runtime/Lib/site-packages/topoforge/__init__.py",
        "runtime/Lib/site-packages/topoforge/web/static/asset-manifest.json",
        "runtime/Lib/site-packages/topoforge/web/static/index.html",
        "topoforge.cmd",
    }
    with zipfile.ZipFile(archive_file) as archive:
        relative_infos, uncompressed_bytes = _validate_archive_members(archive, config)
        missing = sorted(required - set(relative_infos))
        if missing:
            raise ValueError(f"portable archive is missing required files: {missing}")
        manifest_info = relative_infos["manifest.json"]
        manifest = _json_object(
            json.loads(
                _read_member(
                    archive,
                    manifest_info,
                    maximum_bytes=bounds["manifest_max_bytes"],
                ).decode("utf-8")
            ),
            "manifest",
        )
        if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise ValueError("portable manifest schema is unsupported")
        if manifest.get("package_role") != "phase12-windows-x64-portable-candidate":
            raise ValueError("portable manifest package role changed")
        if manifest.get("required_checks_passed") is not True:
            raise ValueError("portable manifest required checks did not pass")
        if manifest.get("target") != config["target"]:
            raise ValueError("portable manifest target differs from the pinned config")
        if manifest.get("source_date_epoch") != config["source_date_epoch"]:
            raise ValueError("portable manifest source-date epoch changed")
        version = manifest.get("topoforge_version")
        if not isinstance(version, str) or not version:
            raise ValueError("portable manifest has no TopoForge version")
        if expected_version is not None and version != expected_version:
            raise ValueError(
                f"portable TopoForge version is {version}, expected {expected_version}"
            )
        expected_name = f"topoforge-{version}-windows-x64-portable.zip"
        if archive_file.name != expected_name:
            raise ValueError(
                f"portable archive filename is {archive_file.name}, expected {expected_name}"
            )

        runtime = _json_object(manifest.get("python_runtime"), "manifest.python_runtime")
        for field, expected in runtime_config.items():
            if runtime.get(field) != expected:
                raise ValueError(f"portable Python runtime field changed: {field}")
        launcher_expectations = {
            "topoforge.cmd": CLI_LAUNCHER.encode("utf-8"),
            "TopoForge-Web.cmd": WEB_LAUNCHER.encode("utf-8"),
        }
        for path, expected in launcher_expectations.items():
            actual = _read_member(archive, relative_infos[path], maximum_bytes=16 * 1024)
            if actual != expected:
                raise ValueError(f"portable launcher contract changed: {path}")

        contents = _validate_manifest_files(archive, relative_infos, manifest)
        project = _json_object(manifest.get("project_wheel"), "manifest.project_wheel")
        project_path = project.get("path")
        if isinstance(project_path, str):
            required.add(project_path)
        project_report, project_paths = _validate_project_wheel_projection(
            archive,
            relative_infos,
            manifest,
            version=version,
            maximum_bytes=min(bounds["portable_member_max_bytes"], 64 * 1024 * 1024),
        )
        provenance = _validate_provenance(
            archive,
            relative_infos,
            manifest,
            config_file,
            project_paths=project_paths,
        )

    if not required <= set(relative_infos):
        raise ValueError("portable archive required-file set changed during inspection")
    return {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "archive": {
            "path": str(archive_file),
            "sha256": _sha256(archive_file),
            "bytes": archive_bytes,
            "member_count": len(relative_infos),
            "uncompressed_bytes": uncompressed_bytes,
        },
        "topoforge_version": version,
        "target": config["target"],
        "runtime": {
            "implementation": runtime["implementation"],
            "version": runtime["version"],
            "provider": runtime["provider"],
            "provider_release": runtime["provider_release"],
            "archive_sha256": runtime["sha256"],
        },
        "contents": contents,
        "project_wheel": project_report,
        "provenance": provenance,
        "launchers": {
            "cli": "topoforge.cmd",
            "web": "TopoForge-Web.cmd",
            "isolated_python": True,
        },
        "cross_host_inspection_passed": True,
        "required_checks_passed": True,
    }


def _extract_verified_archive(
    archive_path: Path,
    destination: Path,
    *,
    package_root: str,
    expected_sha256: str,
    expected_bytes: int,
) -> Path:
    with archive_path.open("rb") as archive_handle:
        opened_bytes = os.fstat(archive_handle.fileno()).st_size
        if opened_bytes != expected_bytes:
            raise ValueError(
                f"portable archive byte count changed before extraction: "
                f"{opened_bytes} != {expected_bytes}"
            )
        if _file_handle_sha256(archive_handle) != expected_sha256:
            raise ValueError("portable archive SHA-256 changed before extraction")

        destination.mkdir(parents=True, exist_ok=False)
        with zipfile.ZipFile(archive_handle) as archive:
            for info in archive.infolist():
                path = _safe_relative_path(info.filename)
                target = destination / Path(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                received = 0
                with archive.open(info) as source, target.open("xb") as output:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        received += len(chunk)
                        if received > info.file_size:
                            raise ValueError(
                                f"portable member exceeds its declared size: {info.filename}"
                            )
                        output.write(chunk)
                if received != info.file_size:
                    raise ValueError(f"portable member size changed: {info.filename}")

        if _file_handle_sha256(archive_handle) != expected_sha256:
            raise ValueError("portable archive SHA-256 changed during extraction")
    extracted = destination / package_root
    if not extracted.is_dir():
        raise ValueError("portable extraction did not produce its package root")
    return extracted


def _windows_batch_command(
    path: Path,
    arguments: list[str],
    *,
    cwd: Path,
) -> list[str]:
    """Return discrete arguments for a verified package-root batch launcher."""
    resolved_path = path.resolve()
    if resolved_path.parent != cwd.resolve():
        raise ValueError("portable batch launcher must be invoked from its package root")
    return [str(resolved_path), *arguments]


def _run_json(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    shell: bool = False,
    timeout_seconds: float = 600.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        shell=shell,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
    )
    record: dict[str, Any] = {
        "command": command,
        "cwd": str(cwd),
        "shell": shell,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    if completed.returncode != 0:
        raise RuntimeError(
            f"portable command failed with exit code {completed.returncode}: {command}\n"
            f"{completed.stderr or completed.stdout}"
        )
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError(f"portable command did not emit JSON: {command}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"portable command emitted a non-object: {command}")
    return payload, record


def _nested_binding_contract(
    report: dict[str, Any],
    *,
    binding: dict[str, Any],
    role: str,
    target_id: str | None,
    windows_target: dict[str, Any],
) -> None:
    nested = _json_object(report.get("candidate_binding"), f"{role}.candidate_binding")
    source = _json_object(binding["source_repository"], "binding.source_repository")
    archive = _json_object(binding["archive"], "binding.archive")
    verifier_hashes = _json_object(binding["verifier_sha256"], "binding.verifier_sha256")
    binding_sha256 = hashlib.sha256(
        (json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()
    expected = {
        "binding_sha256": binding_sha256,
        "archive_sha256": archive["sha256"],
        "source_commit": source["commit"],
        "source_tracked_dirty": False,
        "config_sha256": binding["config_sha256"],
        "build_constraints_sha256": binding["build_constraints_sha256"],
        "verifier_role": role,
        "verifier_sha256": verifier_hashes[role],
        "required_checks_passed": True,
    }
    for key, value in expected.items():
        if nested.get(key) != value:
            raise RuntimeError(f"{role} nested candidate binding differs at {key}")
    if report.get("expected_target") != target_id:
        raise RuntimeError(f"{role} nested expected target differs from portable acceptance")
    target = _json_object(report.get("windows_target"), f"{role}.windows_target")
    if target.get("target_id") != target_id:
        raise RuntimeError(f"{role} nested Windows target identity changed")
    for key in (
        "product_name",
        "display_version",
        "current_build_number",
        "ubr",
        "installation_type",
        "process_machine_code",
        "process_machine",
        "native_machine_code",
        "native_machine",
        "native_x64_verified",
        "target_verified",
    ):
        if target.get(key) != windows_target.get(key):
            raise RuntimeError(f"{role} nested Windows target differs at {key}")
    if target_id is not None and target.get("target_verified") is not True:
        raise RuntimeError(f"{role} nested clean-client target verification did not pass")


def _windows_containment_contract(
    report: dict[str, Any],
    *,
    binding: dict[str, Any],
) -> dict[str, Any]:
    containment = _json_object(
        report.get("windows_process_containment"),
        "system.windows_process_containment",
    )
    source_binding = _json_object(
        containment.get("source_binding"),
        "system.windows_process_containment.source_binding",
    )
    source = _json_object(binding.get("source_repository"), "binding.source_repository")
    verifier_hashes = _json_object(binding.get("verifier_sha256"), "binding.verifier_sha256")
    binding_sha256 = hashlib.sha256(
        (json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()
    expected_root = {
        "platform": "Windows",
        "executed": True,
        "containment_entrypoint": ("topoforge.web.processes.enable_current_process_containment"),
        "job_object_kill_on_close_verified": True,
        "production_cancellation_verified": True,
        "required_checks_passed": True,
    }
    for key, value in expected_root.items():
        if containment.get(key) != value:
            raise RuntimeError(f"Windows containment evidence differs at {key}")
    probe_code_sha256 = containment.get("probe_code_sha256")
    if (
        not isinstance(probe_code_sha256, str)
        or len(probe_code_sha256) != 64
        or any(character not in "0123456789abcdef" for character in probe_code_sha256)
    ):
        raise RuntimeError("Windows containment probe code SHA-256 is invalid")
    expected_source = {
        "candidate_bound": True,
        "candidate_binding_sha256": binding_sha256,
        "source_commit": source["commit"],
        "system_verifier_sha256": verifier_hashes["system"],
        "system_verifier_matches_candidate": True,
        "required_checks_passed": True,
    }
    for key, value in expected_source.items():
        if source_binding.get(key) != value:
            raise RuntimeError(f"Windows containment source binding differs at {key}")

    modes = (
        (
            "leader_exit",
            "leader-exit",
            {
                "leader_exit_code": 0,
                "leader_alive_after_exit": False,
                "child_alive_after_exit": False,
                "kill_on_job_close_verified": True,
            },
        ),
        (
            "cancellation",
            "cancel",
            {
                "leader_alive_after_cancel": False,
                "child_alive_after_cancel": False,
                "production_termination_adapter_exercised": True,
            },
        ),
    )
    for field, expected_mode, expected in modes:
        mode = _json_object(
            containment.get(field),
            f"system.windows_process_containment.{field}",
        )
        for integer_field in ("leader_pid", "leader_process_group_id", "child_pid"):
            value = mode.get(integer_field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise RuntimeError(f"Windows containment {field}.{integer_field} is invalid")
        if (
            mode["leader_pid"] != mode["leader_process_group_id"]
            or mode["leader_pid"] == mode["child_pid"]
        ):
            raise RuntimeError(f"Windows containment {field} process IDs are invalid")
        for identity_field in (
            "leader_process_identity",
            "child_process_identity",
        ):
            identity = mode.get(identity_field)
            if not isinstance(identity, str) or not identity.startswith("windows:"):
                raise RuntimeError(f"Windows containment {field}.{identity_field} is invalid")
        if (
            mode.get("mode") != expected_mode
            or mode.get("containment_enabled") is not True
            or mode.get("required_checks_passed") is not True
        ):
            raise RuntimeError(f"Windows containment {field} did not pass")
        for key, value in expected.items():
            if mode.get(key) != value:
                raise RuntimeError(f"Windows containment {field} differs at {key}")
        exit_code = mode.get("leader_exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise RuntimeError(f"Windows containment {field} leader exit code is invalid")
    return containment


def _profile_hash_arguments(
    *,
    content_identity_sha256: str | None,
    machine_sha256: str | None,
    process_sha256: str | None,
    filament_sha256: str | None,
    required: bool,
) -> dict[str, str | None]:
    raw = {
        "content_identity": content_identity_sha256,
        "machine": machine_sha256,
        "process": process_sha256,
        "filament": filament_sha256,
    }
    normalized: dict[str, str | None] = {}
    for kind, value in raw.items():
        if value is None:
            normalized[kind] = None
            continue
        digest = value.strip().casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise RuntimeError(f"expected Bambu {kind} profile SHA-256 is invalid")
        normalized[kind] = digest
    frozen = all(value is not None for value in normalized.values())
    partial = any(value is not None for value in normalized.values()) and not frozen
    if partial or (required and not frozen):
        raise RuntimeError(
            "official Bambu acceptance requires the frozen profile content identity plus "
            "machine/process/filament resolved SHA-256 values; the path-bearing local "
            "manifest SHA-256 is observed evidence, not a cross-machine expectation"
        )
    return normalized


def _bambu_profile_binding_contract(
    report: dict[str, Any],
    *,
    expected_hashes: dict[str, str | None],
) -> dict[str, Any]:
    studio = _json_object(report.get("bambu_studio"), "bambu.bambu_studio")
    binding = _json_object(
        studio.get("profiles_root_binding"),
        "bambu.bambu_studio.profiles_root_binding",
    )
    if (
        binding.get("required_checks_passed") is not True
        or binding.get("is_executable_sibling") is not True
        or binding.get("relative_to_executable") != "resources/profiles/BBL"
        or binding.get("profile_identity_frozen") is not True
        or binding.get("profile_content_identity_sha256") != expected_hashes["content_identity"]
        or binding.get("expected_profile_content_identity_sha256")
        != expected_hashes["content_identity"]
        or binding.get("profile_content_identity_sha256_matched") is not True
        or binding.get("expected_resolved_profile_sha256")
        != {kind: expected_hashes[kind] for kind in ("machine", "process", "filament")}
    ):
        raise RuntimeError("nested Bambu frozen profile-root identity did not pass")
    selection_mode = binding.get("selection_mode")
    if selection_mode not in {
        "executable-sibling-discovery",
        "explicit-cli-override",
        "environment-override",
    }:
        raise RuntimeError("nested Bambu profile-root selection mode is invalid")
    override_requested = selection_mode != "executable-sibling-discovery"
    if binding.get("override_requested") is not override_requested or (
        override_requested and binding.get("override_authorized_by_frozen_hashes") is not True
    ):
        raise RuntimeError("nested Bambu profile override authorization did not pass")
    for field in (
        "profile_manifest_sha256",
        "profile_content_identity_sha256",
        "source_records_sha256",
        "source_root_identity_sha256",
    ):
        digest = binding.get(field)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RuntimeError(f"nested Bambu {field} is invalid")
    source_records = _json_object(
        binding.get("source_records"),
        "bambu.bambu_studio.profiles_root_binding.source_records",
    )
    resolved_profiles = _json_object(
        binding.get("resolved_profiles"),
        "bambu.bambu_studio.profiles_root_binding.resolved_profiles",
    )
    if set(source_records) != {"machine", "process", "filament"}:
        raise RuntimeError("nested Bambu source profile kind set is invalid")
    if set(resolved_profiles) != {"machine", "process", "filament"}:
        raise RuntimeError("nested Bambu resolved profile kind set is invalid")
    for kind in ("machine", "process", "filament"):
        sources = source_records.get(kind)
        resolved = _json_object(
            resolved_profiles.get(kind),
            f"bambu.bambu_studio.profiles_root_binding.resolved_profiles.{kind}",
        )
        if not isinstance(sources, list) or not sources:
            raise RuntimeError(f"nested Bambu {kind} source records are empty")
        if (
            resolved.get("sha256") != expected_hashes[kind]
            or resolved.get("expected_sha256") != expected_hashes[kind]
            or resolved.get("sha256_matched") is not True
            or resolved.get("source_count") != len(sources)
        ):
            raise RuntimeError(f"nested Bambu {kind} frozen profile identity changed")
    bundle = _json_object(report.get("profile_bundle"), "bambu.profile_bundle")
    manifest = _json_object(bundle.get("manifest"), "bambu.profile_bundle.manifest")
    if (
        bundle.get("profile_identity_frozen") is not True
        or bundle.get("source_records_sha256") != binding["source_records_sha256"]
        or bundle.get("profile_content_identity_sha256") != expected_hashes["content_identity"]
        or bundle.get("expected_profile_content_identity_sha256")
        != expected_hashes["content_identity"]
        or bundle.get("profile_content_identity_sha256_matched") is not True
        or manifest.get("sha256") != binding["profile_manifest_sha256"]
        or bundle.get("required_checks_passed") is not True
    ):
        raise RuntimeError("nested Bambu profile bundle projection changed")
    return binding


def _candidate_binding(
    *,
    inspection: dict[str, Any],
    config_path: Path,
    repository_root: Path,
    expected_target: str | None,
    expected_source_commit: str | None,
) -> dict[str, Any]:
    source = source_repository_record(
        repository_root,
        expected_commit=expected_source_commit,
        require_clean=True,
    )
    archive = _json_object(inspection.get("archive"), "inspection.archive")
    provenance = _json_object(inspection.get("provenance"), "inspection.provenance")
    verifier_hashes = _json_object(
        provenance.get("verifier_sha256"),
        "inspection.provenance.verifier_sha256",
    )
    if provenance.get("source_commit") != source["commit"]:
        raise RuntimeError("portable inspection source commit changed before execution")
    if provenance.get("config_sha256") != evidence_sha256_file(config_path):
        raise RuntimeError("portable inspection config SHA-256 changed before execution")
    return {
        "schema_version": "topoforge-windows-candidate-binding-v1",
        "topoforge_version": inspection["topoforge_version"],
        "expected_target": expected_target or "hosted-server",
        "target_id": None if expected_target is None else WINDOWS_TARGET_IDS[expected_target],
        "archive": {"sha256": archive["sha256"], "bytes": archive["bytes"]},
        "source_repository": source,
        "config_path": str(config_path),
        "config_sha256": evidence_sha256_file(config_path),
        "build_constraints_sha256": provenance["build_constraints_sha256"],
        "project_wheel_sha256": inspection["project_wheel"]["sha256"],
        "verifier_sha256": verifier_hashes,
        "required_checks_passed": True,
    }


def execute_windows_portable(
    archive_path: Path,
    *,
    config_path: Path,
    expected_version: str,
    work_root: Path,
    expected_target: str | None = None,
    expected_source_commit: str | None = None,
    hosted_server: bool = False,
    browser_mode: str = "skip",
    verify_bambu: bool = False,
    bambu_studio_executable: Path | None = None,
    bambu_profiles_root: Path | None = None,
    expected_publisher_subjects: tuple[str, ...] = (),
    expected_certificate_thumbprints: tuple[str, ...] = (),
    expected_profile_content_identity_sha256: str | None = None,
    expected_machine_profile_sha256: str | None = None,
    expected_process_profile_sha256: str | None = None,
    expected_filament_profile_sha256: str | None = None,
) -> dict[str, Any]:
    """Extract and execute one source/archive-bound Windows candidate."""
    if platform.system() != "Windows":
        raise RuntimeError(
            "--execute requires native Windows x64; run cross-host inspection without this flag"
        )
    if platform.machine().casefold() not in {"amd64", "x86_64"}:
        raise RuntimeError("--execute requires an x64 Windows host")
    if expected_target is None and not hosted_server:
        raise RuntimeError("--execute requires --expected-target or explicit --hosted-server")
    if expected_target is not None and hosted_server:
        raise RuntimeError("--expected-target cannot be combined with --hosted-server")
    if expected_target is not None and expected_source_commit is None:
        raise RuntimeError("clean-client execution requires --expected-source-commit")
    if browser_mode not in {"skip", "require"}:
        raise ValueError("browser_mode must be 'skip' or 'require'")
    if hosted_server and browser_mode != "skip":
        raise RuntimeError("hosted Server acceptance must use --browser-mode skip")
    if expected_target is not None and browser_mode != "require":
        raise RuntimeError("clean-client execution requires --browser-mode require")
    if verify_bambu and expected_target is None:
        raise RuntimeError("official Bambu acceptance requires one clean --expected-target")
    if verify_bambu and (
        len(expected_publisher_subjects) != 1 or len(expected_certificate_thumbprints) != 1
    ):
        raise RuntimeError(
            "official Bambu acceptance requires exactly one frozen publisher subject and "
            "certificate thumbprint"
        )
    expected_profile_hashes = _profile_hash_arguments(
        content_identity_sha256=expected_profile_content_identity_sha256,
        machine_sha256=expected_machine_profile_sha256,
        process_sha256=expected_process_profile_sha256,
        filament_sha256=expected_filament_profile_sha256,
        required=verify_bambu,
    )
    if not verify_bambu and any(value is not None for value in expected_profile_hashes.values()):
        raise RuntimeError("Bambu profile hash expectations require --verify-bambu")

    resolved_archive = archive_path.resolve()
    resolved_config = config_path.resolve()
    repository_root = Path(__file__).resolve().parents[1]
    inspection = inspect_windows_portable(
        resolved_archive,
        config_path=resolved_config,
        expected_version=expected_version,
    )
    archive_record = _json_object(inspection.get("archive"), "inspection.archive")
    archive_sha256 = archive_record.get("sha256")
    archive_bytes = archive_record.get("bytes")
    if not isinstance(archive_sha256, str) or not isinstance(archive_bytes, int):
        raise ValueError("portable inspection archive identity is incomplete")
    windows_target = (
        windows_host_record(require_windows=True)
        if expected_target is None
        else windows_target_record(expected_target, require_windows=True)
    )
    target_id = windows_target.get("target_id")

    resolved_work_root = work_root.expanduser().resolve()
    resolved_work_root.mkdir(parents=True, exist_ok=False)
    binding = _candidate_binding(
        inspection=inspection,
        config_path=resolved_config,
        repository_root=repository_root,
        expected_target=expected_target,
        expected_source_commit=expected_source_commit,
    )
    binding_path = resolved_work_root / "candidate-binding.json"
    write_canonical_json(binding_path, binding)
    binding_record = {
        "path": str(binding_path),
        "sha256": evidence_sha256_file(binding_path),
        "archive_sha256": archive_sha256,
        "source_commit": binding["source_repository"]["commit"],
        "source_tracked_dirty": False,
        "config_sha256": binding["config_sha256"],
        "build_constraints_sha256": binding["build_constraints_sha256"],
        "verifier_sha256": binding["verifier_sha256"],
        "expected_target": target_id,
        "required_checks_passed": True,
    }

    config = _load_config(resolved_config)
    extraction_parent = resolved_work_root / "portable path with spaces" / "地形"
    extraction_parent.parent.mkdir(parents=True, exist_ok=True)
    package_root = _extract_verified_archive(
        resolved_archive,
        extraction_parent,
        package_root=config["package_root"],
        expected_sha256=archive_sha256,
        expected_bytes=archive_bytes,
    )
    python = package_root / "runtime" / "python.exe"
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
            "PYTHONUTF8": "1",
        }
    )
    commands: list[dict[str, Any]] = []

    doctor, doctor_record = _run_json(
        _windows_batch_command(package_root / "topoforge.cmd", ["doctor"], cwd=package_root),
        cwd=package_root,
        environment=environment,
        shell=True,
    )
    commands.append(doctor_record)
    if doctor.get("topoforge") != expected_version:
        raise ValueError("portable CLI launcher reported the wrong TopoForge version")

    web_root = resolved_work_root / "launcher web check"
    input_root = web_root / "input"
    input_root.mkdir(parents=True)
    web, web_record = _run_json(
        _windows_batch_command(
            package_root / "TopoForge-Web.cmd",
            [
                "--check",
                "--state-dir",
                str(web_root / "state"),
                "--workspace-root",
                str(web_root / "workspaces"),
                "--input-root",
                str(input_root),
                "--no-open",
            ],
            cwd=package_root,
        ),
        cwd=package_root,
        environment=environment,
        shell=True,
    )
    commands.append(web_record)
    if web.get("required_checks_passed") is not True:
        raise ValueError("portable Web launcher installation check did not pass")

    core = verify_platform_core(
        resolved_work_root / "full core acceptance",
        python_executable=python,
    )
    if core.get("required_checks_passed") is not True:
        raise ValueError("portable full core acceptance did not pass")

    system_report_path = resolved_work_root / "windows system acceptance.json"
    system_command = [
        str(python),
        "-I",
        "-X",
        "utf8",
        str(repository_root / "scripts" / "verify_windows_system.py"),
        "--work-root",
        str(resolved_work_root / "full Web system acceptance"),
        "--require-windows",
        "--web-launcher",
        str(package_root / "TopoForge-Web.cmd"),
        "--browser-mode",
        browser_mode,
        "--candidate-binding",
        str(binding_path),
        "--report",
        str(system_report_path),
    ]
    if expected_target is None:
        system_command.append("--hosted-server")
    else:
        system_command.extend(("--expected-target", expected_target))
    system, system_record = _run_json(
        system_command,
        cwd=package_root,
        environment=environment,
        timeout_seconds=600.0,
    )
    commands.append(system_record)
    if system.get("required_checks_passed") is not True:
        raise ValueError("portable native real-HTTP Web acceptance did not pass")
    _nested_binding_contract(
        system,
        binding=binding,
        role="system",
        target_id=target_id,
        windows_target=windows_target,
    )
    windows_process_containment = _windows_containment_contract(
        system,
        binding=binding,
    )
    real_http = _json_object(system.get("real_http_web"), "system.real_http_web")
    if (
        real_http.get("required_checks_passed") is not True
        or real_http.get("shutdown", {}).get("port_closed") is not True
        or real_http.get("download", {}).get("sha256")
        != real_http.get("job", {}).get("model_3mf_sha256")
    ):
        raise ValueError("portable real HTTP launch/job/download/shutdown contract did not pass")
    browser = _json_object(real_http.get("browser"), "system.real_http_web.browser")
    if browser.get("mode") != browser_mode:
        raise ValueError("portable browser evidence mode changed in nested acceptance")
    if browser_mode == "require" and browser.get("opened") is not True:
        raise ValueError("portable clean-VM default browser launch did not pass")

    bambu: dict[str, Any] | None = None
    if verify_bambu:
        bambu_report_path = resolved_work_root / "windows official Bambu acceptance.json"
        bambu_command = [
            str(python),
            "-I",
            "-X",
            "utf8",
            str(repository_root / "scripts" / "verify_windows_bambu.py"),
            "--work-root",
            str(resolved_work_root / "full official Bambu acceptance"),
            "--require-windows",
            "--expected-target",
            str(expected_target),
            "--candidate-binding",
            str(binding_path),
            "--report",
            str(bambu_report_path),
        ]
        if bambu_studio_executable is not None:
            bambu_command.extend(
                ("--bambu-studio-executable", str(bambu_studio_executable.resolve()))
            )
        if bambu_profiles_root is not None:
            bambu_command.extend(("--bambu-profiles-root", str(bambu_profiles_root.resolve())))
        for subject in expected_publisher_subjects:
            bambu_command.extend(("--expected-publisher-subject", subject))
        for thumbprint in expected_certificate_thumbprints:
            bambu_command.extend(("--expected-certificate-thumbprint", thumbprint))
        for flag, kind in (
            ("--expected-profile-content-identity-sha256", "content_identity"),
            ("--expected-machine-profile-sha256", "machine"),
            ("--expected-process-profile-sha256", "process"),
            ("--expected-filament-profile-sha256", "filament"),
        ):
            expected_hash = expected_profile_hashes[kind]
            if expected_hash is None:
                raise RuntimeError("required Bambu profile hash is missing")
            bambu_command.extend((flag, expected_hash))
        bambu, bambu_record = _run_json(
            bambu_command,
            cwd=package_root,
            environment=environment,
            timeout_seconds=3600.0,
        )
        commands.append(bambu_record)
        if bambu.get("required_checks_passed") is not True:
            raise ValueError("portable authenticated official Bambu acceptance did not pass")
        _nested_binding_contract(
            bambu,
            binding=binding,
            role="bambu",
            target_id=target_id,
            windows_target=windows_target,
        )
        signature = _json_object(
            bambu.get("bambu_studio", {}).get("authenticode"),
            "bambu.bambu_studio.authenticode",
        )
        if (
            signature.get("status") != "Valid"
            or signature.get("operator_identity_frozen") is not True
            or signature.get("required_checks_passed") is not True
        ):
            raise ValueError("portable Bambu Authenticode signer gate did not pass")
        _bambu_profile_binding_contract(
            bambu,
            expected_hashes=expected_profile_hashes,
        )
        project = _json_object(bambu.get("official_project"), "bambu.official_project")
        if (
            project.get("all_projects_reopened") is not True
            or project.get("all_release_gates_passed") is not True
            or project.get("external_profiles_loaded_on_reopen") is not False
        ):
            raise ValueError("portable official Bambu project/reopen contract did not pass")

    if evidence_sha256_file(resolved_archive) != archive_sha256:
        raise RuntimeError("portable archive SHA-256 changed during native acceptance")
    if evidence_sha256_file(resolved_config) != binding["config_sha256"]:
        raise RuntimeError("portable verifier config changed during native acceptance")
    if (
        evidence_sha256_file(repository_root / "packaging" / "build-constraints.txt")
        != binding["build_constraints_sha256"]
    ):
        raise RuntimeError("portable build constraints changed during native acceptance")
    for role, expected_hash in binding["verifier_sha256"].items():
        verifier_path = {
            "builder": repository_root / "scripts" / "build_windows_portable.py",
            "portable": repository_root / "scripts" / "verify_windows_portable.py",
            "system": repository_root / "scripts" / "verify_windows_system.py",
            "bambu": repository_root / "scripts" / "verify_windows_bambu.py",
            "helper": repository_root / "scripts" / "windows_acceptance.py",
        }[role]
        if evidence_sha256_file(verifier_path) != expected_hash:
            raise RuntimeError(f"{role} verifier changed during native acceptance")

    return {
        "expected_target": target_id,
        "windows_target": windows_target,
        "hosted_server": hosted_server,
        "evidence_scope": ("hosted-server-non-release" if hosted_server else "clean-client-target"),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "target_id": target_id,
        },
        "candidate_binding": binding_record,
        "extraction_path": str(package_root),
        "archive_sha256": archive_sha256,
        "archive_sha256_verified_before_after_and_at_completion": True,
        "path_contains_spaces": " " in str(package_root),
        "path_contains_non_ascii": any(ord(character) > 127 for character in str(package_root)),
        "cli_launcher": doctor,
        "web_launcher_installation_check": web,
        "real_http_web": real_http,
        "windows_process_containment": windows_process_containment,
        "core": core,
        "system": system,
        "bambu": bambu,
        "commands": commands,
        "claim_boundary": (
            "target_verified=false is hosted/non-release evidence; browser mode skip never proves "
            "default-browser launch; clean support requires separate matching Win10 and "
            "Win11 reports"
        ),
        "required_checks_passed": True,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    write_canonical_json(path, report)


def _replace_case_insensitive(value: str, needle: str, replacement: str) -> str:
    if not needle:
        return value
    result = value
    start = 0
    while True:
        index = result.casefold().find(needle.casefold(), start)
        if index < 0:
            return result
        result = result[:index] + replacement + result[index + len(needle) :]
        start = index + len(replacement)


def _private_evidence_roots(
    archive: Path,
    *,
    work_root: Path | None,
    bambu_studio_executable: Path | None,
    bambu_profiles_root: Path | None,
    environment: Mapping[str, str],
) -> dict[str, Path]:
    roots = {
        "candidate_archive": archive.expanduser().resolve().parent,
        "repository": Path(__file__).resolve().parents[1],
    }
    if work_root is not None:
        roots["work_root"] = work_root
    if bambu_studio_executable is not None:
        roots["bambu_studio_install_root"] = bambu_studio_executable.expanduser().resolve().parent
    if bambu_profiles_root is not None:
        roots["bambu_profiles_root"] = bambu_profiles_root
    for environment_name, label in (
        ("USERPROFILE", "user_profile"),
        ("LOCALAPPDATA", "local_app_data"),
        ("APPDATA", "roaming_app_data"),
        ("TEMP", "environment_temp"),
        ("TMP", "environment_tmp"),
    ):
        raw_root = environment.get(environment_name)
        if raw_root:
            roots[label] = Path(raw_root)
    return roots


def _public_evidence_projection(
    report: dict[str, Any],
    *,
    private_roots: dict[str, Path],
) -> dict[str, Any]:
    """Return a release-safe evidence projection without operator paths or command logs."""
    replacements: list[tuple[str, str, str]] = []
    seen_roots: set[str] = set()
    for label, raw_root in private_roots.items():
        root = str(raw_root.expanduser().resolve())
        normalized = root.replace("\\", "/").rstrip("/")
        if not normalized or normalized == "/" or (len(normalized) == 2 and normalized[1] == ":"):
            continue
        identity = normalized.casefold()
        if identity in seen_roots:
            continue
        seen_roots.add(identity)
        replacement = f"C:/TopoForge Public Evidence/{label}"
        for variant in {root.rstrip("/\\"), normalized, normalized.replace("/", "\\")}:
            if variant:
                replacements.append((variant, replacement, label))
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    removed_fields: set[str] = set()

    def project(value: Any) -> Any:
        if isinstance(value, dict):
            projected: dict[str, Any] = {}
            for key, item in value.items():
                if key in PUBLIC_PRIVATE_FIELDS:
                    removed_fields.add(key)
                    continue
                projected[key] = project(item)
            return projected
        if isinstance(value, list):
            return [project(item) for item in value]
        if isinstance(value, str):
            projected_value = value
            for source, replacement, _ in replacements:
                projected_value = _replace_case_insensitive(
                    projected_value,
                    source,
                    replacement,
                )
            return (
                projected_value.replace("\\", "/") if projected_value != value else projected_value
            )
        return value

    projected_report = project(report)
    if not isinstance(projected_report, dict):
        raise AssertionError("public evidence projection root changed type")
    private_bytes = (
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    projected_report["public_evidence_projection"] = {
        "schema_version": PUBLIC_EVIDENCE_SCHEMA_VERSION,
        "private_report_sha256": hashlib.sha256(private_bytes).hexdigest(),
        "removed_fields": sorted(removed_fields),
        "redacted_root_labels": sorted({item[2] for item in replacements}),
        "required_checks_passed": True,
    }
    return projected_report


def main() -> int:
    """Inspect, compare, and optionally execute a Windows portable candidate."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--repeat-archive", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--expected-version")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-target", choices=WINDOWS_TARGETS)
    parser.add_argument("--expected-source-commit")
    parser.add_argument("--hosted-server", action="store_true")
    parser.add_argument("--browser-mode", choices=("skip", "require"), default="skip")
    parser.add_argument("--verify-bambu", action="store_true")
    parser.add_argument("--bambu-studio-executable", type=Path)
    parser.add_argument("--bambu-profiles-root", type=Path)
    parser.add_argument("--expected-publisher-subject", action="append", default=[])
    parser.add_argument("--expected-certificate-thumbprint", action="append", default=[])
    parser.add_argument("--expected-profile-content-identity-sha256")
    parser.add_argument("--expected-machine-profile-sha256")
    parser.add_argument("--expected-process-profile-sha256")
    parser.add_argument("--expected-filament-profile-sha256")
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--public-report", type=Path)
    args = parser.parse_args()
    report_path = args.report.resolve()
    public_report_path = args.public_report.resolve() if args.public_report is not None else None
    private_roots = _private_evidence_roots(
        args.archive,
        work_root=args.work_root,
        bambu_studio_executable=args.bambu_studio_executable,
        bambu_profiles_root=args.bambu_profiles_root,
        environment=os.environ,
    )
    try:
        if public_report_path == report_path:
            raise ValueError("--public-report must differ from the private --report path")
        if public_report_path is not None and args.expected_target is None:
            raise ValueError("--public-report is only valid for one clean --expected-target run")
        if args.verify_bambu and not args.execute:
            raise ValueError("--verify-bambu requires --execute")
        if args.execute and args.expected_target is None and not args.hosted_server:
            raise ValueError("--execute requires --expected-target or explicit --hosted-server")
        if args.expected_target is not None and args.hosted_server:
            raise ValueError("--expected-target cannot be combined with --hosted-server")
        if args.expected_target is not None:
            commit = args.expected_source_commit or ""
            if len(commit) != 40 or any(
                character not in "0123456789abcdefABCDEF" for character in commit
            ):
                raise ValueError(
                    "clean --expected-target requires a 40-hex --expected-source-commit"
                )
        if args.hosted_server and args.browser_mode != "skip":
            raise ValueError("--hosted-server requires --browser-mode skip")
        if args.expected_target is not None and args.browser_mode != "require":
            raise ValueError("clean --expected-target requires --browser-mode require")
        if args.browser_mode == "require" and args.work_root is None:
            raise ValueError("--browser-mode require needs --work-root to retain browser evidence")
        if args.verify_bambu and args.work_root is None:
            raise ValueError("--verify-bambu requires --work-root to retain native evidence")
        if args.verify_bambu and args.expected_target is None:
            raise ValueError("--verify-bambu requires one clean --expected-target")
        if args.verify_bambu and (
            len(args.expected_publisher_subject) != 1
            or len(args.expected_certificate_thumbprint) != 1
        ):
            raise ValueError(
                "--verify-bambu requires exactly one --expected-publisher-subject and "
                "exactly one --expected-certificate-thumbprint"
            )
        _profile_hash_arguments(
            content_identity_sha256=args.expected_profile_content_identity_sha256,
            machine_sha256=args.expected_machine_profile_sha256,
            process_sha256=args.expected_process_profile_sha256,
            filament_sha256=args.expected_filament_profile_sha256,
            required=args.verify_bambu,
        )
        if not args.verify_bambu and (
            args.bambu_studio_executable is not None
            or args.bambu_profiles_root is not None
            or args.expected_publisher_subject
            or args.expected_certificate_thumbprint
            or args.expected_profile_content_identity_sha256 is not None
            or args.expected_machine_profile_sha256 is not None
            or args.expected_process_profile_sha256 is not None
            or args.expected_filament_profile_sha256 is not None
        ):
            raise ValueError(
                "Bambu overrides, signer expectations, and profile hashes require --verify-bambu"
            )
        inspection = inspect_windows_portable(
            args.archive,
            config_path=args.config,
            expected_version=args.expected_version,
        )
        version = inspection["topoforge_version"]
        reproducible: dict[str, Any] | None = None
        if args.repeat_archive is not None:
            repeat = inspect_windows_portable(
                args.repeat_archive,
                config_path=args.config,
                expected_version=version,
            )
            matches = inspection["archive"]["sha256"] == repeat["archive"]["sha256"]
            if not matches:
                raise ValueError("Windows portable archives are not byte reproducible")
            reproducible = {
                "primary_sha256": inspection["archive"]["sha256"],
                "repeat_sha256": repeat["archive"]["sha256"],
                "byte_reproducible": True,
            }

        execution: dict[str, Any] | None = None
        if args.execute:
            if args.work_root is not None:
                execution = execute_windows_portable(
                    args.archive,
                    config_path=args.config,
                    expected_version=version,
                    work_root=args.work_root,
                    expected_target=args.expected_target,
                    expected_source_commit=args.expected_source_commit,
                    hosted_server=args.hosted_server,
                    browser_mode=args.browser_mode,
                    verify_bambu=args.verify_bambu,
                    bambu_studio_executable=args.bambu_studio_executable,
                    bambu_profiles_root=args.bambu_profiles_root,
                    expected_publisher_subjects=tuple(args.expected_publisher_subject),
                    expected_certificate_thumbprints=tuple(args.expected_certificate_thumbprint),
                    expected_profile_content_identity_sha256=(
                        args.expected_profile_content_identity_sha256
                    ),
                    expected_machine_profile_sha256=(args.expected_machine_profile_sha256),
                    expected_process_profile_sha256=(args.expected_process_profile_sha256),
                    expected_filament_profile_sha256=(args.expected_filament_profile_sha256),
                )
            else:
                with tempfile.TemporaryDirectory(
                    prefix="topoforge-windows-portable-acceptance-"
                ) as temporary:
                    execution = execute_windows_portable(
                        args.archive,
                        config_path=args.config,
                        expected_version=version,
                        work_root=Path(temporary) / "acceptance",
                        expected_target=args.expected_target,
                        expected_source_commit=args.expected_source_commit,
                        hosted_server=args.hosted_server,
                        browser_mode=args.browser_mode,
                    )
        report = {
            **inspection,
            "reproducibility": reproducible,
            "execution": execution,
            "required_checks_passed": True,
        }
    except Exception as exc:
        failure = {
            "schema_version": VERIFICATION_SCHEMA_VERSION,
            "archive": str(args.archive),
            "error": {"type": exc.__class__.__name__, "message": str(exc)},
            "required_checks_passed": False,
        }
        _write_report(report_path, failure)
        if public_report_path is not None:
            _write_report(
                public_report_path,
                _public_evidence_projection(failure, private_roots=private_roots),
            )
        raise
    _write_report(report_path, report)
    if public_report_path is not None:
        _write_report(
            public_report_path,
            _public_evidence_projection(report, private_roots=private_roots),
        )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
