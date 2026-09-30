"""Offline behavior checks for native packaged supervisor acceptance."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
import scripts.verify_macos_app as verifier
from scripts.macos_app import APP_ROOT, MANIFEST_PATH, bundle_entries, load_config


def _config() -> dict[str, Any]:
    return load_config(Path(__file__).parents[2] / "packaging/macos-arm64-runtime.json")


def _payload_fixture(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    app = tmp_path / APP_ROOT
    source = app / "Contents/Frameworks/runtime/module.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    bounds = _config()["bounds"]
    manifest = {"contents": {"files": bundle_entries(app, bounds=bounds)}}
    manifest_path = app / MANIFEST_PATH
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("{}\n", encoding="utf-8")
    return app, manifest, bounds


def test_payload_check_accepts_unchanged_extracted_app(tmp_path: Path) -> None:
    app, manifest, bounds = _payload_fixture(tmp_path)
    verifier._verify_unchanged_app_payload(app, manifest=manifest, bounds=bounds)


@pytest.mark.parametrize("mutation", ["added-bytecode", "modified", "removed", "mode"])
def test_payload_check_rejects_runtime_changes(tmp_path: Path, mutation: str) -> None:
    app, manifest, bounds = _payload_fixture(tmp_path)
    source = app / "Contents/Frameworks/runtime/module.py"
    if mutation == "added-bytecode":
        bytecode = source.parent / "__pycache__/module.cpython-312.pyc"
        bytecode.parent.mkdir()
        bytecode.write_bytes(b"runtime-created cache")
    elif mutation == "modified":
        source.write_text("value = 2\n", encoding="utf-8")
    elif mutation == "removed":
        source.unlink()
    else:
        if os.name == "nt":
            pytest.skip("POSIX executable-mode changes are not available on Windows")
        source.chmod(source.stat().st_mode ^ 0o100)
    with pytest.raises(RuntimeError, match="changed the app payload"):
        verifier._verify_unchanged_app_payload(app, manifest=manifest, bounds=bounds)


def test_supervisor_probe_runs_the_real_contained_command(tmp_path: Path) -> None:
    payload, command = verifier._run_json(
        [sys.executable, "-I", "-B", "-X", "utf8", "-c", verifier.SUPERVISOR_PROBE],
        cwd=tmp_path,
        environment={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert command["exit_code"] == 0
    assert payload["exit_code"] == 0
    assert payload["stdout"] == "TopoForge packaged supervisor child\n"
    assert payload["stderr"] == ""
    assert Path(payload["supervisor_module"]).name == "process_containment.py"
    assert Path(payload["python_executable"]) == Path(sys.executable).resolve()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exit_code", 9),
        ("stdout", ""),
        ("stderr", "supervisor failed"),
        ("python_executable", "/outside-app/python"),
        ("supervisor_module", "/outside-app/process_containment.py"),
    ],
)
def test_supervisor_acceptance_rejects_failure_or_host_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    app = tmp_path / APP_ROOT
    payload = {
        "exit_code": 0,
        "stdout": "TopoForge packaged supervisor child\n",
        "stderr": "",
        "python_executable": str(app / verifier.PYTHON_PATH),
        "supervisor_module": str(app / "Contents/Resources/process_containment.py"),
    }
    payload[field] = value
    monkeypatch.setattr(verifier, "_run_json", lambda *args, **kwargs: (payload, {}))
    with pytest.raises(RuntimeError, match="supervisor did not complete inside the app"):
        verifier._exercise_packaged_supervisor(app, cwd=tmp_path, environment={})


def test_supervisor_acceptance_retains_command_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / APP_ROOT
    payload = {
        "exit_code": 0,
        "stdout": "TopoForge packaged supervisor child\n",
        "stderr": "",
        "python_executable": str(app / verifier.PYTHON_PATH),
        "supervisor_module": str(app / "Contents/Resources/process_containment.py"),
    }
    record = {"exit_code": 0, "stdout": "retained probe evidence"}
    monkeypatch.setattr(verifier, "_run_json", lambda *args, **kwargs: (payload, record))
    assert verifier._exercise_packaged_supervisor(app, cwd=tmp_path, environment={}) == record
