from __future__ import annotations

import ctypes
import errno
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any

import pytest

from topoforge.web import processes
from topoforge.web.processes import (
    ProcessIdentityMismatchError,
    ProcessInspectionUnavailableError,
    ProcessPlatform,
    process_containment_is_alive,
    process_group_id,
    process_identity,
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


def test_posix_termination_escalates_one_isolated_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, probe = _alive_sequence((True, True))
    signals: list[tuple[int, int]] = []
    sigterm = int(signal.SIGTERM)
    sigkill = int(getattr(signal, "SIGKILL", 9))
    group_states = iter((True, False))
    monkeypatch.setattr(processes, "process_is_alive", probe)
    monkeypatch.setattr(
        processes,
        "process_matches_identity",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        processes,
        "process_group_id",
        lambda _pid, **_kwargs: 43210,
    )
    monkeypatch.setattr(
        processes,
        "_posix_process_group_is_alive",
        lambda _process_group: next(group_states),
    )
    monkeypatch.setattr(processes.signal, "SIGKILL", sigkill, raising=False)
    monkeypatch.setattr(
        processes.os,
        "killpg",
        lambda pid, sent_signal: signals.append((pid, int(sent_signal))),
        raising=False,
    )

    terminate_process_tree(
        43210,
        expected_identity="linux:fixture:123",
        graceful_timeout_seconds=0,
        force_timeout_seconds=0,
        platform_family=ProcessPlatform.POSIX,
    )

    assert signals == [(43210, sigterm), (43210, sigkill)]


class _FakeWindowsProcessBackend:
    def __init__(
        self,
        *,
        identity: str = "windows:123456",
        waits: tuple[bool, ...] = (False, True),
        terminate_error: OSError | None = None,
    ) -> None:
        self.handle = 0xCAFE
        self.identity = identity
        self.waits = iter(waits)
        self.terminate_error = terminate_error
        self.calls: list[tuple[object, ...]] = []

    def open_process(self, pid: int, access: int) -> int | None:
        self.calls.append(("open", pid, access))
        return self.handle

    def creation_identity(self, handle: int, pid: int) -> str:
        self.calls.append(("identity", handle, pid))
        return self.identity

    def wait(self, handle: int, timeout_seconds: float) -> bool:
        self.calls.append(("wait", handle, timeout_seconds))
        return next(self.waits)

    def terminate(self, handle: int, pid: int) -> None:
        self.calls.append(("terminate", handle, pid))
        if self.terminate_error is not None:
            raise self.terminate_error

    def close(self, handle: int) -> None:
        self.calls.append(("close", handle))


def test_windows_termination_keeps_one_exact_handle_through_forced_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeWindowsProcessBackend()
    monkeypatch.setattr(processes, "_windows_process_api", lambda: backend)
    monkeypatch.setattr(
        processes.os,
        "kill",
        lambda *_args: pytest.fail("Windows termination must not signal a reused numeric PID"),
    )

    terminate_process_tree(
        43210,
        expected_identity="windows:123456",
        process_group=43210,
        graceful_timeout_seconds=0.25,
        force_timeout_seconds=0.5,
        platform_family=ProcessPlatform.WINDOWS,
    )

    assert backend.calls == [
        (
            "open",
            43210,
            processes._WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION
            | processes._WINDOWS_PROCESS_TERMINATE
            | processes._WINDOWS_SYNCHRONIZE,
        ),
        ("wait", backend.handle, 0.0),
        ("identity", backend.handle, 43210),
        ("terminate", backend.handle, 43210),
        ("wait", backend.handle, 0.5),
        ("close", backend.handle),
    ]


def test_windows_termination_rejects_pid_reuse_on_the_open_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeWindowsProcessBackend(identity="windows:replacement", waits=(False,))
    sent_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(processes, "_windows_process_api", lambda: backend)
    monkeypatch.setattr(
        processes.os,
        "kill",
        lambda pid, sent_signal: sent_signals.append((pid, sent_signal)),
    )
    monkeypatch.setattr(
        processes,
        "process_is_alive",
        lambda *_args, **_kwargs: pytest.fail("Windows termination reopened the PID"),
    )
    monkeypatch.setattr(
        processes,
        "process_identity",
        lambda *_args, **_kwargs: pytest.fail("Windows termination re-read the PID"),
    )

    with pytest.raises(ProcessIdentityMismatchError, match="changed identity"):
        terminate_process_tree(
            43210,
            expected_identity="windows:original",
            process_group=43210,
            platform_family=ProcessPlatform.WINDOWS,
        )

    assert sent_signals == []
    assert backend.calls[-1] == ("close", backend.handle)
    assert not any(call[0] == "terminate" for call in backend.calls)


def test_windows_termination_closes_handle_when_terminate_process_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = OSError("TerminateProcess denied")
    backend = _FakeWindowsProcessBackend(
        waits=(False,),
        terminate_error=failure,
    )
    monkeypatch.setattr(processes, "_windows_process_api", lambda: backend)
    monkeypatch.setattr(processes.os, "kill", lambda *_args: None)

    with pytest.raises(OSError, match="TerminateProcess denied"):
        terminate_process_tree(
            43210,
            expected_identity=backend.identity,
            process_group=43210,
            platform_family=ProcessPlatform.WINDOWS,
        )

    assert backend.calls[-1] == ("close", backend.handle)


@pytest.mark.parametrize("recorded_group", [None, 54321])
def test_live_windows_containment_requires_the_recorded_creation_group(
    monkeypatch: pytest.MonkeyPatch,
    recorded_group: int | None,
) -> None:
    monkeypatch.setattr(processes, "process_is_alive", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        processes,
        "process_identity",
        lambda *_args, **_kwargs: "windows:123456",
    )

    with pytest.raises(ProcessInspectionUnavailableError, match="process group"):
        process_containment_is_alive(
            43210,
            "windows:123456",
            recorded_group,
            platform_family=ProcessPlatform.WINDOWS,
        )


def test_windows_termination_rejects_foreign_group_before_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeWindowsProcessBackend()
    monkeypatch.setattr(processes, "_windows_process_api", lambda: backend)

    with pytest.raises(ProcessIdentityMismatchError, match="recorded Windows process group"):
        terminate_process_tree(
            43210,
            expected_identity=backend.identity,
            process_group=54321,
            platform_family=ProcessPlatform.WINDOWS,
        )

    assert backend.calls == []


def test_live_windows_termination_refuses_missing_creation_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeWindowsProcessBackend(waits=(False,))
    sent_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(processes, "_windows_process_api", lambda: backend)
    monkeypatch.setattr(
        processes.os,
        "kill",
        lambda pid, sent_signal: sent_signals.append((pid, sent_signal)),
    )

    with pytest.raises(ProcessInspectionUnavailableError, match="without a recorded"):
        terminate_process_tree(
            43210,
            process_group=43210,
            platform_family=ProcessPlatform.WINDOWS,
        )

    assert sent_signals == []
    assert backend.calls[-1] == ("close", backend.handle)


class _FakeCFunction:
    def __init__(self, function: Any) -> None:
        self.function = function
        self.argtypes: object | None = None
        self.restype: object | None = None

    def __call__(self, *args: object) -> int:
        return int(self.function(*args))


class _FakeDarwinLibproc:
    def __init__(
        self,
        snapshots: dict[int, tuple[int, int, int, int]],
        *,
        short_read: bool = False,
        failure_errno: int | None = None,
        fill_first_group_buffer: bool = False,
    ) -> None:
        self.snapshots = snapshots
        self.short_read = short_read
        self.failure_errno = failure_errno
        self.fill_first_group_buffer = fill_first_group_buffer
        self.group_buffer_calls = 0
        self.proc_pidinfo = _FakeCFunction(self._proc_pidinfo)
        self.proc_listpids = _FakeCFunction(self._proc_listpids)

    def _proc_pidinfo(
        self,
        pid: object,
        flavor: object,
        _argument: object,
        output: object,
        size: object,
    ) -> int:
        assert int(flavor) == processes._DARWIN_PROC_PIDTBSDINFO
        assert int(size) == ctypes.sizeof(processes._DarwinProcBsdInfo)
        numeric_pid = int(pid)
        if self.failure_errno is not None:
            ctypes.set_errno(self.failure_errno)
            return 0
        snapshot = self.snapshots.get(numeric_pid)
        if snapshot is None:
            ctypes.set_errno(errno.ESRCH)
            return 0
        status, process_group, seconds, microseconds = snapshot
        information = ctypes.cast(
            output,
            ctypes.POINTER(processes._DarwinProcBsdInfo),
        ).contents
        information.pbi_pid = numeric_pid
        information.pbi_status = status
        information.pbi_pgid = process_group
        information.pbi_start_tvsec = seconds
        information.pbi_start_tvusec = microseconds
        full_size = ctypes.sizeof(processes._DarwinProcBsdInfo)
        return full_size - 1 if self.short_read else full_size

    def _proc_listpids(
        self,
        flavor: object,
        process_group: object,
        output: object,
        size: object,
    ) -> int:
        assert int(flavor) == processes._DARWIN_PROC_PGRP_ONLY
        members = [
            pid for pid, snapshot in self.snapshots.items() if snapshot[1] == int(process_group)
        ]
        if output is None:
            return len(members) * ctypes.sizeof(ctypes.c_int)
        self.group_buffer_calls += 1
        capacity = int(size) // ctypes.sizeof(ctypes.c_int)
        values = ctypes.cast(output, ctypes.POINTER(ctypes.c_int))
        if self.fill_first_group_buffer and self.group_buffer_calls == 1:
            for index in range(capacity):
                values[index] = 0
            return int(size)
        for index, pid in enumerate(members[:capacity]):
            values[index] = pid
        return min(len(members), capacity) * ctypes.sizeof(ctypes.c_int)


def test_darwin_libproc_identity_preserves_microseconds_and_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = _FakeDarwinLibproc(
        {43210: (processes._DARWIN_STATUS_RUNNING, 43210, 1_725_000_000, 7)}
    )
    monkeypatch.setattr(processes.sys, "platform", "darwin")
    monkeypatch.setattr(processes, "_load_darwin_libproc", lambda: library)

    assert ctypes.sizeof(processes._DarwinProcBsdInfo) == 136
    assert process_is_alive(43210, platform_family=ProcessPlatform.POSIX)
    assert process_identity(43210, platform_family=ProcessPlatform.POSIX) == (
        "darwin:43210:1725000000:000007"
    )
    assert process_group_id(43210, platform_family=ProcessPlatform.POSIX) == 43210
    assert process_containment_is_alive(
        43210,
        "darwin:43210:1725000000:000007",
        43210,
        platform_family=ProcessPlatform.POSIX,
    )


def test_darwin_libproc_identity_distinguishes_same_second_pid_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = _FakeDarwinLibproc(
        {43210: (processes._DARWIN_STATUS_SLEEPING, 43210, 1_725_000_000, 999_999)}
    )
    monkeypatch.setattr(processes.sys, "platform", "darwin")
    monkeypatch.setattr(processes, "_load_darwin_libproc", lambda: library)

    assert process_identity(43210, platform_family=ProcessPlatform.POSIX) == (
        "darwin:43210:1725000000:999999"
    )
    assert not process_containment_is_alive(
        43210,
        "darwin:43210:1725000000:000007",
        43210,
        platform_family=ProcessPlatform.POSIX,
    )


def test_darwin_libproc_zombies_are_not_live_group_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = _FakeDarwinLibproc(
        {43210: (processes._DARWIN_STATUS_ZOMBIE, 43210, 1_725_000_000, 123)}
    )
    monkeypatch.setattr(processes.sys, "platform", "darwin")
    monkeypatch.setattr(processes, "_load_darwin_libproc", lambda: library)

    assert not process_is_alive(43210, platform_family=ProcessPlatform.POSIX)
    assert process_identity(43210, platform_family=ProcessPlatform.POSIX) is None
    assert process_group_id(43210, platform_family=ProcessPlatform.POSIX) is None
    assert not processes._posix_process_group_is_alive(43210)


def test_darwin_group_enumeration_retries_a_filled_pid_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = _FakeDarwinLibproc(
        {43210: (processes._DARWIN_STATUS_RUNNING, 43210, 1_725_000_000, 123)},
        fill_first_group_buffer=True,
    )
    monkeypatch.setattr(processes.sys, "platform", "darwin")
    monkeypatch.setattr(processes, "_load_darwin_libproc", lambda: library)

    assert processes._posix_process_group_is_alive(43210)
    assert library.group_buffer_calls == 2


@pytest.mark.parametrize(
    ("library", "message"),
    [
        (
            _FakeDarwinLibproc(
                {43210: (processes._DARWIN_STATUS_RUNNING, 43210, 1, 0)},
                short_read=True,
            ),
            "returned 135 of 136 bytes",
        ),
        (
            _FakeDarwinLibproc({}, failure_errno=errno.EPERM),
            "could not inspect PID",
        ),
        (
            _FakeDarwinLibproc({43210: (99, 43210, 1, 0)}),
            "unknown process status",
        ),
    ],
)
def test_darwin_libproc_failures_are_not_treated_as_process_exit(
    monkeypatch: pytest.MonkeyPatch,
    library: _FakeDarwinLibproc,
    message: str,
) -> None:
    monkeypatch.setattr(processes.sys, "platform", "darwin")
    monkeypatch.setattr(processes, "_load_darwin_libproc", lambda: library)

    with pytest.raises(ProcessInspectionUnavailableError, match=message):
        process_is_alive(43210, platform_family=ProcessPlatform.POSIX)


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


def test_termination_refuses_a_reused_worker_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(processes, "process_is_alive", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        processes,
        "process_matches_identity",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        processes.os,
        "killpg",
        lambda pid, sent_signal: signals.append((pid, int(sent_signal))),
    )

    with pytest.raises(ProcessIdentityMismatchError, match="changed identity"):
        terminate_process_tree(
            43210,
            expected_identity="linux:old-boot:123",
            process_group=43210,
            platform_family=ProcessPlatform.POSIX,
        )

    assert signals == []


def test_posix_termination_refuses_a_forged_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(processes, "process_is_alive", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        processes,
        "process_matches_identity",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        processes.os,
        "killpg",
        lambda pid, sent_signal: signals.append((pid, int(sent_signal))),
    )

    with pytest.raises(ProcessIdentityMismatchError, match="not bound to recorded"):
        terminate_process_tree(
            43210,
            expected_identity="linux:expected:123",
            process_group=54321,
            platform_family=ProcessPlatform.POSIX,
        )

    assert signals == []


def test_posix_termination_does_not_signal_an_unbound_group_after_leader_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(processes, "process_is_alive", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        processes,
        "_posix_process_group_is_alive",
        lambda _process_group: True,
    )
    monkeypatch.setattr(
        processes.os,
        "killpg",
        lambda pid, sent_signal: signals.append((pid, int(sent_signal))),
    )

    with pytest.raises(ProcessInspectionUnavailableError, match="original identity"):
        terminate_process_tree(
            43210,
            expected_identity="linux:expected:123",
            process_group=43210,
            platform_family=ProcessPlatform.POSIX,
        )

    assert signals == []


def test_live_posix_termination_refuses_missing_creation_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(processes, "process_is_alive", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        processes.os,
        "killpg",
        lambda pid, sent_signal: signals.append((pid, int(sent_signal))),
    )

    with pytest.raises(ProcessInspectionUnavailableError, match="without a recorded"):
        terminate_process_tree(
            43210,
            process_group=43210,
            platform_family=ProcessPlatform.POSIX,
        )

    assert signals == []


def test_orphaned_live_posix_group_blocks_terminal_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(processes.sys, "platform", "linux")
    monkeypatch.setattr(processes, "process_is_alive", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        processes,
        "_posix_process_group_is_alive",
        lambda _process_group: True,
    )

    with pytest.raises(ProcessInspectionUnavailableError, match="still has live members"):
        process_containment_is_alive(
            43210,
            "linux:expected:123",
            43210,
            platform_family=ProcessPlatform.POSIX,
        )


@pytest.mark.parametrize("recorded_group", [None, 43210])
def test_live_posix_containment_mismatch_blocks_recovery(
    monkeypatch: pytest.MonkeyPatch,
    recorded_group: int | None,
) -> None:
    monkeypatch.setattr(processes.sys, "platform", "linux")
    monkeypatch.setattr(processes, "process_is_alive", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        processes,
        "process_identity",
        lambda *_args, **_kwargs: "linux:expected:123",
    )
    monkeypatch.setattr(
        processes,
        "process_group_id",
        lambda *_args, **_kwargs: 54321,
    )

    with pytest.raises(ProcessInspectionUnavailableError, match="process group"):
        process_containment_is_alive(
            43210,
            "linux:expected:123",
            recorded_group,
            platform_family=ProcessPlatform.POSIX,
        )


@pytest.mark.parametrize("identity_failure", [None, OSError("inspection denied")])
def test_live_containment_reports_unavailable_identity_inspection(
    monkeypatch: pytest.MonkeyPatch,
    identity_failure: OSError | None,
) -> None:
    monkeypatch.setattr(processes.sys, "platform", "linux")
    monkeypatch.setattr(processes, "process_is_alive", lambda *_args, **_kwargs: True)

    def inspect_identity(*_args: object, **_kwargs: object) -> str | None:
        if identity_failure is not None:
            raise identity_failure
        return None

    monkeypatch.setattr(processes, "process_identity", inspect_identity)

    with pytest.raises(ProcessInspectionUnavailableError, match="live worker PID"):
        process_containment_is_alive(
            43210,
            "linux:expected:123",
            43210,
            platform_family=ProcessPlatform.POSIX,
        )


def test_posix_termination_kills_descendants_after_the_leader_exits() -> None:
    child_code = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print(os.getpid(), flush=True)\n"
        "time.sleep(60)\n"
    )
    parent_code = (
        "import subprocess, sys, time\n"
        f'subprocess.Popen([{sys.executable!r}, "-c", {child_code!r}])\n'
        "time.sleep(60)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline().strip())
    identity = process_identity(process.pid, platform_family=ProcessPlatform.POSIX)
    group = process_group_id(process.pid, platform_family=ProcessPlatform.POSIX)
    assert identity is not None
    assert group == process.pid

    terminate_process_tree(
        process.pid,
        expected_identity=identity,
        process_group=group,
        graceful_timeout_seconds=0.1,
        force_timeout_seconds=2.0,
        platform_family=ProcessPlatform.POSIX,
    )
    process.wait(timeout=5)

    deadline = time.monotonic() + 5.0
    while process_is_alive(child_pid, platform_family=ProcessPlatform.POSIX):
        if time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    assert not process_is_alive(child_pid, platform_family=ProcessPlatform.POSIX)
