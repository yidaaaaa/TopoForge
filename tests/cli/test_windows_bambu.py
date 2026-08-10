from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from typer.testing import CliRunner

import topoforge.cli.app as cli_module
import topoforge.validation.slicers as slicers_module
from topoforge.cli.app import app
from topoforge.validation.slicers import (
    SliceResult,
    SlicerInfo,
    SlicerProfile,
    SliceStatus,
)
from topoforge.validation.slicers._bambu_profiles import (
    DEFAULT_FILAMENT_PROFILE,
    DEFAULT_MACHINE_PROFILE,
    DEFAULT_PROCESS_PROFILE,
)
from topoforge.validation.slicers.base import SlicerAvailability

runner = CliRunner()


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _profiles(root: Path) -> Path:
    _write(root / "machine" / "p2s.json", {"name": DEFAULT_MACHINE_PROFILE})
    _write(root / "process" / "standard.json", {"name": DEFAULT_PROCESS_PROFILE})
    _write(root / "filament" / "pla.json", {"name": DEFAULT_FILAMENT_PROFILE})
    return root


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic Bambu Studio")
    path.chmod(0o755)
    return path


class _SuccessfulBambuAdapter:
    def __init__(self, executable: str | Path | None = None, **_: object) -> None:
        self.executable = None if executable is None else Path(executable).resolve()

    def slice(
        self,
        input_model: Path,
        output_gcode: Path,
        *,
        profile: SlicerProfile | None = None,
        extra_args: Sequence[str] = (),
        timeout_seconds: float = 600.0,
    ) -> SliceResult:
        del extra_args, timeout_seconds
        output_gcode.parent.mkdir(parents=True, exist_ok=True)
        output_gcode.write_text("; BambuStudio 02.07.01.62\n", encoding="utf-8")
        return SliceResult(
            status=SliceStatus.SUCCEEDED,
            slicer=SlicerInfo(
                name="BambuStudio",
                version="02.07.01.62",
                executable=self.executable,
                status=SlicerAvailability.AVAILABLE,
            ),
            profile=(profile or SlicerProfile()).label,
            input_model=input_model.resolve(),
            output_gcode=output_gcode.resolve(),
            gcode_generated=True,
            gcode_size_bytes=output_gcode.stat().st_size,
        )


def test_web_check_prepares_official_profiles_below_state_dir(tmp_path: Path) -> None:
    executable = _executable(tmp_path / "Program Files" / "Bambu Studio" / "bambu-studio.exe")
    profiles = _profiles(executable.parent / "resources" / "profiles" / "BBL")
    state = tmp_path / "Local AppData" / "TopoForge" / "state"
    workspace = tmp_path / "Local AppData" / "TopoForge" / "workspaces"
    inputs = tmp_path / "inputs"
    inputs.mkdir()

    result = runner.invoke(
        app,
        [
            "web",
            "--check",
            "--state-dir",
            str(state),
            "--workspace-root",
            str(workspace),
            "--input-root",
            str(inputs),
            "--bambu-studio-executable",
            str(executable),
            "--bambu-profiles-root",
            str(profiles),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    bundle = payload["bambu_profile_bundle"]
    manifest = Path(bundle["manifest"])
    assert manifest.is_relative_to(state / "bambu-profiles")
    assert manifest.is_file()
    assert payload["bambu_configuration"]["executable"] == str(executable.resolve())
    assert bundle["required_checks_passed"] is True


def test_slice_prepares_raw_profiles_and_reports_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executable = _executable(tmp_path / "Bambu Studio" / "bambu-studio.exe")
    profiles = _profiles(executable.parent / "resources" / "profiles" / "BBL")
    model = tmp_path / "terrain.3mf"
    model.write_bytes(b"3mf")
    output = tmp_path / "terrain.gcode"
    monkeypatch.setattr(slicers_module, "BambuStudioAdapter", _SuccessfulBambuAdapter)
    monkeypatch.setattr(cli_module, "default_web_state_dir", lambda: tmp_path / "application state")

    result = runner.invoke(
        app,
        [
            "slice",
            str(model),
            "--output",
            str(output),
            "--bambu-studio-executable",
            str(executable),
            "--bambu-profiles-root",
            str(profiles),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "succeeded"
    assert Path(payload["bambu_profile_manifest"]).is_file()
    assert len(payload["bambu_profile_manifest_sha256"]) == 64
    assert output.is_file()


def test_web_check_rejects_an_invalid_explicit_profiles_root(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    result = runner.invoke(
        app,
        [
            "web",
            "--check",
            "--state-dir",
            str(tmp_path / "state"),
            "--workspace-root",
            str(tmp_path / "workspaces"),
            "--input-root",
            str(inputs),
            "--bambu-profiles-root",
            str(tmp_path / "missing profiles"),
        ],
    )

    assert result.exit_code == 2
    assert "must contain machine, process, and filament directories" in result.output


def test_implicit_optional_profile_failure_does_not_block_core_web(tmp_path: Path) -> None:
    executable = _executable(tmp_path / "Bambu Studio" / "bambu-studio.exe")
    profiles = _profiles(executable.parent / "resources" / "profiles" / "BBL")
    (profiles / "machine" / "p2s.json").write_text("not JSON", encoding="utf-8")
    inputs = tmp_path / "inputs"
    inputs.mkdir()

    result = runner.invoke(
        app,
        [
            "web",
            "--check",
            "--state-dir",
            str(tmp_path / "state"),
            "--workspace-root",
            str(tmp_path / "workspaces"),
            "--input-root",
            str(inputs),
            "--bambu-studio-executable",
            str(executable),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["bambu_profile_bundle"]["status"] == "unconfigured"
    assert payload["bambu_profile_bundle"]["required_checks_passed"] is False
