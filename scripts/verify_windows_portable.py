#!/usr/bin/env python3
"""Inspect and optionally execute a TopoForge Windows x64 portable archive."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import stat
import subprocess
import tempfile
import time
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import IO, Any

if __package__:
    from scripts.build_windows_portable import (
        CLI_LAUNCHER,
        MANIFEST_SCHEMA_VERSION,
        WEB_LAUNCHER,
        _load_config,
        _register_windows_path,
        _safe_relative_path,
        _sha256,
    )
    from scripts.verify_platform_core import verify_platform_core
else:
    from build_windows_portable import (  # type: ignore[import-not-found]
        CLI_LAUNCHER,
        MANIFEST_SCHEMA_VERSION,
        WEB_LAUNCHER,
        _load_config,
        _register_windows_path,
        _safe_relative_path,
        _sha256,
    )
    from verify_platform_core import verify_platform_core  # type: ignore[import-not-found]

VERIFICATION_SCHEMA_VERSION = "topoforge-windows-portable-verification-v1"
DEFAULT_CONFIG = Path("packaging/windows-x64-runtime.json")


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
) -> dict[str, Any]:
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
    metadata = Parser().parsestr(
        _read_member(archive, metadata_info, maximum_bytes=1024 * 1024).decode("utf-8")
    )
    expected_metadata = {
        "Name": "topoforge",
        "Version": version,
        "Requires-Python": "<3.15,>=3.11",
        "License-Expression": "Apache-2.0",
    }
    for field, expected in expected_metadata.items():
        if metadata.get(field) != expected:
            raise ValueError(
                f"portable installed metadata {field} is {metadata.get(field)!r}, "
                f"expected {expected!r}"
            )
    return {
        "path": wheel_path,
        "sha256": project["sha256"],
        "bytes": project["bytes"],
        "extracted_member_count": len(projected),
        "metadata": expected_metadata,
    }


def _validate_provenance(
    archive: zipfile.ZipFile,
    relative_infos: dict[str, zipfile.ZipInfo],
    manifest: dict[str, Any],
    config_path: Path,
) -> dict[str, Any]:
    dependencies = _json_object(manifest.get("locked_dependencies"), "manifest.locked_dependencies")
    requirements_path = dependencies.get("requirements_path")
    uv_lock_path = dependencies.get("uv_lock_path")
    if not isinstance(requirements_path, str) or not isinstance(uv_lock_path, str):
        raise ValueError("portable dependency provenance paths are missing")
    runtime_config = _json_object(_load_config(config_path)["python_runtime"], "python_runtime")
    provider_license_path = f"provenance/{runtime_config['provider_license_file']}"
    requirements_info = relative_infos.get(requirements_path)
    uv_lock_info = relative_infos.get(uv_lock_path)
    config_relative = f"provenance/{config_path.name}"
    config_info = relative_infos.get(config_relative)
    provider_license_info = relative_infos.get(provider_license_path)
    if any(
        info is None
        for info in (requirements_info, uv_lock_info, config_info, provider_license_info)
    ):
        raise ValueError("portable dependency/runtime provenance files are incomplete")
    assert requirements_info is not None
    assert uv_lock_info is not None
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
    bundled_config = _read_member(archive, config_info, maximum_bytes=4 * 1024 * 1024)
    if bundled_config != config_path.read_bytes():
        raise ValueError("portable bundled runtime config differs from the verifier config")
    provider_license = _read_member(archive, provider_license_info, maximum_bytes=1024 * 1024)
    expected_provider_license = (
        config_path.parent / runtime_config["provider_license_file"]
    ).read_bytes()
    if provider_license != expected_provider_license:
        raise ValueError("portable runtime provider license differs from the source license")

    packages = dependencies.get("packages")
    if not isinstance(packages, list) or dependencies.get("count") != len(packages):
        raise ValueError("portable dependency inventory count changed")
    normalized: list[tuple[str, str]] = []
    for raw in packages:
        package = _json_object(raw, "manifest.locked_dependencies.packages entry")
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise ValueError("portable dependency inventory has an invalid package")
        normalized.append((name, version))
    if normalized != sorted(normalized, key=lambda item: item[0].casefold()):
        raise ValueError("portable dependency inventory is not sorted by name")
    if len({name.casefold().replace("_", "-") for name, _ in normalized}) != len(normalized):
        raise ValueError("portable dependency inventory has duplicate names")

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
        "dependency_count": len(normalized),
        "compiled_extension_count": extension_count,
        "requirements_sha256": dependencies["requirements_sha256"],
        "uv_lock_sha256": dependencies["uv_lock_sha256"],
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
        project_report = _validate_project_wheel_projection(
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


def _run_json(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float = 600.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
    )
    record: dict[str, Any] = {
        "command": command,
        "cwd": str(cwd),
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


def execute_windows_portable(
    archive_path: Path,
    *,
    config_path: Path,
    expected_version: str,
    work_root: Path,
    verify_bambu: bool = False,
    bambu_studio_executable: Path | None = None,
    bambu_profiles_root: Path | None = None,
) -> dict[str, Any]:
    """Extract and execute launchers plus full manufacturing/Web acceptance on Windows."""
    if platform.system() != "Windows":
        raise RuntimeError(
            "--execute requires native Windows x64; run cross-host inspection without this flag"
        )
    if platform.machine().casefold() not in {"amd64", "x86_64"}:
        raise RuntimeError("--execute requires an x64 Windows host")
    resolved_archive = archive_path.resolve()
    resolved_config = config_path.resolve()
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
    config = _load_config(resolved_config)
    extraction_parent = work_root.resolve() / "portable path with spaces" / "地形"
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
    comspec = os.environ.get("COMSPEC", "cmd.exe")

    def batch_command(path: Path, arguments: list[str]) -> list[str]:
        invocation = "call " + subprocess.list2cmdline([str(path), *arguments])
        return [comspec, "/d", "/s", "/c", invocation]

    doctor, doctor_record = _run_json(
        batch_command(package_root / "topoforge.cmd", ["doctor"]),
        cwd=package_root,
        environment=environment,
    )
    commands.append(doctor_record)
    if doctor.get("topoforge") != expected_version:
        raise ValueError("portable CLI launcher reported the wrong TopoForge version")

    web_root = work_root / "launcher web check"
    input_root = web_root / "input"
    input_root.mkdir(parents=True)
    web, web_record = _run_json(
        batch_command(
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
        ),
        cwd=package_root,
        environment=environment,
    )
    commands.append(web_record)
    if web.get("required_checks_passed") is not True:
        raise ValueError("portable Web launcher check did not pass")

    core = verify_platform_core(
        work_root / "full core acceptance",
        python_executable=python,
    )
    if core.get("required_checks_passed") is not True:
        raise ValueError("portable full core acceptance did not pass")

    repository_root = Path(__file__).resolve().parents[1]
    system_report_path = work_root / "windows system acceptance.json"
    system, system_record = _run_json(
        [
            str(python),
            "-I",
            "-X",
            "utf8",
            str(repository_root / "scripts" / "verify_windows_system.py"),
            "--work-root",
            str(work_root / "full Web system acceptance"),
            "--require-windows",
            "--report",
            str(system_report_path),
        ],
        cwd=package_root,
        environment=environment,
    )
    commands.append(system_record)
    if system.get("required_checks_passed") is not True:
        raise ValueError("portable native Web lifecycle acceptance did not pass")
    if system.get("platform", {}).get("native_windows_verified") is not True:
        raise ValueError("portable native Web lifecycle did not verify Windows")

    bambu: dict[str, Any] | None = None
    if verify_bambu:
        bambu_report_path = work_root / "windows official Bambu acceptance.json"
        bambu_command = [
            str(python),
            "-I",
            "-X",
            "utf8",
            str(repository_root / "scripts" / "verify_windows_bambu.py"),
            "--work-root",
            str(work_root / "full official Bambu acceptance"),
            "--require-windows",
            "--report",
            str(bambu_report_path),
        ]
        if bambu_studio_executable is not None:
            bambu_command.extend(
                ("--bambu-studio-executable", str(bambu_studio_executable.resolve()))
            )
        if bambu_profiles_root is not None:
            bambu_command.extend(("--bambu-profiles-root", str(bambu_profiles_root.resolve())))
        bambu, bambu_record = _run_json(
            bambu_command,
            cwd=package_root,
            environment=environment,
            timeout_seconds=3600.0,
        )
        commands.append(bambu_record)
        if bambu.get("required_checks_passed") is not True:
            raise ValueError("portable official Bambu acceptance did not pass")
        if bambu.get("platform", {}).get("native_windows_verified") is not True:
            raise ValueError("portable official Bambu acceptance did not verify Windows")
        project = _json_object(bambu.get("official_project"), "bambu.official_project")
        if (
            project.get("all_projects_reopened") is not True
            or project.get("all_release_gates_passed") is not True
            or project.get("external_profiles_loaded_on_reopen") is not False
        ):
            raise ValueError("portable official Bambu project/reopen contract did not pass")
    return {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "extraction_path": str(package_root),
        "archive_sha256_verified_before_and_after_extraction": archive_sha256,
        "path_contains_spaces": " " in str(package_root),
        "path_contains_non_ascii": any(ord(character) > 127 for character in str(package_root)),
        "cli_launcher": doctor,
        "web_launcher": web,
        "core": core,
        "system": system,
        "bambu": bambu,
        "commands": commands,
        "required_checks_passed": True,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    """Inspect, compare, and optionally execute a Windows portable candidate."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--repeat-archive", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--expected-version")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--verify-bambu", action="store_true")
    parser.add_argument("--bambu-studio-executable", type=Path)
    parser.add_argument("--bambu-profiles-root", type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report_path = args.report.resolve()
    try:
        if args.verify_bambu and not args.execute:
            raise ValueError("--verify-bambu requires --execute")
        if args.verify_bambu and args.work_root is None:
            raise ValueError("--verify-bambu requires --work-root to retain native evidence")
        if not args.verify_bambu and (
            args.bambu_studio_executable is not None or args.bambu_profiles_root is not None
        ):
            raise ValueError("Bambu overrides require --verify-bambu")
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
                    verify_bambu=args.verify_bambu,
                    bambu_studio_executable=args.bambu_studio_executable,
                    bambu_profiles_root=args.bambu_profiles_root,
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
        raise
    _write_report(report_path, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
