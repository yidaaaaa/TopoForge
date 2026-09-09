"""Real subprocess regressions and explicit Windows containment contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from topoforge import process_containment
from topoforge.validation.slicers.base import run_command


@pytest.mark.parametrize("leader_exits", [False, True])
def test_command_completion_stops_descendant_writes_and_preserves_output(
    tmp_path: Path, leader_exits: bool
) -> None:
    ready = tmp_path / "child-ready"
    marker = tmp_path / "late-output"
    child_script = (
        "import pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text('ready'); "
        "time.sleep(3); pathlib.Path(sys.argv[2]).write_text('unexpected output')"
    )
    parent_script = "\n".join(
        (
            "import os, pathlib, subprocess, sys, time",
            "child = subprocess.Popen([sys.executable, '-c', sys.argv[1], *sys.argv[2:]])",
            "deadline = time.monotonic() + 10",
            "while not pathlib.Path(sys.argv[2]).exists() and time.monotonic() < deadline:",
            "    time.sleep(0.01)",
            "os.write(1, 'child-started 路径'.encode('utf-8'))",
            "os.write(2, b'\\xffwarning')",
            "" if leader_exits else "child.wait()",
        )
    )
    result = run_command(
        [sys.executable, "-c", parent_script, child_script, str(ready), str(marker)],
        timeout_seconds=2.0,
    )

    assert ready.exists(), "Fixture child must start before exercising the timeout"
    timed_out = not (leader_exits and os.name == "nt")
    # On Windows, the supervisor's normal exit also closes the Job Object.
    assert result.returncode == (124 if timed_out else 0)
    assert result.stdout == "child-started 路径"
    suffix = "\nTimed out after 2 seconds." if timed_out else ""
    assert result.stderr == "�warning" + suffix
    assert result.duration_seconds < 9.0
    assert not marker.exists()
    time.sleep(3.2)
    assert not marker.exists(), "A descendant continued writing after timeout returned"


def test_command_preserves_environment_cwd_and_nonzero_status(tmp_path: Path) -> None:
    environment = {**os.environ, "TOPOFORGE_COMMAND_FIXTURE": "terrain"}
    result = run_command(
        [
            sys.executable,
            "-c",
            "import os,sys; print(os.environ['TOPOFORGE_COMMAND_FIXTURE']); "
            "print(os.getcwd(), file=sys.stderr); sys.exit(7)",
        ],
        cwd=tmp_path,
        env=environment,
        timeout_seconds=10.0,
    )
    assert result.returncode == 7
    assert result.stdout == "terrain\n"
    assert Path(result.stderr.strip()) == tmp_path


def test_missing_command_retains_normalized_failure(tmp_path: Path) -> None:
    result = run_command([str(tmp_path / "missing-command")], timeout_seconds=10.0)
    assert result.returncode == 127
    assert result.stdout == ""
    assert result.stderr


def test_windows_launch_bypasses_redirector_and_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = str(tmp_path / "base python.exe")
    monkeypatch.setattr(sys, "_base_executable", base)
    command = ["slicer.exe", "model with spaces.stl"]
    launch, options = process_containment._command_launch(command, windows=True)
    assert launch == [base, "-I", "-S", str(Path(process_containment.__file__).resolve()), *command]
    assert options == {"creationflags": 0x00000200}
    launch, options = process_containment._command_launch(command, windows=False, parent_pipe_fd=42)
    assert launch == [
        sys.executable,
        "-I",
        "-S",
        str(Path(process_containment.__file__).resolve()),
        "42",
        *command,
    ]
    assert options == {"start_new_session": True, "pass_fds": (42,)}


def test_windows_supervisor_contains_before_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    def contain() -> None:
        events.append("contained")

    def call(command: list[str]) -> int:
        assert command == ["slicer.exe", "--help"]
        events.append("started")
        return 7

    monkeypatch.setattr(process_containment, "enable_windows_process_containment", contain)
    monkeypatch.setattr(subprocess, "call", call)
    assert process_containment._supervise_command(["slicer.exe", "--help"]) == 7
    assert events == ["contained", "started"]


def test_windows_supervisor_refuses_uncontained_launch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def denied() -> None:
        raise OSError("Job assignment denied; close the restrictive parent application")

    def forbidden(command: list[str]) -> int:
        pytest.fail(f"Command must never start without containment: {command}")

    monkeypatch.setattr(process_containment, "enable_windows_process_containment", denied)
    monkeypatch.setattr(subprocess, "call", forbidden)
    assert process_containment._supervise_command(["slicer.exe"]) == 127
    assert "Job assignment denied" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="POSIX worker signals and private command groups")
@pytest.mark.parametrize("force", [False, True])
def test_worker_exit_stops_its_isolated_slicer_group(tmp_path: Path, force: bool) -> None:
    ready = tmp_path / "nested-child-ready"
    marker = tmp_path / "nested-late-output"
    child = (
        "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); "
        "time.sleep(3); pathlib.Path(sys.argv[2]).write_text('unexpected output')"
    )
    slicer = "import subprocess,sys; subprocess.run([sys.executable, '-c', *sys.argv[1:]])"
    worker = (
        "import sys; from topoforge.validation.slicers.base import run_command; "
        "run_command([sys.executable, '-c', *sys.argv[1:]], timeout_seconds=20)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", worker, slicer, child, str(ready), str(marker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            assert process.poll() is None, "The test worker exited before starting its slicer"
            time.sleep(0.02)
        assert ready.exists()
        if force:
            process.kill()
        else:
            process.terminate()
        process.wait(timeout=5)
        time.sleep(3.2)
        assert not marker.exists(), "Worker cancellation leaked a private slicer group"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX child reaping contract")
def test_command_can_wait_for_all_of_its_own_children() -> None:
    script = "\n".join(
        (
            "import os, subprocess, sys",
            "child = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(7)'])",
            "pid, status = os.waitpid(-1, 0)",
            "assert pid == child.pid and os.waitstatus_to_exitcode(status) == 7",
            "try:",
            "    os.waitpid(-1, 0)",
            "except ChildProcessError:",
            "    print('all owned children reaped')",
            "else:",
            "    raise AssertionError('Unexpected child process was injected')",
        )
    )
    result = run_command([sys.executable, "-c", script], timeout_seconds=2.0)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "all owned children reaped\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX negative signal exit statuses")
def test_command_preserves_target_signal_exit_status() -> None:
    result = run_command(
        [sys.executable, "-c", "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"],
        timeout_seconds=2.0,
    )
    assert result.returncode == -15
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.skipif(os.name == "nt", reason="POSIX inherited file descriptors")
@pytest.mark.parametrize("descriptors", [(1, 2), (0, 1, 2)])
def test_command_captures_output_when_callers_standard_streams_are_closed(
    tmp_path: Path, descriptors: tuple[int, ...]
) -> None:
    report = tmp_path / "closed-streams.json"
    script = "\n".join(
        (
            "import json, os, pathlib, runpy, sys",
            "run = runpy.run_path(sys.argv[2])['run_contained_command']",
            f"for fd in {descriptors!r}: os.close(fd)",
            "command = [sys.executable, '-c', \"print('captured output')\"]",
            "result = run(command, timeout_seconds=2)",
            "report = {'code': result.returncode,",
            "          'stdout': result.stdout, 'stderr': result.stderr}",
            "pathlib.Path(sys.argv[1]).write_text(json.dumps(report))",
            "os._exit(0)",
        )
    )
    subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(report), process_containment.__file__],
        check=True,
        timeout=10,
    )
    assert json.loads(report.read_text()) == {
        "code": 0,
        "stdout": "captured output\n",
        "stderr": "",
    }
