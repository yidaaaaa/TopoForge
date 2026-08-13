from __future__ import annotations

import base64
import copy
import csv
import errno
import hashlib
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest
import scripts.verify_windows_portable as portable_verifier
import scripts.windows_acceptance as windows_acceptance
import yaml
from scripts.build_windows_portable import (
    CLI_LAUNCHER,
    DEPENDENCY_RECORD_SCHEMA_VERSION,
    RUNTIME_SITE_PACKAGES_SCHEMA_VERSION,
    WEB_LAUNCHER,
    _inspect_project_wheel,
    _installed_file_projection,
    _load_config,
    _metadata_values,
    _normalize_dependency_install,
    _projection_sha256,
    _publish_verified_archive,
    _safe_relative_path,
    _sha256,
    _verify_runtime_archive,
    _write_reproducible_zip,
)
from scripts.verify_platform_core import verify_platform_core
from scripts.verify_windows_portable import (
    _bambu_profile_binding_contract,
    _extract_verified_archive,
    _nested_binding_contract,
    _profile_hash_arguments,
    _validate_archive_members,
    _validate_build_provenance,
    _validate_dependency_install_projection,
    _validate_manifest_files,
    _windows_batch_command,
    _windows_containment_contract,
)

RUNTIME_SHA256 = "18bcc65b17921806b72cdc88bcf000bf67a2c99a8fc381fe1629f2b9ba56858d"


def _repository_root() -> Path:
    return Path(__file__).parents[2]


def _config() -> dict[str, object]:
    return _load_config(_repository_root() / "packaging/windows-x64-runtime.json")


def _write_members(
    path: Path,
    members: list[tuple[str, bytes]],
    *,
    timestamp: tuple[int, int, int, int, int, int] = (2020, 2, 2, 0, 0, 0),
) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members:
            info = zipfile.ZipInfo(name, date_time=timestamp)
            info.create_system = 0
            info.external_attr = 0
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)


def _record_hash(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    return f"sha256={digest.decode('ascii')}"


def _write_dependency_install_fixture(site_packages: Path) -> None:
    payloads = {
        "demo_package/__init__.py": b"value = 1\n",
        "demo_package/native.pyd": b"MZ-demo-native",
        "demo_package-1.0.dist-info/METADATA": (
            b"Metadata-Version: 2.1\nName: Demo_Package\nVersion: 1.0\n"
        ),
        "demo_package-1.0.dist-info/INSTALLER": b"uv\n",
    }
    for relative, payload in payloads.items():
        path = site_packages / Path(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    script = site_packages / "Scripts" / "demo.exe"
    script.parent.mkdir()
    script.write_bytes(b"MZ-demo-script")
    target_local_script = site_packages / "bin" / "normalizer.exe"
    target_local_script.parent.mkdir()
    target_local_script.write_bytes(b"MZ-normalizer-script")
    (site_packages / ".lock").write_bytes(b"")

    rows = [
        (relative, _record_hash(payload), str(len(payload)))
        for relative, payload in payloads.items()
    ]
    rows.extend(
        [
            (
                "../../../Scripts/demo.exe",
                _record_hash(script.read_bytes()),
                str(script.stat().st_size),
            ),
            (
                "bin/normalizer.exe",
                _record_hash(target_local_script.read_bytes()),
                str(target_local_script.stat().st_size),
            ),
            ("demo_package-1.0.dist-info/RECORD", "", ""),
        ]
    )
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    record = site_packages / "demo_package-1.0.dist-info" / "RECORD"
    record.write_text(output.getvalue(), encoding="utf-8")


def _normalized_dependency_fixture(
    tmp_path: Path,
) -> tuple[Path, list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    (site_packages / "README.txt").write_bytes(b"embedded runtime baseline\n")
    runtime_baseline = _installed_file_projection(
        site_packages,
        maximum_files=100,
        maximum_file_bytes=1024 * 1024,
    )
    _write_dependency_install_fixture(site_packages)
    dependencies, _extension_count, projection = _normalize_dependency_install(
        site_packages,
        original_dist_info=set(),
        runtime_baseline=runtime_baseline,
        maximum_files=100,
        maximum_file_bytes=1024 * 1024,
        maximum_record_bytes=1024 * 1024,
    )
    return site_packages, dependencies, projection, runtime_baseline


def _dependency_projection_manifest(
    dependencies: list[dict[str, Any]],
    projection: dict[str, Any],
    runtime_baseline: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "count": len(dependencies),
        "packages": dependencies,
        "runtime_site_packages": {
            "schema_version": RUNTIME_SITE_PACKAGES_SCHEMA_VERSION,
            "file_count": len(runtime_baseline),
            "files_sha256": _projection_sha256(runtime_baseline),
            "files": runtime_baseline,
        },
        "record_projection": projection,
    }


AMBIGUOUS_OR_MALFORMED_METADATA = (
    pytest.param(
        b"Metadata-Version: 2.1\nName: Demo_Package\nName: Demo_Package\nVersion: 1.0\n",
        id="duplicate-same-name",
    ),
    pytest.param(
        b"Metadata-Version: 2.1\nName: Demo_Package\nName: Other\nVersion: 1.0\n",
        id="duplicate-conflicting-name",
    ),
    pytest.param(
        b"Metadata-Version: 2.1\nName: Demo_Package\nVersion: 1.0\nVersion: 1.0\n",
        id="duplicate-same-version",
    ),
    pytest.param(
        b"Metadata-Version: 2.1\nName: Demo_Package\nVersion: 1.0\nVersion: 2.0\n",
        id="duplicate-conflicting-version",
    ),
    pytest.param(
        b" orphan continuation\nMetadata-Version: 2.1\nName: Demo_Package\nVersion: 1.0\n",
        id="parser-defect",
    ),
    pytest.param(
        b"Metadata-Version: 2.1\nName: Demo_Package\nVersion: \xff\n",
        id="non-utf8",
    ),
)


def test_windows_runtime_config_is_immutable_and_bounded() -> None:
    config = _config()
    runtime = config["python_runtime"]
    target = config["target"]
    bounds = config["bounds"]

    assert isinstance(runtime, dict)
    assert runtime["version"] == "3.12.13"
    assert runtime["provider"] == "python-build-standalone"
    assert runtime["provider_release"] == "20260807"
    assert runtime["provider_license"] == "MPL-2.0"
    assert runtime["provider_license_file"] == "MPL-2.0.txt"
    assert runtime["provider_source_url"] == (
        "https://github.com/astral-sh/python-build-standalone/tree/"
        "00c8a06113f11220667c3bcf5fab1672ff9e78ef"
    )
    assert runtime["url"].startswith(
        "https://github.com/astral-sh/python-build-standalone/releases/download/20260807/"
    )
    assert runtime["sha256"] == RUNTIME_SHA256
    assert runtime["bytes"] == 21_962_247
    assert runtime["member_count"] == 3_338
    assert runtime["uncompressed_bytes"] == 63_276_851

    assert isinstance(target, dict)
    assert target["os"] == "Windows"
    assert target["architecture"] == "x86_64"
    assert target["python_platform"] == "x86_64-pc-windows-msvc"
    assert target["candidate_os_versions"] == [
        "Windows 10 22H2 x64",
        "Windows 11 x64",
    ]

    assert isinstance(bounds, dict)
    assert runtime["bytes"] < bounds["runtime_archive_max_bytes"]
    assert runtime["member_count"] < bounds["runtime_member_count_max"]
    assert runtime["uncompressed_bytes"] < bounds["runtime_uncompressed_max_bytes"]


@pytest.mark.parametrize(
    "unsafe",
    [
        "../escape.txt",
        "/absolute.txt",
        "C:/drive.txt",
        "runtime\\python.exe",
        "runtime/CON.txt",
        "runtime/COM¹.txt",
        "runtime/trailing.",
        "runtime/trailing ",
        "runtime/bad?.txt",
        "runtime//double.txt",
    ],
)
def test_portable_paths_reject_traversal_and_windows_hazards(unsafe: str) -> None:
    with pytest.raises(ValueError, match="path"):
        _safe_relative_path(unsafe)


def test_portable_paths_accept_spaces_and_non_ascii() -> None:
    path = _safe_relative_path("workspace with spaces/地形/model.3mf")
    assert path.as_posix() == "workspace with spaces/地形/model.3mf"


def test_public_evidence_projection_removes_logs_and_redacts_private_roots(
    tmp_path: Path,
) -> None:
    work_root = tmp_path / "operator home" / "clean evidence"
    report = {
        "execution": {
            "commands": [
                {
                    "command": ["python.exe", "verify.py"],
                    "cwd": str(work_root),
                    "stdout": "private output",
                    "stderr": "private error",
                }
            ],
            "artifact_path": str(work_root / "portable path" / "地形" / "model.3mf"),
        },
        "required_checks_passed": True,
    }

    projected = portable_verifier._public_evidence_projection(
        report,
        private_roots={"work_root": work_root},
    )

    def nested_keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {nested for item in value.values() for nested in nested_keys(item)}
        if isinstance(value, list):
            return {nested for item in value for nested in nested_keys(item)}
        return set()

    private_keys = {"command", "commands", "cwd", "stderr", "stdout"}
    assert private_keys.isdisjoint(nested_keys(projected))
    assert str(work_root) not in json.dumps(projected, ensure_ascii=False)
    assert projected["execution"]["artifact_path"].startswith(
        "C:/TopoForge Public Evidence/work_root/"
    )
    assert projected["public_evidence_projection"]["removed_fields"] == ["commands"]


def test_private_evidence_roots_keep_temp_labels_and_bambu_overrides(
    tmp_path: Path,
) -> None:
    environment_temp = tmp_path / "TEMP"
    environment_tmp = tmp_path / "TMP"
    executable = tmp_path / "custom studio" / "BambuStudio.exe"
    profiles_root = tmp_path / "custom profiles" / "BBL"
    roots = portable_verifier._private_evidence_roots(
        tmp_path / "candidate" / "candidate.zip",
        work_root=tmp_path / "work",
        bambu_studio_executable=executable,
        bambu_profiles_root=profiles_root,
        environment={
            "TEMP": str(environment_temp),
            "TMP": str(environment_tmp),
        },
    )

    assert roots["environment_temp"] == environment_temp
    assert roots["environment_tmp"] == environment_tmp
    assert roots["bambu_studio_install_root"] == executable.resolve().parent
    assert roots["bambu_profiles_root"] == profiles_root

    report = {
        "environment_temp": str(environment_temp / "private.log"),
        "environment_tmp": str(environment_tmp / "private.log"),
        "bambu_executable": str(executable),
        "bambu_profile": str(profiles_root / "machine.json"),
        "required_checks_passed": True,
    }
    projected = portable_verifier._public_evidence_projection(
        report,
        private_roots=roots,
    )
    encoded = json.dumps(projected, ensure_ascii=False)
    for private_path in (
        environment_temp,
        environment_tmp,
        executable.parent,
        profiles_root,
    ):
        assert str(private_path) not in encoded
    assert {
        "bambu_profiles_root",
        "bambu_studio_install_root",
        "environment_temp",
        "environment_tmp",
    } <= set(projected["public_evidence_projection"]["redacted_root_labels"])


def test_runtime_archive_requires_exact_size_and_hash(tmp_path: Path) -> None:
    config = copy.deepcopy(_config())
    runtime = config["python_runtime"]
    bounds = config["bounds"]
    assert isinstance(runtime, dict)
    assert isinstance(bounds, dict)
    runtime["bytes"] = 3
    runtime["sha256"] = hashlib.sha256(b"abc").hexdigest()
    bounds["runtime_archive_max_bytes"] = 4
    archive = tmp_path / "runtime.tar.gz"
    archive.write_bytes(b"abc")
    _verify_runtime_archive(archive, config)

    archive.write_bytes(b"abd")
    with pytest.raises(ValueError, match="SHA-256"):
        _verify_runtime_archive(archive, config)


def test_reproducible_zip_has_stable_bytes_and_metadata(tmp_path: Path) -> None:
    config = _config()
    bounds = config["bounds"]
    assert isinstance(bounds, dict)
    package = tmp_path / "TopoForge-Windows-x64"
    (package / "nested").mkdir(parents=True)
    (package / "alpha.txt").write_text("alpha\n", encoding="utf-8")
    (package / "nested" / "地形.txt").write_text("terrain\n", encoding="utf-8")
    first = tmp_path / "first.zip"
    repeat = tmp_path / "repeat.zip"

    for destination in (first, repeat):
        _write_reproducible_zip(
            package,
            destination,
            source_date_epoch=1_580_601_600,
            bounds=bounds,
            overwrite=False,
        )

    assert _sha256(first) == _sha256(repeat)
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == [
            "TopoForge-Windows-x64/alpha.txt",
            "TopoForge-Windows-x64/nested/地形.txt",
        ]
        assert all(info.date_time == (2020, 2, 2, 0, 0, 0) for info in archive.infolist())


def test_portable_member_validator_rejects_case_collisions(tmp_path: Path) -> None:
    archive_path = tmp_path / "case-collision.zip"
    _write_members(
        archive_path,
        [
            ("TopoForge-Windows-x64/A.txt", b"a"),
            ("TopoForge-Windows-x64/a.txt", b"b"),
        ],
    )
    with (
        zipfile.ZipFile(archive_path) as archive,
        pytest.raises(ValueError, match="paths collide on Windows"),
    ):
        _validate_archive_members(archive, _config())


def test_portable_member_validator_rejects_parent_escape(tmp_path: Path) -> None:
    archive_path = tmp_path / "escape.zip"
    _write_members(
        archive_path,
        [("TopoForge-Windows-x64/../escape.txt", b"escape")],
    )
    with (
        zipfile.ZipFile(archive_path) as archive,
        pytest.raises(ValueError, match="canonical portable relative path"),
    ):
        _validate_archive_members(archive, _config())


def test_portable_member_validator_rejects_file_directory_collisions(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "file-directory-collision.zip"
    _write_members(
        archive_path,
        [
            ("TopoForge-Windows-x64/runtime", b"file"),
            ("TopoForge-Windows-x64/runtime/python.exe", b"binary"),
        ],
    )
    with (
        zipfile.ZipFile(archive_path) as archive,
        pytest.raises(ValueError, match="both a file and directory"),
    ):
        _validate_archive_members(archive, _config())


def test_portable_manifest_validator_detects_payload_tamper(tmp_path: Path) -> None:
    archive_path = tmp_path / "tampered.zip"
    manifest_bytes = b"{}\n"
    _write_members(
        archive_path,
        [
            ("TopoForge-Windows-x64/manifest.json", manifest_bytes),
            ("TopoForge-Windows-x64/payload.txt", b"actual"),
        ],
    )
    manifest = {
        "contents": {
            "file_count": 1,
            "uncompressed_bytes": 6,
            "files": [
                {
                    "path": "payload.txt",
                    "bytes": 6,
                    "sha256": hashlib.sha256(b"other!").hexdigest(),
                }
            ],
        }
    }
    with zipfile.ZipFile(archive_path) as archive:
        relative, _ = _validate_archive_members(archive, _config())
        with pytest.raises(ValueError, match="SHA-256 changed"):
            _validate_manifest_files(archive, relative, manifest)


def test_dependency_install_preserves_normalized_record_projection(tmp_path: Path) -> None:
    site_packages, dependencies, projection, runtime_baseline = _normalized_dependency_fixture(
        tmp_path
    )

    record = site_packages / "demo_package-1.0.dist-info" / "RECORD"
    assert record.is_file()
    assert not (site_packages / "demo_package-1.0.dist-info" / "INSTALLER").exists()
    assert not (site_packages / "Scripts").exists()
    assert not (site_packages / "bin").exists()
    assert not (site_packages / ".lock").exists()
    assert dependencies[0]["name"] == "Demo_Package"
    assert dependencies[0]["record_sha256"] == hashlib.sha256(record.read_bytes()).hexdigest()
    assert projection["schema_version"] == DEPENDENCY_RECORD_SCHEMA_VERSION
    assert projection["installed_file_count"] == dependencies[0]["installed_file_count"]
    assert runtime_baseline == [
        {
            "path": "README.txt",
            "bytes": len(b"embedded runtime baseline\n"),
            "sha256": hashlib.sha256(b"embedded runtime baseline\n").hexdigest(),
        }
    ]


@pytest.mark.parametrize("metadata_payload", AMBIGUOUS_OR_MALFORMED_METADATA)
def test_portable_metadata_parser_rejects_ambiguous_or_malformed_identity(
    metadata_payload: bytes,
) -> None:
    with pytest.raises(ValueError, match="METADATA"):
        _metadata_values(
            metadata_payload,
            context="portable fixture",
            required_fields=("Metadata-Version", "Name", "Version"),
        )


@pytest.mark.parametrize("metadata_payload", AMBIGUOUS_OR_MALFORMED_METADATA)
def test_dependency_install_rejects_ambiguous_or_malformed_metadata(
    tmp_path: Path,
    metadata_payload: bytes,
) -> None:
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    runtime_baseline = _installed_file_projection(
        site_packages,
        maximum_files=100,
        maximum_file_bytes=1024 * 1024,
    )
    _write_dependency_install_fixture(site_packages)
    metadata_path = site_packages / "demo_package-1.0.dist-info" / "METADATA"
    metadata_path.write_bytes(metadata_payload)

    with pytest.raises(ValueError, match="METADATA"):
        _normalize_dependency_install(
            site_packages,
            original_dist_info=set(),
            runtime_baseline=runtime_baseline,
            maximum_files=100,
            maximum_file_bytes=1024 * 1024,
            maximum_record_bytes=1024 * 1024,
        )


@pytest.mark.parametrize("metadata_payload", AMBIGUOUS_OR_MALFORMED_METADATA)
def test_dependency_projection_verifier_rejects_bound_malicious_metadata(
    tmp_path: Path,
    metadata_payload: bytes,
) -> None:
    site_packages, packages, projection, runtime_baseline = _normalized_dependency_fixture(tmp_path)
    metadata_relative = "demo_package-1.0.dist-info/METADATA"
    metadata_path = site_packages / Path(*metadata_relative.split("/"))
    metadata_path.write_bytes(metadata_payload)
    record_path = site_packages / "demo_package-1.0.dist-info" / "RECORD"
    with record_path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.reader(source))
    rows = [
        (
            row[0],
            _record_hash(metadata_payload),
            str(len(metadata_payload)),
        )
        if row[0] == metadata_relative
        else (row[0], row[1], row[2])
        for row in rows
    ]
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    record_path.write_text(output.getvalue(), encoding="utf-8")

    all_entries = _installed_file_projection(
        site_packages,
        maximum_files=100,
        maximum_file_bytes=1024 * 1024,
    )
    dependency_entries = [entry for entry in all_entries if entry["path"] != "README.txt"]
    package = packages[0]
    package["record_sha256"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
    package["installed_file_count"] = len(dependency_entries)
    package["installed_bytes"] = sum(int(entry["bytes"]) for entry in dependency_entries)
    package["installed_files_sha256"] = _projection_sha256(dependency_entries)
    projection["installed_file_count"] = len(dependency_entries)
    projection["installed_bytes"] = package["installed_bytes"]
    projection["installed_files_sha256"] = package["installed_files_sha256"]
    dependencies = _dependency_projection_manifest(packages, projection, runtime_baseline)
    members = [
        (
            "TopoForge-Windows-x64/runtime/Lib/site-packages/"
            + path.relative_to(site_packages).as_posix(),
            path.read_bytes(),
        )
        for path in sorted(site_packages.rglob("*"))
        if path.is_file()
    ]
    archive_path = tmp_path / "malicious-metadata.zip"
    _write_members(archive_path, members)
    requirements = b"demo-package==1.0 \\\n    --hash=sha256:" + b"a" * 64 + b"\n"

    with zipfile.ZipFile(archive_path) as archive:
        relative_infos, _uncompressed = _validate_archive_members(archive, _config())
        with pytest.raises(ValueError, match="METADATA"):
            _validate_dependency_install_projection(
                archive,
                relative_infos,
                dependencies,
                requirements,
                project_paths=set(),
                maximum_files=100,
                maximum_record_bytes=1024 * 1024,
            )


@pytest.mark.parametrize(
    "metadata_payload",
    (
        b"Metadata-Version: 2.4\nName: topoforge\nName: topoforge\nVersion: 0.11.0\n"
        b"Requires-Python: <3.15,>=3.11\nLicense-Expression: Apache-2.0\n",
        b"Metadata-Version: 2.4\nName: topoforge\nVersion: 0.11.0\nVersion: 9.9.9\n"
        b"Requires-Python: <3.15,>=3.11\nLicense-Expression: Apache-2.0\n",
        b" broken\nMetadata-Version: 2.4\nName: topoforge\nVersion: 0.11.0\n"
        b"Requires-Python: <3.15,>=3.11\nLicense-Expression: Apache-2.0\n",
    ),
)
def test_project_wheel_inspection_rejects_ambiguous_or_malformed_metadata(
    tmp_path: Path,
    metadata_payload: bytes,
) -> None:
    wheel = tmp_path / "topoforge-0.11.0-py3-none-any.whl"
    _write_members(
        wheel,
        [("topoforge-0.11.0.dist-info/METADATA", metadata_payload)],
    )

    with pytest.raises(ValueError, match="METADATA"):
        _inspect_project_wheel(wheel, "0.11.0")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("tampered-record-member", "RECORD member SHA-256 changed"),
        (
            "unrecorded-file",
            "installed dependency files differ from runtime baseline plus wheel RECORDs",
        ),
    ],
)
def test_dependency_install_rejects_tampered_or_unrecorded_files(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    runtime_baseline = _installed_file_projection(
        site_packages,
        maximum_files=100,
        maximum_file_bytes=1024 * 1024,
    )
    _write_dependency_install_fixture(site_packages)
    if mutation == "tampered-record-member":
        (site_packages / "demo_package" / "__init__.py").write_bytes(b"value = 2\n")
    else:
        (site_packages / "unlocked.py").write_bytes(b"not in a wheel RECORD\n")

    with pytest.raises(ValueError, match=message):
        _normalize_dependency_install(
            site_packages,
            original_dist_info=set(),
            runtime_baseline=runtime_baseline,
            maximum_files=100,
            maximum_file_bytes=1024 * 1024,
            maximum_record_bytes=1024 * 1024,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("none", ""),
        ("tampered-record-member", "RECORD member SHA-256 changed"),
        (
            "unrecorded-file",
            "site-packages differs from runtime, locked wheels, and project wheel",
        ),
    ],
)
def test_dependency_projection_verifier_requires_exact_record_coverage(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    site_packages, packages, projection, runtime_baseline = _normalized_dependency_fixture(tmp_path)
    dependencies = _dependency_projection_manifest(packages, projection, runtime_baseline)
    members = [
        (
            "TopoForge-Windows-x64/runtime/Lib/site-packages/"
            + path.relative_to(site_packages).as_posix(),
            path.read_bytes(),
        )
        for path in sorted(site_packages.rglob("*"))
        if path.is_file()
    ]
    if mutation == "tampered-record-member":
        target = "TopoForge-Windows-x64/runtime/Lib/site-packages/demo_package/__init__.py"
        members = [
            (path, b"value = 2\n" if path == target else payload) for path, payload in members
        ]
    elif mutation == "unrecorded-file":
        members.append(
            (
                "TopoForge-Windows-x64/runtime/Lib/site-packages/unlocked.py",
                b"not in a locked wheel\n",
            )
        )
    archive_path = tmp_path / f"{mutation}.zip"
    _write_members(archive_path, members)
    requirements = b"demo-package==1.0 \\\n    --hash=sha256:" + b"a" * 64 + b"\n"

    with zipfile.ZipFile(archive_path) as archive:
        relative_infos, _uncompressed = _validate_archive_members(archive, _config())
        if mutation == "none":
            report = _validate_dependency_install_projection(
                archive,
                relative_infos,
                dependencies,
                requirements,
                project_paths=set(),
                maximum_files=100,
                maximum_record_bytes=1024 * 1024,
            )
            assert report["site_packages_exactly_covered"] is True
            assert report["dependency_count"] == 1
        else:
            with pytest.raises(ValueError, match=message):
                _validate_dependency_install_projection(
                    archive,
                    relative_infos,
                    dependencies,
                    requirements,
                    project_paths=set(),
                    maximum_files=100,
                    maximum_record_bytes=1024 * 1024,
                )


def test_windows_launchers_use_embedded_isolated_python() -> None:
    for launcher in (CLI_LAUNCHER, WEB_LAUNCHER):
        assert "%~dp0" in launcher
        assert "runtime\\python.exe" in launcher
        assert "-I -X utf8 -m topoforge.cli.app" in launcher
        assert "PYTHONNOUSERSITE=1" in launcher
    assert "-m topoforge.cli.app web %*" in WEB_LAUNCHER


def test_windows_batch_command_uses_verified_package_root(tmp_path: Path) -> None:
    package_root = tmp_path / "portable path with spaces" / "地形" / "TopoForge-Windows-x64"

    doctor = _windows_batch_command(
        package_root / "topoforge.cmd",
        ["doctor"],
        cwd=package_root,
    )
    assert doctor == [str((package_root / "topoforge.cmd").resolve()), "doctor"]

    state = package_root / "state & path"
    web = _windows_batch_command(
        package_root / "TopoForge-Web.cmd",
        ["--check", "--state-dir", str(state)],
        cwd=package_root,
    )
    assert web == [
        str((package_root / "TopoForge-Web.cmd").resolve()),
        "--check",
        "--state-dir",
        str(state),
    ]

    with pytest.raises(ValueError, match="package root"):
        _windows_batch_command(
            package_root / "topoforge.cmd",
            ["doctor"],
            cwd=tmp_path,
        )


def test_portable_official_bambu_acceptance_is_explicit_and_uses_embedded_python() -> None:
    source = (_repository_root() / "scripts" / "verify_windows_portable.py").read_text(
        encoding="utf-8"
    )

    assert 'parser.add_argument("--verify-bambu", action="store_true")' in source
    assert '"--verify-bambu requires --execute"' in source
    assert '"--verify-bambu requires --work-root to retain native evidence"' in source
    assert "resolved_work_root = work_root.expanduser().resolve()" in source
    assert source.count("shell=True") == 2
    assert 'str(python),\n            "-I",\n            "-X",\n            "utf8"' in source
    assert '"scripts" / "verify_windows_bambu.py"' in source
    assert '"--require-windows"' in source
    assert '"--expected-target"' in source
    assert '"--expected-source-commit"' in source
    assert '"--browser-mode"' in source
    assert '"--candidate-binding"' in source
    assert '"--expected-publisher-subject"' in source
    assert '"--expected-certificate-thumbprint"' in source
    assert '"--expected-profile-content-identity-sha256"' in source
    assert '"--expected-machine-profile-sha256"' in source
    assert '"--expected-process-profile-sha256"' in source
    assert '"--expected-filament-profile-sha256"' in source
    assert "_bambu_profile_binding_contract(" in source
    assert 'project.get("all_projects_reopened") is not True' in source
    assert 'project.get("external_profiles_loaded_on_reopen") is not False' in source


def test_verified_archive_publication_preserves_previous_candidate_on_failure(
    tmp_path: Path,
) -> None:
    staged = tmp_path / "staged.zip"
    destination = tmp_path / "candidate.zip"
    staged.write_bytes(b"new candidate")
    destination.write_bytes(b"previous verified candidate")

    with pytest.raises(ValueError, match="verification did not pass"):
        _publish_verified_archive(
            staged,
            destination,
            verification={"required_checks_passed": False},
            overwrite=True,
        )

    assert staged.read_bytes() == b"new candidate"
    assert destination.read_bytes() == b"previous verified candidate"


def test_verified_archive_publication_atomically_replaces_after_success(
    tmp_path: Path,
) -> None:
    staged = tmp_path / "staged.zip"
    destination = tmp_path / "candidate.zip"
    staged.write_bytes(b"verified candidate")
    destination.write_bytes(b"previous candidate")

    _publish_verified_archive(
        staged,
        destination,
        verification={"required_checks_passed": True},
        overwrite=True,
    )

    assert not staged.exists()
    assert destination.read_bytes() == b"verified candidate"


def test_verified_archive_publication_is_atomic_across_filesystems(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged_directory = tmp_path / "staging"
    destination_directory = tmp_path / "destination"
    staged_directory.mkdir()
    destination_directory.mkdir()
    staged = staged_directory / "staged.zip"
    destination = destination_directory / "candidate.zip"
    staged.write_bytes(b"verified cross-filesystem candidate")
    destination.write_bytes(b"previous candidate")
    real_replace = os.replace

    def replace(source: str | Path, target: str | Path) -> None:
        if Path(source) == staged and Path(target) == destination:
            raise OSError(errno.EXDEV, "simulated cross-device replace")
        real_replace(source, target)

    monkeypatch.setattr("scripts.build_windows_portable.os.replace", replace)

    _publish_verified_archive(
        staged,
        destination,
        verification={"required_checks_passed": True},
        overwrite=True,
    )

    assert not staged.exists()
    assert destination.read_bytes() == b"verified cross-filesystem candidate"
    assert list(destination_directory.glob(".candidate.zip.*.tmp")) == []


def test_portable_report_writer_uses_atomic_canonical_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "portable-report.json"
    report = {"required_checks_passed": True}
    calls: list[tuple[Path, dict[str, Any]]] = []

    def write_canonical_json(path: Path, payload: dict[str, Any]) -> None:
        calls.append((path, payload))

    monkeypatch.setattr(
        portable_verifier,
        "write_canonical_json",
        write_canonical_json,
    )

    portable_verifier._write_report(destination, report)

    assert calls == [(destination, report)]


def test_verified_extraction_rechecks_open_archive_identity(tmp_path: Path) -> None:
    archive_path = tmp_path / "candidate.zip"
    _write_members(
        archive_path,
        [("TopoForge-Windows-x64/runtime/python.exe", b"python")],
    )
    destination = tmp_path / "extracted"

    with pytest.raises(ValueError, match="SHA-256 changed before extraction"):
        _extract_verified_archive(
            archive_path,
            destination,
            package_root="TopoForge-Windows-x64",
            expected_sha256="0" * 64,
            expected_bytes=archive_path.stat().st_size,
        )

    assert not destination.exists()


def test_platform_core_reports_missing_explicit_interpreter(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "python.exe"
    with pytest.raises(FileNotFoundError, match="--python-executable"):
        verify_platform_core(
            tmp_path / "acceptance",
            python_executable=missing,
        )


def test_windows_portable_ci_contract() -> None:
    root = _repository_root()
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["windows-portable"]
    job_steps = job["steps"]
    steps = json.dumps(job_steps, sort_keys=True)

    assert job["needs"] == "windows-core"
    assert job["runs-on"] == "windows-2022"
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" in steps
    assert '"architecture": "x64"' in steps
    assert "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020" in steps
    assert steps.count("scripts/build_windows_portable.py") == 2
    assert "scripts/verify_windows_portable.py" in steps
    assert "--repeat-archive" in steps
    assert "--execute" in steps
    assert "--verify-bambu" not in steps
    assert "Report Windows portable verification failure" in steps
    assert "failure() && steps.windows-portable-verification.outcome == 'failure'" in steps
    assert "::error title=Windows portable verification" in steps
    assert "Get-FileHash" in steps
    assert "SHA256SUMS" in steps
    assert "::notice title=Windows portable SHA-256" in steps

    success_upload = next(
        step
        for step in job_steps
        if step.get("name") == "Retain verified Windows portable candidate and evidence"
    )
    success_paths = {
        line.strip() for line in success_upload["with"]["path"].splitlines() if line.strip()
    }
    assert success_upload["if"] == "success()"
    assert (
        success_upload["uses"] == "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    )
    assert "dist/windows-primary/*.zip" in success_paths
    assert "dist/windows-primary/SHA256SUMS" in success_paths

    failure_upload = next(
        step
        for step in job_steps
        if step.get("name") == "Retain failed Windows portable diagnostics only"
    )
    failure_paths = [
        line.strip() for line in failure_upload["with"]["path"].splitlines() if line.strip()
    ]
    assert failure_upload["if"] == "failure()"
    assert (
        failure_upload["uses"] == "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    )
    assert failure_paths == [
        "artifacts/logs/ci-windows-portable-primary-build.json",
        "artifacts/logs/ci-windows-portable-repeat-build.json",
        "artifacts/logs/ci-windows-portable-verification.json",
    ]
    assert all(path.endswith(".json") for path in failure_paths)
    assert not any(
        ".zip" in path.casefold() or "sha256sums" in path.casefold() for path in failure_paths
    )


def test_public_release_requires_full_windows_evidence_before_stage_and_publish() -> None:
    root = _repository_root()
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    prepare = workflow["jobs"]["prepare"]
    release = workflow["jobs"]["release"]
    indexed = {step.get("name"): (index, step) for index, step in enumerate(prepare["steps"])}

    evidence_index, evidence = indexed["Verify exact Windows archive and clean-system reports"]
    stage_index, _ = indexed["Stage verified Windows portable archive"]
    assets_index, _ = indexed["Stage release assets and checksums"]
    publish = next(
        step for step in release["steps"] if step.get("name") == "Publish GitHub Release"
    )

    assert "scripts/verify_release_evidence.py" in evidence["run"]
    assert "--artifact-root dist/windows-evidence" in evidence["run"]
    assert "--metadata-only" not in evidence["run"]
    assert "steps.windows-evidence.outputs.required == 'true'" in evidence["if"]
    assert evidence_index < stage_index < assets_index
    assert release["needs"] == "prepare"
    assert release["permissions"] == {"actions": "read", "contents": "write"}
    assert all(
        not step.get("uses", "").startswith("actions/checkout@") for step in release["steps"]
    )
    assert 'gh release create "$RELEASE_TAG"' in publish["run"]


def test_source_binding_rejects_dirty_and_wrong_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "1" * 40

    def dirty_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        stdout = commit + "\n" if "rev-parse" in command else " M tracked.py\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(windows_acceptance.subprocess, "run", dirty_run)
    with pytest.raises(RuntimeError, match=r"bounded Git status:\n M tracked[.]py"):
        windows_acceptance.source_repository_record(
            tmp_path,
            expected_commit=commit,
            require_clean=True,
        )

    def clean_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        stdout = commit + "\n" if "rev-parse" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(windows_acceptance.subprocess, "run", clean_run)
    with pytest.raises(RuntimeError, match="expected"):
        windows_acceptance.source_repository_record(
            tmp_path,
            expected_commit="2" * 40,
            require_clean=True,
        )


def test_nested_binding_rejects_target_registry_mismatch() -> None:
    archive_sha256 = "a" * 64
    verifier_sha256 = "b" * 64
    build_constraints_sha256 = "e" * 64
    binding = {
        "archive": {"sha256": archive_sha256},
        "source_repository": {"commit": "c" * 40},
        "config_sha256": "d" * 64,
        "build_constraints_sha256": build_constraints_sha256,
        "verifier_sha256": {
            "builder": "1" * 64,
            "portable": "2" * 64,
            "system": verifier_sha256,
            "bambu": "3" * 64,
            "helper": "4" * 64,
        },
    }
    parent_target = {
        "target_id": "windows-10-22h2-x64",
        "product_name": "Windows 10 Pro",
        "display_version": "22H2",
        "current_build_number": 19045,
        "ubr": 6216,
        "installation_type": "Client",
        "process_machine_code": 0x0000,
        "process_machine": "UNKNOWN",
        "native_machine_code": 0x8664,
        "native_machine": "AMD64",
        "native_x64_verified": True,
        "target_verified": True,
    }
    binding_sha256 = hashlib.sha256(
        (json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()
    report = {
        "candidate_binding": {
            "binding_sha256": binding_sha256,
            "archive_sha256": archive_sha256,
            "source_commit": "c" * 40,
            "source_tracked_dirty": False,
            "config_sha256": "d" * 64,
            "build_constraints_sha256": build_constraints_sha256,
            "verifier_role": "system",
            "verifier_sha256": verifier_sha256,
            "required_checks_passed": True,
        },
        "expected_target": "windows-10-22h2-x64",
        "windows_target": {**parent_target, "ubr": 9999},
    }

    with pytest.raises(RuntimeError, match="differs at ubr"):
        _nested_binding_contract(
            report,
            binding=binding,
            role="system",
            target_id="windows-10-22h2-x64",
            windows_target=parent_target,
        )


def _windows_containment_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    binding: dict[str, Any] = {
        "source_repository": {"commit": "c" * 40},
        "verifier_sha256": {"system": "d" * 64},
    }
    binding_sha256 = hashlib.sha256(
        (json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()

    def process_mode(mode: str, *, leader_pid: int, child_pid: int) -> dict[str, object]:
        return {
            "containment_enabled": True,
            "leader_pid": leader_pid,
            "leader_process_group_id": leader_pid,
            "leader_process_identity": f"windows:{leader_pid}00",
            "child_pid": child_pid,
            "child_process_identity": f"windows:{child_pid}00",
            "mode": mode,
            "leader_exit_code": 0 if mode == "leader-exit" else 1,
            "required_checks_passed": True,
        }

    leader_exit = {
        **process_mode("leader-exit", leader_pid=101, child_pid=102),
        "leader_alive_after_exit": False,
        "child_alive_after_exit": False,
        "kill_on_job_close_verified": True,
    }
    cancellation = {
        **process_mode("cancel", leader_pid=201, child_pid=202),
        "leader_alive_after_cancel": False,
        "child_alive_after_cancel": False,
        "production_termination_adapter_exercised": True,
    }
    report: dict[str, Any] = {
        "windows_process_containment": {
            "platform": "Windows",
            "executed": True,
            "containment_entrypoint": (
                "topoforge.web.processes.enable_current_process_containment"
            ),
            "probe_code_sha256": "e" * 64,
            "source_binding": {
                "candidate_bound": True,
                "candidate_binding_sha256": binding_sha256,
                "source_commit": "c" * 40,
                "system_verifier_sha256": "d" * 64,
                "system_verifier_matches_candidate": True,
                "required_checks_passed": True,
            },
            "leader_exit": leader_exit,
            "cancellation": cancellation,
            "job_object_kill_on_close_verified": True,
            "production_cancellation_verified": True,
            "required_checks_passed": True,
        }
    }
    return report, binding


def test_nested_windows_containment_is_source_bound_and_complete() -> None:
    report, binding = _windows_containment_fixture()

    containment = _windows_containment_contract(
        report,
        binding=binding,
    )

    assert containment["job_object_kill_on_close_verified"] is True
    assert containment["production_cancellation_verified"] is True


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (
            ("leader_exit", "child_alive_after_exit"),
            True,
            "leader_exit differs at child_alive_after_exit",
        ),
        (
            ("source_binding", "candidate_binding_sha256"),
            "f" * 64,
            "source binding differs at candidate_binding_sha256",
        ),
        (
            ("cancellation", "child_process_identity"),
            "linux:not-windows",
            "cancellation.child_process_identity is invalid",
        ),
    ],
)
def test_nested_windows_containment_rejects_tampering(
    path: tuple[str, str],
    value: object,
    message: str,
) -> None:
    report, binding = _windows_containment_fixture()
    containment = report["windows_process_containment"]
    assert isinstance(containment, dict)
    nested = containment[path[0]]
    assert isinstance(nested, dict)
    nested[path[1]] = value

    with pytest.raises(RuntimeError, match=message):
        _windows_containment_contract(
            report,
            binding=binding,
        )


def test_candidate_binding_rejects_tampered_nested_verifier(
    tmp_path: Path,
) -> None:
    verifier = tmp_path / "verify_windows_system.py"
    constraints_sha256 = windows_acceptance.sha256_file(
        _repository_root() / "packaging" / "build-constraints.txt"
    )
    verifier.write_text("print('changed')\n", encoding="utf-8")
    binding_path = tmp_path / "candidate-binding.json"
    windows_acceptance.write_canonical_json(
        binding_path,
        {
            "schema_version": "topoforge-windows-candidate-binding-v1",
            "expected_target": "win10-22h2",
            "target_id": "windows-10-22h2-x64",
            "required_checks_passed": True,
            "archive": {"sha256": "a" * 64, "bytes": 123},
            "source_repository": {
                "commit": "b" * 40,
                "expected_commit": "b" * 40,
                "tracked_dirty": False,
                "clean_required": True,
                "required_checks_passed": True,
            },
            "config_sha256": "c" * 64,
            "build_constraints_sha256": constraints_sha256,
            "verifier_sha256": {
                "builder": "1" * 64,
                "portable": "2" * 64,
                "system": "d" * 64,
                "bambu": "3" * 64,
                "helper": "4" * 64,
            },
        },
    )

    with pytest.raises(RuntimeError, match="verifier SHA-256 differs"):
        windows_acceptance.load_candidate_binding(
            binding_path,
            verifier_role="system",
            verifier_path=verifier,
            expected_target="win10-22h2",
        )


def test_embedded_build_provenance_is_source_bound_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "a" * 64
    commit = "b" * 40
    config = tmp_path / "windows-x64-runtime.json"
    constraints = tmp_path / "build-constraints.txt"
    config.write_text("{}\n", encoding="utf-8")
    constraints.write_text("hatchling==1\n", encoding="utf-8")
    monkeypatch.setattr(portable_verifier, "evidence_sha256_file", lambda _path: digest)
    embedded = {
        "source_commit": commit,
        "source_tracked_dirty": False,
        "config_sha256": digest,
        "build_constraints_sha256": digest,
        "verifier_sha256": {
            role: digest for role in ("builder", "portable", "system", "bambu", "helper")
        },
        "required_checks_passed": True,
    }

    report = _validate_build_provenance(
        {"build_provenance": embedded},
        config_path=config,
        build_constraints_path=constraints,
    )

    assert report["source_commit"] == commit
    assert report["source_dirty"] is False
    assert set(report["verifier_sha256"]) == {
        "builder",
        "portable",
        "system",
        "bambu",
        "helper",
    }

    dirty = copy.deepcopy(embedded)
    dirty["source_tracked_dirty"] = True
    with pytest.raises(ValueError, match="clean source commit"):
        _validate_build_provenance(
            {"build_provenance": dirty},
            config_path=config,
            build_constraints_path=constraints,
        )

    missing_builder = copy.deepcopy(embedded)
    del missing_builder["verifier_sha256"]["builder"]
    with pytest.raises(ValueError, match="role set changed"):
        _validate_build_provenance(
            {"build_provenance": missing_builder},
            config_path=config,
            build_constraints_path=constraints,
        )


def test_portable_verifier_requires_explicit_clean_or_hosted_execution_contract() -> None:
    source = (_repository_root() / "scripts" / "verify_windows_portable.py").read_text(
        encoding="utf-8"
    )

    assert '"--execute requires --expected-target or explicit --hosted-server"' in source
    assert '"clean --expected-target requires a 40-hex --expected-source-commit"' in source
    assert '"--hosted-server requires --browser-mode skip"' in source
    assert '"clean --expected-target requires --browser-mode require"' in source
    assert '"--verify-bambu requires exactly one --expected-publisher-subject and "' in source


def test_portable_verifier_requires_build_constraints_provenance() -> None:
    source = (_repository_root() / "scripts" / "verify_windows_portable.py").read_text(
        encoding="utf-8"
    )

    assert '"provenance/build-constraints.txt"' in source
    assert '"build_constraints_sha256"' in source
    assert '"portable build constraints differ from the verifier source"' in source


def _portable_bambu_profile_fixture(
    *,
    profiles_root: str = "C:/Program Files/Bambu Studio/resources/profiles/BBL",
    manifest_sha256: str = "9" * 64,
) -> tuple[
    dict[str, Any],
    dict[str, str | None],
]:
    expected: dict[str, str | None] = {
        "content_identity": "a" * 64,
        "machine": "b" * 64,
        "process": "c" * 64,
        "filament": "d" * 64,
    }
    source_records = {
        kind: [
            {
                "kind": kind,
                "name": f"{kind} source",
                "path": f"{kind}/source.json",
                "sha256": "1" * 64,
                "size_bytes": 10,
            }
        ]
        for kind in ("machine", "process", "filament")
    }
    resolved = {
        kind: {
            "path": f"{profiles_root}/prepared/{kind}.json",
            "sha256": expected[kind],
            "size_bytes": 10,
            "name": kind,
            "expected_sha256": expected[kind],
            "sha256_matched": True,
            "source_count": 1,
        }
        for kind in ("machine", "process", "filament")
    }
    binding = {
        "path": profiles_root,
        "selection_mode": "executable-sibling-discovery",
        "expected_executable_sibling_path": profiles_root,
        "relative_to_executable": "resources/profiles/BBL",
        "is_executable_sibling": True,
        "override_requested": False,
        "override_authorized_by_frozen_hashes": None,
        "profile_identity_frozen": True,
        "profile_manifest_sha256": manifest_sha256,
        "profile_content_identity_sha256": expected["content_identity"],
        "expected_profile_content_identity_sha256": expected["content_identity"],
        "profile_content_identity_sha256_matched": True,
        "resolved_profiles": resolved,
        "expected_resolved_profile_sha256": {
            kind: expected[kind] for kind in ("machine", "process", "filament")
        },
        "source_records": source_records,
        "source_records_sha256": "e" * 64,
        "source_root_identity_sha256": "f" * 64,
        "required_checks_passed": True,
    }
    report = {
        "bambu_studio": {"profiles_root_binding": binding},
        "profile_bundle": {
            "manifest": {
                "sha256": manifest_sha256,
            },
            "machine": resolved["machine"],
            "process": resolved["process"],
            "filament": resolved["filament"],
            "source_records_sha256": "e" * 64,
            "profile_content_identity_sha256": expected["content_identity"],
            "expected_profile_content_identity_sha256": expected["content_identity"],
            "profile_content_identity_sha256_matched": True,
            "profile_identity_frozen": True,
            "required_checks_passed": True,
        },
    }
    return report, expected


def test_portable_bambu_profile_hashes_require_complete_set() -> None:
    digest = "A" * 64

    assert _profile_hash_arguments(
        content_identity_sha256=digest,
        machine_sha256=digest,
        process_sha256=digest,
        filament_sha256=digest,
        required=True,
    ) == {
        "content_identity": "a" * 64,
        "machine": "a" * 64,
        "process": "a" * 64,
        "filament": "a" * 64,
    }

    with pytest.raises(
        RuntimeError,
        match="profile content identity plus machine/process/filament",
    ):
        _profile_hash_arguments(
            content_identity_sha256=digest,
            machine_sha256=None,
            process_sha256=digest,
            filament_sha256=digest,
            required=True,
        )


def test_portable_bambu_profile_binding_requires_sibling_and_exact_hashes() -> None:
    report, expected = _portable_bambu_profile_fixture()

    binding = _bambu_profile_binding_contract(
        report,
        expected_hashes=expected,
    )

    assert binding["is_executable_sibling"] is True
    assert binding["profile_identity_frozen"] is True

    tampered = copy.deepcopy(report)
    tampered["bambu_studio"]["profiles_root_binding"]["is_executable_sibling"] = False
    with pytest.raises(RuntimeError, match="frozen profile-root identity"):
        _bambu_profile_binding_contract(
            tampered,
            expected_hashes=expected,
        )

    tampered = copy.deepcopy(report)
    tampered["bambu_studio"]["profiles_root_binding"]["resolved_profiles"]["process"]["sha256"] = (
        "0" * 64
    )
    with pytest.raises(RuntimeError, match="process frozen profile identity changed"):
        _bambu_profile_binding_contract(
            tampered,
            expected_hashes=expected,
        )

    tampered = copy.deepcopy(report)
    tampered["profile_bundle"]["manifest"]["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="profile bundle projection changed"):
        _bambu_profile_binding_contract(
            tampered,
            expected_hashes=expected,
        )


def test_portable_bambu_profile_binding_is_stable_across_install_roots() -> None:
    first, expected = _portable_bambu_profile_fixture(
        profiles_root="C:/Program Files/Bambu Studio/resources/profiles/BBL",
        manifest_sha256="8" * 64,
    )
    second, second_expected = _portable_bambu_profile_fixture(
        profiles_root="D:/Apps/Bambu Studio/resources/profiles/BBL",
        manifest_sha256="9" * 64,
    )

    first_binding = _bambu_profile_binding_contract(first, expected_hashes=expected)
    second_binding = _bambu_profile_binding_contract(
        second,
        expected_hashes=second_expected,
    )

    assert first_binding["path"] != second_binding["path"]
    assert first_binding["profile_manifest_sha256"] != second_binding["profile_manifest_sha256"]
    assert (
        first_binding["profile_content_identity_sha256"]
        == second_binding["profile_content_identity_sha256"]
        == expected["content_identity"]
    )
