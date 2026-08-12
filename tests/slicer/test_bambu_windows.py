from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import topoforge.validation.slicers.bambu as bambu_module
from topoforge.validation.bambu_projects import isolated_environment, release_gate
from topoforge.validation.slicers import (
    BambuStudioAdapter,
    CommandExecution,
    SlicerAvailability,
)
from topoforge.validation.slicers._bambu_windows import (
    discover_bambu_profiles_root,
    discover_windows_bambu_studio,
    windows_bambu_studio_candidates,
)


class _ProbeRunner:
    def __call__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandExecution:
        del command, timeout_seconds, env, cwd
        return CommandExecution(0, "BambuStudio-02.07.01.62:", "", 0.01)


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic executable")
    path.chmod(0o755)
    return path


def test_windows_discovery_checks_machine_then_user_install_roots(tmp_path: Path) -> None:
    program_files = tmp_path / "Program Files"
    local_app_data = tmp_path / "Users" / "Maker" / "AppData" / "Local"
    environment = {
        "ProgramW6432": str(program_files),
        "ProgramFiles": str(program_files),
        "LOCALAPPDATA": str(local_app_data),
    }
    candidates = windows_bambu_studio_candidates(environ=environment)
    assert candidates == (
        program_files / "Bambu Studio" / "bambu-studio.exe",
        program_files / "Bambu Studio" / "BambuStudio.exe",
        local_app_data / "Programs" / "Bambu Studio" / "bambu-studio.exe",
        local_app_data / "Programs" / "Bambu Studio" / "BambuStudio.exe",
    )
    user_executable = _executable(candidates[2])

    assert (
        discover_windows_bambu_studio(
            environ=environment,
            system="Windows",
        )
        == user_executable.resolve()
    )
    assert (
        discover_windows_bambu_studio(
            environ=environment,
            system="Linux",
        )
        is None
    )


def test_adapter_uses_standard_windows_fallback_but_keeps_manual_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    discovered = _executable(tmp_path / "Program Files" / "Bambu Studio" / "bambu-studio.exe")
    monkeypatch.delenv("TOPOFORGE_BAMBU_STUDIO", raising=False)
    monkeypatch.delenv("BAMBU_STUDIO", raising=False)
    monkeypatch.setattr(bambu_module, "discover_windows_bambu_studio", lambda: discovered)

    automatic = BambuStudioAdapter(runner=_ProbeRunner())
    assert automatic.probe().status is SlicerAvailability.AVAILABLE
    assert automatic.executable == discovered

    missing_override = tmp_path / "manual" / "missing.exe"
    manual = BambuStudioAdapter(missing_override, runner=_ProbeRunner())
    assert manual.executable == missing_override
    assert manual.probe().status is SlicerAvailability.UNAVAILABLE


def test_profile_root_uses_override_or_official_sibling_layout(tmp_path: Path) -> None:
    executable = _executable(tmp_path / "Bambu Studio" / "bambu-studio.exe")
    sibling = executable.parent / "resources" / "profiles" / "BBL"
    override = tmp_path / "copied profiles" / "BBL"
    for root in (sibling, override):
        for kind in ("machine", "process", "filament"):
            (root / kind).mkdir(parents=True, exist_ok=True)

    assert discover_bambu_profiles_root(executable) == sibling.resolve()
    assert (
        discover_bambu_profiles_root(
            executable,
            environ={"TOPOFORGE_BAMBU_PROFILES": str(override)},
        )
        == override.resolve()
    )


def test_project_environment_isolates_windows_application_data_and_temp(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime with spaces" / "地形"
    environment = isolated_environment(
        runtime,
        system="Windows",
        environ={"SystemRoot": r"C:\Windows"},
    )

    assert environment["APPDATA"] == str(runtime / "home" / "AppData" / "Roaming")
    assert environment["LOCALAPPDATA"] == str(runtime / "home" / "AppData" / "Local")
    assert environment["TEMP"] == str(runtime / "temp")
    assert environment["TMP"] == str(runtime / "temp")
    assert environment["USERPROFILE"] == str(runtime / "home")
    assert "XDG_CONFIG_HOME" not in environment
    assert "APPIMAGE_EXTRACT_AND_RUN" not in environment
    assert Path(environment["APPDATA"]).is_dir()
    assert Path(environment["TEMP"]).is_dir()


def test_project_release_gate_records_generator_version_from_gcode(tmp_path: Path) -> None:
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text("; BambuStudio 02.99.00.01\n", encoding="utf-8")

    _metrics, _gate, version = release_gate(
        gcode,
        expected_version="02.99.00.01",
        stdout="",
        stderr="",
    )

    assert version == "02.99.00.01"
