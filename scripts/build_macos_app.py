#!/usr/bin/env python3
"""Build a deterministic unsigned TopoForge.app candidate on native macOS arm64."""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import shutil
import stat
import subprocess
import tempfile
import tomllib
import urllib.request
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

if __package__:
    from scripts.macos_app import (
        APP_ROOT,
        BUILD_SCHEMA_VERSION,
        CLI_LAUNCHER_PATH,
        DEFAULT_CONFIG,
        INFO_PLIST_PATH,
        MANIFEST_PATH,
        MANIFEST_SCHEMA_VERSION,
        PYTHON_PATH,
        WEB_LAUNCHER_PATH,
        archive_sidecar,
        bundle_entries,
        canonical_json_bytes,
        ensure_expected_files,
        inspect_archive,
        load_config,
        macho_slices,
        payload_sha256,
        register_macos_path,
        safe_relative_path,
        sha256_file,
        write_json_with_sha256,
        write_reproducible_zip,
    )
else:
    from macos_app import (  # type: ignore[import-not-found]
        APP_ROOT,
        BUILD_SCHEMA_VERSION,
        CLI_LAUNCHER_PATH,
        DEFAULT_CONFIG,
        INFO_PLIST_PATH,
        MANIFEST_PATH,
        MANIFEST_SCHEMA_VERSION,
        PYTHON_PATH,
        WEB_LAUNCHER_PATH,
        archive_sidecar,
        bundle_entries,
        canonical_json_bytes,
        ensure_expected_files,
        inspect_archive,
        load_config,
        macho_slices,
        payload_sha256,
        register_macos_path,
        safe_relative_path,
        sha256_file,
        write_json_with_sha256,
        write_reproducible_zip,
    )

if __package__:
    from scripts.macos_macho import (
        PYTHON_FRAMEWORK_BUNDLE_PREFIX,
        PYTHON_FRAMEWORK_INSTALL_PREFIX,
        is_macho_bytes,
        macho_closure_records,
        macho_closure_summary,
        macho_rewrite_plans,
    )
else:
    from macos_macho import (  # type: ignore[import-not-found]
        PYTHON_FRAMEWORK_BUNDLE_PREFIX,
        PYTHON_FRAMEWORK_INSTALL_PREFIX,
        is_macho_bytes,
        macho_closure_records,
        macho_closure_summary,
        macho_rewrite_plans,
    )

CLI_LAUNCHER = """#!/bin/sh
set -eu
CONTENTS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH
unset DYLD_FALLBACK_LIBRARY_PATH DYLD_FALLBACK_FRAMEWORK_PATH
export PYTHONUTF8=1
export PYTHONNOUSERSITE=1
exec "$CONTENTS_DIR/Frameworks/Python.framework/Versions/3.12/bin/python3.12" \
  -I -X utf8 -m topoforge.cli.app "$@"
"""

WEB_LAUNCHER = """#!/bin/sh
set -eu
for argument in "$@"; do
  case "$argument" in
    --host|--host=*)
      echo "TopoForge.app fixes Web binding to 127.0.0.1; --host is not accepted." >&2
      exit 64
      ;;
  esac
done
CONTENTS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH
unset DYLD_FALLBACK_LIBRARY_PATH DYLD_FALLBACK_FRAMEWORK_PATH
export PYTHONUTF8=1
export PYTHONNOUSERSITE=1
exec "$CONTENTS_DIR/Frameworks/Python.framework/Versions/3.12/bin/python3.12" \
  -I -X utf8 -m topoforge.cli.app web --host 127.0.0.1 "$@"
"""

README = """TopoForge.app Phase 13A unsigned arm64 candidate
=================================================

This archive is an unsigned engineering candidate for native Apple Silicon on macOS 15 and 26.
It is not a release, is not notarized, has no Gatekeeper first-launch evidence, and does not claim
public macOS support. Intel, macOS 14, and preview macOS versions are outside the 0.12.x candidate
matrix. Official Bambu Studio automation remains separate, unverified Phase 13B work.

The application includes CPython, locked runtime dependencies, TopoForge Core/CLI, and production
Web assets. It does not require a system Python, uv, Node, or a source checkout. Durable state and
workspaces default to ~/Library/Application Support/TopoForge. The application launcher binds only
to 127.0.0.1.
"""


def _run(
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
    record = {
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


def require_native_macos_arm64(
    *,
    system: str | None = None,
    machine: str | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    """Reject non-native and out-of-matrix app builds with a corrective action."""
    actual_system = platform.system() if system is None else system
    actual_machine = platform.machine() if machine is None else machine
    actual_version = platform.mac_ver()[0] if version is None else version
    if actual_system != "Darwin" or actual_machine != "arm64":
        raise RuntimeError(
            "TopoForge.app can only be built on native arm64 macOS. "
            "Use a macOS 15 or macOS 26 Apple Silicon host; Linux may run contract tests only."
        )
    try:
        major = int(actual_version.split(".", 1)[0])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"macOS version is not parseable: {actual_version!r}") from exc
    if major not in {15, 26}:
        raise RuntimeError(
            "TopoForge.app build host must be stable macOS 15 or 26 arm64; "
            "macOS 14, Intel, and preview releases are outside the 0.12.x candidate matrix."
        )
    translated = subprocess.run(
        ["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"],
        check=False,
        capture_output=True,
        text=True,
    )
    if translated.returncode == 0 and translated.stdout.strip() == "1":
        raise RuntimeError("TopoForge.app build must not run under Rosetta translation")
    return {
        "system": actual_system,
        "machine": actual_machine,
        "macos_version": actual_version,
        "macos_major": major,
        "translated": False,
        "native_arm64": True,
    }


def _source_record(repository: Path) -> dict[str, Any]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise RuntimeError("TopoForge.app build requires a completely clean source tree")
    if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
        raise RuntimeError("source checkout does not resolve to one full Git commit")
    return {"commit": head, "tracked_dirty": False}


def _project_version(repository: Path) -> str:
    project = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))
    version = project.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml has no project version")
    return version


def _verify_runtime_archive(path: Path, config: dict[str, Any]) -> None:
    runtime = config["python_runtime"]
    bounds = config["bounds"]
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(
            f"official CPython runtime archive is missing: {path}. "
            "Provide --runtime-archive or allow the pinned python.org HTTPS download."
        )
    size = path.stat().st_size
    if size > bounds["runtime_archive_max_bytes"]:
        raise ValueError("CPython runtime archive exceeds its configured safety bound")
    if size != runtime["bytes"]:
        raise ValueError(f"CPython runtime archive is {size} bytes, expected {runtime['bytes']}")
    digest = sha256_file(path)
    if digest != runtime["sha256"]:
        raise ValueError(f"CPython runtime SHA-256 is {digest}, expected {runtime['sha256']}")


def _download_runtime(destination: Path, config: dict[str, Any]) -> None:
    runtime = config["python_runtime"]
    bounds = config["bounds"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.part")
    temporary.unlink(missing_ok=True)
    try:
        request = urllib.request.Request(
            runtime["url"], headers={"User-Agent": "TopoForge-macOS-App-Builder/1"}
        )
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            temporary.open("xb") as output,
        ):
            if response.geturl() != runtime["url"]:
                raise ValueError("CPython runtime download was redirected from the pinned URL")
            raw_length = response.headers.get("Content-Length")
            if raw_length is not None and int(raw_length) != runtime["bytes"]:
                raise ValueError("CPython runtime Content-Length differs from the pinned identity")
            received = 0
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                received += len(chunk)
                if received > bounds["runtime_archive_max_bytes"]:
                    raise ValueError("CPython runtime download exceeds its configured safety bound")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        _verify_runtime_archive(temporary, config)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _acquire_runtime(
    runtime_archive: Path | None,
    cache_dir: Path,
    config: dict[str, Any],
) -> Path:
    if runtime_archive is not None:
        selected = runtime_archive.expanduser().resolve()
    else:
        selected = cache_dir.resolve() / config["python_runtime"]["archive_name"]
        if not selected.exists():
            _download_runtime(selected, config)
    _verify_runtime_archive(selected, config)
    return selected


def _extract_framework(
    runtime_archive: Path,
    destination: Path,
    config: dict[str, Any],
    *,
    scratch_root: Path,
) -> None:
    scratch_root.mkdir(parents=True, exist_ok=True)
    expanded = scratch_root / "expanded-pkg"
    _run(
        ["/usr/sbin/pkgutil", "--expand-full", str(runtime_archive), str(expanded)],
        cwd=scratch_root,
        environment=os.environ.copy(),
    )
    runtime = config["python_runtime"]
    components = sorted(expanded.rglob("Python_Framework.pkg"))
    expected_component = expanded / "Python_Framework.pkg"
    if components != [expected_component] or not expected_component.is_dir():
        raise ValueError(
            "official CPython package must contain exactly one top-level "
            "Python_Framework.pkg component"
        )
    package_info = expected_component / "PackageInfo"
    if not package_info.is_file() or package_info.is_symlink():
        raise ValueError("official CPython framework component has no regular PackageInfo")
    try:
        package_root = ElementTree.parse(package_info).getroot()
    except ElementTree.ParseError as exc:
        raise ValueError("official CPython framework PackageInfo is invalid XML") from exc
    framework_version = runtime["framework_version"]
    if (
        package_root.tag != "pkg-info"
        or package_root.get("identifier")
        != f"org.python.Python.PythonFramework-{framework_version}"
        or package_root.get("install-location") != "/Library/Frameworks/Python.framework"
    ):
        raise ValueError("official CPython framework PackageInfo identity changed")

    # pkgutil --expand-full materializes the component payload at Payload/;
    # its PackageInfo install-location supplies the Python.framework wrapper.
    framework = expected_component / "Payload"
    source_primary = framework / "Versions" / framework_version / "Python"
    if not source_primary.is_file() or source_primary.is_symlink():
        raise ValueError("official CPython framework payload has no regular primary Mach-O")
    expected = runtime["source_primary_macho"]
    slices = macho_slices(source_primary)
    if [item["architecture"] for item in slices] != expected["architectures"]:
        raise ValueError("official CPython primary Mach-O architecture set changed")
    if {item["architecture"]: item["minimum_macos"] for item in slices} != expected[
        "minimum_macos"
    ]:
        raise ValueError("official CPython primary Mach-O deployment identity changed")
    shutil.copytree(framework, destination, symlinks=True)
    signature = destination / "Versions" / runtime["framework_version"] / "_CodeSignature"
    if signature.exists():
        if not signature.is_dir() or signature.is_symlink():
            raise ValueError("official CPython framework signature directory is unsafe")
        shutil.rmtree(signature)


def _remove_source_only_runtime_entries(framework: Path, config: dict[str, Any]) -> None:
    """Remove the pinned upstream Intel-only and build-time payload entries."""
    for name in config["python_runtime"]["source_only_paths"]:
        relative = safe_relative_path(name)
        candidate = framework.joinpath(*relative.parts)
        try:
            metadata = candidate.lstat()
        except FileNotFoundError as exc:
            raise ValueError(
                f"official CPython source-only payload entry is missing: {name}"
            ) from exc
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            raise ValueError(f"official CPython source-only payload entry is unsafe: {name}")
        candidate.unlink()

        # The pinned python.org payload stores AppleDouble records beside some
        # build-time objects. pkgutil may materialize them as files or consume
        # them as metadata, so remove them only when they remain regular files.
        apple_double = candidate.with_name(f"._{candidate.name}")
        try:
            sidecar_metadata = apple_double.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(sidecar_metadata.st_mode):
            raise ValueError(
                f"official CPython source-only metadata entry is unsafe: "
                f"{apple_double.relative_to(framework).as_posix()}"
            )
        apple_double.unlink()


def _is_macho(path: Path) -> bool:
    return is_macho_bytes(path.read_bytes())


def _strip_signature(path: Path) -> None:
    status = subprocess.run(
        ["/usr/bin/codesign", "--display", "--verbose=2", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if status.returncode == 0:
        subprocess.run(
            ["/usr/bin/codesign", "--remove-signature", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
    final = subprocess.run(
        ["/usr/bin/codesign", "--display", "--verbose=2", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if final.returncode == 0:
        raise RuntimeError(f"Mach-O file remains signed after normalization: {path}")


def _apply_macho_rewrite(path: Path, plan: dict[str, Any]) -> None:
    command = ["/usr/bin/install_name_tool"]
    for change in plan["changes"]:
        command.extend(["-change", change["old"], change["new"]])
    if plan["dylib_id"] is not None:
        command.extend(["-id", plan["dylib_id"]])
    for rpath in dict.fromkeys(plan["delete_rpaths"]):
        command.extend(["-delete_rpath", rpath])
    if len(command) == 1:
        return
    command.append(str(path))
    subprocess.run(command, check=True, capture_output=True, text=True)


def _normalize_macho(root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    target = config["target"]
    paths = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink() and _is_macho(path)
    ]
    if not paths:
        raise ValueError("embedded app contains no Mach-O files")

    for path in paths:
        slices = macho_slices(path)
        architectures = [item["architecture"] for item in slices]
        if "arm64" not in architectures:
            raise ValueError(f"Mach-O dependency has no arm64 slice: {path}")
        if architectures == ["arm64"]:
            continue
        temporary = path.with_name(f".{path.name}.arm64")
        temporary.unlink(missing_ok=True)
        mode = stat.S_IMODE(path.stat().st_mode)
        subprocess.run(
            ["/usr/bin/lipo", str(path), "-thin", "arm64", "-output", str(temporary)],
            check=True,
            capture_output=True,
            text=True,
        )
        os.chmod(temporary, mode)
        os.replace(temporary, path)

    bundle_directories = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if not path.is_symlink() and path.is_dir()
    }
    payloads = {path.relative_to(root).as_posix(): path.read_bytes() for path in paths}
    plans = macho_rewrite_plans(
        payloads,
        executable_path=PYTHON_PATH,
        bundle_directories=bundle_directories,
        absolute_rewrites={
            PYTHON_FRAMEWORK_INSTALL_PREFIX: PYTHON_FRAMEWORK_BUNDLE_PREFIX,
        },
    )
    for plan in plans:
        path = root.joinpath(*PurePosixPath(plan["path"]).parts)
        _apply_macho_rewrite(path, plan)

    for path in paths:
        _strip_signature(path)

    final_payloads = {path.relative_to(root).as_posix(): path.read_bytes() for path in paths}
    closure_records = macho_closure_records(
        final_payloads,
        executable_path=PYTHON_PATH,
        bundle_directories=bundle_directories,
    )
    closure_summary = macho_closure_summary(closure_records)
    if closure_summary["rpath_count"] != 0:
        raise ValueError("normalized embedded Mach-O files retain LC_RPATH commands")
    closure_by_path = {record["path"]: record for record in closure_records}

    records: list[dict[str, Any]] = []
    for path in paths:
        final = macho_slices(path)
        if [item["architecture"] for item in final] != ["arm64"]:
            raise ValueError(f"embedded Mach-O is not arm64-only: {path}")
        minimum = final[0]["minimum_macos"]
        minimum_parts = tuple(int(item) for item in minimum.split("."))
        target_parts = tuple(int(item) for item in target["deployment_target"].split("."))
        if minimum_parts > target_parts:
            raise ValueError(
                f"embedded Mach-O requires macOS {minimum}, above app target "
                f"{target['deployment_target']}: {path}"
            )
        relative = path.relative_to(root).as_posix()
        records.append(
            {
                "path": relative,
                "architecture": "arm64",
                "minimum_macos": minimum,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                **closure_by_path[relative],
            }
        )
    return records


def _canonical_name(name: str) -> str:
    output: list[str] = []
    separator = False
    for character in name.casefold():
        if character in "-_.":
            separator = True
            continue
        if separator and output:
            output.append("-")
        output.append(character)
        separator = False
    return "".join(output)


def _dependency_inventory(site_packages: Path) -> list[dict[str, str]]:
    packages: list[dict[str, str]] = []
    names: set[str] = set()
    for directory in sorted(
        site_packages.glob("*.dist-info"), key=lambda item: item.name.casefold()
    ):
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(f"dependency metadata path is unsafe: {directory}")
        metadata_path = directory / "METADATA"
        if not metadata_path.is_file() or metadata_path.is_symlink():
            raise ValueError(f"dependency METADATA is missing: {directory.name}")
        try:
            metadata = Parser().parsestr(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"dependency METADATA is unreadable: {directory.name}") from exc
        values = {field: metadata.get_all(field, []) for field in ("Name", "Version")}
        if any(len(items) != 1 for items in values.values()):
            raise ValueError(f"dependency METADATA identity is ambiguous: {directory.name}")
        name = str(values["Name"][0])
        version = str(values["Version"][0])
        canonical = _canonical_name(name)
        if not canonical or canonical in names:
            raise ValueError(f"dependency inventory has a duplicate or invalid name: {name}")
        names.add(canonical)
        packages.append({"name": name, "canonical_name": canonical, "version": version})
    packages.sort(key=lambda item: item["canonical_name"])
    return packages


def _normalize_site_packages(site_packages: Path) -> None:
    for name in ("bin", "Scripts"):
        path = site_packages / name
        if path.exists():
            if not path.is_dir() or path.is_symlink():
                raise ValueError(f"dependency script path is unsafe: {path}")
            shutil.rmtree(path)
    installer_lock = site_packages / ".lock"
    if installer_lock.exists():
        if not installer_lock.is_file() or installer_lock.is_symlink():
            raise ValueError("dependency installer lock is unsafe")
        installer_lock.unlink()
    for path in sorted(site_packages.rglob("*"), reverse=True):
        if path.is_symlink():
            raise ValueError(f"dependency installation contains a symlink: {path}")
        if path.is_file() and (path.suffix == ".pyc" or "__pycache__" in path.parts):
            path.unlink()
        elif path.is_dir() and path.name == "__pycache__":
            shutil.rmtree(path)


def _extract_project_wheel(wheel: Path, destination: Path, bounds: dict[str, Any]) -> int:
    seen: dict[str, tuple[str, str]] = {}
    for existing in destination.rglob("*"):
        relative = PurePosixPath(existing.relative_to(destination).as_posix())
        register_macos_path(relative, seen, "directory" if existing.is_dir() else "file")
    count = 0
    expanded = 0
    with zipfile.ZipFile(wheel) as archive:
        if archive.comment or len(archive.infolist()) > bounds["bundle_member_count_max"]:
            raise ValueError("project wheel has invalid global metadata")
        for info in archive.infolist():
            raw = info.filename[:-1] if info.is_dir() else info.filename
            if not raw:
                continue
            relative = safe_relative_path(raw)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK or info.flag_bits & 0x1:
                raise ValueError(f"project wheel contains an unsafe member: {info.filename}")
            register_macos_path(relative, seen, "directory" if info.is_dir() else "file")
            if info.is_dir():
                continue
            if info.file_size > bounds["bundle_member_max_bytes"]:
                raise ValueError(f"project wheel member exceeds its size bound: {info.filename}")
            expanded += info.file_size
            if expanded > bounds["bundle_uncompressed_max_bytes"]:
                raise ValueError("project wheel exceeds its expansion bound")
            output = destination.joinpath(*relative.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, output.open("xb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
            if output.stat().st_size != info.file_size:
                raise ValueError(f"project wheel member byte count changed: {info.filename}")
            count += 1
    return count


def _project_wheel_identity(wheel: Path, version: str) -> dict[str, Any]:
    metadata_name = f"topoforge-{version}.dist-info/METADATA"
    with zipfile.ZipFile(wheel) as archive:
        try:
            metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
        except (KeyError, UnicodeError) as exc:
            raise ValueError(f"TopoForge wheel metadata is missing: {metadata_name}") from exc
    expected = {
        "Name": "topoforge",
        "Version": version,
        "Requires-Python": "<3.15,>=3.11",
        "License-Expression": "Apache-2.0",
    }
    if any(metadata.get_all(field, []) != [value] for field, value in expected.items()):
        raise ValueError("TopoForge wheel metadata identity changed")
    return {
        "filename": wheel.name,
        "sha256": sha256_file(wheel),
        "bytes": wheel.stat().st_size,
        "metadata": expected,
    }


def _assert_no_repository_path(root: Path, repository: Path) -> None:
    forbidden = {
        str(repository.resolve()).encode(),
        str(repository.resolve()).encode("utf-16-le"),
    }
    for path in root.rglob("*"):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size > 16 * 1024 * 1024
            or path.suffix.casefold()
            not in {
                "",
                ".cfg",
                ".csv",
                ".ini",
                ".json",
                ".md",
                ".pth",
                ".py",
                ".txt",
                ".xml",
                ".yaml",
                ".yml",
            }
        ):
            continue
        payload = path.read_bytes()
        if any(value and value in payload for value in forbidden):
            raise ValueError(f"app payload contains the build checkout path: {path}")


def _write_support_files(app: Path, repository: Path, version: str) -> None:
    plist = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": "TopoForge",
        "CFBundleExecutable": "TopoForge",
        "CFBundleIdentifier": "org.topoforge.app",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "TopoForge",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSArchitecturePriority": ["arm64"],
        "LSMinimumSystemVersion": "15.0",
        "LSRequiresNativeExecution": True,
        "NSHighResolutionCapable": True,
    }
    plist_path = app / INFO_PLIST_PATH
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True))
    for relative, payload in (
        (CLI_LAUNCHER_PATH, CLI_LAUNCHER),
        (WEB_LAUNCHER_PATH, WEB_LAUNCHER),
        ("Contents/Resources/README-UNSIGNED-CANDIDATE.txt", README),
    ):
        path = app / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8", newline="\n")
        if relative in {CLI_LAUNCHER_PATH, WEB_LAUNCHER_PATH}:
            path.chmod(0o755)
    licenses = app / "Contents" / "Resources" / "licenses"
    licenses.mkdir(parents=True)
    for name in ("LICENSE", "DATA_LICENSES.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(repository / name, licenses / name)


def _normalize_tree_metadata(root: Path, epoch: int) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if not path.is_symlink():
            os.utime(path, (epoch, epoch), follow_symlinks=False)
    os.utime(root, (epoch, epoch), follow_symlinks=False)


def build_macos_app(
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
    """Build and statically verify one unsigned arm64 TopoForge.app archive."""
    host = require_native_macos_arm64()
    repository = repository_root.resolve()
    config_file = config_path.resolve()
    config = load_config(config_file)
    bounds = config["bounds"]
    runtime = config["python_runtime"]
    version = _project_version(repository)
    if expected_version is not None and version != expected_version:
        raise ValueError(f"project version is {version}, expected {expected_version}")
    source = _source_record(repository)
    build_constraints = repository / "packaging" / "build-constraints.txt"
    ensure_expected_files(
        [
            repository / "uv.lock",
            repository / "pyproject.toml",
            build_constraints,
            config_file,
            Path(__file__).resolve(),
            repository / "scripts" / "macos_app.py",
            repository / "scripts" / "macos_macho.py",
            repository / "scripts" / "verify_macos_app.py",
            repository / "scripts" / "verify_macos_system.py",
        ]
    )
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"topoforge-{version}-macos-arm64-unsigned-candidate.zip"
    if destination.exists() and not overwrite:
        raise FileExistsError(f"app archive already exists: {destination}")
    runtime_path = _acquire_runtime(runtime_archive, cache_dir, config)
    environment = os.environ.copy()
    environment.update(
        {
            "MACOSX_DEPLOYMENT_TARGET": config["target"]["deployment_target"],
            "PYTHONHASHSEED": "0",
            "PYTHONUTF8": "1",
            "SOURCE_DATE_EPOCH": str(config["source_date_epoch"]),
        }
    )
    commands: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="topoforge-macos-arm64-app-") as raw_temporary:
        temporary = Path(raw_temporary).resolve()
        app = temporary / APP_ROOT
        framework = app / "Contents" / "Frameworks" / "Python.framework"
        _extract_framework(
            runtime_path,
            framework,
            config,
            scratch_root=temporary,
        )
        _remove_source_only_runtime_entries(framework, config)
        site_packages = (
            framework
            / "Versions"
            / runtime["framework_version"]
            / "lib"
            / "python3.12"
            / "site-packages"
        )
        site_packages.mkdir(parents=True, exist_ok=True)
        requirements = temporary / "locked-runtime-requirements.txt"
        commands.append(
            _run(
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
            _run(
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
                    "aarch64-apple-darwin",
                    "--python-version",
                    "3.12",
                    "--strict",
                    "--no-compile",
                    "--quiet",
                ],
                cwd=repository,
                environment=environment,
            )
        )
        _normalize_site_packages(site_packages)
        dependencies = _dependency_inventory(site_packages)

        wheel_dir = temporary / "wheel"
        wheel_dir.mkdir()
        commands.append(
            _run(
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
            raise ValueError(f"expected one TopoForge wheel, found {len(wheels)}")
        wheel = wheels[0]
        wheel_identity = _project_wheel_identity(wheel, version)
        wheel_member_count = _extract_project_wheel(wheel, site_packages, bounds)
        if not (site_packages / "topoforge" / "web" / "static" / "index.html").is_file():
            raise ValueError("TopoForge wheel does not contain production Web assets")

        _write_support_files(app, repository, version)
        provenance = app / "Contents" / "Resources" / "provenance"
        provenance.mkdir(parents=True)
        for source_path in (
            requirements,
            repository / "uv.lock",
            repository / "pyproject.toml",
            build_constraints,
            config_file,
            wheel,
        ):
            shutil.copyfile(source_path, provenance / source_path.name)

        macho = _normalize_macho(app, config)
        macho_summary = macho_closure_summary(macho)
        primary = next(item for item in macho if item["path"] == PYTHON_PATH)
        if {
            "architecture": primary["architecture"],
            "minimum_macos": primary["minimum_macos"],
        } != runtime["embedded_primary_macho"]:
            raise ValueError("embedded primary CPython Mach-O identity changed")
        _assert_no_repository_path(app, repository)

        verifier_hashes = {
            role: sha256_file(path)
            for role, path in {
                "builder": Path(__file__).resolve(),
                "shared": repository / "scripts" / "macos_app.py",
                "macho": repository / "scripts" / "macos_macho.py",
                "archive": repository / "scripts" / "verify_macos_app.py",
                "system": repository / "scripts" / "verify_macos_system.py",
            }.items()
        }
        build_identity = {
            "config_sha256": sha256_file(config_file),
            "uv_lock_sha256": sha256_file(repository / "uv.lock"),
            "pyproject_sha256": sha256_file(repository / "pyproject.toml"),
            "build_constraints_sha256": sha256_file(build_constraints),
            "verifier_sha256": verifier_hashes,
        }
        _normalize_tree_metadata(app, config["source_date_epoch"])
        files = bundle_entries(app, bounds=bounds)
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "package_role": "phase13a-macos-arm64-unsigned-candidate",
            "topoforge_version": version,
            "target": config["target"],
            "source": source,
            "build_host": host,
            "build_identity": build_identity,
            "python_runtime": {
                **runtime,
                "macho_architectures": ["arm64"],
                "primary_macho_minimum_macos": primary["minimum_macos"],
                "macho_file_count": len(macho),
                "macho_minimum_versions": sorted({item["minimum_macos"] for item in macho}),
                "macho_closure": macho_summary,
                "macho_files": macho,
            },
            "locked_dependencies": {
                "count": len(dependencies),
                "packages": dependencies,
                "requirements_path": f"Contents/Resources/provenance/{requirements.name}",
                "requirements_sha256": sha256_file(requirements),
                "uv_lock_path": "Contents/Resources/provenance/uv.lock",
                "uv_lock_sha256": build_identity["uv_lock_sha256"],
            },
            "project_wheel": {
                **wheel_identity,
                "path": f"Contents/Resources/provenance/{wheel.name}",
                "extracted_member_count": wheel_member_count,
            },
            "launchers": {
                "web": WEB_LAUNCHER_PATH,
                "cli": CLI_LAUNCHER_PATH,
                "web_host": "127.0.0.1",
                "python_isolated_mode": True,
                "system_python_required": False,
                "uv_required": False,
                "node_required": False,
                "source_checkout_required": False,
                "default_data_root": "~/Library/Application Support/TopoForge",
            },
            "contents": {
                "file_count": len(files),
                "payload_sha256": payload_sha256(files),
                "files": files,
            },
            "unsigned": True,
            "notarized": False,
            "public_support_status": "unverified",
            "bambu_phase13b_evidence": False,
            "source_date_epoch": config["source_date_epoch"],
            "required_checks_passed": True,
        }
        manifest_payload = canonical_json_bytes(manifest)
        if len(manifest_payload) > bounds["manifest_max_bytes"]:
            raise ValueError("app manifest exceeds its configured size bound")
        manifest_path = app / MANIFEST_PATH
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(manifest_payload)
        os.utime(
            manifest_path,
            (config["source_date_epoch"], config["source_date_epoch"]),
            follow_symlinks=False,
        )

        staged = temporary / destination.name
        write_reproducible_zip(
            app,
            staged,
            source_date_epoch=config["source_date_epoch"],
            bounds=bounds,
            overwrite=False,
        )
        verification = inspect_archive(
            staged,
            config=config,
            expected_source_commit=source["commit"],
        )
        if verification["required_checks_passed"] is not True:
            raise RuntimeError("static app archive verification did not pass")
        os.replace(staged, destination)

    archive_sidecar(destination)
    return {
        "schema_version": BUILD_SCHEMA_VERSION,
        "package_role": "phase13a-macos-arm64-unsigned-candidate",
        "topoforge_version": version,
        "source": source,
        "host": host,
        "runtime_archive": {
            "filename": runtime_path.name,
            "sha256": runtime["sha256"],
            "bytes": runtime["bytes"],
        },
        "archive": {
            "path": str(destination),
            "filename": destination.name,
            "sha256": sha256_file(destination),
            "bytes": destination.stat().st_size,
            "sidecar": str(destination.with_name(f"{destination.name}.sha256")),
        },
        "build_identity": build_identity,
        "app_payload_sha256": verification["contents"]["payload_sha256"],
        "dependency_count": len(dependencies),
        "macho_file_count": len(macho),
        "macho_closure": macho_summary,
        "verification": verification,
        "commands": commands,
        "unsigned": True,
        "notarized": False,
        "public_support_status": "unverified",
        "clean_system_evidence": False,
        "gatekeeper_evidence": False,
        "bambu_phase13b_evidence": False,
        "required_checks_passed": True,
    }


def main() -> int:
    """Build the native unsigned app candidate and write bounded evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--runtime-archive", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path("cache/macos-app"))
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--version")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    try:
        report = build_macos_app(
            repository_root=repository,
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
            write_json_with_sha256(
                args.report.resolve(),
                {
                    "schema_version": BUILD_SCHEMA_VERSION,
                    "error": {"type": exc.__class__.__name__, "message": str(exc)},
                    "required_checks_passed": False,
                },
            )
        raise
    if args.report is not None:
        if (
            len(canonical_json_bytes(report))
            > load_config(args.config)["bounds"]["evidence_max_bytes"]
        ):
            raise ValueError("macOS app build report exceeds its configured evidence bound")
        write_json_with_sha256(args.report.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
