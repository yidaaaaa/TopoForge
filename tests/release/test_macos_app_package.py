"""Offline contracts for the unsigned Phase 13A macOS arm64 app candidate."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import time
import zipfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest
import scripts.build_macos_app as builder
import scripts.macos_app as macos_app_module
import scripts.verify_macos_app as app_verifier
import yaml
from scripts.macos_app import (
    APP_ROOT,
    CLI_LAUNCHER_PATH,
    INFO_PLIST_PATH,
    MANIFEST_PATH,
    MANIFEST_SCHEMA_VERSION,
    PYTHON_PATH,
    VERIFICATION_SCHEMA_VERSION,
    WEB_LAUNCHER_PATH,
    bundle_entries,
    canonical_json_bytes,
    inspect_archive,
    load_config,
    macho_slices,
    macho_slices_bytes,
    payload_sha256,
    register_macos_path,
    safe_relative_path,
    sha256_file,
    write_reproducible_zip,
)
from scripts.macos_macho import (
    PYTHON_FRAMEWORK_BUNDLE_PREFIX,
    PYTHON_FRAMEWORK_INSTALL_PREFIX,
    macho_closure_records,
    macho_closure_summary,
    macho_dynamic_slices_bytes,
    macho_rewrite_plans,
)
from scripts.verify_macos_app import verify_macos_app
from scripts.verify_macos_system import (
    _RECOVERY_RASTER_SHAPE,
    SYSTEM_SCHEMA_VERSION,
    _recovery_job_request,
    _strict_artifact_reopen_passed,
    validate_evidence_report,
    verify_macos_system,
)

from topoforge.models import BuildConfig
from topoforge.platform_paths import macos_application_data_root
from topoforge.raster.sampling import resolve_sampling_decision, triangle_count_for_shape


def _repository_root() -> Path:
    return Path(__file__).parents[2]


def _config_path() -> Path:
    return _repository_root() / "packaging" / "macos-arm64-runtime.json"


def _config() -> dict[str, Any]:
    return load_config(_config_path())


def _write_file(path: Path, payload: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)


def _encoded_version(value: str) -> int:
    parts = [int(item) for item in value.split(".")]
    parts.extend([0] * (3 - len(parts)))
    return (parts[0] << 16) | (parts[1] << 8) | parts[2]


def _macho_string_command(command: int, value: str, *, dylib: bool) -> bytes:
    encoded = value.encode("utf-8") + b"\0"
    prefix_size = 24 if dylib else 12
    command_size = (prefix_size + len(encoded) + 7) // 8 * 8
    if dylib:
        prefix = struct.pack("<6I", command, command_size, prefix_size, 0, 0, 0)
    else:
        prefix = struct.pack("<3I", command, command_size, prefix_size)
    return prefix + encoded + b"\0" * (command_size - prefix_size - len(encoded))


def _thin_macho(
    architecture: str = "arm64",
    minimum_macos: str = "11.0",
    *,
    file_type: int = 2,
    dylib_id: str | None = None,
    dependencies: tuple[tuple[int, str], ...] = (),
    rpaths: tuple[str, ...] = (),
) -> bytes:
    cpu = {"arm64": 0x0100000C, "x86_64": 0x01000007}[architecture]
    minimum = _encoded_version(minimum_macos)
    commands = [struct.pack("<6I", 0x32, 24, 1, minimum, minimum, 0)]
    if dylib_id is not None:
        commands.append(_macho_string_command(0x0D, dylib_id, dylib=True))
    commands.extend(
        _macho_string_command(command, value, dylib=True) for command, value in dependencies
    )
    commands.extend(_macho_string_command(0x8000001C, value, dylib=False) for value in rpaths)
    header = struct.pack(
        "<8I",
        0xFEEDFACF,
        cpu,
        0,
        file_type,
        len(commands),
        sum(len(command) for command in commands),
        0,
        0,
    )
    return header + b"".join(commands)


def _fat_universal2_macho() -> bytes:
    x86 = _thin_macho("x86_64", "10.13")
    arm = _thin_macho("arm64", "11.0")
    first_offset = 256
    second_offset = 512
    header = struct.pack(">2I", 0xCAFEBABE, 2)
    entries = b"".join(
        (
            struct.pack(">5I", 0x01000007, 0, first_offset, len(x86), 0),
            struct.pack(">5I", 0x0100000C, 0, second_offset, len(arm), 0),
        )
    )
    payload = bytearray(second_offset + len(arm))
    payload[: len(header + entries)] = header + entries
    payload[first_offset : first_offset + len(x86)] = x86
    payload[second_offset : second_offset + len(arm)] = arm
    return bytes(payload)


def test_runtime_extraction_uses_an_existing_scratch_root_outside_the_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "python.pkg"
    runtime.write_bytes(b"fixture")
    app = tmp_path / "bundle" / APP_ROOT
    destination = app / "Contents/Frameworks/Python.framework"
    scratch_root = tmp_path / "private scratch"

    class ExpectedStop(RuntimeError):
        pass

    def inspect_command(
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> dict[str, Any]:
        del environment
        assert cwd == scratch_root
        assert cwd.is_dir()
        assert Path(command[-1]) == scratch_root / "expanded-pkg"
        assert app not in Path(command[-1]).parents
        raise ExpectedStop

    monkeypatch.setattr(builder, "_run", inspect_command)

    with pytest.raises(ExpectedStop):
        builder._extract_framework(
            runtime,
            destination,
            _config(),
            scratch_root=scratch_root,
        )


def test_runtime_extraction_uses_the_official_framework_component_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "python.pkg"
    runtime.write_bytes(b"fixture")
    destination = tmp_path / "TopoForge.app/Contents/Frameworks/Python.framework"
    scratch_root = tmp_path / "private scratch"
    config = _config()
    framework_version = config["python_runtime"]["framework_version"]

    def expand_fixture(
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> dict[str, Any]:
        del command, cwd, environment
        component = scratch_root / "expanded-pkg/Python_Framework.pkg"
        payload = component / "Payload"
        primary = payload / "Versions" / framework_version / "Python"
        _write_file(primary, b"universal2 fixture", 0o755)
        _write_file(
            component / "PackageInfo",
            (
                b'<?xml version="1.0" encoding="utf-8"?>\n'
                b'<pkg-info identifier="org.python.Python.PythonFramework-3.12" '
                b'install-location="/Library/Frameworks/Python.framework"/>\n'
            ),
        )
        _write_file(
            payload / "Versions" / framework_version / "_CodeSignature/CodeResources",
            b"upstream signature fixture",
        )
        return {}

    monkeypatch.setattr(builder, "_run", expand_fixture)
    monkeypatch.setattr(
        builder,
        "macho_slices",
        lambda _path: [
            {"architecture": "x86_64", "minimum_macos": "10.13"},
            {"architecture": "arm64", "minimum_macos": "11.0"},
        ],
    )

    builder._extract_framework(
        runtime,
        destination,
        config,
        scratch_root=scratch_root,
    )

    assert (destination / "Versions" / framework_version / "Python").read_bytes() == (
        b"universal2 fixture"
    )
    assert not (destination / "Versions" / framework_version / "_CodeSignature").exists()
    assert not (destination / "PackageInfo").exists()


def test_builder_removes_only_the_pinned_source_only_runtime_entries(tmp_path: Path) -> None:
    config = _config()
    framework = tmp_path / "Python.framework"
    source_only_paths = config["python_runtime"]["source_only_paths"]
    retained = framework / "Versions/3.12/bin/python3.12"
    _write_file(retained, _fat_universal2_macho(), 0o755)

    for name in source_only_paths:
        path = framework.joinpath(*PurePosixPath(name).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith("/python3-intel64"):
            path.symlink_to("python3.12-intel64")
        else:
            path.write_bytes(b"source-only fixture")
            path.with_name(f"._{path.name}").write_bytes(b"AppleDouble fixture")

    builder._remove_source_only_runtime_entries(framework, config)

    assert retained.is_file()
    for name in source_only_paths:
        path = framework.joinpath(*PurePosixPath(name).parts)
        assert not os.path.lexists(path)
        assert not os.path.lexists(path.with_name(f"._{path.name}"))


def _fixture_archive(
    root: Path,
    *,
    architecture: str = "arm64",
    minimum_macos: str = "11.0",
    web_payload: bytes = b"<!doctype html><title>TopoForge fixture</title>\n",
    extra_macho_payloads: dict[str, bytes] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    app = root / APP_ROOT
    python = app / PYTHON_PATH
    _write_file(python, _thin_macho(architecture, minimum_macos), 0o755)
    _write_file(app / CLI_LAUNCHER_PATH, builder.CLI_LAUNCHER.encode(), 0o755)
    _write_file(app / WEB_LAUNCHER_PATH, builder.WEB_LAUNCHER.encode(), 0o755)
    for relative, payload in sorted((extra_macho_payloads or {}).items()):
        _write_file(app.joinpath(*PurePosixPath(relative).parts), payload, 0o755)
    plist = {
        "CFBundleExecutable": "TopoForge",
        "CFBundleIdentifier": "org.topoforge.app",
        "CFBundleName": "TopoForge",
        "CFBundlePackageType": "APPL",
        "LSMinimumSystemVersion": "15.0",
    }
    _write_file(
        app / INFO_PLIST_PATH,
        plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True),
    )
    site_packages = (
        app / "Contents/Frameworks/Python.framework/Versions/3.12/lib/python3.12/site-packages"
    )
    _write_file(site_packages / "topoforge/web/static/index.html", web_payload)
    _write_file(site_packages / "fixture_dependency.py", b"VALUE = 1\n")
    _write_file(
        app / "Contents/Frameworks/Python.framework/Versions/3.12/Resources/runtime.txt",
        b"runtime fixture\n",
    )
    framework = app / "Contents/Frameworks/Python.framework"
    (framework / "Versions/Current").symlink_to("3.12")
    (framework / "Resources").symlink_to("Versions/Current/Resources")

    config = _config()
    files = bundle_entries(app, bounds=config["bounds"])
    primary = macho_slices(python)[0]
    macho_payloads = {
        PYTHON_PATH: python.read_bytes(),
        **(extra_macho_payloads or {}),
    }
    if architecture == "arm64":
        closure = macho_closure_records(macho_payloads, executable_path=PYTHON_PATH)
    else:
        # Preserve the deliberately invalid fixture until archive inspection exercises
        # the architecture gate; closure verification itself is arm64-only by contract.
        closure = [
            {
                "path": PYTHON_PATH,
                "file_type": 2,
                "dylib_id": None,
                "rpaths": [],
                "dependencies": [],
            }
        ]
    macho = []
    for closure_record in closure:
        path = app.joinpath(*PurePosixPath(closure_record["path"]).parts)
        identity = macho_slices(path)[0]
        macho.append(
            {
                "path": closure_record["path"],
                "architecture": identity["architecture"],
                "minimum_macos": identity["minimum_macos"],
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                **closure_record,
            }
        )
    digest = "1" * 64
    uv_lock_digest = "2" * 64
    runtime = deepcopy(config["python_runtime"])
    runtime.update(
        {
            "macho_architectures": ["arm64"],
            "primary_macho_minimum_macos": primary["minimum_macos"],
            "macho_file_count": len(macho),
            "macho_minimum_versions": sorted({item["minimum_macos"] for item in macho}),
            "macho_closure": macho_closure_summary(closure),
            "macho_files": macho,
        }
    )
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "package_role": "phase13a-macos-arm64-unsigned-candidate",
        "topoforge_version": "0.10.3",
        "target": config["target"],
        "source": {"commit": "a" * 40, "tracked_dirty": False},
        "build_identity": {
            "config_sha256": digest,
            "uv_lock_sha256": uv_lock_digest,
            "pyproject_sha256": "3" * 64,
            "build_constraints_sha256": "4" * 64,
            "verifier_sha256": {
                "builder": "5" * 64,
                "shared": "6" * 64,
                "macho": "7" * 64,
                "archive": "7" * 64,
                "system": "8" * 64,
            },
        },
        "python_runtime": runtime,
        "locked_dependencies": {
            "count": 1,
            "packages": [
                {
                    "name": "fixture-dependency",
                    "canonical_name": "fixture-dependency",
                    "version": "1.0",
                }
            ],
            "requirements_path": "Contents/Resources/provenance/requirements.txt",
            "requirements_sha256": "9" * 64,
            "uv_lock_path": "Contents/Resources/provenance/uv.lock",
            "uv_lock_sha256": uv_lock_digest,
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
    _write_file(app / MANIFEST_PATH, canonical_json_bytes(manifest))
    archive = root / "topoforge-0.10.3-macos-arm64-unsigned-candidate.zip"
    write_reproducible_zip(
        app,
        archive,
        source_date_epoch=config["source_date_epoch"],
        bounds=config["bounds"],
        overwrite=False,
    )
    return archive


def _archive_with_missing_internal_link(source: Path, destination: Path) -> None:
    config = _config()
    value = time.gmtime(config["source_date_epoch"])
    timestamp = (*value[:5], value.tm_sec // 2 * 2)
    with zipfile.ZipFile(source) as archive:
        members = {
            info.filename: {
                "payload": archive.read(info),
                "external_attr": info.external_attr,
            }
            for info in archive.infolist()
        }
    manifest_name = f"{APP_ROOT}/{MANIFEST_PATH}"
    manifest = json.loads(members[manifest_name]["payload"])
    target = "missing-target"
    relative = "Contents/zz-missing-link"
    target_payload = target.encode()
    manifest["contents"]["files"].append(
        {
            "path": relative,
            "kind": "symlink",
            "target": target,
            "bytes": len(target_payload),
            "sha256": hashlib.sha256(target_payload).hexdigest(),
        }
    )
    manifest["contents"]["files"].sort(key=lambda item: item["path"])
    manifest["contents"]["file_count"] = len(manifest["contents"]["files"])
    manifest["contents"]["payload_sha256"] = payload_sha256(manifest["contents"]["files"])
    members[manifest_name]["payload"] = canonical_json_bytes(manifest)
    members[f"{APP_ROOT}/{relative}"] = {
        "payload": target_payload,
        "external_attr": (stat.S_IFLNK | 0o777) << 16,
    }
    with zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for name in sorted(members):
            record = members[name]
            info = zipfile.ZipInfo(name, date_time=timestamp)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.flag_bits |= 0x800
            info.external_attr = record["external_attr"]
            archive.writestr(info, record["payload"])


def _valid_system_report(archive: Path) -> dict[str, Any]:
    archive_report = inspect_archive(
        archive,
        config=_config(),
        expected_source_commit="a" * 40,
    )
    archive_report["native_execution"] = {
        "loaded_non_system_macho": [
            "/candidate/TopoForge.app/Contents/Frameworks/libcrypto.3.dylib",
            "/candidate/TopoForge.app/Contents/Frameworks/libssl.3.dylib",
            "/candidate/TopoForge.app/Contents/Frameworks/_hashlib.cpython-312-darwin.so",
            "/candidate/TopoForge.app/Contents/Frameworks/_ssl.cpython-312-darwin.so",
        ],
        "loaded_non_system_macho_count": 4,
        "apple_system_image_count": 1,
        "host_external_macho_count": 0,
        "tls_probe": {
            "openssl_version": "OpenSSL fixture",
            "sha256": "a" * 64,
            "sha256_constructor": "b" * 64,
            "ssl_context_type": "SSLContext",
            "https_connection_type": "HTTPSConnection",
            "https_handler_type": "HTTPSHandler",
            "required_checks_passed": True,
        },
        "status": "passed",
        "native_arm64": True,
        "required_checks_passed": True,
    }
    return {
        "schema_version": SYSTEM_SCHEMA_VERSION,
        "package_role": "phase13a-macos-arm64-unsigned-candidate",
        "evidence_scope": "hosted-package",
        "target_id": "macos-15-arm64",
        "host": {
            "system": "Darwin",
            "machine": "arm64",
            "macos_version": "15.7.9",
            "macos_major": 15,
            "native_arm64": True,
            "translated": False,
        },
        "source": archive_report["source"],
        "archive": archive_report["archive"],
        "app_payload_sha256": archive_report["contents"]["payload_sha256"],
        "archive_verification": archive_report,
        "web_lifecycle": {
            "completed_job": {
                "job_id": "fixture-job",
                "workflow_id": "fixture-workflow",
                "artifact_sha256": {
                    "model_stl": "a" * 64,
                    "model_3mf": "b" * 64,
                    "preview_glb": "c" * 64,
                },
            },
            "strict_reopen": {},
            "backup_restore": {
                "backup_id": "fixture-backup",
                "archive_sha256": "d" * 64,
                "restored_job_id": "fixture-restored",
                "required_checks_passed": True,
            },
            "restart_cancellation": {
                "original_worker_pid": 1234,
                "same_worker_recovered": True,
                "terminal_state": "cancelled",
                "process_reaped": True,
                "required_checks_passed": True,
            },
            "server_binding": "127.0.0.1",
            "commands": [{"command": ["fixture"]}],
            "required_checks_passed": True,
        },
        "package_evidence": True,
        "hosted_package_evidence": True,
        "clean_system_evidence": False,
        "signed": False,
        "notarized": False,
        "quarantine_first_launch_evidence": False,
        "gatekeeper_evidence": False,
        "bambu_phase13b_evidence": False,
        "public_support_status": "unverified",
        "required_checks_passed": True,
    }


def test_runtime_identity_is_exactly_pinned_to_official_cpython_31210() -> None:
    config = _config()
    runtime = config["python_runtime"]

    assert runtime["provider"] == "Python Software Foundation"
    assert runtime["version"] == "3.12.10"
    assert runtime["url"] == (
        "https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg"
    )
    assert runtime["sha256"] == ("8373e58da4ea146b3eb1c1f9834f19a319440b6b679b06050b1f9ee3237aa8e4")
    assert runtime["bytes"] == 45_720_356
    assert runtime["source_architecture"] == "universal2"
    assert runtime["source_primary_macho"] == {
        "architectures": ["x86_64", "arm64"],
        "minimum_macos": {"x86_64": "10.13", "arm64": "11.0"},
    }
    assert runtime["embedded_primary_macho"] == {
        "architecture": "arm64",
        "minimum_macos": "11.0",
    }
    assert runtime["source_only_paths"] == [
        "Versions/3.12/bin/python3-intel64",
        "Versions/3.12/bin/python3.12-intel64",
        "Versions/3.12/lib/itcl4.3.2/libitclstub4.3.2.a",
        "Versions/3.12/lib/libtclstub8.6.a",
        "Versions/3.12/lib/libtkstub8.6.a",
        "Versions/3.12/lib/python3.12/config-3.12-darwin/python.o",
        "Versions/3.12/lib/tdbc1.1.10/libtdbcstub1.1.10.a",
    ]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("version", "3.12.11"),
        ("url", "https://www.python.org/ftp/python/3.12.10/other.pkg"),
        ("sha256", "0" * 64),
        ("bytes", 45_720_355),
        ("source_architecture", "arm64"),
        ("embedded_architecture", "universal2"),
    ],
)
def test_runtime_config_rejects_any_pinned_identity_drift(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    raw = json.loads(_config_path().read_text(encoding="utf-8"))
    raw["python_runtime"][field] = replacement
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="pinned CPython runtime identity changed"):
        load_config(path)


def test_runtime_config_rejects_source_only_payload_drift(tmp_path: Path) -> None:
    raw = json.loads(_config_path().read_text(encoding="utf-8"))
    raw["python_runtime"]["source_only_paths"].pop()
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="source-only payload identity changed"):
        load_config(path)


def test_runtime_config_rejects_macho_and_matrix_drift(tmp_path: Path) -> None:
    raw = json.loads(_config_path().read_text(encoding="utf-8"))
    raw["python_runtime"]["source_primary_macho"]["minimum_macos"]["arm64"] = "12.0"
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="universal2 Mach-O identity changed"):
        load_config(path)

    raw = json.loads(_config_path().read_text(encoding="utf-8"))
    raw["target"]["unsupported_0_12_x"].remove("Intel x86_64")
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="exclusions must remain explicit"):
        load_config(path)


@pytest.mark.parametrize(
    ("system", "machine", "version"),
    [
        ("Linux", "arm64", "15.7.9"),
        ("Darwin", "x86_64", "15.7.9"),
        ("Darwin", "arm64", "14.8.9"),
        ("Darwin", "arm64", "27.0"),
    ],
)
def test_native_builder_rejects_non_candidate_hosts(
    system: str,
    machine: str,
    version: str,
) -> None:
    with pytest.raises(RuntimeError, match=r"native arm64 macOS|stable macOS 15 or 26"):
        builder.require_native_macos_arm64(
            system=system,
            machine=machine,
            version=version,
        )


@pytest.mark.parametrize("version", ["15.7.9", "26.6.1"])
def test_native_builder_accepts_only_nontranslated_frozen_hosts(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
) -> None:
    monkeypatch.setattr(
        builder.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    report = builder.require_native_macos_arm64(
        system="Darwin",
        machine="arm64",
        version=version,
    )

    assert report["native_arm64"] is True
    assert report["macos_major"] == int(version.split(".")[0])


def test_native_builder_rejects_rosetta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        builder.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="1\n", stderr=""),
    )
    with pytest.raises(RuntimeError, match="Rosetta"):
        builder.require_native_macos_arm64(
            system="Darwin",
            machine="arm64",
            version="15.7.9",
        )


def test_real_builder_fails_closed_before_work_on_linux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(builder.platform, "system", lambda: "Linux")
    monkeypatch.setattr(builder.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(builder.platform, "mac_ver", lambda: ("15.7.9", ("", "", ""), ""))

    with pytest.raises(RuntimeError, match="Linux may run contract tests only"):
        builder.build_macos_app(
            repository_root=_repository_root(),
            config_path=_config_path(),
            output_dir=tmp_path / "output",
            runtime_archive=None,
            cache_dir=tmp_path / "cache",
            uv_executable="uv",
            expected_version=None,
            overwrite=False,
        )


def test_native_archive_execution_fails_closed_with_corrective_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_verifier.platform, "system", lambda: "Linux")
    monkeypatch.setattr(app_verifier.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(app_verifier.platform, "mac_ver", lambda: ("", ("", "", ""), ""))

    with pytest.raises(RuntimeError, match="Run this command on native Apple Silicon"):
        app_verifier._native_host(_config())


def test_macho_parser_measures_thin_and_universal2_fixtures() -> None:
    thin = macho_slices_bytes(_thin_macho("arm64", "11.0"))
    universal = macho_slices_bytes(_fat_universal2_macho())

    assert [(item["architecture"], item["minimum_macos"]) for item in thin] == [("arm64", "11.0")]
    assert [(item["architecture"], item["minimum_macos"]) for item in universal] == [
        ("x86_64", "10.13"),
        ("arm64", "11.0"),
    ]


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "Contents\\Windows",
        "Contents/../../escape",
        "C:/drive",
    ],
)
def test_bundle_paths_reject_noncanonical_or_escaping_names(path: str) -> None:
    with pytest.raises(ValueError):
        safe_relative_path(path)


def test_macos_path_registry_rejects_case_and_unicode_normalization_collisions() -> None:
    seen: dict[str, tuple[str, str]] = {}
    register_macos_path(PurePosixPath("TopoForge.app/Contents/File"), seen, "file")
    with pytest.raises(ValueError, match="collide"):
        register_macos_path(PurePosixPath("TopoForge.app/contents/file"), seen, "file")

    seen = {}
    register_macos_path(PurePosixPath("TopoForge.app/Caf\u00e9"), seen, "file")
    with pytest.raises(ValueError, match="collide"):
        register_macos_path(PurePosixPath("TopoForge.app/Cafe\u0301"), seen, "file")


def test_bundle_closure_accepts_framework_link_chains_and_rejects_hardlinks(
    tmp_path: Path,
) -> None:
    archive = _fixture_archive(tmp_path / "valid")
    app = archive.parent / APP_ROOT
    records = bundle_entries(app, bounds=_config()["bounds"])

    assert any(item["kind"] == "symlink" for item in records)

    hardlink_app = tmp_path / "hardlink" / APP_ROOT
    first = hardlink_app / "Contents/first"
    _write_file(first, b"payload")
    os.link(first, hardlink_app / "Contents/second")
    with pytest.raises(ValueError, match="hard-linked"):
        bundle_entries(hardlink_app, bounds=_config()["bounds"])


def test_bundle_closure_does_not_trust_direntry_link_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _fixture_archive(tmp_path / "fixture")
    app = archive.parent / APP_ROOT
    real_scandir = os.scandir

    class EntryWithoutReliableStat:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self.name = entry.name
            self.path = entry.path

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            path = Path(self.path)
            return path.is_dir() if follow_symlinks else stat.S_ISDIR(path.lstat().st_mode)

        def is_symlink(self) -> bool:
            return Path(self.path).is_symlink()

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            del follow_symlinks
            raise AssertionError("bundle closure must use a fresh lexical lstat")

    @contextmanager
    def scandir_without_reliable_stat(path: Path) -> Any:
        with real_scandir(path) as entries:
            yield iter(EntryWithoutReliableStat(entry) for entry in entries)

    monkeypatch.setattr(macos_app_module.os, "scandir", scandir_without_reliable_stat)

    records = bundle_entries(app, bounds=_config()["bounds"])

    assert any(record["path"] == INFO_PLIST_PATH for record in records)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO fixture requires POSIX")
def test_bundle_closure_rejects_special_files_and_escaping_links(tmp_path: Path) -> None:
    fifo_app = tmp_path / "fifo" / APP_ROOT
    fifo = fifo_app / "Contents/special"
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="special object"):
        bundle_entries(fifo_app, bounds=_config()["bounds"])

    link_app = tmp_path / "link" / APP_ROOT
    link = link_app / "Contents/link"
    link.parent.mkdir(parents=True)
    link.symlink_to("/tmp/outside")
    with pytest.raises(ValueError, match="absolute"):
        bundle_entries(link_app, bounds=_config()["bounds"])


def test_reproducible_archive_and_static_closure_pass(tmp_path: Path) -> None:
    archive = _fixture_archive(tmp_path / "primary")
    app = archive.parent / APP_ROOT
    repeat = archive.parent / "repeat.zip"
    write_reproducible_zip(
        app,
        repeat,
        source_date_epoch=_config()["source_date_epoch"],
        bounds=_config()["bounds"],
        overwrite=False,
    )

    assert archive.read_bytes() == repeat.read_bytes()
    report = inspect_archive(
        archive,
        config=_config(),
        expected_source_commit="a" * 40,
    )
    assert report["required_checks_passed"] is True
    assert report["macho"]["architecture"] == "arm64"
    assert report["info_plist"]["LSMinimumSystemVersion"] == "15.0"
    with zipfile.ZipFile(archive) as candidate:
        infos = candidate.infolist()
        assert [item.filename for item in infos] == sorted(item.filename for item in infos)
        assert all(not item.extra and not item.comment for item in infos)
        assert all(item.compress_type == zipfile.ZIP_DEFLATED for item in infos)
        assert all(item.create_system == 3 for item in infos)


def test_rewritten_internal_macho_closure_archive_is_deterministic(tmp_path: Path) -> None:
    library = "Contents/Frameworks/Python.framework/Versions/3.12/lib"
    dynamic = f"{library}/python3.12/lib-dynload"
    libcrypto = f"{library}/libcrypto.3.dylib"
    libssl = f"{library}/libssl.3.dylib"
    extra = {
        libcrypto: _thin_macho(
            file_type=6,
            dylib_id="@loader_path/libcrypto.3.dylib",
        ),
        libssl: _thin_macho(
            file_type=6,
            dylib_id="@loader_path/libssl.3.dylib",
            dependencies=((0x0C, "@loader_path/libcrypto.3.dylib"),),
        ),
        f"{dynamic}/_ssl.cpython-312-darwin.so": _thin_macho(
            file_type=8,
            dependencies=(
                (0x0C, "@loader_path/../../libssl.3.dylib"),
                (0x0C, "@loader_path/../../libcrypto.3.dylib"),
            ),
        ),
        f"{dynamic}/_hashlib.cpython-312-darwin.so": _thin_macho(
            file_type=8,
            dependencies=((0x0C, "@loader_path/../../libcrypto.3.dylib"),),
        ),
    }
    archive = _fixture_archive(
        tmp_path / "normalized",
        extra_macho_payloads=extra,
    )
    repeat = archive.with_name("repeat-normalized.zip")
    write_reproducible_zip(
        archive.parent / APP_ROOT,
        repeat,
        source_date_epoch=_config()["source_date_epoch"],
        bounds=_config()["bounds"],
        overwrite=False,
    )

    assert archive.read_bytes() == repeat.read_bytes()
    report = inspect_archive(archive, config=_config())
    assert report["macho"]["file_count"] == 5
    assert report["macho"]["dependency_count"] == 4
    assert report["macho"]["bundled_dependency_count"] == 4
    assert report["macho"]["apple_system_dependency_count"] == 0
    assert report["macho"]["dylib_id_count"] == 2
    assert report["macho"]["rpath_count"] == 0
    assert report["macho"]["required_checks_passed"] is True


def test_static_archive_rejects_noncanonical_member_order(tmp_path: Path) -> None:
    source = _fixture_archive(tmp_path / "primary")
    mutated = tmp_path / "reordered.zip"
    with zipfile.ZipFile(source) as archive:
        records = [(info, archive.read(info)) for info in archive.infolist()]
    with zipfile.ZipFile(
        mutated,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for original, payload in reversed(records):
            info = zipfile.ZipInfo(original.filename, date_time=original.date_time)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.flag_bits |= 0x800
            info.external_attr = original.external_attr
            archive.writestr(info, payload)

    with pytest.raises(ValueError, match="canonical order"):
        inspect_archive(mutated, config=_config())


def test_static_archive_rejects_extra_file_outside_manifest(tmp_path: Path) -> None:
    archive = _fixture_archive(tmp_path / "primary")
    app = archive.parent / APP_ROOT
    _write_file(app / "Contents/extra.txt", b"undeclared")
    mutated = archive.parent / "extra.zip"
    write_reproducible_zip(
        app,
        mutated,
        source_date_epoch=_config()["source_date_epoch"],
        bounds=_config()["bounds"],
        overwrite=False,
    )

    with pytest.raises(ValueError, match="does not cover the archive exactly"):
        inspect_archive(mutated, config=_config())


@pytest.mark.parametrize(
    ("architecture", "minimum_macos", "message"),
    [
        ("x86_64", "10.13", "not arm64-only"),
        ("arm64", "16.0", "above app target"),
    ],
)
def test_static_archive_parses_and_rejects_invalid_macho(
    tmp_path: Path,
    architecture: str,
    minimum_macos: str,
    message: str,
) -> None:
    archive = _fixture_archive(
        tmp_path / architecture,
        architecture=architecture,
        minimum_macos=minimum_macos,
    )
    with pytest.raises(ValueError, match=message):
        inspect_archive(archive, config=_config())


def test_static_archive_rejects_missing_internal_symlink_target(tmp_path: Path) -> None:
    archive = _fixture_archive(tmp_path / "primary")
    mutated = tmp_path / "missing-link.zip"
    _archive_with_missing_internal_link(archive, mutated)

    with pytest.raises(ValueError, match="symlink target is missing"):
        inspect_archive(mutated, config=_config())


def test_repeat_archive_verifier_requires_byte_identical_candidate(tmp_path: Path) -> None:
    primary = _fixture_archive(tmp_path / "primary")
    repeat_dir = tmp_path / "repeat"
    repeat_dir.mkdir()
    repeat = repeat_dir / primary.name
    shutil.copy2(primary, repeat)

    report = verify_macos_app(
        primary,
        config_path=_config_path(),
        execute=False,
        work_root=None,
        expected_source_commit="a" * 40,
        repeat_archive=repeat,
    )
    assert report["reproducibility"] == {
        "primary_sha256": sha256_file(primary),
        "repeat_sha256": sha256_file(primary),
        "byte_identical": True,
        "source_and_payload_identical": True,
        "required_checks_passed": True,
    }

    changed = _fixture_archive(
        tmp_path / "changed",
        web_payload=b"<!doctype html><title>changed</title>\n",
    )
    with pytest.raises(RuntimeError, match="not byte-identical"):
        verify_macos_app(
            primary,
            config_path=_config_path(),
            execute=False,
            work_root=None,
            expected_source_commit="a" * 40,
            repeat_archive=changed,
        )


@pytest.mark.skipif(os.name == "nt", reason="macOS launchers require a POSIX shell execution host")
def test_launcher_info_plist_and_application_support_contract(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    for name in ("LICENSE", "DATA_LICENSES.md", "THIRD_PARTY_NOTICES.md"):
        (repository / name).write_text(f"{name}\n", encoding="utf-8")
    app = tmp_path / "候选 app with spaces" / APP_ROOT
    builder._write_support_files(app, repository, "0.10.3")
    capture = tmp_path / "launcher-capture.txt"
    fake_python = app / PYTHON_PATH
    _write_file(
        fake_python,
        (
            b"#!/bin/sh\n"
            b"{\n"
            b"  printf '%s\\n' \"${DYLD_LIBRARY_PATH-unset}\"\n"
            b"  printf '%s\\n' \"${DYLD_FRAMEWORK_PATH-unset}\"\n"
            b"  printf '%s\\n' \"${DYLD_FALLBACK_LIBRARY_PATH-unset}\"\n"
            b"  printf '%s\\n' \"${DYLD_FALLBACK_FRAMEWORK_PATH-unset}\"\n"
            b"  printf '%s\\n' \"$@\"\n"
            b'} > "$CAPTURE"\n'
        ),
        0o755,
    )
    environment = os.environ.copy()
    environment.update(
        {
            "DYLD_LIBRARY_PATH": "/host/library",
            "DYLD_FRAMEWORK_PATH": "/host/framework",
            "DYLD_FALLBACK_LIBRARY_PATH": "/host/fallback-library",
            "DYLD_FALLBACK_FRAMEWORK_PATH": "/host/fallback-framework",
        }
    )
    environment["CAPTURE"] = str(capture)

    subprocess.run(
        [str(app / CLI_LAUNCHER_PATH), "doctor"],
        check=True,
        cwd=tmp_path,
        env=environment,
    )
    cli_lines = capture.read_text(encoding="utf-8").splitlines()
    assert cli_lines[:4] == ["unset"] * 4
    assert cli_lines[4:] == [
        "-I",
        "-X",
        "utf8",
        "-m",
        "topoforge.cli.app",
        "doctor",
    ]

    subprocess.run(
        [str(app / WEB_LAUNCHER_PATH), "--check", "--no-open"],
        check=True,
        cwd=tmp_path,
        env=environment,
    )
    web_lines = capture.read_text(encoding="utf-8").splitlines()
    assert web_lines[:4] == ["unset"] * 4
    assert web_lines[4:11] == [
        "-I",
        "-X",
        "utf8",
        "-m",
        "topoforge.cli.app",
        "web",
        "--host",
    ]
    assert web_lines[11:] == ["127.0.0.1", "--check", "--no-open"]

    rejected = subprocess.run(
        [str(app / WEB_LAUNCHER_PATH), "--host=0.0.0.0"],
        check=False,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 64
    assert "fixes Web binding to 127.0.0.1" in rejected.stderr

    plist = plistlib.loads((app / INFO_PLIST_PATH).read_bytes())
    assert plist["CFBundleExecutable"] == "TopoForge"
    assert plist["LSArchitecturePriority"] == ["arm64"]
    assert plist["LSMinimumSystemVersion"] == "15.0"
    assert macos_application_data_root(home=Path("/Users/test")) == (
        Path("/Users/test/Library/Application Support/TopoForge")
    )


def test_builder_source_identity_rejects_untracked_files(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"],
        cwd=repository,
        check=True,
    )
    (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)

    assert builder._source_record(repository)["tracked_dirty"] is False
    (repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="completely clean source tree"):
        builder._source_record(repository)


def test_strict_packaged_artifact_reopen_uses_role_contracts() -> None:
    mesh = {
        "finite_vertices": True,
        "finite_face_normals": True,
        "watertight": True,
        "winding_consistent": True,
        "manifold": True,
        "positive_volume": True,
        "flat_bottom": True,
        "connected_components": 1,
        "degenerate_faces": 0,
        "duplicate_faces": 0,
        "triangle_count": 48,
    }
    assert _strict_artifact_reopen_passed("model_stl", mesh)
    assert _strict_artifact_reopen_passed("preview_glb", mesh)
    assert not (_strict_artifact_reopen_passed("model_stl", {**mesh, "duplicate_faces": 1}))

    three_mf = {
        "unit": "millimeter",
        "object_count": 1,
        "build_item_count": 1,
        "vertex_count": 32,
        "triangle_count": 48,
        "strict_warning_count": 0,
    }
    assert _strict_artifact_reopen_passed("model_3mf", three_mf)
    assert not (
        _strict_artifact_reopen_passed("model_3mf", {**three_mf, "strict_warning_count": 1})
    )
    assert not _strict_artifact_reopen_passed("unknown", {})


def test_recovery_probe_strict_budget_accepts_its_full_source_grid(tmp_path: Path) -> None:
    request = _recovery_job_request(tmp_path / "source.tif", tmp_path / "workspace")
    config = BuildConfig.model_validate(request["launch"]["build"])

    decision = resolve_sampling_decision(
        _RECOVERY_RASTER_SHAPE,
        ground_width_m=1.0,
        ground_depth_m=1.0,
        config=config,
    )

    assert decision.target_shape == _RECOVERY_RASTER_SHAPE
    assert decision.estimated_triangle_count == triangle_count_for_shape(_RECOVERY_RASTER_SHAPE)


def test_evidence_schema_accepts_hosted_package_and_clean_scope_fixture(
    tmp_path: Path,
) -> None:
    archive = _fixture_archive(tmp_path / "candidate")
    report = _valid_system_report(archive)
    validate_evidence_report(report)

    clean = deepcopy(report)
    clean["evidence_scope"] = "clean-system"
    clean["hosted_package_evidence"] = False
    clean["clean_system_evidence"] = True
    validate_evidence_report(clean)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"clean_system_evidence": True}, "True was expected to be False|False was expected"),
        ({"signed": True}, "False was expected"),
        ({"public_support_status": "supported"}, "unverified"),
    ],
)
def test_evidence_schema_rejects_claim_promotion(
    tmp_path: Path,
    mutation: dict[str, Any],
    message: str,
) -> None:
    report = _valid_system_report(_fixture_archive(tmp_path / "candidate"))
    report.update(mutation)

    with pytest.raises(jsonschema.ValidationError, match=message):
        validate_evidence_report(report)


def test_evidence_validator_rejects_cross_binding_drift(tmp_path: Path) -> None:
    report = _valid_system_report(_fixture_archive(tmp_path / "candidate"))
    report["source"] = {"commit": "b" * 40, "tracked_dirty": False}

    with pytest.raises(ValueError, match="source differs"):
        validate_evidence_report(report)


def test_github_hosted_runner_cannot_emit_clean_system_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(RuntimeError, match="cannot emit clean-system"):
        verify_macos_system(
            tmp_path / "missing.zip",
            config_path=_config_path(),
            work_root=tmp_path / "work",
            expected_source_commit="a" * 40,
            expected_target="macos-15-arm64",
            evidence_scope="clean-system",
        )


def test_macos_workflow_builds_one_candidate_and_accepts_same_sha_on_both_hosts() -> None:
    workflow_path = _repository_root() / ".github/workflows/macos.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    jobs = workflow["jobs"]
    package = jobs["package-arm64"]
    acceptance = jobs["packaged-app-acceptance"]

    assert package["needs"] == "native-arm64"
    assert package["runs-on"] == "macos-15"
    package_runs = "\n".join(str(step.get("run", "")) for step in package["steps"])
    package_steps = {step.get("name"): step for step in package["steps"]}
    assert package_runs.count("scripts/build_macos_app.py") == 2
    assert "scripts/verify_macos_app.py" in package_runs
    assert "--repeat-archive" in package_runs
    assert 'cmp "$archive" "$repeat"' in package_runs
    assert "git status --porcelain --untracked-files=all" in package_runs
    assert package_steps["Build primary unsigned app candidate"]["id"] == "primary-build"
    diagnostic = package_steps["Report primary candidate build failure"]
    assert diagnostic["if"] == "failure() && steps.primary-build.outcome == 'failure'"
    assert "scripts/report_json_failure.py" in diagnostic["run"]
    assert "ci-macos-app-primary-build.json" in diagnostic["run"]

    assert acceptance["needs"] == "package-arm64"
    assert acceptance["strategy"]["matrix"]["include"] == [
        {"target_id": "macos-15-arm64", "runner": "macos-15"},
        {"target_id": "macos-26-arm64", "runner": "macos-26"},
    ]
    assert set(acceptance["env"]) == {
        "EXPECTED_SOURCE_COMMIT",
        "EXPECTED_ARCHIVE_FILENAME",
        "EXPECTED_ARCHIVE_SHA256",
        "EXPECTED_APP_PAYLOAD_SHA256",
    }
    acceptance_runs = "\n".join(str(step.get("run", "")) for step in acceptance["steps"])
    assert "scripts/verify_macos_system.py" in acceptance_runs
    assert "--evidence-scope hosted-package" in acceptance_runs
    assert "--evidence-scope clean-system" not in acceptance_runs
    assert "EXPECTED_ARCHIVE_SHA256" in acceptance_runs
    assert "EXPECTED_SOURCE_COMMIT" in acceptance_runs
    acceptance_steps = {step.get("name"): step for step in acceptance["steps"]}
    diagnostic = acceptance_steps["Report packaged acceptance failure"]
    assert diagnostic["if"] == "failure() && steps.packaged-system.outcome == 'failure'"
    assert "scripts/report_json_failure.py" in diagnostic["run"]
    assert "hosted-package.json" in diagnostic["run"]

    action_revision = re.compile(r"^[^@]+@[0-9a-f]{40}$")
    artifact_names: list[str] = []
    for job in jobs.values():
        for step in job["steps"]:
            if "uses" in step:
                assert action_revision.fullmatch(step["uses"])
            if str(step.get("uses", "")).startswith("actions/upload-artifact@"):
                artifact_names.append(str(step["with"]["name"]).casefold())
    assert artifact_names
    assert all(
        prohibited not in name
        for name in artifact_names
        for prohibited in ("verified", "release", "supported")
    )
    assert "topoforge-macos-arm64-unsigned-hosted-package-candidate" in artifact_names
    assert "actions/download-artifact@" in workflow_text
    assert "Enforce candidate artifact bounds and detached hashes" in workflow_text


def test_documentation_keeps_phase13a_unverified_and_phase13b_separate() -> None:
    root = _repository_root()
    docs = (root / "docs/macos-support.md").read_text(encoding="utf-8")
    matrix = json.loads((root / "docs/macos-support-matrix.json").read_text(encoding="utf-8"))
    compact_docs = re.sub(r"\s+", " ", docs)

    assert "does **not** currently claim macOS support" in docs
    assert "7036ea340734d284b9b406b43dbb9547ba6e28186fe16aee4433a5e4ff0c6e78" in docs
    assert "invalidates the archive for all uses" in docs
    assert "hosted-package" in docs
    assert "cannot establish clean-system" in compact_docs
    assert matrix["public_support_status"] == "unverified"
    package = matrix["phase13a_package_infrastructure"]
    assert package["invalidated_candidates"][0]["usable_candidate"] is False
    assert package["dynamic_library_closure"]["host_filesystem_resolution_allowed"] is False
    assert package["dynamic_library_closure"]["native_loaded_image_inventory_required"] is True
    assert package["workflow_run_status"] == (
        "run-31670153746-passed-hosted-but-candidate-invalidated"
    )
    assert package["signed"] is False
    assert package["notarized"] is False
    assert package["gatekeeper_evidence"] is False
    assert package["bambu_phase13b_evidence"] is False
    assert {item["id"] for item in matrix["excluded_targets"]} == {
        "macos-14-arm64",
        "macos-intel-x86-64",
        "macos-27-beta-arm64",
    }


def test_builder_contract_thins_and_unsigns_every_macho() -> None:
    source = (_repository_root() / "scripts/build_macos_app.py").read_text(encoding="utf-8")

    assert '["/usr/bin/lipo", str(path), "-thin", "arm64"' in source
    assert '["/usr/bin/codesign", "--remove-signature", str(path)]' in source
    assert "embedded Mach-O is not arm64-only" in source
    assert "above app target" in source
    assert "export DYLD_FRAMEWORK_PATH" not in builder.CLI_LAUNCHER
    assert "export DYLD_FRAMEWORK_PATH" not in builder.WEB_LAUNCHER
    assert "DYLD_FALLBACK_FRAMEWORK_PATH" in builder.CLI_LAUNCHER
    assert "DYLD_FALLBACK_FRAMEWORK_PATH" in builder.WEB_LAUNCHER
    assert VERIFICATION_SCHEMA_VERSION == "topoforge-macos-app-verification-v2"


def _cpython_macho_payloads_with_absolute_dependencies() -> dict[str, bytes]:
    framework = "Contents/Frameworks/Python.framework/Versions/3.12"
    installed = "/Library/Frameworks/Python.framework/Versions/3.12"
    library = f"{framework}/lib"
    installed_library = f"{installed}/lib"
    dynamic = f"{library}/python3.12/lib-dynload"
    installed_names = {
        name: f"{installed_library}/{name}"
        for name in (
            "libcrypto.3.dylib",
            "libform.6.dylib",
            "libmenu.6.dylib",
            "libncurses.6.dylib",
            "libpanel.6.dylib",
            "libssl.3.dylib",
            "libtcl8.6.dylib",
            "libtk8.6.dylib",
        )
    }
    framework_binary = f"{framework}/Python"
    installed_framework_binary = f"{installed}/Python"
    payloads = {
        framework_binary: _thin_macho(
            file_type=6,
            dylib_id=installed_framework_binary,
        ),
        PYTHON_PATH: _thin_macho(
            dependencies=((0x0C, installed_framework_binary),),
        ),
        f"{framework}/Resources/Python.app/Contents/MacOS/Python": _thin_macho(
            dependencies=((0x0C, installed_framework_binary),),
        ),
    }
    for name in installed_names:
        dependencies: tuple[tuple[int, str], ...] = ()
        if name in {
            "libform.6.dylib",
            "libmenu.6.dylib",
            "libpanel.6.dylib",
        }:
            dependencies = ((0x0C, installed_names["libncurses.6.dylib"]),)
        elif name == "libssl.3.dylib":
            dependencies = ((0x0C, installed_names["libcrypto.3.dylib"]),)
        payloads[f"{library}/{name}"] = _thin_macho(
            file_type=6,
            dylib_id=installed_names[name],
            dependencies=dependencies,
        )
    payloads.update(
        {
            f"{dynamic}/_hashlib.cpython-312-darwin.so": _thin_macho(
                file_type=8,
                dependencies=((0x0C, installed_names["libcrypto.3.dylib"]),),
            ),
            f"{dynamic}/_ssl.cpython-312-darwin.so": _thin_macho(
                file_type=8,
                dependencies=(
                    (0x0C, installed_names["libssl.3.dylib"]),
                    (0x0C, installed_names["libcrypto.3.dylib"]),
                ),
                rpaths=("/Users/runner/work/python-build/lib", "@loader_path/../.."),
            ),
            f"{dynamic}/_curses.cpython-312-darwin.so": _thin_macho(
                file_type=8,
                dependencies=((0x0C, installed_names["libncurses.6.dylib"]),),
            ),
            f"{dynamic}/_curses_panel.cpython-312-darwin.so": _thin_macho(
                file_type=8,
                dependencies=(
                    (0x0C, installed_names["libncurses.6.dylib"]),
                    (0x0C, installed_names["libpanel.6.dylib"]),
                ),
            ),
            f"{dynamic}/_tkinter.cpython-312-darwin.so": _thin_macho(
                file_type=8,
                dependencies=(
                    (0x0C, installed_names["libtcl8.6.dylib"]),
                    (0x0C, installed_names["libtk8.6.dylib"]),
                ),
            ),
        }
    )
    return payloads


def test_macho_rewrite_covers_all_observed_cpython_absolute_edges() -> None:
    payloads = _cpython_macho_payloads_with_absolute_dependencies()

    plans = macho_rewrite_plans(
        payloads,
        executable_path=PYTHON_PATH,
        absolute_rewrites={
            PYTHON_FRAMEWORK_INSTALL_PREFIX: PYTHON_FRAMEWORK_BUNDLE_PREFIX,
        },
    )

    changes = [change for plan in plans for change in plan["changes"]]
    assert len(changes) == 14
    assert all(change["old"].startswith(PYTHON_FRAMEWORK_INSTALL_PREFIX) for change in changes)
    assert all(change["new"].startswith("@loader_path/") for change in changes)
    assert {Path(change["old"]).name for change in changes} == {
        "Python",
        "libcrypto.3.dylib",
        "libncurses.6.dylib",
        "libpanel.6.dylib",
        "libssl.3.dylib",
        "libtcl8.6.dylib",
        "libtk8.6.dylib",
    }
    ssl_plan = next(plan for plan in plans if plan["path"].endswith("/_ssl.cpython-312-darwin.so"))
    assert ssl_plan["delete_rpaths"] == [
        "/Users/runner/work/python-build/lib",
        "@loader_path/../..",
    ]
    dylib_plans = [plan for plan in plans if plan["dylib_id"] is not None]
    assert dylib_plans
    assert all(
        plan["dylib_id"] == f"@loader_path/{Path(plan['path']).name}" for plan in dylib_plans
    )


def test_macho_closure_resolves_loader_executable_rpath_and_all_load_kinds() -> None:
    executable = "Contents/MacOS/TopoForge"
    library = "Contents/Frameworks/libfixture.dylib"
    plugin = "Contents/PlugIns/fixture.so"
    payloads = {
        executable: _thin_macho(),
        library: _thin_macho(file_type=6, dylib_id="@loader_path/libfixture.dylib"),
        plugin: _thin_macho(
            file_type=8,
            dependencies=(
                (0x0C, "@rpath/libfixture.dylib"),
                (0x80000018, "@loader_path/../Frameworks/libfixture.dylib"),
                (0x8000001F, "@executable_path/../Frameworks/libfixture.dylib"),
                (0x20, "/usr/lib/libobjc.A.dylib"),
                (
                    0x80000023,
                    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation",
                ),
            ),
            rpaths=("@loader_path/../Frameworks",),
        ),
    }

    records = macho_closure_records(payloads, executable_path=executable)
    plugin_record = next(record for record in records if record["path"] == plugin)

    assert [item["command"] for item in plugin_record["dependencies"]] == [
        "LC_LOAD_DYLIB",
        "LC_LOAD_WEAK_DYLIB",
        "LC_REEXPORT_DYLIB",
        "LC_LAZY_LOAD_DYLIB",
        "LC_LOAD_UPWARD_DYLIB",
    ]
    assert [item["scope"] for item in plugin_record["dependencies"]] == [
        "app-bundle",
        "app-bundle",
        "app-bundle",
        "apple-system",
        "apple-system",
    ]
    assert plugin_record["dependencies"][0]["resolved_path"] == library
    assert plugin_record["dependencies"][3]["resolved_path"] == "/usr/lib/libobjc.A.dylib"
    assert plugin_record["dependencies"][4]["resolved_path"].startswith("/System/Library/")
    assert macho_dynamic_slices_bytes(payloads[plugin], label=plugin)[0]["rpaths"] == [
        "@loader_path/../Frameworks"
    ]


def test_macho_rewrite_makes_apple_system_rpath_load_independent_of_rpath() -> None:
    executable = "Contents/MacOS/TopoForge"
    plugin = "Contents/PlugIns/fixture.so"
    payloads = {
        executable: _thin_macho(),
        plugin: _thin_macho(
            file_type=8,
            dependencies=((0x0C, "@rpath/libSystem.B.dylib"),),
            rpaths=("/usr/lib",),
        ),
    }

    plans = macho_rewrite_plans(
        payloads,
        executable_path=executable,
        absolute_rewrites={},
    )
    plugin_plan = next(plan for plan in plans if plan["path"] == plugin)

    assert plugin_plan["changes"] == [
        {"old": "@rpath/libSystem.B.dylib", "new": "/usr/lib/libSystem.B.dylib"}
    ]
    assert plugin_plan["delete_rpaths"] == ["/usr/lib"]


@pytest.mark.parametrize(
    "external",
    [
        "/Library/Frameworks/Python.framework/Versions/3.12/lib/libssl.3.dylib",
        "/usr/local/lib/libssl.3.dylib",
        "/opt/homebrew/lib/libssl.3.dylib",
        "/Users/runner/build/libssl.3.dylib",
        "/var/folders/build/libssl.3.dylib",
    ],
)
def test_final_macho_closure_rejects_every_non_system_absolute_path(external: str) -> None:
    plugin = "Contents/PlugIns/fixture.so"
    with pytest.raises(ValueError, match="non-system absolute path"):
        macho_closure_records(
            {
                "Contents/MacOS/TopoForge": _thin_macho(),
                plugin: _thin_macho(file_type=8, dependencies=((0x0C, external),)),
            },
            executable_path="Contents/MacOS/TopoForge",
        )


@pytest.mark.parametrize(
    ("dependency", "rpaths", "message"),
    [
        ("@rpath/missing.dylib", ("@loader_path/../Frameworks",), "unresolved"),
        ("/usr/lib/libSystem.B.dylib", ("@loader_path/../../../../escape",), "escapes"),
        ("/usr/lib/libSystem.B.dylib", ("/opt/homebrew/lib",), "external build path"),
    ],
)
def test_macho_closure_rejects_unresolved_escaping_and_host_rpaths(
    dependency: str,
    rpaths: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        macho_closure_records(
            {
                "Contents/MacOS/TopoForge": _thin_macho(),
                "Contents/PlugIns/fixture.so": _thin_macho(
                    file_type=8,
                    dependencies=((0x0C, dependency),),
                    rpaths=rpaths,
                ),
                "Contents/Frameworks/sentinel": _thin_macho(file_type=8),
            },
            executable_path="Contents/MacOS/TopoForge",
        )


def _archive_with_extra_macho(
    source: Path,
    destination: Path,
    relative: str,
    payload: bytes,
) -> None:
    with zipfile.ZipFile(source) as archive:
        members = {
            info.filename: {"payload": archive.read(info), "external_attr": info.external_attr}
            for info in archive.infolist()
        }
        timestamp = archive.infolist()[0].date_time
    manifest_name = f"{APP_ROOT}/{MANIFEST_PATH}"
    manifest = json.loads(members[manifest_name]["payload"])
    record = {
        "path": relative,
        "kind": "file",
        "mode": 0o755,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    manifest["contents"]["files"].append(record)
    manifest["contents"]["files"].sort(key=lambda item: item["path"])
    manifest["contents"]["file_count"] = len(manifest["contents"]["files"])
    manifest["contents"]["payload_sha256"] = payload_sha256(manifest["contents"]["files"])
    manifest["python_runtime"]["macho_file_count"] += 1
    members[manifest_name]["payload"] = canonical_json_bytes(manifest)
    members[f"{APP_ROOT}/{relative}"] = {
        "payload": payload,
        "external_attr": (stat.S_IFREG | 0o755) << 16,
    }
    with zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=timestamp)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.flag_bits |= 0x800
            info.external_attr = members[name]["external_attr"]
            archive.writestr(info, members[name]["payload"])


def test_archive_rejects_real_ssl_framework_escape_even_if_host_path_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _fixture_archive(tmp_path / "source")
    mutated = tmp_path / "external-ssl.zip"
    relative = (
        "Contents/Frameworks/Python.framework/Versions/3.12/lib/python3.12/"
        "lib-dynload/_ssl.cpython-312-darwin.so"
    )
    payload = _thin_macho(
        file_type=8,
        dependencies=(
            (
                0x0C,
                "/Library/Frameworks/Python.framework/Versions/3.12/lib/libssl.3.dylib",
            ),
        ),
    )
    _archive_with_extra_macho(source, mutated, relative, payload)
    monkeypatch.setattr(Path, "exists", lambda _path: True)

    with pytest.raises(ValueError, match="non-system absolute path"):
        inspect_archive(mutated, config=_config())


def test_native_loaded_image_inventory_never_accepts_host_frameworks(tmp_path: Path) -> None:
    app = tmp_path / "candidate" / APP_ROOT
    app_image = app / "Contents/Frameworks/Python.framework/Versions/3.12/lib/libssl.3.dylib"
    inventory = app_verifier._loaded_image_inventory(
        [
            str(app_image),
            "/usr/lib/libSystem.B.dylib",
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation",
            "/Library/Frameworks/Python.framework/Versions/3.12/lib/libssl.3.dylib",
        ],
        app,
    )

    assert inventory["app"] == [str(app_image)]
    assert len(inventory["apple_system"]) == 2
    assert inventory["external"] == [
        "/Library/Frameworks/Python.framework/Versions/3.12/lib/libssl.3.dylib"
    ]
    for module in ("ssl", "_ssl", "hashlib", "_hashlib", "http.client", "urllib.request"):
        assert f'"{module}"' in app_verifier.DEPENDENCY_PROBE
    assert "_dyld_get_image_name" in app_verifier.DEPENDENCY_PROBE


def test_malformed_macho_magic_is_not_skipped_by_bundle_discovery(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.so"
    malformed.write_bytes(b"\xcf\xfa\xed\xfe")

    assert builder._is_macho(malformed)
    with pytest.raises(ValueError, match="truncated"):
        macho_dynamic_slices_bytes(malformed.read_bytes(), label=malformed.name)


def test_final_closure_records_exact_apple_system_paths() -> None:
    records = macho_closure_records(
        {PYTHON_PATH: _thin_macho(dependencies=((0x0C, "/usr/lib/libSystem.B.dylib"),))},
        executable_path=PYTHON_PATH,
    )

    assert records[0]["dependencies"][0]["resolved_path"] == "/usr/lib/libSystem.B.dylib"
