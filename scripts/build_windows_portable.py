#!/usr/bin/env python3
"""Build a reproducible, bounded Windows x64 TopoForge portable archive."""

# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import base64
import csv
import errno
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import IO, Any

_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(_SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIRECTORY))
import windows_acceptance as _windows_evidence  # noqa: E402

source_repository_record = _windows_evidence.source_repository_record

BUILD_SCHEMA_VERSION = "topoforge-windows-portable-build-v1"
MANIFEST_SCHEMA_VERSION = "topoforge-windows-portable-v2"
DEPENDENCY_RECORD_SCHEMA_VERSION = "topoforge-windows-dependency-record-projection-v1"
RUNTIME_SITE_PACKAGES_SCHEMA_VERSION = "topoforge-windows-runtime-site-packages-v1"
DEFAULT_CONFIG = Path("packaging/windows-x64-runtime.json")
WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
    *(f"com{number}" for number in "¹²³"),
    *(f"lpt{number}" for number in "¹²³"),
}
WINDOWS_INVALID_CHARACTERS = set('<>:"|?*')
TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".csv",
    ".ini",
    ".json",
    ".md",
    ".pth",
    ".py",
    ".rst",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
CLI_LAUNCHER = (
    "@echo off\r\n"
    "setlocal\r\n"
    'set "TOPOFORGE_ROOT=%~dp0"\r\n'
    'set "PYTHONUTF8=1"\r\n'
    'set "PYTHONNOUSERSITE=1"\r\n'
    '"%TOPOFORGE_ROOT%runtime\\python.exe" -I -X utf8 '
    "-m topoforge.cli.app %*\r\n"
    'set "TOPOFORGE_EXIT=%ERRORLEVEL%"\r\n'
    "endlocal & exit /b %TOPOFORGE_EXIT%\r\n"
)
WEB_LAUNCHER = (
    "@echo off\r\n"
    "setlocal\r\n"
    'set "TOPOFORGE_ROOT=%~dp0"\r\n'
    'set "PYTHONUTF8=1"\r\n'
    'set "PYTHONNOUSERSITE=1"\r\n'
    '"%TOPOFORGE_ROOT%runtime\\python.exe" -I -X utf8 '
    "-m topoforge.cli.app web %*\r\n"
    'set "TOPOFORGE_EXIT=%ERRORLEVEL%"\r\n'
    "endlocal & exit /b %TOPOFORGE_EXIT%\r\n"
)
WINDOWS_README = """TopoForge Windows x64 portable candidate
===========================================

English
-------
1. Extract the entire ZIP to a normal local folder. Do not run it inside the ZIP.
2. Double-click TopoForge-Web.cmd. Keep its console window open while using the app.
3. TopoForge opens the local bilingual Web application in your default browser.
4. Press Ctrl+C in the console to stop the local server.
5. For CLI use, open Command Prompt in this folder and run:
     topoforge.cmd doctor
     topoforge.cmd --help

No administrator access or system Python is required. TopoForge does not modify PATH.
Durable state and workspaces default to %LOCALAPPDATA%\\TopoForge.
This package is a Phase 12 release candidate until the same archive passes the declared
clean Windows 10 22H2 x64 and Windows 11 x64 acceptance gates. Bambu Studio automation
is a separate capability and must not be inferred from core/Web portable operation.

中文
----
1. 请先把整个 ZIP 解压到普通本地文件夹，不要直接在压缩包内运行。
2. 双击 TopoForge-Web.cmd，并在使用期间保持控制台窗口开启。
3. TopoForge 会在默认浏览器中打开本地双语 Web 应用。
4. 在控制台按 Ctrl+C 可停止本地服务。
5. 如需命令行，请在本目录打开命令提示符并运行：
     topoforge.cmd doctor
     topoforge.cmd --help

无需管理员权限或系统 Python，也不会修改 PATH。
持久状态和工作区默认位于 %LOCALAPPDATA%\\TopoForge。
在同一归档通过干净 Windows 10 22H2 x64 与 Windows 11 x64 验收前，
该包仍是 Phase 12 发布候选。Bambu Studio 自动化是独立能力，不能由
核心/Web 便携运行结果推断为已支持。
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _load_config(path: Path) -> dict[str, Any]:
    try:
        config = _json_object(json.loads(path.read_text(encoding="utf-8")), "config")
    except (OSError, ValueError) as exc:
        raise ValueError(f"Windows runtime config is unreadable: {path}") from exc
    if config.get("schema_version") != "topoforge-windows-runtime-config-v1":
        raise ValueError("Windows runtime config schema is unsupported")
    package_root = _string(config.get("package_root"), "package_root")
    _safe_relative_path(package_root)

    target = _json_object(config.get("target"), "target")
    if target.get("os") != "Windows" or target.get("architecture") != "x86_64":
        raise ValueError("Windows runtime config target must be Windows x86_64")
    if target.get("python_platform") != "x86_64-pc-windows-msvc":
        raise ValueError("Windows runtime config must use the x86_64 MSVC Python platform")

    runtime = _json_object(config.get("python_runtime"), "python_runtime")
    for field in (
        "implementation",
        "version",
        "abi",
        "provider",
        "provider_release",
        "provider_license",
        "provider_license_file",
        "provider_source_url",
        "archive_name",
        "url",
        "sha256",
        "archive_root",
    ):
        _string(runtime.get(field), f"python_runtime.{field}")
    for field in ("bytes", "member_count", "uncompressed_bytes"):
        _positive_int(runtime.get(field), f"python_runtime.{field}")
    if len(runtime["sha256"]) != 64:
        raise ValueError("python_runtime.sha256 must contain 64 hexadecimal characters")
    try:
        int(runtime["sha256"], 16)
    except ValueError as exc:
        raise ValueError("python_runtime.sha256 is not hexadecimal") from exc
    if not runtime["url"].startswith("https://"):
        raise ValueError("python_runtime.url must use HTTPS")
    if not runtime["provider_source_url"].startswith("https://github.com/"):
        raise ValueError("python_runtime.provider_source_url must use GitHub HTTPS")
    if PurePosixPath(runtime["provider_license_file"]).name != runtime["provider_license_file"]:
        raise ValueError("python_runtime.provider_license_file must be a filename")
    if PurePosixPath(runtime["archive_name"]).name != runtime["archive_name"]:
        raise ValueError("python_runtime.archive_name must be a filename")
    _safe_relative_path(runtime["archive_root"])

    bounds = _json_object(config.get("bounds"), "bounds")
    for field in (
        "runtime_archive_max_bytes",
        "runtime_member_max_bytes",
        "runtime_member_count_max",
        "runtime_uncompressed_max_bytes",
        "portable_archive_max_bytes",
        "portable_member_max_bytes",
        "portable_member_count_max",
        "portable_uncompressed_max_bytes",
        "manifest_max_bytes",
    ):
        _positive_int(bounds.get(field), f"bounds.{field}")
    source_date_epoch = _positive_int(config.get("source_date_epoch"), "source_date_epoch")
    if not 315532800 <= source_date_epoch <= 4354819199:
        raise ValueError("source_date_epoch must fit the ZIP timestamp range")
    return config


def _safe_relative_path(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise ValueError(f"path is not a canonical portable relative path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or ".." in path.parts:
        raise ValueError(f"path is not a canonical portable relative path: {name!r}")
    for part in path.parts:
        if (
            not part
            or part in {".", ".."}
            or part.endswith((" ", "."))
            or any(ord(character) < 32 for character in part)
            or any(character in WINDOWS_INVALID_CHARACTERS for character in part)
        ):
            raise ValueError(f"path is unsafe on Windows: {name!r}")
        reserved_stem = part.split(".", 1)[0].casefold()
        if reserved_stem in WINDOWS_RESERVED_NAMES:
            raise ValueError(f"path uses a reserved Windows filename: {name!r}")
    return path


def _register_windows_path(
    path: PurePosixPath,
    seen: dict[str, tuple[str, str]],
    *,
    is_directory: bool = False,
) -> None:
    for length in range(1, len(path.parts) + 1):
        prefix = PurePosixPath(*path.parts[:length]).as_posix()
        folded = prefix.casefold()
        kind = "directory" if length < len(path.parts) or is_directory else "file"
        previous = seen.get(folded)
        if previous is None:
            seen[folded] = (prefix, kind)
            continue
        previous_path, previous_kind = previous
        if previous_path != prefix:
            raise ValueError(f"paths collide on Windows: {previous_path!r} and {prefix!r}")
        if previous_kind != kind:
            raise ValueError(f"path is both a file and directory on Windows: {prefix!r}")


def _copy_exact(source: IO[bytes], destination: Path, expected_bytes: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with destination.open("xb") as output:
        while True:
            chunk = source.read(min(1024 * 1024, expected_bytes - written + 1))
            if not chunk:
                break
            written += len(chunk)
            if written > expected_bytes:
                raise ValueError(f"archive member exceeds declared size: {destination}")
            output.write(chunk)
    if written != expected_bytes:
        raise ValueError(
            f"archive member size changed while reading {destination}: "
            f"expected {expected_bytes}, received {written}"
        )


def _verify_runtime_archive(path: Path, config: dict[str, Any]) -> None:
    runtime = _json_object(config["python_runtime"], "python_runtime")
    bounds = _json_object(config["bounds"], "bounds")
    if not path.is_file():
        raise FileNotFoundError(
            f"Windows runtime archive does not exist: {path}. "
            "Provide --runtime-archive or allow the pinned HTTPS download."
        )
    size = path.stat().st_size
    if size > bounds["runtime_archive_max_bytes"]:
        raise ValueError("Windows runtime archive exceeds its configured safety bound")
    if size != runtime["bytes"]:
        raise ValueError(
            f"Windows runtime archive byte count is {size}, expected {runtime['bytes']}"
        )
    digest = _sha256(path)
    if digest != runtime["sha256"]:
        raise ValueError(
            f"Windows runtime archive SHA-256 is {digest}, expected {runtime['sha256']}"
        )


def _download_runtime(destination: Path, config: dict[str, Any]) -> None:
    runtime = _json_object(config["python_runtime"], "python_runtime")
    bounds = _json_object(config["bounds"], "bounds")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".part",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            request = urllib.request.Request(
                runtime["url"],
                headers={"User-Agent": "TopoForge-Windows-Portable-Builder/1"},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                raw_length = response.headers.get("Content-Length")
                if raw_length is not None and int(raw_length) > bounds["runtime_archive_max_bytes"]:
                    raise ValueError("Windows runtime download exceeds its configured safety bound")
                received = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > bounds["runtime_archive_max_bytes"]:
                        raise ValueError(
                            "Windows runtime download exceeds its configured safety bound"
                        )
                    temporary.write(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())
        _verify_runtime_archive(temporary_path, config)
        os.replace(temporary_path, destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _acquire_runtime(
    *,
    runtime_archive: Path | None,
    cache_dir: Path,
    config: dict[str, Any],
) -> Path:
    runtime = _json_object(config["python_runtime"], "python_runtime")
    if runtime_archive is not None:
        resolved = runtime_archive.expanduser().resolve()
        _verify_runtime_archive(resolved, config)
        return resolved
    cached = (cache_dir / runtime["archive_name"]).resolve()
    if not cached.exists():
        _download_runtime(cached, config)
    _verify_runtime_archive(cached, config)
    return cached


def _extract_runtime(
    archive_path: Path,
    destination: Path,
    config: dict[str, Any],
) -> dict[str, int]:
    runtime = _json_object(config["python_runtime"], "python_runtime")
    bounds = _json_object(config["bounds"], "bounds")
    archive_root = runtime["archive_root"]
    members: list[tarfile.TarInfo] = []
    names: set[str] = set()
    windows_paths: dict[str, tuple[str, str]] = {}
    uncompressed_bytes = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            if len(members) >= bounds["runtime_member_count_max"]:
                raise ValueError("Windows runtime archive exceeds its member-count bound")
            if not member.isfile():
                raise ValueError(
                    f"Windows runtime archive contains a non-regular member: {member.name}"
                )
            path = _safe_relative_path(member.name)
            if not path.parts or path.parts[0] != archive_root or len(path.parts) < 2:
                raise ValueError(
                    f"Windows runtime member is outside {archive_root}/: {member.name}"
                )
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            relative_path = _safe_relative_path(relative)
            _register_windows_path(relative_path, windows_paths)
            if relative in names:
                raise ValueError(
                    f"Windows runtime archive has a duplicate Windows path: {relative}"
                )
            if member.size > bounds["runtime_member_max_bytes"]:
                raise ValueError(f"Windows runtime member exceeds its size bound: {relative}")
            uncompressed_bytes += member.size
            if uncompressed_bytes > bounds["runtime_uncompressed_max_bytes"]:
                raise ValueError("Windows runtime archive exceeds its expansion bound")
            names.add(relative)
            members.append(member)

        if len(members) != runtime["member_count"]:
            raise ValueError(
                f"Windows runtime member count is {len(members)}, "
                f"expected {runtime['member_count']}"
            )
        if uncompressed_bytes != runtime["uncompressed_bytes"]:
            raise ValueError(
                f"Windows runtime expands to {uncompressed_bytes} bytes, "
                f"expected {runtime['uncompressed_bytes']}"
            )
        for member in members:
            relative = PurePosixPath(*PurePosixPath(member.name).parts[1:])
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"Windows runtime member is unreadable: {member.name}")
            with source:
                _copy_exact(source, destination / Path(*relative.parts), member.size)

    required = ("python.exe", "pythonw.exe", "python312.dll", "LICENSE.txt")
    missing = [name for name in required if not (destination / name).is_file()]
    if missing:
        raise ValueError(f"Windows runtime is missing required files: {missing}")
    return {
        "member_count": len(members),
        "uncompressed_bytes": uncompressed_bytes,
    }


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    record: dict[str, Any] = {
        "command": command,
        "cwd": str(cwd),
        "exit_code": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed with exit code {completed.returncode}: {command}\n"
            f"{completed.stderr[-4000:] or completed.stdout[-4000:]}"
        )
    return record


def _project_version(repository_root: Path) -> str:
    project = tomllib.loads((repository_root / "pyproject.toml").read_text(encoding="utf-8"))
    version = project.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml has no project version")
    return version


def _canonical_distribution_name(name: str) -> str:
    normalized: list[str] = []
    separator = False
    for character in name.casefold():
        if character in "-_.":
            separator = True
            continue
        if separator and normalized:
            normalized.append("-")
        normalized.append(character)
        separator = False
    return "".join(normalized)


def _metadata_values(
    payload: bytes,
    *,
    context: str,
    required_fields: tuple[str, ...],
) -> dict[str, str]:
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{context} METADATA is not UTF-8") from exc
    metadata = Parser().parsestr(decoded)
    if metadata.defects:
        defect_names = ", ".join(type(defect).__name__ for defect in metadata.defects)
        raise ValueError(f"{context} METADATA is malformed: {defect_names}")
    values: dict[str, str] = {}
    for field in required_fields:
        raw_values = metadata.get_all(field, [])
        if len(raw_values) != 1:
            raise ValueError(f"{context} METADATA must contain exactly one {field} field")
        value = str(raw_values[0])
        if (
            not value
            or value != value.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError(f"{context} METADATA {field} field is invalid")
        values[field] = value
    return values


def _projection_sha256(entries: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        entries,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _installed_file_projection(
    root: Path,
    *,
    maximum_files: int,
    maximum_file_bytes: int,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    windows_paths: dict[str, tuple[str, str]] = {}
    candidates = sorted(
        root.rglob("*"),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )
    for path in candidates:
        if path.is_symlink():
            raise ValueError(f"installed dependency path is a symlink: {path}")
        if not path.is_file():
            continue
        if len(entries) >= maximum_files:
            raise ValueError("installed dependency projection exceeds its file-count bound")
        relative = _safe_relative_path(path.relative_to(root).as_posix())
        _register_windows_path(relative, windows_paths)
        size = path.stat().st_size
        if size > maximum_file_bytes:
            raise ValueError(f"installed dependency file exceeds its size bound: {relative}")
        entries.append(
            {
                "path": relative.as_posix(),
                "bytes": size,
                "sha256": _sha256(path),
            }
        )
    entries.sort(key=lambda entry: (entry["path"].casefold(), entry["path"]))
    return entries


def _record_digest(path: Path) -> str:
    encoded = base64.urlsafe_b64encode(bytes.fromhex(_sha256(path))).rstrip(b"=")
    return encoded.decode("ascii")


def _read_record_rows(
    record_path: Path,
    *,
    maximum_bytes: int,
    maximum_rows: int,
) -> list[tuple[str, str, str]]:
    size = record_path.stat().st_size
    if size > maximum_bytes:
        raise ValueError(f"dependency RECORD exceeds its size bound: {record_path}")
    try:
        payload = record_path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"dependency RECORD is unreadable UTF-8: {record_path}") from exc
    rows: list[tuple[str, str, str]] = []
    reader = csv.reader(io.StringIO(payload, newline=""))
    for row in reader:
        if len(rows) >= maximum_rows:
            raise ValueError(f"dependency RECORD exceeds its row-count bound: {record_path}")
        if len(row) != 3 or not row[0] or len(row[0]) > 1024:
            raise ValueError(f"dependency RECORD contains an invalid row: {record_path}")
        rows.append((row[0], row[1], row[2]))
    if not rows:
        raise ValueError(f"dependency RECORD is empty: {record_path}")
    return rows


def _normalize_distribution_record(
    site_packages: Path,
    directory: Path,
    *,
    maximum_record_bytes: int,
    maximum_rows: int,
    maximum_file_bytes: int,
) -> tuple[dict[str, Any], set[str], list[dict[str, Any]]]:
    record_path = directory / "RECORD"
    if not record_path.is_file() or record_path.is_symlink():
        raise ValueError(f"dependency wheel RECORD is missing or unsafe: {record_path}")
    original_rows = _read_record_rows(
        record_path,
        maximum_bytes=maximum_record_bytes,
        maximum_rows=maximum_rows,
    )
    metadata_path = directory / "METADATA"
    if not metadata_path.is_file() or metadata_path.is_symlink():
        raise ValueError(f"dependency METADATA is missing or unsafe: {metadata_path}")
    try:
        metadata_payload = metadata_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"dependency metadata is unreadable: {metadata_path}") from exc
    metadata = _metadata_values(
        metadata_payload,
        context=f"dependency {metadata_path}",
        required_fields=("Metadata-Version", "Name", "Version"),
    )
    name = metadata["Name"]
    version = metadata["Version"]

    generated = {"INSTALLER", "REQUESTED", "direct_url.json"}
    for generated_name in generated:
        generated_path = directory / generated_name
        if generated_path.is_symlink():
            raise ValueError(f"generated dependency metadata is unsafe: {generated_path}")
        generated_path.unlink(missing_ok=True)

    record_relative = record_path.relative_to(site_packages).as_posix()
    retained: list[dict[str, Any]] = []
    retained_paths: set[str] = set()
    record_self_count = 0
    windows_paths: dict[str, tuple[str, str]] = {}
    seen_original: set[str] = set()
    for raw_path, hash_spec, raw_size in original_rows:
        if "\\" in raw_path or "\x00" in raw_path:
            raise ValueError(f"dependency RECORD path is unsafe: {raw_path!r}")
        raw_parts = PurePosixPath(raw_path).parts
        if ".." in raw_parts:
            if not any(part.casefold() in {"bin", "scripts"} for part in raw_parts):
                raise ValueError(f"dependency RECORD escapes site-packages: {raw_path}")
            continue
        relative = _safe_relative_path(raw_path)
        relative_text = relative.as_posix()
        _register_windows_path(relative, windows_paths)
        if relative_text in seen_original:
            raise ValueError(f"dependency RECORD contains a duplicate path: {relative_text}")
        seen_original.add(relative_text)
        if relative.parent == PurePosixPath(directory.name) and relative.name in generated:
            continue
        if relative_text == record_relative:
            record_self_count += 1
            if hash_spec or raw_size:
                raise ValueError(f"dependency RECORD self row is not canonical: {record_path}")
            continue
        installed = site_packages / Path(*relative.parts)
        if not installed.is_file() or installed.is_symlink():
            raise ValueError(f"dependency RECORD member is missing or unsafe: {relative_text}")
        size = installed.stat().st_size
        if size > maximum_file_bytes:
            raise ValueError(f"dependency RECORD member exceeds its size bound: {relative_text}")
        if raw_size != str(size):
            raise ValueError(f"dependency RECORD member size changed: {relative_text}")
        expected_hash = f"sha256={_record_digest(installed)}"
        if hash_spec != expected_hash:
            raise ValueError(f"dependency RECORD member SHA-256 changed: {relative_text}")
        retained_paths.add(relative_text)
        retained.append(
            {
                "path": relative_text,
                "bytes": size,
                "sha256": _sha256(installed),
            }
        )
    if record_self_count != 1:
        raise ValueError(f"dependency RECORD must contain exactly one self row: {record_path}")
    metadata_relative = metadata_path.relative_to(site_packages).as_posix()
    if metadata_relative not in retained_paths:
        raise ValueError(f"dependency RECORD does not bind its METADATA: {record_path}")

    retained.sort(key=lambda entry: (entry["path"].casefold(), entry["path"]))
    normalized_rows = [
        (
            entry["path"],
            "sha256="
            + base64.urlsafe_b64encode(bytes.fromhex(entry["sha256"])).rstrip(b"=").decode("ascii"),
            str(entry["bytes"]),
        )
        for entry in retained
    ]
    normalized_rows.append((record_relative, "", ""))
    normalized_rows.sort(key=lambda row: (row[0].casefold(), row[0]))
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(normalized_rows)
    normalized_payload = output.getvalue().encode("utf-8")
    if len(normalized_payload) > maximum_record_bytes:
        raise ValueError(f"normalized dependency RECORD exceeds its size bound: {record_path}")
    record_path.write_bytes(normalized_payload)

    record_entry = {
        "path": record_relative,
        "bytes": len(normalized_payload),
        "sha256": _sha256(record_path),
    }
    all_entries = sorted(
        [*retained, record_entry],
        key=lambda entry: (entry["path"].casefold(), entry["path"]),
    )
    return (
        {
            "name": name,
            "version": version,
            "dist_info": directory.name,
            "record_path": record_relative,
            "record_sha256": record_entry["sha256"],
            "installed_file_count": len(all_entries),
            "installed_bytes": sum(int(entry["bytes"]) for entry in all_entries),
            "installed_files_sha256": _projection_sha256(all_entries),
        },
        {str(entry["path"]) for entry in all_entries},
        all_entries,
    )


def _normalize_dependency_install(
    site_packages: Path,
    *,
    original_dist_info: set[str],
    runtime_baseline: list[dict[str, Any]],
    maximum_files: int,
    maximum_file_bytes: int,
    maximum_record_bytes: int,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    for scripts_name in ("bin", "Scripts"):
        scripts = site_packages / scripts_name
        if scripts.exists():
            if not scripts.is_dir() or scripts.is_symlink():
                raise ValueError(f"dependency script path is unsafe: {scripts}")
            shutil.rmtree(scripts)
    installer_lock = site_packages / ".lock"
    if installer_lock.exists():
        if not installer_lock.is_file() or installer_lock.is_symlink():
            raise ValueError(f"dependency installer lock path is unsafe: {installer_lock}")
        installer_lock.unlink()

    new_dist_info = sorted(
        (
            path
            for path in site_packages.glob("*.dist-info")
            if path.name.casefold() not in original_dist_info
        ),
        key=lambda item: item.name.casefold(),
    )
    dependencies: list[dict[str, Any]] = []
    dependency_paths: set[str] = set()
    dependency_entries: list[dict[str, Any]] = []
    names: set[str] = set()
    for directory in new_dist_info:
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(f"dependency metadata path is unsafe: {directory}")
        package, package_paths, package_entries = _normalize_distribution_record(
            site_packages,
            directory,
            maximum_record_bytes=maximum_record_bytes,
            maximum_rows=maximum_files,
            maximum_file_bytes=maximum_file_bytes,
        )
        canonical_name = _canonical_distribution_name(str(package["name"]))
        if canonical_name in names:
            raise ValueError(f"dependency inventory contains a duplicate: {package['name']}")
        overlap = dependency_paths & package_paths
        if overlap:
            raise ValueError(f"dependency RECORD projections overlap: {sorted(overlap)[:20]}")
        names.add(canonical_name)
        dependencies.append(package)
        dependency_paths.update(package_paths)
        dependency_entries.extend(package_entries)
        if len(dependency_paths) > maximum_files:
            raise ValueError("dependency RECORD projection exceeds its file-count bound")

    current = _installed_file_projection(
        site_packages,
        maximum_files=maximum_files,
        maximum_file_bytes=maximum_file_bytes,
    )
    baseline_by_path = {str(entry["path"]): entry for entry in runtime_baseline}
    current_by_path = {str(entry["path"]): entry for entry in current}
    baseline_paths = set(baseline_by_path)
    if baseline_paths & dependency_paths:
        raise ValueError("dependency RECORD projection overlaps the embedded runtime baseline")
    expected_paths = baseline_paths | dependency_paths
    if set(current_by_path) != expected_paths:
        extra = sorted(set(current_by_path) - expected_paths)
        missing = sorted(expected_paths - set(current_by_path))
        raise ValueError(
            "installed dependency files differ from runtime baseline plus wheel RECORDs; "
            f"extra={extra[:20]}, missing={missing[:20]}"
        )
    for path, expected in baseline_by_path.items():
        if current_by_path[path] != expected:
            raise ValueError(f"embedded runtime site-packages file changed: {path}")

    forbidden_binary_suffixes = {".dylib", ".so"}
    forbidden_binaries = sorted(
        str(path.relative_to(site_packages))
        for path in site_packages.rglob("*")
        if path.is_file() and path.suffix.casefold() in forbidden_binary_suffixes
    )
    if forbidden_binaries:
        raise ValueError(
            f"non-Windows compiled dependencies were installed: {forbidden_binaries[:20]}"
        )
    extension_count = sum(
        1
        for path in site_packages.rglob("*")
        if path.is_file() and path.suffix.casefold() == ".pyd"
    )
    if extension_count == 0:
        raise ValueError("Windows dependency installation contains no compiled .pyd extensions")
    dependencies.sort(key=lambda item: _canonical_distribution_name(str(item["name"])))
    dependency_entries.sort(key=lambda entry: (entry["path"].casefold(), entry["path"]))
    return (
        dependencies,
        extension_count,
        {
            "schema_version": DEPENDENCY_RECORD_SCHEMA_VERSION,
            "installed_file_count": len(dependency_entries),
            "installed_bytes": sum(int(entry["bytes"]) for entry in dependency_entries),
            "installed_files_sha256": _projection_sha256(dependency_entries),
        },
    )


def _assert_no_build_path(root: Path, repository_root: Path) -> None:
    forbidden = {
        str(repository_root.resolve()).encode("utf-8"),
        str(repository_root.resolve()).encode("utf-16-le"),
    }
    for path in root.rglob("*"):
        if (
            not path.is_file()
            or path.stat().st_size > 16 * 1024 * 1024
            or path.suffix.casefold() not in TEXT_SUFFIXES
        ):
            continue
        payload = path.read_bytes()
        if any(value and value in payload for value in forbidden):
            raise ValueError(f"portable dependency contains the build checkout path: {path}")


def _inspect_project_wheel(path: Path, version: str) -> dict[str, Any]:
    dist_info = f"topoforge-{version}.dist-info"
    metadata_name = f"{dist_info}/METADATA"
    with zipfile.ZipFile(path) as archive:
        try:
            metadata_payload = archive.read(metadata_name)
        except KeyError as exc:
            raise ValueError(f"TopoForge wheel has no {metadata_name}") from exc
    expected = {
        "Name": "topoforge",
        "Version": version,
        "Requires-Python": "<3.15,>=3.11",
        "License-Expression": "Apache-2.0",
    }
    metadata = _metadata_values(
        metadata_payload,
        context="TopoForge wheel",
        required_fields=("Metadata-Version", *expected),
    )
    for field, value in expected.items():
        if metadata[field] != value:
            raise ValueError(
                f"TopoForge wheel metadata {field} is {metadata[field]!r}, expected {value!r}"
            )
    return {
        "filename": path.name,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
        "metadata": expected,
    }


def _extract_wheel(
    wheel: Path,
    destination: Path,
    *,
    bounds: dict[str, Any],
) -> int:
    existing = {
        path.relative_to(destination).as_posix().casefold() for path in destination.rglob("*")
    }
    windows_paths: dict[str, tuple[str, str]] = {}
    for path in destination.rglob("*"):
        relative = _safe_relative_path(path.relative_to(destination).as_posix())
        _register_windows_path(relative, windows_paths, is_directory=path.is_dir())

    extracted = 0
    uncompressed_bytes = 0
    with zipfile.ZipFile(wheel) as archive:
        infos = archive.infolist()
        if len(infos) > bounds["portable_member_count_max"]:
            raise ValueError("TopoForge wheel exceeds the portable member-count bound")
        for info in infos:
            raw_name = info.filename[:-1] if info.is_dir() else info.filename
            if not raw_name:
                continue
            relative = _safe_relative_path(raw_name)
            _register_windows_path(relative, windows_paths, is_directory=info.is_dir())
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK or info.flag_bits & 0x1:
                raise ValueError(f"TopoForge wheel contains an unsafe member: {info.filename}")
            if info.is_dir():
                continue
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ValueError(f"TopoForge wheel uses unsupported compression: {info.filename}")
            if info.file_size > bounds["portable_member_max_bytes"]:
                raise ValueError(f"TopoForge wheel member exceeds its size bound: {info.filename}")
            uncompressed_bytes += info.file_size
            if uncompressed_bytes > bounds["portable_uncompressed_max_bytes"]:
                raise ValueError("TopoForge wheel exceeds the portable expansion bound")
            key = relative.as_posix().casefold()
            if key in existing:
                raise ValueError(
                    f"TopoForge wheel collides with a dependency path: {relative.as_posix()}"
                )
            existing.add(key)
            with archive.open(info) as source:
                _copy_exact(source, destination / Path(*relative.parts), info.file_size)
            extracted += 1
    return extracted


def _write_support_files(package_root: Path, repository_root: Path) -> None:
    (package_root / "topoforge.cmd").write_bytes(CLI_LAUNCHER.encode("utf-8"))
    (package_root / "TopoForge-Web.cmd").write_bytes(WEB_LAUNCHER.encode("utf-8"))
    (package_root / "README-Windows.txt").write_bytes(WINDOWS_README.encode("utf-8"))
    for name in ("LICENSE", "DATA_LICENSES.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(repository_root / name, package_root / name)


def _files_for_manifest(
    package_root: Path,
    *,
    bounds: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    files: list[dict[str, Any]] = []
    windows_paths: dict[str, tuple[str, str]] = {}
    uncompressed_bytes = 0
    candidates = list(package_root.rglob("*"))
    symbolic_links = [path for path in candidates if path.is_symlink()]
    if symbolic_links:
        raise ValueError(f"portable staging tree contains a symbolic link: {symbolic_links[0]}")
    for path in sorted(
        (candidate for candidate in candidates if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(package_root).as_posix(),
    ):
        relative = path.relative_to(package_root).as_posix()
        relative_path = _safe_relative_path(relative)
        _register_windows_path(relative_path, windows_paths)
        size = path.stat().st_size
        if size > bounds["portable_member_max_bytes"]:
            raise ValueError(f"portable member exceeds its size bound: {relative}")
        uncompressed_bytes += size
        if uncompressed_bytes > bounds["portable_uncompressed_max_bytes"]:
            raise ValueError("portable staging tree exceeds its expansion bound")
        files.append({"path": relative, "bytes": size, "sha256": _sha256(path)})
        if len(files) > bounds["portable_member_count_max"] - 1:
            raise ValueError("portable staging tree exceeds its member-count bound")
    return files, uncompressed_bytes


def _zip_timestamp(source_date_epoch: int) -> tuple[int, int, int, int, int, int]:
    value = time.gmtime(source_date_epoch)
    second = value.tm_sec - (value.tm_sec % 2)
    return (value.tm_year, value.tm_mon, value.tm_mday, value.tm_hour, value.tm_min, second)


def _write_reproducible_zip(
    package_root: Path,
    destination: Path,
    *,
    source_date_epoch: int,
    bounds: dict[str, Any],
    overwrite: bool,
) -> None:
    if destination.exists() and not overwrite:
        raise FileExistsError(
            f"portable archive already exists: {destination}. Use --force to replace this file."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    timestamp = _zip_timestamp(source_date_epoch)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(
            temporary_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=False,
        ) as archive:
            for path in sorted(
                (candidate for candidate in package_root.rglob("*") if candidate.is_file()),
                key=lambda candidate: candidate.relative_to(package_root).as_posix(),
            ):
                relative = path.relative_to(package_root).as_posix()
                member_name = f"{package_root.name}/{relative}"
                info = zipfile.ZipInfo(member_name, date_time=timestamp)
                info.create_system = 0
                info.external_attr = 0
                info.compress_type = zipfile.ZIP_DEFLATED
                with (
                    path.open("rb") as source,
                    archive.open(info, "w", force_zip64=False) as output,
                ):
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        size = temporary_path.stat().st_size
        if size > bounds["portable_archive_max_bytes"]:
            raise ValueError(
                f"portable archive is {size} bytes, above the configured "
                f"{bounds['portable_archive_max_bytes']}-byte bound"
            )
        os.replace(temporary_path, destination)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _replace_across_filesystems(source: Path, destination: Path) -> None:
    """Copy to the destination filesystem, then publish with one atomic replace."""
    temporary_destination: Path | None = None
    source_size = source.stat().st_size
    source_sha256 = _sha256(source)
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as target:
            temporary_destination = Path(target.name)
            with source.open("rb") as payload:
                shutil.copyfileobj(payload, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        if temporary_destination.stat().st_size != source_size:
            raise OSError("cross-filesystem archive copy changed the byte count")
        if _sha256(temporary_destination) != source_sha256:
            raise OSError("cross-filesystem archive copy changed the SHA-256")
        os.replace(temporary_destination, destination)
        temporary_destination = None
        source.unlink()
    finally:
        if temporary_destination is not None:
            temporary_destination.unlink(missing_ok=True)


def _publish_verified_archive(
    staged_archive: Path,
    destination: Path,
    *,
    verification: dict[str, Any],
    overwrite: bool,
) -> None:
    """Atomically publish one archive only after strict verification passes."""
    if verification.get("required_checks_passed") is not True:
        raise ValueError("cross-host portable archive verification did not pass")
    if not staged_archive.is_file():
        raise FileNotFoundError(f"verified portable archive is missing: {staged_archive}")
    if destination.exists() and not overwrite:
        raise FileExistsError(
            f"portable archive already exists: {destination}. Use --force to replace this file."
        )
    try:
        os.replace(staged_archive, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _replace_across_filesystems(staged_archive, destination)


def build_windows_portable(
    *,
    repository_root: Path,
    config_path: Path,
    output_dir: Path,
    runtime_archive: Path | None,
    cache_dir: Path,
    uv_executable: str,
    expected_version: str | None,
    overwrite: bool,
) -> dict[str, Any]:
    """Build and cross-host inspect one Windows x64 portable archive."""
    repository = repository_root.resolve()
    config_file = config_path.resolve()
    config = _load_config(config_file)
    runtime = _json_object(config["python_runtime"], "python_runtime")
    target = _json_object(config["target"], "target")
    bounds = _json_object(config["bounds"], "bounds")
    version = _project_version(repository)
    if expected_version is not None and version != expected_version:
        raise ValueError(f"project version is {version}, expected --version {expected_version}")
    build_constraints = repository / "packaging" / "build-constraints.txt"
    if not build_constraints.is_file():
        raise FileNotFoundError(f"reproducible build constraints are missing: {build_constraints}")
    source = source_repository_record(
        repository,
        expected_commit=None,
        require_clean=True,
    )
    verifier_paths = {
        "builder": Path(__file__).resolve(),
        "portable": repository / "scripts" / "verify_windows_portable.py",
        "system": repository / "scripts" / "verify_windows_system.py",
        "bambu": repository / "scripts" / "verify_windows_bambu.py",
        "helper": repository / "scripts" / "windows_acceptance.py",
    }
    build_provenance = {
        "source_commit": source["commit"],
        "source_tracked_dirty": False,
        "config_sha256": _sha256(config_file),
        "build_constraints_sha256": _sha256(build_constraints),
        "verifier_sha256": {
            role: _sha256(verifier_path) for role, verifier_path in verifier_paths.items()
        },
        "required_checks_passed": True,
    }
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"topoforge-{version}-windows-x64-portable.zip"
    if destination.exists() and not overwrite:
        raise FileExistsError(
            f"portable archive already exists: {destination}. Use --force to replace this file."
        )

    runtime_path = _acquire_runtime(
        runtime_archive=runtime_archive,
        cache_dir=cache_dir.resolve(),
        config=config,
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "PYTHONUTF8": "1",
            "SOURCE_DATE_EPOCH": str(config["source_date_epoch"]),
        }
    )
    commands: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="topoforge-windows-portable-") as raw_temporary:
        temporary = Path(raw_temporary).resolve()
        package_root = temporary / config["package_root"]
        embedded_runtime = package_root / "runtime"
        runtime_summary = _extract_runtime(runtime_path, embedded_runtime, config)
        site_packages = embedded_runtime / "Lib" / "site-packages"
        site_packages.mkdir(parents=True, exist_ok=True)
        original_dist_info = {path.name.casefold() for path in site_packages.glob("*.dist-info")}
        runtime_baseline_files = _installed_file_projection(
            site_packages,
            maximum_files=bounds["portable_member_count_max"],
            maximum_file_bytes=bounds["portable_member_max_bytes"],
        )
        runtime_site_packages = {
            "schema_version": RUNTIME_SITE_PACKAGES_SCHEMA_VERSION,
            "file_count": len(runtime_baseline_files),
            "files_sha256": _projection_sha256(runtime_baseline_files),
            "files": runtime_baseline_files,
        }

        requirements = temporary / "locked-runtime-requirements.txt"
        commands.append(
            _run_command(
                [
                    uv_executable,
                    "export",
                    "--locked",
                    "--no-dev",
                    "--no-emit-project",
                    "--no-annotate",
                    "--no-header",
                    "--no-sources",
                    "--quiet",
                    "--output-file",
                    str(requirements),
                ],
                cwd=repository,
                environment=environment,
            )
        )
        commands.append(
            _run_command(
                [
                    uv_executable,
                    "pip",
                    "install",
                    "--target",
                    str(site_packages),
                    "--requirements",
                    str(requirements),
                    "--require-hashes",
                    "--only-binary",
                    ":all:",
                    "--python-platform",
                    target["python_platform"],
                    "--python-version",
                    ".".join(runtime["version"].split(".")[:2]),
                    "--strict",
                    "--quiet",
                ],
                cwd=repository,
                environment=environment,
            )
        )
        dependencies, extension_count, dependency_projection = _normalize_dependency_install(
            site_packages,
            original_dist_info=original_dist_info,
            runtime_baseline=runtime_baseline_files,
            maximum_files=bounds["portable_member_count_max"],
            maximum_file_bytes=bounds["portable_member_max_bytes"],
            maximum_record_bytes=bounds["manifest_max_bytes"],
        )
        _assert_no_build_path(site_packages, repository)

        wheel_dir = temporary / "wheel"
        wheel_dir.mkdir()
        commands.append(
            _run_command(
                [
                    uv_executable,
                    "build",
                    "--wheel",
                    "--no-sources",
                    "--build-constraints",
                    str(build_constraints),
                    "--require-hashes",
                    "--quiet",
                    "--out-dir",
                    str(wheel_dir),
                ],
                cwd=repository,
                environment=environment,
            )
        )
        wheels = sorted(wheel_dir.glob("topoforge-*.whl"))
        if len(wheels) != 1:
            raise ValueError(f"expected one TopoForge wheel in {wheel_dir}, found {len(wheels)}")
        wheel = wheels[0]
        wheel_summary = _inspect_project_wheel(wheel, version)
        wheel_member_count = _extract_wheel(wheel, site_packages, bounds=bounds)

        _write_support_files(package_root, repository)
        provenance = package_root / "provenance"
        provenance.mkdir()
        shutil.copyfile(requirements, provenance / requirements.name)
        shutil.copyfile(repository / "uv.lock", provenance / "uv.lock")
        shutil.copyfile(config_file, provenance / config_file.name)
        shutil.copyfile(build_constraints, provenance / build_constraints.name)
        provider_license = config_file.parent / runtime["provider_license_file"]
        if not provider_license.is_file():
            raise FileNotFoundError(f"runtime provider license is missing: {provider_license}")
        shutil.copyfile(provider_license, provenance / provider_license.name)
        shutil.copyfile(wheel, provenance / wheel.name)

        files, payload_bytes = _files_for_manifest(package_root, bounds=bounds)
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "package_role": "phase12-windows-x64-portable-candidate",
            "topoforge_version": version,
            "target": target,
            "python_runtime": {
                **runtime,
                **runtime_summary,
            },
            "build_provenance": build_provenance,
            "locked_dependencies": {
                "count": len(dependencies),
                "packages": dependencies,
                "requirements_path": f"provenance/{requirements.name}",
                "requirements_sha256": _sha256(requirements),
                "uv_lock_path": "provenance/uv.lock",
                "uv_lock_sha256": _sha256(repository / "uv.lock"),
                "build_constraints_path": f"provenance/{build_constraints.name}",
                "build_constraints_sha256": _sha256(build_constraints),
                "compiled_extension_count": extension_count,
                "runtime_site_packages": runtime_site_packages,
                "record_projection": dependency_projection,
            },
            "project_wheel": {
                **wheel_summary,
                "path": f"provenance/{wheel.name}",
                "extracted_member_count": wheel_member_count,
            },
            "launchers": {
                "cli": "topoforge.cmd",
                "web": "TopoForge-Web.cmd",
                "python_isolated_mode": True,
                "system_python_required": False,
                "administrator_required": False,
            },
            "contents": {
                "file_count": len(files),
                "uncompressed_bytes": payload_bytes,
                "files": files,
            },
            "source_date_epoch": config["source_date_epoch"],
            "required_checks_passed": True,
        }
        manifest_bytes = (
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        if len(manifest_bytes) > bounds["manifest_max_bytes"]:
            raise ValueError("portable manifest exceeds its configured size bound")
        if payload_bytes + len(manifest_bytes) > bounds["portable_uncompressed_max_bytes"]:
            raise ValueError("portable package exceeds its configured expansion bound")
        (package_root / "manifest.json").write_bytes(manifest_bytes)

        staged_destination = temporary / destination.name
        _write_reproducible_zip(
            package_root,
            staged_destination,
            source_date_epoch=config["source_date_epoch"],
            bounds=bounds,
            overwrite=False,
        )

        verification_report = temporary / ".verification.json"
        try:
            verify_command = [
                sys.executable,
                str(repository / "scripts" / "verify_windows_portable.py"),
                "--archive",
                str(staged_destination),
                "--config",
                str(config_file),
                "--expected-version",
                version,
                "--report",
                str(verification_report),
            ]
            commands.append(_run_command(verify_command, cwd=repository, environment=environment))
            verification = json.loads(verification_report.read_text(encoding="utf-8"))
            _publish_verified_archive(
                staged_destination,
                destination,
                verification=verification,
                overwrite=overwrite,
            )
        finally:
            verification_report.unlink(missing_ok=True)

    return {
        "schema_version": BUILD_SCHEMA_VERSION,
        "archive": {
            "path": str(destination),
            "sha256": _sha256(destination),
            "bytes": destination.stat().st_size,
        },
        "topoforge_version": version,
        "target": target,
        "runtime_archive": {
            "path": str(runtime_path),
            "sha256": runtime["sha256"],
            "bytes": runtime["bytes"],
        },
        "dependency_count": len(dependencies),
        "compiled_extension_count": extension_count,
        "build_provenance": build_provenance,
        "verification": verification,
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
    """Build the pinned Windows x64 portable candidate and emit evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--runtime-archive", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path("cache/windows-portable"))
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--version")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    try:
        report = build_windows_portable(
            repository_root=repository_root,
            config_path=args.config,
            output_dir=args.output_dir,
            runtime_archive=args.runtime_archive,
            cache_dir=args.cache_dir,
            uv_executable=args.uv,
            expected_version=args.version,
            overwrite=args.force,
        )
    except Exception as exc:
        if args.report is not None:
            _write_report(
                args.report.resolve(),
                {
                    "schema_version": BUILD_SCHEMA_VERSION,
                    "error": {"type": exc.__class__.__name__, "message": str(exc)},
                    "required_checks_passed": False,
                },
            )
        raise
    if args.report is not None:
        _write_report(args.report.resolve(), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
