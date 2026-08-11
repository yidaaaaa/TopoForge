"""CLI doctor external-tool identity regressions."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import topoforge.validation.slicers as slicers_module
from topoforge.cli.app import app
from topoforge.validation.slicers import SlicerAvailability, SlicerInfo

runner = CliRunner()


class _DoctorBambuAdapter:
    def __init__(self, info: SlicerInfo) -> None:
        self.executable = info.executable
        self._info = info

    def probe(self) -> SlicerInfo:
        return self._info


def _install_doctor_probe(monkeypatch, info: SlicerInfo) -> None:
    monkeypatch.setattr(
        slicers_module,
        "BambuStudioAdapter",
        lambda: _DoctorBambuAdapter(info),
    )


def test_doctor_reuses_bambu_adapter_probe_without_claiming_support(
    tmp_path: Path, monkeypatch
) -> None:
    executable = (tmp_path / "BambuStudio").resolve()
    _install_doctor_probe(
        monkeypatch,
        SlicerInfo(
            name="BambuStudio",
            version="03.01.02.04",
            executable=executable,
            status=SlicerAvailability.AVAILABLE,
        ),
    )

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    bambu = payload["bambu_studio"]
    assert bambu["status"] == "available"
    assert bambu["version"] == "03.01.02.04"
    assert bambu["executable"] == str(executable.resolve())
    assert bambu["automation_support_status"] == "unverified"
    assert "no official profile" in bambu["claim_boundary"]


def test_doctor_does_not_report_versionless_bambu_probe_as_available(
    tmp_path: Path, monkeypatch
) -> None:
    _install_doctor_probe(
        monkeypatch,
        SlicerInfo(
            name="BambuStudio",
            version=None,
            executable=(tmp_path / "BambuStudio").resolve(),
            status=SlicerAvailability.FAILED,
            detail="official executable did not emit a parseable version",
        ),
    )

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    bambu = json.loads(result.stdout)["bambu_studio"]
    assert bambu["status"] == "failed"
    assert bambu["version"] is None
    assert "parseable version" in bambu["detail"]
