from __future__ import annotations

import http.client
import io
import json
import platform
import socket
import urllib.parse
from pathlib import Path

import pytest
import scripts.verify_windows_system as system_verifier
import scripts.windows_acceptance as windows_acceptance
from scripts.verify_windows_system import verify_windows_system

from topoforge.web.models import WebAppConfig


def _native_x64_architecture() -> dict[str, object]:
    return {
        "process_machine_code": 0x0000,
        "process_machine": "UNKNOWN",
        "native_machine_code": 0x8664,
        "native_machine": "AMD64",
        "native_x64_verified": True,
    }


class _FakeServerProcess:
    pid = 424_242

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float) -> int:
        assert timeout == 15.0
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_server_shutdown_uses_the_recorded_process_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeServerProcess()
    calls: list[tuple[int, str | None, int | None]] = []

    def terminate(
        pid: int,
        *,
        expected_identity: str | None = None,
        process_group: int | None = None,
        **_kwargs: object,
    ) -> None:
        calls.append((pid, expected_identity, process_group))

    monkeypatch.setattr(system_verifier, "terminate_process_tree", terminate)

    result = system_verifier._stop_server(
        process,  # type: ignore[arg-type]
        expected_identity="windows:123456",
        process_group=process.pid,
    )

    assert calls == [(process.pid, "windows:123456", process.pid)]
    assert process.killed is False
    assert result == {"method": "identity-bound-process-tree", "exit_code": 0}


def test_server_shutdown_without_identity_uses_only_the_held_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeServerProcess()
    monkeypatch.setattr(
        system_verifier,
        "terminate_process_tree",
        lambda *_args, **_kwargs: pytest.fail("PID-only tree termination is unsafe"),
    )

    result = system_verifier._stop_server(
        process,  # type: ignore[arg-type]
        expected_identity=None,
        process_group=None,
    )

    assert process.killed is True
    assert result == {"method": "exact-child-fallback", "exit_code": -9}


def test_windows_server_command_wraps_candidate_launcher_in_kill_on_close_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    launcher = tmp_path / "TopoForge-Web.cmd"
    launcher.write_text("@echo off\r\n", encoding="utf-8")
    config = WebAppConfig(
        state_dir=state,
        workspace_root=workspace,
        input_roots=(inputs,),
    )
    monkeypatch.setattr(system_verifier.platform, "system", lambda: "Windows")
    redirector = r"C:\hostedtoolcache\venv\Scripts\python.exe"
    base_interpreter = r"C:\hostedtoolcache\Python\3.12\python.exe"
    monkeypatch.setattr(
        system_verifier,
        "worker_interpreter_launch",
        lambda: (base_interpreter, {"__PYVENV_LAUNCHER__": redirector}),
    )

    command, environment, options, record = system_verifier._server_command(
        web_launcher=launcher,
        config=config,
        port=8123,
        hosted_server=False,
    )

    assert command[:6] == [
        base_interpreter,
        "-I",
        "-X",
        "utf8",
        "-c",
        system_verifier._WINDOWS_SERVER_CONTAINMENT_WRAPPER_CODE,
    ]
    assert command[6:10] == [
        system_verifier.os.environ.get("COMSPEC", "cmd.exe"),
        "/d",
        "/s",
        "/c",
    ]
    assert str(launcher.resolve()) in command[10]
    assert "--port 8123" in command[10]
    assert options == {
        "creationflags": getattr(system_verifier.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    }
    assert environment["__PYVENV_LAUNCHER__"] == redirector
    assert record["kind"] == "candidate-batch-launcher"
    assert record["containment"] == "kill-on-close-job-wrapper"
    assert record["contained_process_tree"] is True


def test_native_system_acceptance_exercises_lifecycle_and_paths(tmp_path: Path) -> None:
    hosted_server = platform.system() == "Windows"
    report = verify_windows_system(
        tmp_path / "native system acceptance",
        hosted_server=hosted_server,
    )

    assert report["schema_version"] == "topoforge-windows-system-verification-v2"
    assert report["required_checks_passed"] is True
    assert report["hosted_server"] is hosted_server
    assert report["platform"]["native_windows_verified"] is (platform.system() == "Windows")
    assert report["path_contract"]["contains_spaces"] is True
    assert report["path_contract"]["contains_non_ascii"] is True

    http = report["real_http_web"]
    assert http["health"]["status"] == "ok"
    assert http["root"]["packaged_application_served"] is True
    assert http["job"]["state"] == "completed"
    assert http["download"]["sha256"] == http["job"]["model_3mf_sha256"]
    assert http["download"]["three_mf"]["strict_warning_count"] == 0
    assert http["browser"]["mode"] == "skip"
    assert http["browser"]["attempted"] is False
    assert http["browser"]["dispatch"] == {
        "attempted": False,
        "accepted": None,
        "required_checks_passed": True,
    }
    assert http["browser"]["confirmed_load"]["required"] is False
    assert http["browser"]["confirmed_load"]["confirmed"] is None
    assert http["shutdown"]["port_closed"] is True
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", http["shutdown"]["port"]), timeout=0.2)

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

    containment = report["windows_process_containment"]
    assert containment["required_checks_passed"] is True
    assert containment["containment_entrypoint"] == (
        "topoforge.web.processes.enable_current_process_containment"
    )
    assert len(containment["probe_code_sha256"]) == 64
    if platform.system() == "Windows":
        assert containment["executed"] is True
        assert containment["job_object_kill_on_close_verified"] is True
        assert containment["production_cancellation_verified"] is True
        assert containment["leader_exit"]["leader_exit_code"] == 0
        assert containment["leader_exit"]["child_alive_after_exit"] is False
        assert containment["cancellation"]["child_alive_after_cancel"] is False
    else:
        assert containment["executed"] is False
        assert containment["job_object_kill_on_close_verified"] is False
        assert containment["production_cancellation_verified"] is False
        assert "not native Windows Job Object evidence" in containment["claim_boundary"]


def test_native_system_acceptance_refuses_a_non_windows_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_verifier.platform, "system", lambda: "Linux")
    work_root = tmp_path / "must not be created"

    with pytest.raises(RuntimeError, match="native Windows"):
        verify_windows_system(
            work_root,
            expected_target="win10-22h2",
            browser_mode="require",
        )

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


@pytest.mark.parametrize(
    ("expected_target", "registry", "target_id"),
    [
        (
            "win10-22h2",
            {
                "ProductName": "Windows 10 Pro",
                "DisplayVersion": "22H2",
                "CurrentBuildNumber": "19045",
                "UBR": 6216,
                "InstallationType": "Client",
            },
            "windows-10-22h2-x64",
        ),
        (
            "win11",
            {
                "ProductName": "Windows 10 Pro",
                "DisplayVersion": "24H2",
                "CurrentBuildNumber": "26100",
                "UBR": 4946,
                "InstallationType": "Client",
            },
            "windows-11-x64",
        ),
    ],
)
def test_windows_target_requires_exact_client_identity(
    expected_target: str,
    registry: dict[str, object],
    target_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_acceptance.platform, "system", lambda: "Windows")
    monkeypatch.setattr(windows_acceptance.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        windows_acceptance,
        "_windows_architecture_record",
        _native_x64_architecture,
    )
    monkeypatch.setattr(windows_acceptance, "_registry_values", lambda: registry)

    report = windows_acceptance.windows_target_record(
        expected_target,
        require_windows=True,
    )

    assert report["target_id"] == target_id
    assert report["target_verified"] is True
    assert report["installation_type"] == "Client"
    assert report["native_machine"] == "AMD64"
    assert report["native_x64_verified"] is True


@pytest.mark.parametrize(
    ("expected_target", "registry"),
    [
        (
            "win10-22h2",
            {
                "ProductName": "Windows 10 Pro",
                "DisplayVersion": "22H2",
                "CurrentBuildNumber": "19044",
                "UBR": 1,
                "InstallationType": "Client",
            },
        ),
        (
            "win10-22h2",
            {
                "ProductName": "Windows Server 2022 Datacenter",
                "DisplayVersion": "21H2",
                "CurrentBuildNumber": "20348",
                "UBR": 1,
                "InstallationType": "Server",
            },
        ),
        (
            "win11",
            {
                "ProductName": "Windows Server 2025 Datacenter",
                "DisplayVersion": "24H2",
                "CurrentBuildNumber": "26100",
                "UBR": 1,
                "InstallationType": "Client",
            },
        ),
        (
            "win11",
            {
                "ProductName": "Windows 11 Pro",
                "DisplayVersion": "22H2",
                "CurrentBuildNumber": "19045",
                "UBR": 1,
                "InstallationType": "Client",
            },
        ),
        (
            "win11",
            {
                "ProductName": "Windows 12 Pro",
                "DisplayVersion": "26H2",
                "CurrentBuildNumber": "30000",
                "UBR": 1,
                "InstallationType": "Client",
            },
        ),
    ],
)
def test_windows_target_rejects_wrong_build_and_server(
    expected_target: str,
    registry: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_acceptance.platform, "system", lambda: "Windows")
    monkeypatch.setattr(windows_acceptance.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        windows_acceptance,
        "_windows_architecture_record",
        _native_x64_architecture,
    )
    monkeypatch.setattr(windows_acceptance, "_registry_values", lambda: registry)

    with pytest.raises(RuntimeError, match=r"does not match|InstallationType=Client"):
        windows_acceptance.windows_target_record(expected_target, require_windows=True)


def test_windows_target_rejects_arm64_emulation_as_x64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_acceptance.platform, "system", lambda: "Windows")
    monkeypatch.setattr(windows_acceptance.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        windows_acceptance,
        "_windows_architecture_record",
        lambda: {
            "process_machine_code": 0x0000,
            "process_machine": "UNKNOWN",
            "native_machine_code": 0xAA64,
            "native_machine": "ARM64",
            "native_x64_verified": False,
        },
    )

    with pytest.raises(RuntimeError, match="native Windows x64"):
        windows_acceptance.windows_target_record("win11", require_windows=True)


class _FakeHttpResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = io.BytesIO(payload)
        self.headers: dict[str, str] = {}

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._payload.read(size)


def test_http_artifact_download_rejects_job_record_sha_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        system_verifier.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _FakeHttpResponse(b"tampered model"),
    )

    with pytest.raises(RuntimeError, match="differs from the JobRecord"):
        system_verifier._download_artifact(
            "http://127.0.0.1:8765/model.3mf",
            tmp_path / "downloaded.3mf",
            "0" * 64,
        )


def test_clean_vm_browser_mode_rejects_failed_default_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_verifier.webbrowser, "open", lambda *_args, **_kwargs: False)

    with pytest.raises(RuntimeError, match="default browser launch returned false"):
        system_verifier._launch_default_browser("http://127.0.0.1:8765/")


def _request_browser_callback(url: str) -> int:
    parsed = urllib.parse.urlsplit(url)
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port is not None
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2.0)
    try:
        connection.request(
            "GET",
            urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, "")),
            headers={"User-Agent": "TopoForge-test-browser"},
        )
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def test_clean_vm_browser_mode_requires_nonce_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_statuses: list[int] = []

    def open_browser(url: str, **_kwargs: object) -> bool:
        callback_statuses.append(_request_browser_callback(url))
        callback_statuses.append(_request_browser_callback(url))
        return True

    monkeypatch.setattr(system_verifier.webbrowser, "open", open_browser)

    report = system_verifier._confirm_default_browser_load(
        "http://127.0.0.1:8765/",
        timeout_seconds=2.0,
    )

    assert callback_statuses == [302, 410]
    assert report["dispatch"] == {
        "attempted": True,
        "accepted": True,
        "required_checks_passed": True,
    }
    confirmed = report["confirmed_load"]
    assert confirmed["required"] is True
    assert confirmed["confirmed"] is True
    assert confirmed["one_time_nonce"] is True
    assert len(confirmed["nonce_sha256"]) == 64
    assert confirmed["request_method"] == "GET"
    assert confirmed["request_path"] == "/__topoforge_browser_loaded__"
    assert confirmed["remote_address"] == "127.0.0.1"
    assert confirmed["redirect_target"] == "http://127.0.0.1:8765/"
    assert report["required_checks_passed"] is True


def test_clean_vm_browser_mode_rejects_dispatch_without_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_verifier.webbrowser, "open", lambda *_args, **_kwargs: True)

    with pytest.raises(TimeoutError, match="no valid nonce callback"):
        system_verifier._confirm_default_browser_load(
            "http://127.0.0.1:8765/",
            timeout_seconds=0.05,
        )


def test_clean_vm_browser_mode_rejects_wrong_nonce_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def open_browser(url: str, **_kwargs: object) -> bool:
        parsed = urllib.parse.urlsplit(url)
        wrong_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, "nonce=wrong", "")
        )
        assert _request_browser_callback(wrong_url) == 404
        return True

    monkeypatch.setattr(system_verifier.webbrowser, "open", open_browser)

    with pytest.raises(TimeoutError, match="no valid nonce callback"):
        system_verifier._confirm_default_browser_load(
            "http://127.0.0.1:8765/",
            timeout_seconds=0.05,
        )


def test_real_server_health_rejects_early_process_exit() -> None:
    class ExitedProcess:
        returncode = 7

        @staticmethod
        def poll() -> int:
            return 7

    server_log = io.BytesIO(b"INFO startup\nERROR lifespan failed: fixture detail\n")
    with pytest.raises(RuntimeError, match="lifespan failed: fixture detail"):
        system_verifier._wait_for_server(
            "http://127.0.0.1:8765",
            ExitedProcess(),  # type: ignore[arg-type]
            server_log=server_log,
        )


def test_real_server_health_bounds_early_exit_log_tail() -> None:
    class ExitedProcess:
        returncode = 3

        @staticmethod
        def poll() -> int:
            return 3

    marker = b"bounded-tail-marker"
    server_log = io.BytesIO(b"x" * system_verifier._SERVER_LOG_TAIL_BYTES + marker)
    with pytest.raises(RuntimeError) as captured:
        system_verifier._wait_for_server(
            "http://127.0.0.1:8765",
            ExitedProcess(),  # type: ignore[arg-type]
            server_log=server_log,
        )

    message = str(captured.value)
    assert marker.decode("ascii") in message
    assert len(message.encode("utf-8")) < system_verifier._SERVER_LOG_TAIL_BYTES + 256


def test_real_server_http_failure_includes_bounded_log_tail() -> None:
    marker = b"bounded-http-failure-marker"
    server_log = io.BytesIO(b"x" * system_verifier._SERVER_LOG_TAIL_BYTES + marker)

    failure = system_verifier._server_failure_with_log(
        RuntimeError("HTTP 500 from loopback server"),
        server_log,
    )

    message = str(failure)
    assert "HTTP 500 from loopback server" in message
    assert marker.decode("ascii") in message
    assert len(message.encode("utf-8")) < system_verifier._SERVER_LOG_TAIL_BYTES + 256


def test_clean_target_refuses_skipped_browser_before_creating_root(tmp_path: Path) -> None:
    work_root = tmp_path / "must not be created"

    with pytest.raises(RuntimeError, match="browser-mode require"):
        verify_windows_system(
            work_root,
            expected_target="win10-22h2",
            browser_mode="skip",
        )

    assert not work_root.exists()


def test_clean_target_requires_portable_candidate_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        system_verifier,
        "windows_target_record",
        lambda *_args, **_kwargs: {
            "target_id": "windows-10-22h2-x64",
            "target_verified": True,
        },
    )
    work_root = tmp_path / "must not be created without binding"

    with pytest.raises(RuntimeError, match="--candidate-binding"):
        verify_windows_system(
            work_root,
            expected_target="win10-22h2",
            browser_mode="require",
        )

    assert not work_root.exists()


def test_windows_containment_probe_uses_production_job_object_before_child() -> None:
    source = system_verifier._WINDOWS_CONTAINMENT_PROBE_CODE

    compile(source, "<windows-containment-probe>", "exec")
    assert "enable_current_process_containment" in source
    assert source.index("enable_current_process_containment()") < source.index(
        "child = subprocess.Popen"
    )
    assert '"leader-exit"' in source
    assert '"cancel"' in source
    assert "tempfile.NamedTemporaryFile" in source
    assert "os.fsync(stream.fileno())" in source
    assert 'with_name(f".{output_path.name}.tmp")' not in source


def test_windows_containment_report_binds_candidate_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_verifier.platform, "system", lambda: "Linux")
    verifier_sha256 = windows_acceptance.sha256_file(Path(system_verifier.__file__).resolve())
    binding = {
        "binding_sha256": "a" * 64,
        "source_commit": "b" * 40,
        "verifier_sha256": verifier_sha256,
    }

    report = system_verifier._windows_process_containment_acceptance(
        tmp_path / "not-created-on-linux",
        binding,
    )

    assert report["executed"] is False
    assert report["source_binding"] == {
        "candidate_bound": True,
        "candidate_binding_sha256": "a" * 64,
        "source_commit": "b" * 40,
        "system_verifier_sha256": verifier_sha256,
        "system_verifier_matches_candidate": True,
        "required_checks_passed": True,
    }
    assert not (tmp_path / "not-created-on-linux").exists()


def test_windows_containment_report_rejects_wrong_bound_verifier(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="differs from candidate binding"):
        system_verifier._windows_process_containment_acceptance(
            tmp_path / "must-not-be-created",
            {
                "binding_sha256": "a" * 64,
                "source_commit": "b" * 40,
                "verifier_sha256": "c" * 64,
            },
        )

    assert not (tmp_path / "must-not-be-created").exists()


def test_windows_evidence_json_uses_random_atomic_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "evidence.json"
    replacements: list[tuple[Path, Path]] = []
    real_replace = windows_acceptance.os.replace

    def record_replace(source: str | Path, target: str | Path) -> None:
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(windows_acceptance.os, "replace", record_replace)
    windows_acceptance.write_canonical_json(destination, {"z": "地形", "a": 1})

    assert destination.read_text(encoding="utf-8") == ('{\n  "a": 1,\n  "z": "地形"\n}\n')
    assert len(replacements) == 1
    temporary, replaced_destination = replacements[0]
    assert replaced_destination == destination
    assert temporary.parent == destination.parent
    assert temporary.name.startswith(".evidence.json.")
    assert temporary.name.endswith(".tmp")
    assert temporary.name != ".evidence.json.tmp"
    assert not temporary.exists()
    assert json.loads(destination.read_text(encoding="utf-8")) == {"a": 1, "z": "地形"}


def test_windows_evidence_json_replace_failure_preserves_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "evidence.json"
    destination.write_text("previous evidence\n", encoding="utf-8")

    def fail_replace(_source: str | Path, _target: str | Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(windows_acceptance.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        windows_acceptance.write_canonical_json(destination, {"replacement": True})

    assert destination.read_text(encoding="utf-8") == "previous evidence\n"
    assert [
        path.name
        for path in tmp_path.iterdir()
        if path.name.startswith(".evidence.json.") and path.name.endswith(".tmp")
    ] == []


def test_windows_evidence_json_reconciles_replace_error_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "evidence.json"
    real_replace = windows_acceptance.os.replace

    def replace_then_fail(source: str | Path, target: str | Path) -> None:
        real_replace(source, target)
        raise OSError("injected post-commit replace error")

    monkeypatch.setattr(windows_acceptance.os, "replace", replace_then_fail)
    windows_acceptance.write_canonical_json(destination, {"reconciled": True})

    assert destination.read_text(encoding="utf-8") == '{\n  "reconciled": true\n}\n'
    assert list(tmp_path.glob(".evidence.json.*.tmp")) == []


@pytest.mark.skipif(
    windows_acceptance.os.name == "nt",
    reason="Windows does not use POSIX directory fsync",
)
def test_windows_evidence_json_reports_committed_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "evidence.json"
    real_fsync = windows_acceptance.os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        information = windows_acceptance.os.fstat(descriptor)
        if windows_acceptance.stat.S_ISDIR(information.st_mode):
            raise OSError("injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(windows_acceptance.os, "fsync", fail_directory_fsync)
    with pytest.raises(
        windows_acceptance.EvidencePublicationError,
        match="committed, but directory durability is uncertain",
    ) as captured:
        windows_acceptance.write_canonical_json(destination, {"replacement": True})

    assert captured.value.committed is True
    assert captured.value.destination == destination
    assert destination.read_text(encoding="utf-8") == '{\n  "replacement": true\n}\n'
    assert not captured.value.temporary.exists()
