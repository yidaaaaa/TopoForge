#!/usr/bin/env python3
"""Statically inspect and natively execute an unsigned TopoForge.app archive."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

if __package__:
    from scripts.macos_app import (
        CLI_LAUNCHER_PATH,
        DEFAULT_CONFIG,
        MANIFEST_PATH,
        PYTHON_PATH,
        VERIFICATION_SCHEMA_VERSION,
        WEB_LAUNCHER_PATH,
        canonical_json_bytes,
        extract_archive,
        inspect_archive,
        load_config,
        macho_slices,
        sha256_file,
        write_json_with_sha256,
    )
    from scripts.verify_platform_core import verify_platform_core
else:
    from macos_app import (  # type: ignore[import-not-found]
        CLI_LAUNCHER_PATH,
        DEFAULT_CONFIG,
        MANIFEST_PATH,
        PYTHON_PATH,
        VERIFICATION_SCHEMA_VERSION,
        WEB_LAUNCHER_PATH,
        canonical_json_bytes,
        extract_archive,
        inspect_archive,
        load_config,
        macho_slices,
        sha256_file,
        write_json_with_sha256,
    )
    from verify_platform_core import verify_platform_core  # type: ignore[import-not-found]

DEPENDENCY_PROBE = r"""
import _hashlib
import _ssl
import ctypes
import hashlib
import http.client
import importlib
import importlib.metadata
import json
import pathlib
import ssl
import sys
import urllib.request

modules = ("ssl", "_ssl", "hashlib", "_hashlib", "http.client", "urllib.request",
           "fastapi", "lib3mf", "manifold3d", "numpy", "PIL", "pydantic", "pyproj",
           "rasterio", "scipy", "shapely", "trimesh", "typer", "uvicorn", "topoforge")
imports = {}
for name in modules:
    module = importlib.import_module(name)
    imports[name] = str(pathlib.Path(module.__file__).resolve())

context = ssl.create_default_context()
tls_probe = {
    "openssl_version": ssl.OPENSSL_VERSION,
    "sha256": hashlib.sha256(b"TopoForge Mach-O closure probe").hexdigest(),
    "sha256_constructor": _hashlib.openssl_sha256(b"TopoForge").hexdigest(),
    "ssl_context_type": type(context).__name__,
    "https_connection_type": http.client.HTTPSConnection.__name__,
    "https_handler_type": urllib.request.HTTPSHandler.__name__,
    "required_checks_passed": True,
}

dyld = ctypes.CDLL(None)
image_count = dyld._dyld_image_count
image_count.argtypes = []
image_count.restype = ctypes.c_uint32
image_name = dyld._dyld_get_image_name
image_name.argtypes = [ctypes.c_uint32]
image_name.restype = ctypes.c_char_p
loaded_images = []
for index in range(image_count()):
    value = image_name(index)
    if value:
        loaded_images.append(value.decode("utf-8"))

packages = []
for distribution in importlib.metadata.distributions():
    name = distribution.metadata.get("Name")
    if name:
        packages.append({"name": name, "version": distribution.version})
print(json.dumps({
    "python_executable": str(pathlib.Path(sys.executable).resolve()),
    "python_version": sys.version.split()[0],
    "sys_path": sys.path,
    "imports": imports,
    "packages": sorted(packages, key=lambda item: item["name"].casefold()),
    "tls_probe": tls_probe,
    "loaded_images": sorted(set(loaded_images)),
}, sort_keys=True))
"""

_APPLE_SYSTEM_IMAGE_PREFIXES = ("/System/Library/", "/usr/lib/")


def _native_host(config: dict[str, Any]) -> dict[str, Any]:
    system = platform.system()
    machine = platform.machine()
    version = platform.mac_ver()[0]
    corrective = "Run this command on native Apple Silicon with stable macOS 15 or macOS 26."
    if system != "Darwin" or machine != "arm64":
        raise RuntimeError(
            f"native packaged-app execution requires Darwin arm64; observed {system} {machine}. "
            f"{corrective}"
        )
    try:
        major = int(version.split(".", 1)[0])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"macOS version is not parseable: {version!r}. {corrective}") from exc
    if major not in config["target"]["candidate_major_versions"]:
        raise RuntimeError(f"macOS {version} is outside the frozen candidate matrix. {corrective}")
    translated = subprocess.run(
        ["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"],
        check=False,
        capture_output=True,
        text=True,
    )
    if translated.returncode == 0 and translated.stdout.strip() == "1":
        raise RuntimeError("packaged-app execution must not run under Rosetta translation")
    return {
        "system": system,
        "machine": machine,
        "macos_version": version,
        "macos_major": major,
        "native_arm64": True,
        "translated": False,
    }


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


def _run_json(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
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
            f"packaged command failed with exit code {completed.returncode}: {command}\n"
            f"{completed.stderr[-4000:] or completed.stdout[-4000:]}"
        )
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError(f"packaged command did not emit JSON: {command}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"packaged command emitted a non-object: {command}")
    return payload, record


def _require_rejected(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    expected_exit_code: int,
    expected_message: str,
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
    if completed.returncode != expected_exit_code or expected_message not in completed.stderr:
        raise RuntimeError(
            f"packaged command did not fail closed as expected: {command}; "
            f"exit={completed.returncode}, stderr={completed.stderr[-4000:]!r}"
        )
    return record


def _inside_app(path: str, app: Path) -> bool:
    candidate = Path(path)
    return candidate.is_absolute() and candidate.resolve().is_relative_to(app.resolve())


def _loaded_image_inventory(images: Any, app: Path) -> dict[str, list[str]]:
    if not isinstance(images, list) or not images:
        raise RuntimeError("packaged TLS probe did not report loaded Mach-O images")
    app_images: list[str] = []
    apple_images: list[str] = []
    external_images: list[str] = []
    for value in images:
        if not isinstance(value, str) or not value or "\0" in value:
            raise RuntimeError("packaged TLS probe reported an invalid loaded image path")
        if value.startswith(_APPLE_SYSTEM_IMAGE_PREFIXES):
            apple_images.append(value)
        elif _inside_app(value, app):
            app_images.append(value)
        else:
            external_images.append(value)
    return {
        "app": sorted(set(app_images)),
        "apple_system": sorted(set(apple_images)),
        "external": sorted(set(external_images)),
    }


def execute_archive(
    archive: Path,
    *,
    config: dict[str, Any],
    work_root: Path,
    expected_source_commit: str | None,
) -> tuple[Path, dict[str, Any]]:
    """Execute the static candidate from a space/non-ASCII path on native arm64 macOS."""
    host = _native_host(config)
    static = inspect_archive(
        archive,
        config=config,
        expected_source_commit=expected_source_commit,
    )
    root = work_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    path_probe = root / "候选 app with spaces"
    app = extract_archive(archive, path_probe, config=config)
    if " " not in str(app) or not any(ord(character) > 127 for character in str(app)):
        raise RuntimeError("packaged app path probe must contain spaces and non-ASCII characters")
    manifest = json.loads((app / MANIFEST_PATH).read_text(encoding="utf-8"))
    python = app / PYTHON_PATH
    cli_launcher = app / CLI_LAUNCHER_PATH
    web_launcher = app / WEB_LAUNCHER_PATH
    if any(not os.access(path, os.X_OK) for path in (python, cli_launcher, web_launcher)):
        raise RuntimeError("packaged Python, CLI launcher, and app launcher must be executable")

    macho_records = manifest["python_runtime"]["macho_files"]
    for record in macho_records:
        path = app / Path(*PurePosixPath(record["path"]).parts)
        if sha256_file(path) != record["sha256"]:
            raise RuntimeError(f"extracted Mach-O SHA-256 changed: {record['path']}")
        slices = macho_slices(path)
        if len(slices) != 1 or slices[0]["architecture"] != "arm64":
            raise RuntimeError(f"extracted Mach-O is not arm64-only: {record['path']}")
        if slices[0]["minimum_macos"] != record["minimum_macos"]:
            raise RuntimeError(f"extracted Mach-O deployment target changed: {record['path']}")
        signature = subprocess.run(
            ["/usr/bin/codesign", "--display", "--verbose=2", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if signature.returncode == 0:
            raise RuntimeError(f"Phase 13A Mach-O unexpectedly remains signed: {record['path']}")

    codesign = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        check=False,
        capture_output=True,
        text=True,
    )
    if codesign.returncode == 0:
        raise RuntimeError("Phase 13A candidate unexpectedly carries a valid code signature")

    environment = os.environ.copy()
    fake_home = root / "用户 home with spaces"
    fake_home.mkdir()
    for variable in (
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
        "DYLD_FALLBACK_FRAMEWORK_PATH",
    ):
        environment.pop(variable, None)
    environment.update(
        {
            "HOME": str(fake_home),
            "CFFIXED_USER_HOME": str(fake_home),
            "PYTHONUTF8": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    commands: list[dict[str, Any]] = []
    dependency_probe, command = _run_json(
        [str(python), "-I", "-X", "utf8", "-c", DEPENDENCY_PROBE],
        cwd=root,
        environment=environment,
    )
    commands.append(command)
    outside_sys_path = [
        value
        for value in dependency_probe["sys_path"]
        if not isinstance(value, str) or not _inside_app(value, app)
    ]
    if outside_sys_path:
        raise RuntimeError(f"packaged sys.path escaped the app: {outside_sys_path}")
    outside_imports = [
        value
        for value in dependency_probe["imports"].values()
        if not isinstance(value, str) or not _inside_app(value, app)
    ]
    if outside_imports:
        raise RuntimeError(
            f"packaged application imported dependencies outside the app: {outside_imports}"
        )
    if not _inside_app(str(dependency_probe["python_executable"]), app):
        raise RuntimeError("packaged Python executable resolved outside the app")
    loaded_images = _loaded_image_inventory(dependency_probe.get("loaded_images"), app)
    if loaded_images["external"]:
        raise RuntimeError(
            "packaged process loaded non-system Mach-O images outside the app: "
            f"{loaded_images['external'][:20]}"
        )
    required_loaded_images = {
        "_ssl.cpython-312-darwin.so",
        "_hashlib.cpython-312-darwin.so",
        "libssl.3.dylib",
        "libcrypto.3.dylib",
    }
    observed_loaded_names = {Path(value).name for value in loaded_images["app"]}
    missing_loaded_images = sorted(required_loaded_images - observed_loaded_names)
    if missing_loaded_images:
        raise RuntimeError(
            "packaged TLS probe did not load required embedded Mach-O images: "
            f"{missing_loaded_images}"
        )
    expected_dependencies = {
        (_canonical_name(item["name"]), item["version"])
        for item in manifest["locked_dependencies"]["packages"]
    }
    observed_dependencies = {
        (_canonical_name(item["name"]), item["version"])
        for item in dependency_probe["packages"]
        if _canonical_name(item["name"]) != "topoforge"
    }
    if observed_dependencies != expected_dependencies:
        missing = sorted(expected_dependencies - observed_dependencies)
        extra = sorted(observed_dependencies - expected_dependencies)
        raise RuntimeError(
            f"packaged dependency identity differs from the manifest; "
            f"missing={missing[:20]}, extra={extra[:20]}"
        )

    doctor, command = _run_json(
        [str(cli_launcher), "doctor"],
        cwd=root,
        environment=environment,
    )
    commands.append(command)
    if doctor.get("python") != manifest["python_runtime"]["version"]:
        raise RuntimeError("packaged doctor reported the wrong Python version")
    web_check, command = _run_json(
        [str(web_launcher), "--check", "--no-open"],
        cwd=root,
        environment=environment,
    )
    commands.append(command)
    host_rejection = _require_rejected(
        [str(web_launcher), "--host=0.0.0.0", "--check", "--no-open"],
        cwd=root,
        environment=environment,
        expected_exit_code=64,
        expected_message="fixes Web binding to 127.0.0.1",
    )
    commands.append(host_rejection)
    expected_data_root = fake_home / "Library" / "Application Support" / "TopoForge"
    if Path(web_check["state_dir"]) != expected_data_root / "state":
        raise RuntimeError("packaged Web state default is outside Application Support/TopoForge")
    if Path(web_check["workspace_root"]) != expected_data_root / "workspaces":
        raise RuntimeError(
            "packaged Web workspace default is outside Application Support/TopoForge"
        )
    if [Path(value) for value in web_check["input_roots"]] != [fake_home]:
        raise RuntimeError("packaged Web input boundary must default to the current user home")
    if web_check.get("loopback_only") is not True:
        raise RuntimeError("packaged Web installation did not report a loopback-only boundary")
    if web_check.get("required_checks_passed") is not True:
        raise RuntimeError("packaged Web installation check did not pass")

    platform_core = verify_platform_core(
        root / "platform core with spaces" / "地形",
        python_executable=python,
        environment_overrides={
            "HOME": str(fake_home),
            "CFFIXED_USER_HOME": str(fake_home),
            "PYTHONNOUSERSITE": "1",
        },
    )
    static.update(
        {
            "host": host,
            "extracted_app": str(app),
            "path_contract": {
                "contains_spaces": True,
                "contains_non_ascii": True,
                "required_checks_passed": True,
            },
            "macho": {**static["macho"]},
            "dependencies": {
                "count": len(expected_dependencies),
                "imports": dependency_probe["imports"],
                "source_checkout_leak": False,
                "sys_path_closed": True,
                "tls_probe": dependency_probe["tls_probe"],
                "loaded_non_system_macho": loaded_images["app"],
                "required_checks_passed": True,
            },
            "doctor": doctor,
            "web_check": {
                **web_check,
                "external_host_rejected": True,
            },
            "platform_core": platform_core,
            "commands": commands,
            "native_execution": {
                "status": "passed",
                "native_arm64": True,
                "loaded_non_system_macho": loaded_images["app"],
                "loaded_non_system_macho_count": len(loaded_images["app"]),
                "apple_system_image_count": len(loaded_images["apple_system"]),
                "host_external_macho_count": 0,
                "tls_probe": dependency_probe["tls_probe"],
                "required_checks_passed": True,
            },
            "signed": False,
            "notarized": False,
            "clean_system_evidence": False,
            "gatekeeper_evidence": False,
            "bambu_phase13b_evidence": False,
            "required_checks_passed": True,
        }
    )
    return app, static


def verify_macos_app(
    archive: Path,
    *,
    config_path: Path,
    execute: bool,
    work_root: Path | None,
    expected_source_commit: str | None,
    repeat_archive: Path | None = None,
) -> dict[str, Any]:
    """Run static verification everywhere and optional native packaged execution."""
    config = load_config(config_path)
    if execute:
        if work_root is None:
            raise ValueError("--execute requires an exclusive --work-root")
        _app, report = execute_archive(
            archive,
            config=config,
            work_root=work_root,
            expected_source_commit=expected_source_commit,
        )
    else:
        report = inspect_archive(
            archive,
            config=config,
            expected_source_commit=expected_source_commit,
        )
    if repeat_archive is None:
        return report
    repeat = inspect_archive(
        repeat_archive,
        config=config,
        expected_source_commit=expected_source_commit,
    )
    if report["archive"]["sha256"] != repeat["archive"]["sha256"]:
        raise RuntimeError("repeat macOS app archive is not byte-identical")
    for field in (
        "source",
        "build_identity",
        "python_runtime",
        "locked_dependencies",
        "contents",
        "macho",
    ):
        if report[field] != repeat[field]:
            raise RuntimeError(f"repeat macOS app identity changed: {field}")
    report["reproducibility"] = {
        "primary_sha256": report["archive"]["sha256"],
        "repeat_sha256": repeat["archive"]["sha256"],
        "byte_identical": True,
        "source_and_payload_identical": True,
        "required_checks_passed": True,
    }
    return report


def main() -> int:
    """Validate the archive and retain a bounded evidence report."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--expected-source-commit")
    parser.add_argument("--repeat-archive", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report_path = args.report.expanduser().resolve()
    try:
        report = verify_macos_app(
            args.archive.expanduser().resolve(),
            config_path=args.config.expanduser().resolve(),
            execute=args.execute,
            work_root=None if args.work_root is None else args.work_root.expanduser(),
            expected_source_commit=args.expected_source_commit,
            repeat_archive=(
                None if args.repeat_archive is None else args.repeat_archive.expanduser().resolve()
            ),
        )
    except Exception as exc:
        failure = {
            "schema_version": VERIFICATION_SCHEMA_VERSION,
            "error": {"type": exc.__class__.__name__, "message": str(exc)},
            "native_execution": {
                "status": "failed" if args.execute else "not-run",
                "corrective_action": (
                    "Run on native arm64 macOS 15 or 26 and fix the reported packaged-app gate."
                ),
            },
            "clean_system_evidence": False,
            "gatekeeper_evidence": False,
            "signed": False,
            "bambu_phase13b_evidence": False,
            "required_checks_passed": False,
        }
        write_json_with_sha256(report_path, failure)
        raise
    bounds = load_config(args.config)["bounds"]
    if len(canonical_json_bytes(report)) > bounds["evidence_max_bytes"]:
        raise ValueError("macOS app verification report exceeds its configured evidence bound")
    write_json_with_sha256(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
