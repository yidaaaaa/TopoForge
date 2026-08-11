#!/usr/bin/env python3
"""Verify TopoForge source/wheel archives and an installed CLI smoke build."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import unicodedata
import zipfile
from configparser import ConfigParser
from configparser import Error as ConfigParserError
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any

MIB = 1024 * 1024
SDIST_ARCHIVE_MAX_BYTES = 64 * MIB
WHEEL_ARCHIVE_MAX_BYTES = 64 * MIB
ARCHIVE_MEMBER_COUNT_MAX = 4_096
ARCHIVE_MEMBER_MAX_BYTES = 32 * MIB
ARCHIVE_EXPANDED_MAX_BYTES = 256 * MIB
WINDOWS_INVALID_COMPONENT_CHARACTERS = frozenset('<>:"|?*')
WINDOWS_RESERVED_NAMES = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
        *(f"com{number}" for number in "¹²³"),
        *(f"lpt{number}" for number in "¹²³"),
    }
)


class _ExactEntryPointConfigParser(ConfigParser):
    """Parse entry-point names without ConfigParser's case folding."""

    optionxform = staticmethod(str)  # type: ignore[assignment]


REQUIRED_SDIST_FILES = frozenset(
    {
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        ".github/workflows/windows-clean-release-evidence.yml",
        "DATA_LICENSES.md",
        "LICENSE",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "benchmarks/baseline.json",
        "docs/release.md",
        "docs/windows-support.md",
        "packaging/MPL-2.0.txt",
        "packaging/bambu-studio-windows-identity-policy.json",
        "packaging/build-constraints.txt",
        "packaging/release-evidence.schema.json",
        "packaging/windows-x64-runtime.json",
        "pyproject.toml",
        "reference_regions/catalog.yaml",
        "scripts/build_windows_portable.py",
        "scripts/resolve_bambu_profiles.py",
        "scripts/run_benchmarks.py",
        "scripts/run_playwright_server.py",
        "scripts/verify_phase11_lifecycle.py",
        "scripts/verify_platform_core.py",
        "scripts/verify_public_tree.py",
        "scripts/verify_reference_regions.py",
        "scripts/verify_release.py",
        "scripts/verify_release_evidence.py",
        "scripts/verify_release_rollback.py",
        "scripts/verify_windows_bambu.py",
        "scripts/verify_windows_portable.py",
        "scripts/verify_windows_system.py",
        "scripts/windows_acceptance.py",
        "src/topoforge/__init__.py",
        "src/topoforge/cli/app.py",
        "src/topoforge/engine/build.py",
        "src/topoforge/exporters/three_mf.py",
        "src/topoforge/mesh/terrain.py",
        "src/topoforge/overlays/geometry.py",
        "src/topoforge/platforms.py",
        "src/topoforge/provenance/writer.py",
        "src/topoforge/raster/processing.py",
        "src/topoforge/tiling/extract.py",
        "src/topoforge/util/atomic.py",
        "src/topoforge/validation/bambu_projects.py",
        "src/topoforge/validation/manufacturing.py",
        "src/topoforge/validation/slicers/_bambu_profiles.py",
        "src/topoforge/validation/slicers/_bambu_windows.py",
        "src/topoforge/validation/slicers/bambu.py",
        "src/topoforge/validation/slicers/base.py",
        "src/topoforge/web/api.py",
        "src/topoforge/web/jobs.py",
        "src/topoforge/web/map_tiles.py",
        "src/topoforge/web/models.py",
        "src/topoforge/web/processes.py",
        "src/topoforge/web/security.py",
        "src/topoforge/web/static/asset-manifest.json",
        "src/topoforge/web/static/index.html",
        "src/topoforge/web/worker.py",
        "src/topoforge/workflow/maintenance.py",
        "src/topoforge/workflow/local.py",
        "src/topoforge/workflow/ux.py",
        "tests/cli/test_tiling_cli.py",
        "tests/cli/test_windows_bambu.py",
        "tests/integration/test_aoi_clipping.py",
        "tests/integration/test_bundle_integrity.py",
        "tests/integration/test_completed_workflow_verifier.py",
        "tests/integration/test_geographic_crs_aoi.py",
        "tests/integration/test_local_build.py",
        "tests/integration/test_orientation.py",
        "tests/integration/test_tile_extraction.py",
        "tests/integration/test_workflow_maintenance.py",
        "tests/release/test_phase8_contracts.py",
        "tests/release/test_public_tree.py",
        "tests/release/test_release_evidence.py",
        "tests/release/test_windows_bambu_acceptance.py",
        "tests/release/test_windows_portable.py",
        "tests/release/test_windows_system.py",
        "tests/slicer/test_adapters.py",
        "tests/slicer/test_bambu_windows.py",
        "tests/unit/test_bambu_profile_resolver.py",
        "tests/unit/test_bambu_profiles.py",
        "tests/unit/test_atomic_io.py",
        "tests/unit/test_manufacturing_gate.py",
        "tests/unit/test_platform_paths.py",
        "tests/unit/test_platforms.py",
        "tests/unit/test_provenance_writer.py",
        "tests/unit/test_synthetic_raster.py",
        "tests/web/test_api.py",
        "tests/web/test_jobs.py",
        "tests/web/test_map_tiles.py",
        "tests/web/test_processes.py",
        "uv.lock",
        "web/package-lock.json",
        "web/package.json",
        "web/playwright.config.ts",
        "web/src/App.test.tsx",
        "web/src/App.tsx",
        "web/src/api.ts",
        "web/src/components/AssemblyPanel.test.tsx",
        "web/src/components/AssemblyPanel.tsx",
        "web/src/components/MapPanel.test.ts",
        "web/src/components/MapPanel.tsx",
        "web/src/components/ResultsPanel.tsx",
        "web/src/components/TerrainPreview.test.ts",
        "web/src/components/TerrainPreview.tsx",
        "web/src/types.ts",
        "web/tests/workspace.spec.ts",
    }
)


def sha256_file(path: Path) -> str:
    """Return a streaming SHA-256 digest."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_archive_size(path: Path, *, maximum_bytes: int, label: str) -> int:
    if not path.is_file():
        raise FileNotFoundError(f"{label} does not exist: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise ValueError(f"{label} is empty: {path}")
    if size > maximum_bytes:
        raise ValueError(f"{label} is {size} bytes, above the {maximum_bytes}-byte archive bound")
    return size


def _canonical_archive_member(name: str, *, is_directory: bool) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise ValueError(f"archive member is not a canonical relative path: {name!r}")
    raw_name = name[:-1] if is_directory and name.endswith("/") else name
    path = PurePosixPath(raw_name)
    if (
        not raw_name
        or path.is_absolute()
        or path.as_posix() != raw_name
        or "." in path.parts
        or ".." in path.parts
    ):
        raise ValueError(f"archive member is not a canonical relative path: {name!r}")
    for part in path.parts:
        normalized = unicodedata.normalize("NFC", part)
        reserved_stem = part.split(".", 1)[0].casefold()
        if (
            normalized != part
            or part.endswith((" ", "."))
            or any(unicodedata.category(character).startswith("C") for character in part)
            or any(character in WINDOWS_INVALID_COMPONENT_CHARACTERS for character in part)
            or reserved_stem in WINDOWS_RESERVED_NAMES
        ):
            raise ValueError(f"archive member has an unsafe platform component: {name!r}")
    return path


def _archive_collision_key(value: str) -> str:
    return unicodedata.normalize("NFC", value.casefold())


def _register_archive_member(
    path: PurePosixPath,
    *,
    is_directory: bool,
    explicit_paths: dict[str, str],
    path_kinds: dict[str, tuple[str, str]],
) -> None:
    rendered = path.as_posix()
    folded = _archive_collision_key(rendered)
    previous_explicit = explicit_paths.get(folded)
    if previous_explicit is not None:
        if previous_explicit == rendered:
            raise ValueError(f"archive contains a duplicate member: {rendered}")
        raise ValueError(f"archive members collide by case: {previous_explicit!r} and {rendered!r}")
    explicit_paths[folded] = rendered

    for length in range(1, len(path.parts) + 1):
        prefix = PurePosixPath(*path.parts[:length]).as_posix()
        key = _archive_collision_key(prefix)
        kind = "directory" if length < len(path.parts) or is_directory else "file"
        previous = path_kinds.get(key)
        if previous is None:
            path_kinds[key] = (prefix, kind)
            continue
        previous_path, previous_kind = previous
        if previous_path != prefix:
            raise ValueError(f"archive paths collide by case: {previous_path!r} and {prefix!r}")
        if previous_kind != kind:
            raise ValueError(f"archive path is both a file and directory: {prefix!r}")


def _read_tar_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
) -> None:
    source = archive.extractfile(member)
    if source is None:
        raise ValueError(f"sdist member is unreadable: {member.name}")
    received = 0
    with source:
        while True:
            chunk = source.read(min(MIB, member.size - received + 1))
            if not chunk:
                break
            received += len(chunk)
            if received > member.size:
                raise ValueError(f"sdist member exceeds its declared size: {member.name}")
    if received != member.size:
        raise ValueError(
            f"sdist member byte count changed: {member.name} ({received} != {member.size})"
        )


def _inspect_sdist_members(
    archive: tarfile.TarFile,
    *,
    expected_root: str,
) -> tuple[set[str], int, int]:
    files: set[str] = set()
    explicit_paths: dict[str, str] = {}
    path_kinds: dict[str, tuple[str, str]] = {}
    member_count = 0
    expanded_bytes = 0
    for member in archive:
        member_count += 1
        if member_count > ARCHIVE_MEMBER_COUNT_MAX:
            raise ValueError(
                f"sdist has more than {ARCHIVE_MEMBER_COUNT_MAX} members, above the "
                f"{ARCHIVE_MEMBER_COUNT_MAX}-member bound"
            )
        is_directory = member.isdir()
        if not member.isfile() and not is_directory:
            raise ValueError(
                "sdist contains a non-regular member "
                f"(links, devices, and FIFOs are forbidden): {member.name}"
            )
        path = _canonical_archive_member(member.name, is_directory=is_directory)
        _register_archive_member(
            path,
            is_directory=is_directory,
            explicit_paths=explicit_paths,
            path_kinds=path_kinds,
        )
        if not path.parts or path.parts[0] != expected_root:
            raise ValueError(
                f"sdist member is outside the single {expected_root!r} top-level directory: "
                f"{member.name}"
            )
        if len(path.parts) == 1:
            if not is_directory:
                raise ValueError("sdist top-level path must be a directory")
            if member.size != 0:
                raise ValueError("sdist directory entry has a non-zero size")
            continue
        relative = PurePosixPath(*path.parts[1:]).as_posix()
        if is_directory:
            if member.size != 0:
                raise ValueError(f"sdist directory entry has a non-zero size: {member.name}")
            continue
        if member.size > ARCHIVE_MEMBER_MAX_BYTES:
            raise ValueError(
                f"sdist member {relative!r} is {member.size} bytes, above the "
                f"{ARCHIVE_MEMBER_MAX_BYTES}-byte member bound"
            )
        expanded_bytes += member.size
        if expanded_bytes > ARCHIVE_EXPANDED_MAX_BYTES:
            raise ValueError(f"sdist expands above the {ARCHIVE_EXPANDED_MAX_BYTES}-byte bound")
        _read_tar_member(archive, member)
        files.add(relative)
    if member_count == 0:
        raise ValueError("sdist archive is empty")
    return files, member_count, expanded_bytes


def _read_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    received = 0
    with archive.open(info) as source:
        while True:
            chunk = source.read(min(MIB, info.file_size - received + 1))
            if not chunk:
                break
            received += len(chunk)
            if received > info.file_size:
                raise ValueError(f"wheel member exceeds its declared size: {info.filename}")
    if received != info.file_size:
        raise ValueError(
            f"wheel member byte count changed: {info.filename} ({received} != {info.file_size})"
        )


def _inspect_wheel_members(
    archive: zipfile.ZipFile,
    *,
    expected_roots: set[str],
) -> tuple[dict[str, zipfile.ZipInfo], int, int]:
    infos = archive.infolist()
    if not infos:
        raise ValueError("wheel archive is empty")
    if len(infos) > ARCHIVE_MEMBER_COUNT_MAX:
        raise ValueError(
            f"wheel has {len(infos)} members, above the {ARCHIVE_MEMBER_COUNT_MAX}-member bound"
        )
    if archive.comment:
        raise ValueError("wheel must not contain a ZIP comment")

    files: dict[str, zipfile.ZipInfo] = {}
    explicit_paths: dict[str, str] = {}
    path_kinds: dict[str, tuple[str, str]] = {}
    roots: set[str] = set()
    expanded_bytes = 0
    for info in infos:
        is_directory = info.is_dir()
        path = _canonical_archive_member(info.filename, is_directory=is_directory)
        _register_archive_member(
            path,
            is_directory=is_directory,
            explicit_paths=explicit_paths,
            path_kinds=path_kinds,
        )
        roots.add(path.parts[0])
        mode_type = stat.S_IFMT((info.external_attr >> 16) & 0xFFFF)
        allowed_modes = {0, stat.S_IFDIR} if is_directory else {0, stat.S_IFREG}
        if mode_type not in allowed_modes:
            raise ValueError(
                "wheel contains a non-regular member "
                f"(links, devices, and FIFOs are forbidden): {info.filename}"
            )
        if info.flag_bits & 0x1:
            raise ValueError(f"wheel contains an encrypted member: {info.filename}")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ValueError(f"wheel uses unsupported compression: {info.filename}")
        if is_directory:
            if info.file_size != 0:
                raise ValueError(f"wheel directory entry has a non-zero size: {info.filename}")
            continue
        if info.file_size > ARCHIVE_MEMBER_MAX_BYTES:
            raise ValueError(
                f"wheel member {info.filename!r} is {info.file_size} bytes, above the "
                f"{ARCHIVE_MEMBER_MAX_BYTES}-byte member bound"
            )
        expanded_bytes += info.file_size
        if expanded_bytes > ARCHIVE_EXPANDED_MAX_BYTES:
            raise ValueError(f"wheel expands above the {ARCHIVE_EXPANDED_MAX_BYTES}-byte bound")
        _read_zip_member(archive, info)
        files[path.as_posix()] = info
    if roots != expected_roots:
        raise ValueError(
            f"wheel top-level paths are {sorted(roots)}, expected {sorted(expected_roots)}"
        )
    return files, len(infos), expanded_bytes


def _wheel_record_hash(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    return f"sha256={digest.decode('ascii')}"


def _validate_wheel_record(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    *,
    record_name: str,
) -> None:
    """Require RECORD to bind every regular wheel member exactly once."""
    try:
        record_text = archive.read(record_name).decode("utf-8")
        rows = list(csv.reader(io.StringIO(record_text, newline=""), strict=True))
    except (UnicodeDecodeError, csv.Error, KeyError) as exc:
        raise ValueError("wheel RECORD is unreadable or is not valid CSV") from exc
    if not rows:
        raise ValueError("wheel RECORD is empty")

    records: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3:
            raise ValueError("wheel RECORD rows must contain exactly three fields")
        raw_path, recorded_hash, recorded_size = row
        try:
            canonical_path = _canonical_archive_member(
                raw_path,
                is_directory=False,
            ).as_posix()
        except ValueError as exc:
            raise ValueError(f"wheel RECORD contains an unsafe path: {raw_path!r}") from exc
        if canonical_path != raw_path:
            raise ValueError(f"wheel RECORD path is not canonical: {raw_path!r}")
        if raw_path in records:
            raise ValueError(f"wheel RECORD contains a duplicate path: {raw_path}")
        records[raw_path] = (recorded_hash, recorded_size)

    member_paths = set(members)
    record_paths = set(records)
    if record_paths != member_paths:
        raise ValueError(
            "wheel RECORD member set differs from the archive: "
            f"unrecorded={sorted(member_paths - record_paths)}, "
            f"missing={sorted(record_paths - member_paths)}"
        )

    for member_path in sorted(member_paths):
        recorded_hash, recorded_size = records[member_path]
        if member_path == record_name:
            if recorded_hash or recorded_size:
                raise ValueError("wheel RECORD must leave its own hash and size empty")
            continue
        payload = archive.read(member_path)
        expected_hash = _wheel_record_hash(payload)
        expected_size = str(len(payload))
        if recorded_hash != expected_hash:
            raise ValueError(f"wheel RECORD hash changed for {member_path}")
        if recorded_size != expected_size:
            raise ValueError(f"wheel RECORD size changed for {member_path}")


def _validate_wheel_metadata(payload: bytes) -> dict[str, str]:
    try:
        metadata = Parser().parsestr(payload.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError("wheel WHEEL metadata is not UTF-8") from exc
    if metadata.defects:
        raise ValueError(f"wheel WHEEL metadata has parsing defects: {metadata.defects!r}")
    expected = {
        "Wheel-Version": "1.0",
        "Root-Is-Purelib": "true",
        "Tag": "py3-none-any",
    }
    for key, value in expected.items():
        if metadata.get_all(key) != [value]:
            raise ValueError(f"wheel WHEEL {key} must be exactly {value!r}")
    generators = metadata.get_all("Generator")
    if (
        generators is None
        or len(generators) != 1
        or not generators[0]
        or generators[0] != generators[0].strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in generators[0])
    ):
        raise ValueError("wheel WHEEL must contain exactly one non-empty Generator")
    return {**expected, "Generator": generators[0]}


def _validate_core_metadata(payload: bytes, *, version: str) -> dict[str, str]:
    try:
        metadata_text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("wheel METADATA is not UTF-8") from exc
    metadata = Parser().parsestr(metadata_text)
    if metadata.defects:
        raise ValueError(f"wheel METADATA has parsing defects: {metadata.defects!r}")
    if metadata.get_all("Metadata-Version") != ["2.4"]:
        raise ValueError("wheel METADATA Metadata-Version must occur exactly once as '2.4'")
    expected = {
        "Name": "topoforge",
        "Version": version,
        "Requires-Python": "<3.15,>=3.11",
        "License-Expression": "Apache-2.0",
    }
    for key, value in expected.items():
        if metadata.get_all(key) != [value]:
            raise ValueError(f"wheel METADATA {key} must occur exactly once as {value!r}")
    return expected


def _validate_console_entry_points(payload: bytes) -> None:
    try:
        entry_points = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("wheel console entry points are not UTF-8") from exc
    parser = _ExactEntryPointConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(entry_points)
    except ConfigParserError as exc:
        raise ValueError("wheel console entry points are not valid INI") from exc
    if (
        parser.defaults()
        or parser.sections() != ["console_scripts"]
        or dict(parser.items("console_scripts", raw=True)) != {"topoforge": "topoforge.cli.app:app"}
    ):
        raise ValueError("wheel console entry point is missing, duplicated, or incorrect")


def _single_archive(directory: Path, suffix: str) -> Path:
    matches = sorted(directory.glob(f"*{suffix}"))
    if len(matches) != 1:
        raise ValueError(f"expected one *{suffix} archive in {directory}, found {len(matches)}")
    return matches[0].resolve()


def _exact_release_archives(directory: Path, version: str) -> tuple[Path, Path]:
    """Return canonical archives from a directory closed over uv's fixed marker."""
    expected_sdist = f"topoforge-{version}.tar.gz"
    expected_wheel = f"topoforge-{version}-py3-none-any.whl"
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise ValueError(f"release archive directory is unreadable: {directory}") from exc
    expected_names = sorted((".gitignore", expected_sdist, expected_wheel))
    observed_names = [entry.name for entry in entries]
    if observed_names != expected_names:
        raise ValueError(
            "release archive directory must contain exactly uv's marker and the canonical "
            "sdist and wheel: "
            f"observed={observed_names}, expected={expected_names}"
        )
    metadata_by_name: dict[str, os.stat_result] = {}
    for entry in entries:
        metadata = entry.lstat()
        if entry.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(f"release archive must be a real single-link file: {entry}")
        metadata_by_name[entry.name] = metadata
    marker = directory / ".gitignore"
    marker_payload = marker.read_bytes()
    marker_after = marker.lstat()
    marker_before = metadata_by_name[".gitignore"]
    if (
        marker_payload != b"*"
        or marker_after.st_dev != marker_before.st_dev
        or marker_after.st_ino != marker_before.st_ino
        or marker_after.st_size != marker_before.st_size
        or marker_after.st_mtime_ns != marker_before.st_mtime_ns
    ):
        raise ValueError("uv release archive marker changed or is not the exact b'*' payload")
    return directory / expected_sdist, directory / expected_wheel


def inspect_sdist(path: Path, version: str) -> dict[str, Any]:
    """Verify the source archive has an intentional, bounded content set."""
    archive_bytes = _bounded_archive_size(
        path,
        maximum_bytes=SDIST_ARCHIVE_MAX_BYTES,
        label="sdist archive",
    )
    expected_root = f"topoforge-{version}"
    forbidden_roots = {
        ".agent",
        ".agents",
        ".codex",
        ".hypothesis",
        "artifacts",
        "build",
        "cache",
        "dist",
        "downloads",
        "outputs",
    }
    with tarfile.open(path, "r:gz") as archive:
        files, member_count, expanded_bytes = _inspect_sdist_members(
            archive,
            expected_root=expected_root,
        )
    required_files = REQUIRED_SDIST_FILES | {
        f"scripts/rollback-topoforge-{version}.sh",
    }
    missing = sorted(required_files - files)
    if missing:
        raise ValueError(f"sdist is missing required files: {missing}")
    forbidden = sorted(
        name
        for name in files
        if name == "AGENTS.md"
        or PurePosixPath(name).parts[0] in forbidden_roots
        or any(
            part in {"node_modules", "playwright-report", "test-results"}
            for part in PurePosixPath(name).parts
        )
        or "__pycache__" in PurePosixPath(name).parts
        or name.endswith((".pyc", ".pyo"))
    )
    if forbidden:
        raise ValueError(f"sdist contains forbidden generated/private files: {forbidden[:20]}")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": archive_bytes,
        "member_count": member_count,
        "file_count": len(files),
        "expanded_bytes": expanded_bytes,
        "top_level_paths": [expected_root],
        "bounds": {
            "archive_max_bytes": SDIST_ARCHIVE_MAX_BYTES,
            "member_count_max": ARCHIVE_MEMBER_COUNT_MAX,
            "member_max_bytes": ARCHIVE_MEMBER_MAX_BYTES,
            "expanded_max_bytes": ARCHIVE_EXPANDED_MAX_BYTES,
        },
        "forbidden_member_count": 0,
        "required_files_present": sorted(required_files),
    }


def inspect_wheel(path: Path, version: str) -> dict[str, Any]:
    """Verify wheel metadata, licenses, entry point, and package boundaries."""
    archive_bytes = _bounded_archive_size(
        path,
        maximum_bytes=WHEEL_ARCHIVE_MAX_BYTES,
        label="wheel archive",
    )
    dist_info = f"topoforge-{version}.dist-info"
    metadata_name = f"{dist_info}/METADATA"
    entry_points_name = f"{dist_info}/entry_points.txt"
    wheel_metadata_name = f"{dist_info}/WHEEL"
    record_name = f"{dist_info}/RECORD"
    required_licenses = {
        f"{dist_info}/licenses/DATA_LICENSES.md",
        f"{dist_info}/licenses/LICENSE",
        f"{dist_info}/licenses/THIRD_PARTY_NOTICES.md",
    }
    required_web_files = {
        "topoforge/web/static/asset-manifest.json",
        "topoforge/web/static/index.html",
    }
    required_files = (
        required_licenses
        | required_web_files
        | {
            metadata_name,
            entry_points_name,
            wheel_metadata_name,
            record_name,
        }
    )
    with zipfile.ZipFile(path) as archive:
        members, member_count, expanded_bytes = _inspect_wheel_members(
            archive,
            expected_roots={"topoforge", dist_info},
        )
        names = set(members)
        missing = sorted(required_files - names)
        if missing:
            raise ValueError(f"wheel is missing required files: {missing}")
        _validate_wheel_record(archive, members, record_name=record_name)
        wheel_metadata = _validate_wheel_metadata(archive.read(wheel_metadata_name))
        _validate_console_entry_points(archive.read(entry_points_name))
        for name in names:
            member = PurePosixPath(name)
            if "__pycache__" in member.parts or name.endswith((".pyc", ".pyo")):
                raise ValueError(f"wheel contains generated Python cache: {name}")
        web_manifest = json.loads(
            archive.read("topoforge/web/static/asset-manifest.json").decode("utf-8")
        )
        if web_manifest.get("schema_version") != "topoforge-web-assets-v1":
            raise ValueError("wheel Web asset manifest schema is invalid")
        web_assets = web_manifest.get("assets")
        web_hashes = web_manifest.get("sha256")
        web_sizes = web_manifest.get("sizes")
        if not isinstance(web_assets, list) or not web_assets:
            raise ValueError("wheel Web asset manifest has no assets")
        if not isinstance(web_hashes, dict) or not isinstance(web_sizes, dict):
            raise ValueError("wheel Web asset manifest has no checksum or size map")
        if any(not isinstance(raw, str) for raw in web_assets):
            raise ValueError("wheel Web asset manifest contains a non-string path")
        if len(set(web_assets)) != len(web_assets):
            raise ValueError("wheel Web asset manifest contains duplicate asset paths")
        asset_set = set(web_assets)
        if set(web_hashes) != asset_set or set(web_sizes) != asset_set:
            raise ValueError("wheel Web asset/checksum/size path sets differ")
        static_prefix = "topoforge/web/static/"
        packaged_assets = {
            name.removeprefix(static_prefix)
            for name in names
            if name.startswith(static_prefix) and name != f"{static_prefix}asset-manifest.json"
        }
        if packaged_assets != asset_set:
            stale = sorted(packaged_assets - asset_set)
            missing_assets = sorted(asset_set - packaged_assets)
            raise ValueError(
                "wheel Web static tree differs from its manifest: "
                f"unmanifested={stale}, missing={missing_assets}"
            )
        for raw in web_assets:
            canonical = _canonical_archive_member(raw, is_directory=False).as_posix()
            if canonical != raw:
                raise ValueError(f"wheel Web asset path is not canonical: {raw}")
            member = f"{static_prefix}{raw}"
            payload = archive.read(member)
            if hashlib.sha256(payload).hexdigest() != web_hashes[raw]:
                raise ValueError(f"wheel Web asset checksum changed: {raw}")
            size = web_sizes[raw]
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ValueError(f"wheel Web asset byte count is invalid: {raw}")
            if len(payload) != size:
                raise ValueError(f"wheel Web asset byte count changed: {raw}")
        if web_manifest.get("languages") != ["zh-CN", "en"]:
            raise ValueError("wheel Web languages are incomplete")
        if web_manifest.get("frameworks") != ["React", "MapLibre", "Three.js"]:
            raise ValueError("wheel Web framework manifest is incomplete")
        expected_metadata = _validate_core_metadata(archive.read(metadata_name), version=version)
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": archive_bytes,
        "member_count": member_count,
        "file_count": len(names),
        "expanded_bytes": expanded_bytes,
        "top_level_paths": ["topoforge", dist_info],
        "bounds": {
            "archive_max_bytes": WHEEL_ARCHIVE_MAX_BYTES,
            "member_count_max": ARCHIVE_MEMBER_COUNT_MAX,
            "member_max_bytes": ARCHIVE_MEMBER_MAX_BYTES,
            "expanded_max_bytes": ARCHIVE_EXPANDED_MAX_BYTES,
        },
        "metadata": expected_metadata,
        "license_files": sorted(required_licenses),
        "entry_point": "topoforge = topoforge.cli.app:app",
        "wheel_metadata": wheel_metadata,
        "record_closed": True,
        "web": {
            "asset_count": len(web_assets),
            "languages": web_manifest["languages"],
            "frameworks": web_manifest["frameworks"],
            "required_checks_passed": True,
        },
    }


def _run_command(
    command: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    record = {
        "command": command,
        "cwd": str(cwd),
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    if completed.returncode != 0:
        raise RuntimeError(json.dumps(record, indent=2))
    return record


def _venv_executable(
    environment_dir: Path,
    name: str,
    *,
    windows: bool | None = None,
) -> Path:
    """Resolve one virtual-environment executable on POSIX or Windows."""
    is_windows = os.name == "nt" if windows is None else windows
    directory = "Scripts" if is_windows else "bin"
    executable = f"{name}.exe" if is_windows else name
    return environment_dir / directory / executable


def installed_smoke(
    wheel: Path,
    *,
    version: str,
    repository_root: Path,
    wheelhouse: Path | None,
) -> dict[str, Any]:
    """Install the wheel in a fresh venv and run outside the source checkout."""
    with tempfile.TemporaryDirectory(prefix="topoforge-release-") as raw_temp:
        root = Path(raw_temp).resolve() / "release path with spaces" / "地形"
        root.mkdir(parents=True)
        environment_dir = root / "venv"
        work_dir = root / "work"
        work_dir.mkdir()
        commands: list[dict[str, Any]] = []
        commands.append(
            _run_command(["uv", "venv", "--python", "3.12", str(environment_dir)], cwd=root)
        )
        python = _venv_executable(environment_dir, "python")
        cli = _venv_executable(environment_dir, "topoforge")
        install_command = ["uv", "pip", "install", "--python", str(python)]
        if wheelhouse is not None:
            install_command.extend(["--offline", "--find-links", str(wheelhouse.resolve())])
        install_command.append(str(wheel))
        commands.append(_run_command(install_command, cwd=root))
        smoke_env = os.environ.copy()
        smoke_env["PYTHONNOUSERSITE"] = "1"
        smoke_env["PYTHONPATH"] = ""
        smoke_env["PYTHONUTF8"] = "1"
        smoke_env["PYTHONIOENCODING"] = "utf-8"
        import_record = _run_command(
            [
                str(python),
                "-c",
                (
                    "import json, pathlib, topoforge; "
                    "print(json.dumps({'version': topoforge.__version__, "
                    "'origin': str(pathlib.Path(topoforge.__file__).resolve())}))"
                ),
            ],
            cwd=work_dir,
            env=smoke_env,
        )
        commands.append(import_record)
        imported = json.loads(import_record["stdout"])
        origin = Path(imported["origin"]).resolve()
        if imported["version"] != version:
            raise ValueError(f"installed version is {imported['version']}, expected {version}")
        if origin.is_relative_to(repository_root.resolve()):
            raise ValueError(f"installed import leaked into repository checkout: {origin}")
        doctor = _run_command([str(cli), "doctor"], cwd=work_dir, env=smoke_env)
        commands.append(doctor)
        doctor_payload = json.loads(doctor["stdout"])
        if doctor_payload.get("topoforge") != version:
            raise ValueError("installed doctor command did not report the release version")
        web_check = _run_command(
            [
                str(cli),
                "web",
                "--check",
                "--state-dir",
                str(root / "web-state"),
                "--workspace-root",
                str(root / "web-workspaces"),
                "--input-root",
                str(work_dir),
                "--no-open",
            ],
            cwd=work_dir,
            env=smoke_env,
        )
        commands.append(web_check)
        web_payload = json.loads(web_check["stdout"])
        if web_payload.get("required_checks_passed") is not True:
            raise ValueError("installed Web application check did not pass")
        if web_payload.get("assets", {}).get("languages") != ["zh-CN", "en"]:
            raise ValueError("installed Web application languages are incomplete")
        raster = work_dir / "smoke.tif"
        synthetic = _run_command(
            [
                str(cli),
                "synthetic",
                "--output",
                str(raster),
                "--rows",
                "16",
                "--columns",
                "20",
            ],
            cwd=work_dir,
            env=smoke_env,
        )
        commands.append(synthetic)
        output = work_dir / "bundle"
        build = _run_command(
            [
                str(cli),
                "build",
                "--dem",
                str(raster),
                "--output",
                str(output),
                "--size-mm",
                "40",
                "0",
                "--base-mm",
                "2",
                "--max-height-mm",
                "20",
                "--sampling-mode",
                "source-preserving",
                "--resource-budget-mode",
                "strict",
                "--max-grid-cells",
                "10000",
                "--max-estimated-triangles",
                "50000",
            ],
            cwd=work_dir,
            env=smoke_env,
        )
        commands.append(build)
        build_payload = json.loads(build["stdout"])
        if build_payload.get("required_checks_passed") is not True:
            raise ValueError("installed CLI smoke build did not pass required checks")
        inspection = _run_command(
            [str(cli), "inspect", str(output / "model.3mf")],
            cwd=work_dir,
            env=smoke_env,
        )
        commands.append(inspection)
        inspection_payload = json.loads(inspection["stdout"])
        if inspection_payload.get("strict_warning_count") != 0:
            raise ValueError("installed CLI strict 3MF inspection reported warnings")

        system_command = [
            str(python),
            "-I",
            "-X",
            "utf8",
            str(repository_root / "scripts" / "verify_windows_system.py"),
            "--work-root",
            str(root / "installed Web system acceptance"),
        ]
        if os.name == "nt":
            system_command.extend(
                ["--require-windows", "--hosted-server", "--browser-mode", "skip"]
            )
        system_command.extend(
            [
                "--report",
                str(root / "installed-system-verification.json"),
            ]
        )
        system_record = _run_command(
            system_command,
            cwd=work_dir,
            env=smoke_env,
        )
        commands.append(system_record)
        system_payload = json.loads(system_record["stdout"])
        if system_payload.get("required_checks_passed") is not True:
            raise ValueError("installed Web lifecycle acceptance did not pass")
        if os.name == "nt" and (
            system_payload.get("platform", {}).get("native_windows_verified") is not True
        ):
            raise ValueError("installed Web lifecycle did not verify native Windows")
        return {
            "isolated_environment": True,
            "repository_import_leakage": False,
            "installed_version": version,
            "installed_origin": str(origin),
            "path_contract": {
                "root": str(root),
                "contains_spaces": " " in str(root),
                "contains_non_ascii": any(ord(character) > 127 for character in str(root)),
                "required_checks_passed": True,
            },
            "doctor": doctor_payload,
            "web": web_payload,
            "build_required_checks_passed": True,
            "three_mf_warning_count": 0,
            "system": system_payload,
            "commands": commands,
        }


def verify_release(
    primary_dir: Path,
    *,
    repeat_dir: Path | None,
    version: str,
    install: bool,
    repository_root: Path,
    wheelhouse: Path | None,
) -> dict[str, Any]:
    """Verify release archives, reproducibility, and optional installation."""
    sdist, wheel = _exact_release_archives(primary_dir, version)
    report: dict[str, Any] = {
        "schema_version": 1,
        "topoforge_version": version,
        "sdist": inspect_sdist(sdist, version),
        "wheel": inspect_wheel(wheel, version),
        "reproducible_archives": None,
        "installed_smoke": None,
        "required_checks_passed": False,
    }
    if repeat_dir is not None:
        repeated_sdist, repeated_wheel = _exact_release_archives(repeat_dir, version)
        comparisons = {
            "sdist": sha256_file(sdist) == sha256_file(repeated_sdist),
            "wheel": sha256_file(wheel) == sha256_file(repeated_wheel),
        }
        if not all(comparisons.values()):
            raise ValueError(f"release archives are not byte reproducible: {comparisons}")
        report["reproducible_archives"] = comparisons
    if install:
        report["installed_smoke"] = installed_smoke(
            wheel,
            version=version,
            repository_root=repository_root,
            wheelhouse=wheelhouse,
        )
    report["required_checks_passed"] = True
    return report


def _write_github_output(path: Path, report: dict[str, Any]) -> None:
    """Append verified immutable archive identities to one GitHub output file."""
    lines = (
        f"wheel_filename={Path(str(report['wheel']['path'])).name}",
        f"wheel_sha256={report['wheel']['sha256']}",
        f"sdist_filename={Path(str(report['sdist']['path'])).name}",
        f"sdist_sha256={report['sdist']['sha256']}",
    )
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def main() -> int:
    """Run the command-line release verifier."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary-dir", type=Path, required=True)
    parser.add_argument("--repeat-dir", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    report = verify_release(
        args.primary_dir.resolve(),
        repeat_dir=args.repeat_dir.resolve() if args.repeat_dir else None,
        version=args.version,
        install=args.install,
        repository_root=repository_root,
        wheelhouse=args.wheelhouse,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.github_output is not None:
        _write_github_output(args.github_output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
