from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest
import yaml
from scripts.build_windows_portable import (
    CLI_LAUNCHER,
    WEB_LAUNCHER,
    _load_config,
    _publish_verified_archive,
    _safe_relative_path,
    _sha256,
    _verify_runtime_archive,
    _write_reproducible_zip,
)
from scripts.verify_platform_core import verify_platform_core
from scripts.verify_windows_portable import (
    _extract_verified_archive,
    _validate_archive_members,
    _validate_manifest_files,
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


def test_windows_launchers_use_embedded_isolated_python() -> None:
    for launcher in (CLI_LAUNCHER, WEB_LAUNCHER):
        assert "%~dp0" in launcher
        assert "runtime\\python.exe" in launcher
        assert "-I -X utf8 -m topoforge.cli.app" in launcher
        assert "PYTHONNOUSERSITE=1" in launcher
    assert "-m topoforge.cli.app web %*" in WEB_LAUNCHER


def test_portable_official_bambu_acceptance_is_explicit_and_uses_embedded_python() -> None:
    source = (_repository_root() / "scripts" / "verify_windows_portable.py").read_text(
        encoding="utf-8"
    )

    assert 'parser.add_argument("--verify-bambu", action="store_true")' in source
    assert '"--verify-bambu requires --execute"' in source
    assert '"--verify-bambu requires --work-root to retain native evidence"' in source
    assert 'str(python),\n            "-I",\n            "-X",\n            "utf8"' in source
    assert '"scripts" / "verify_windows_bambu.py"' in source
    assert '"--require-windows"' in source
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
    steps = json.dumps(job["steps"], sort_keys=True)

    assert job["needs"] == "windows-core"
    assert job["runs-on"] == "windows-2022"
    assert "actions/setup-python@v5" in steps
    assert '"architecture": "x64"' in steps
    assert "actions/setup-node@v4" in steps
    assert steps.count("scripts/build_windows_portable.py") == 2
    assert "scripts/verify_windows_portable.py" in steps
    assert "--repeat-archive" in steps
    assert "--execute" in steps
    assert "--verify-bambu" not in steps
    assert "actions/upload-artifact@v4" in steps


def test_public_release_does_not_publish_candidate_before_clean_vm_gates() -> None:
    workflow = (_repository_root() / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "windows-x64-portable" not in workflow.casefold()
