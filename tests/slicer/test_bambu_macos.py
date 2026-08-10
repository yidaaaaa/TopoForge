"""macOS Bambu Studio application discovery contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from topoforge.validation.slicers import CommandExecution, SlicerAvailability
from topoforge.validation.slicers.bambu import (
    BambuStudioAdapter,
    macos_bambu_executable_candidates,
)


class ProbeRunner:
    """Return the current official version banner without invoking Bambu Studio."""

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
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_macos_candidates_cover_system_and_user_applications() -> None:
    home = Path("/Users/maker")

    candidates = macos_bambu_executable_candidates(
        home=home,
        applications_root=Path("/Applications"),
    )

    suffix = Path("BambuStudio.app/Contents/MacOS/BambuStudio")
    assert candidates == (
        Path("/Applications") / suffix,
        home / "Applications" / suffix,
    )


def test_macos_adapter_discovers_official_app_bundle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TOPOFORGE_BAMBU_STUDIO", raising=False)
    monkeypatch.delenv("BAMBU_STUDIO", raising=False)
    monkeypatch.setenv("PATH", "")
    executable = _executable(tmp_path / "Applications/BambuStudio.app/Contents/MacOS/BambuStudio")

    adapter = BambuStudioAdapter(
        runner=ProbeRunner(),
        platform_name="darwin",
        home=tmp_path / "home",
        applications_root=tmp_path / "Applications",
    )

    assert adapter.executable == executable.resolve()
    assert adapter.probe().status is SlicerAvailability.AVAILABLE


def test_environment_override_precedes_macos_app_discovery(tmp_path: Path, monkeypatch) -> None:
    configured = _executable(tmp_path / "configured/BambuStudio")
    _executable(tmp_path / "Applications/BambuStudio.app/Contents/MacOS/BambuStudio")
    monkeypatch.setenv("TOPOFORGE_BAMBU_STUDIO", str(configured))

    adapter = BambuStudioAdapter(
        runner=ProbeRunner(),
        platform_name="darwin",
        home=tmp_path / "home",
        applications_root=tmp_path / "Applications",
    )

    assert adapter.executable == configured.resolve()


def test_non_macos_adapter_does_not_scan_app_bundles(tmp_path: Path, monkeypatch) -> None:
    _executable(tmp_path / "Applications/BambuStudio.app/Contents/MacOS/BambuStudio")
    monkeypatch.delenv("TOPOFORGE_BAMBU_STUDIO", raising=False)
    monkeypatch.delenv("BAMBU_STUDIO", raising=False)
    monkeypatch.setenv("PATH", "")

    adapter = BambuStudioAdapter(
        runner=ProbeRunner(),
        platform_name="linux",
        home=tmp_path / "home",
        applications_root=tmp_path / "Applications",
    )

    assert adapter.executable is None
    assert adapter.probe().status is SlicerAvailability.UNAVAILABLE
