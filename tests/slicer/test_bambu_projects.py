"""Bambu project version and macOS isolation regressions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

import topoforge.validation.bambu_projects as bambu_projects_module
from topoforge.validation.bambu_projects import (
    frozen_source_bambu_version,
    isolated_environment,
    probe_bambu_studio,
    release_gate,
)
from topoforge.validation.slicers import CommandExecution


class _ProbeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandExecution:
        del timeout_seconds, cwd
        self.calls.append((tuple(command), env))
        return CommandExecution(0, "BambuStudio-02.07.01.62:", "", 0.01)


def test_darwin_bambu_environment_uses_private_macos_user_directories(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    environment = isolated_environment(
        runtime,
        platform_name="darwin",
        environ={
            "PATH": "/usr/bin",
            "APPIMAGE_EXTRACT_AND_RUN": "1",
            "XDG_CONFIG_HOME": "/real/config",
            "XDG_CACHE_HOME": "/real/cache",
            "XDG_RUNTIME_DIR": "/real/runtime",
        },
    )

    home = runtime / "home"
    assert environment["HOME"] == str(home)
    assert environment["CFFIXED_USER_HOME"] == str(home)
    assert environment["TMPDIR"] == str(runtime / "tmp")
    assert "APPIMAGE_EXTRACT_AND_RUN" not in environment
    assert not any(key.startswith("XDG_") for key in environment)
    assert (home / "Library" / "Application Support").is_dir()
    assert (home / "Library" / "Preferences").is_dir()
    assert (home / "Library" / "Caches").is_dir()


def test_project_gcode_version_must_match_frozen_source_slice(tmp_path: Path) -> None:
    gcode = tmp_path / "plate.gcode"
    gcode.write_text("; BambuStudio 03.00.00.01\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="does not match the frozen"):
        release_gate(gcode, expected_version="02.07.01.62", stdout="", stderr="")


def test_frozen_source_version_requires_available_bambu_probe() -> None:
    manifest = {
        "slicer": {
            "name": "BambuStudio",
            "version": "02.07.01.62",
            "status": "available",
        }
    }
    assert frozen_source_bambu_version(manifest) == "02.07.01.62"

    manifest["slicer"]["version"] = None
    with pytest.raises(RuntimeError, match="must freeze"):
        frozen_source_bambu_version(manifest)


def test_probe_record_is_version_parsed_and_hash_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _ProbeRunner()
    monkeypatch.setattr(bambu_projects_module, "run_command", runner)
    executable = tmp_path / "BambuStudio"
    executable.write_bytes(b"test executable identity\n")

    report = probe_bambu_studio(
        executable,
        runtime=tmp_path / "probe",
        timeout_seconds=5,
        evidence_root=tmp_path / "evidence",
    )

    assert report["version"] == "02.07.01.62"
    assert report["process_exit_code"] == 0
    assert len(report["stdout_sha256"]) == 64
    assert len(report["stderr_sha256"]) == 64
    assert report["stdout_path"] == "bambu-studio-probe.stdout.log"
    assert report["stderr_path"] == "bambu-studio-probe.stderr.log"
    assert (tmp_path / "evidence" / report["stdout_path"]).read_text() == (
        "BambuStudio-02.07.01.62:"
    )
    assert runner.calls[0][0] == (str(executable), "--help")
    assert runner.calls[0][1] is not None
