from __future__ import annotations

import json
from pathlib import Path

import pytest
import scripts.verify_windows_bambu as bambu_verifier
from scripts.verify_windows_bambu import _bambu_override, verify_windows_bambu


def test_native_bambu_acceptance_refuses_non_windows_before_creating_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bambu_verifier.platform, "system", lambda: "Linux")
    work_root = tmp_path / "must not be created"

    with pytest.raises(RuntimeError, match="native Windows"):
        verify_windows_bambu(work_root, require_windows=True)

    assert not work_root.exists()


def test_bambu_executable_override_restores_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "TOPOFORGE_BAMBU_STUDIO"
    monkeypatch.setenv(key, "original.exe")
    executable = tmp_path / "Bambu Studio" / "bambu-studio.exe"

    with _bambu_override(executable):
        assert bambu_verifier.os.environ[key] == str(executable)

    assert bambu_verifier.os.environ[key] == "original.exe"


def test_bambu_acceptance_main_retains_failure_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "failure report.json"

    def fail(*_: object, **__: object) -> dict[str, object]:
        raise RuntimeError("synthetic official Bambu failure")

    monkeypatch.setattr(bambu_verifier, "verify_windows_bambu", fail)
    monkeypatch.setattr(
        bambu_verifier.sys,
        "argv",
        [
            "verify_windows_bambu.py",
            "--work-root",
            str(tmp_path / "work root"),
            "--report",
            str(report_path),
        ],
    )

    assert bambu_verifier.main() == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "topoforge-windows-bambu-verification-v1"
    assert report["error"]["type"] == "RuntimeError"
    assert report["error"]["message"] == "synthetic official Bambu failure"
    assert report["required_checks_passed"] is False


def test_bambu_acceptance_contract_reuses_normative_workflow_gates() -> None:
    source = (Path(__file__).parents[2] / "scripts" / "verify_windows_bambu.py").read_text(
        encoding="utf-8"
    )

    assert "project_evidence_enabled=True" in source
    assert "verify_bambu_project_evidence" in source
    assert '"external_profiles_loaded_on_reopen"' in source
    assert '"--require-windows"' in source
    assert "official Bambu Studio software slice/export/reopen/reslice evidence" in source
