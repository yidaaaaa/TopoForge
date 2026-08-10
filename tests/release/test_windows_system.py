from __future__ import annotations

import platform
from pathlib import Path

import pytest
import scripts.verify_windows_system as system_verifier
from scripts.verify_windows_system import verify_windows_system


def test_native_system_acceptance_exercises_lifecycle_and_paths(tmp_path: Path) -> None:
    report = verify_windows_system(tmp_path / "native system acceptance")

    assert report["schema_version"] == "topoforge-windows-system-verification-v1"
    assert report["required_checks_passed"] is True
    assert report["platform"]["native_windows_verified"] is (platform.system() == "Windows")
    assert report["path_contract"]["contains_spaces"] is True
    assert report["path_contract"]["contains_non_ascii"] is True

    completed = report["completed_job"]
    assert completed["required_checks_passed"] is True
    assert completed["ready_stages"] == completed["expected_stages"]
    assert completed["three_mf"]["strict_warning_count"] == 0
    assert report["restart_recovery"]["artifact_reopened"] is True

    backup = report["backup_restore"]
    assert backup["required_checks_passed"] is True
    assert backup["restored_artifact_sha256"] == completed["artifact_sha256"]
    assert backup["restored_three_mf"]["strict_warning_count"] == 0

    process = report["process_lifecycle"]
    assert process["recovered_state"] == "running"
    assert process["cancelling_state"] == "cancelling"
    assert process["terminal_state"] == "cancelled"
    assert process["process_alive_after_cancel"] is False
    expected_option = "creationflags" if platform.system() == "Windows" else "start_new_session"
    assert expected_option in process["worker_options"]


def test_native_system_acceptance_refuses_a_non_windows_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_verifier.platform, "system", lambda: "Linux")
    work_root = tmp_path / "must not be created"

    with pytest.raises(RuntimeError, match="native Windows"):
        verify_windows_system(work_root, require_windows=True)

    assert not work_root.exists()


def test_release_forms_execute_the_native_system_acceptance() -> None:
    root = Path(__file__).parents[2]
    portable = (root / "scripts/verify_windows_portable.py").read_text(encoding="utf-8")
    release = (root / "scripts/verify_release.py").read_text(encoding="utf-8")

    for source in (portable, release):
        assert "verify_windows_system.py" in source
        assert '"-I"' in source
    assert '"--require-windows"' in portable
    assert "installed Web system acceptance" in release
