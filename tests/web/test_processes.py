from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest

from topoforge.web import processes
from topoforge.web.processes import (
    ProcessPlatform,
    process_is_alive,
    terminate_process_tree,
    worker_process_options,
)


def _alive_sequence(values: tuple[bool, ...]) -> tuple[Iterator[bool], Any]:
    sequence = iter(values)

    def probe(
        pid: int,
        *,
        platform_family: ProcessPlatform | None = None,
    ) -> bool:
        assert pid == 43210
        assert platform_family is not None
        return next(sequence)

    return sequence, probe


def test_worker_process_options_are_platform_explicit() -> None:
    assert worker_process_options(ProcessPlatform.POSIX) == {"start_new_session": True}
    assert worker_process_options(ProcessPlatform.WINDOWS) == {"creationflags": 0x00000200}


def test_process_liveness_rejects_nonpositive_pid() -> None:
    assert process_is_alive(0, platform_family=ProcessPlatform.POSIX) is False
    with pytest.raises(ValueError, match="positive"):
        terminate_process_tree(0, platform_family=ProcessPlatform.POSIX)


@pytest.mark.parametrize(
    ("returncode", "state", "expected"),
    [
        (0, "S+\n", True),
        (0, "Z+\n", False),
        (1, "", False),
    ],
)
def test_darwin_posix_liveness_uses_ps_state(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    state: str,
    expected: bool,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode, stdout=state, stderr="")

    monkeypatch.setattr(processes.sys, "platform", "darwin")
    monkeypatch.setattr(processes.subprocess, "run", fake_run)

    assert process_is_alive(43210, platform_family=ProcessPlatform.POSIX) is expected
    assert calls[0][0] == ["/bin/ps", "-o", "stat=", "-p", "43210"]
    assert calls[0][1] == {
        "check": False,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }


def test_non_darwin_posix_liveness_skips_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(processes.sys, "platform", "linux")
    monkeypatch.setattr(
        processes.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("non-Darwin POSIX must not invoke ps"),
    )

    assert process_is_alive(os.getpid(), platform_family=ProcessPlatform.POSIX) is True


def test_posix_termination_escalates_one_isolated_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, probe = _alive_sequence((True, True, False))
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(processes, "process_is_alive", probe)
    monkeypatch.setattr(
        processes.os,
        "killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    terminate_process_tree(
        43210,
        graceful_timeout_seconds=0,
        force_timeout_seconds=0,
        platform_family=ProcessPlatform.POSIX,
    )

    assert signals == [(43210, signal.SIGTERM), (43210, signal.SIGKILL)]


def test_windows_termination_uses_new_group_then_taskkill_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, probe = _alive_sequence((True, True, False))
    sent_signals: list[tuple[int, int]] = []
    taskkill_calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        taskkill_calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="SUCCESS", stderr="")

    monkeypatch.setattr(processes, "process_is_alive", probe)
    monkeypatch.setattr(
        processes.os,
        "kill",
        lambda pid, sent_signal: sent_signals.append((pid, sent_signal)),
    )
    monkeypatch.setattr(processes.subprocess, "run", fake_run)

    terminate_process_tree(
        43210,
        graceful_timeout_seconds=0,
        force_timeout_seconds=0,
        platform_family=ProcessPlatform.WINDOWS,
    )

    assert sent_signals == [(43210, getattr(signal, "CTRL_BREAK_EVENT", 1))]
    assert taskkill_calls[0][0] == ["taskkill", "/PID", "43210", "/T", "/F"]
    assert taskkill_calls[0][1]["check"] is False
    assert "shell" not in taskkill_calls[0][1]


def test_windows_liveness_uses_win32_process_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[int] = []
    monkeypatch.setattr(
        processes,
        "_windows_process_is_alive",
        lambda pid: observed.append(pid) is None,
    )

    assert process_is_alive(43210, platform_family=ProcessPlatform.WINDOWS) is True
    assert observed == [43210]
