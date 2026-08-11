"""CLI doctor external-tool identity regressions."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from topoforge.cli.app import app

runner = CliRunner()


def test_doctor_reuses_bambu_adapter_probe_without_claiming_support(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "BambuStudio"
    executable.write_text("#!/bin/sh\nprintf BambuStudio-03.01.02.04:\n\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("TOPOFORGE_BAMBU_STUDIO", str(executable))

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    bambu = payload["bambu_studio"]
    assert bambu["status"] == "available"
    assert bambu["version"] == "03.01.02.04"
    assert bambu["executable"] == str(executable.resolve())
    assert bambu["automation_support_status"] == "unverified"
    assert "no official profile" in bambu["claim_boundary"]
