"""Contain external commands before they can spawn descendants.

Supervisors run this file directly using only the standard library. Windows
bypasses virtual-environment redirectors and assigns a Job Object before launch.
POSIX keeps a parent-liveness watcher in the private command group, so killing a
Web worker also stops slicers that were isolated for their own timeout.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

_WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_WINDOWS_JOB_HANDLE: Any | None = None
_CLEANUP_TIMEOUT_SECONDS = 5.0


def enable_windows_process_containment() -> None:
    """Put a Windows process in a kill-on-close Job Object before it spawns children."""
    global _WINDOWS_JOB_HANDLE
    if os.name != "nt":
        raise OSError("Windows process containment is unavailable on this host")
    if _WINDOWS_JOB_HANDLE is not None:
        return
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operation_count", ctypes.c_ulonglong),
            ("write_operation_count", ctypes.c_ulonglong),
            ("other_operation_count", ctypes.c_ulonglong),
            ("read_transfer_count", ctypes.c_ulonglong),
            ("write_transfer_count", ctypes.c_ulonglong),
            ("other_transfer_count", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time_limit", ctypes.c_longlong),
            ("per_job_user_time_limit", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("basic_limit_information", BasicLimitInformation),
            ("io_info", IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_job = kernel32.CreateJobObjectW
    create_job.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    create_job.restype = wintypes.HANDLE
    set_information = kernel32.SetInformationJobObject
    set_information.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    set_information.restype = wintypes.BOOL
    assign_process = kernel32.AssignProcessToJobObject
    assign_process.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    assign_process.restype = wintypes.BOOL
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_job(None, None)
    if not handle:
        error = ctypes.get_last_error()
        raise OSError(error, "CreateJobObjectW failed for the isolated process")
    information = ExtendedLimitInformation()
    information.basic_limit_information.limit_flags = _WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not set_information(
        handle,
        _WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        close_handle(handle)
        raise OSError(error, "SetInformationJobObject failed for the isolated process")
    if not assign_process(handle, get_current_process()):
        error = ctypes.get_last_error()
        close_handle(handle)
        raise OSError(
            error,
            "AssignProcessToJobObject failed; run TopoForge outside a restrictive parent job",
        )
    _WINDOWS_JOB_HANDLE = handle


def _command_launch(
    command: Sequence[str], *, windows: bool, parent_pipe_fd: int | None = None
) -> tuple[list[str], dict[str, Any]]:
    if not windows:
        if parent_pipe_fd is None:
            raise ValueError("POSIX command launch requires a parent-liveness pipe")
        return [
            sys.executable,
            "-I",
            "-S",
            str(Path(__file__).resolve()),
            str(parent_pipe_fd),
            *command,
        ], {"start_new_session": True, "pass_fds": (parent_pipe_fd,)}
    # Bypass the venv python.exe redirector: killing that wrapper alone would
    # leave the actual supervisor, its Job Object, and the slicer alive.
    executable = getattr(sys, "_base_executable", None)
    if not isinstance(executable, str) or not executable or "\x00" in executable:
        raise OSError("Cannot locate the base Python interpreter; repair the Python installation")
    return [executable, "-I", "-S", str(Path(__file__).resolve()), *command], {
        "creationflags": 0x00000200
    }


def _kill_command(process: subprocess.Popen[str], *, windows: bool) -> None:
    if windows:
        # The supervisor owns the only non-inherited Job Object handle. Its
        # exit closes that handle and terminates all contained descendants.
        process.kill()
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


def _parent_liveness_pipe() -> tuple[int, int]:
    if os.name == "nt":
        raise OSError("POSIX parent-liveness pipes are unavailable on this host")
    import fcntl

    read_fd, write_fd = os.pipe()
    try:
        # Closed standard streams can make os.pipe() allocate 0, 1 or 2. Keep
        # both ends above those descriptors before Popen installs capture pipes.
        if read_fd <= 2:
            safe_fd = fcntl.fcntl(read_fd, fcntl.F_DUPFD_CLOEXEC, 3)
            os.close(read_fd)
            read_fd = safe_fd
        if write_fd <= 2:
            safe_fd = fcntl.fcntl(write_fd, fcntl.F_DUPFD_CLOEXEC, 3)
            os.close(write_fd)
            write_fd = safe_fd
        return read_fd, write_fd
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise


def run_contained_command(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Capture a command, killing its isolated descendants before timeout returns."""
    windows = os.name == "nt"
    parent_read_fd: int | None = None
    parent_write_fd: int | None = None
    if not windows:
        # os.pipe() descriptors are non-inheritable. Only this command's read
        # end is explicitly passed; unrelated concurrent commands cannot retain
        # the write end and hide the calling worker's death.
        parent_read_fd, parent_write_fd = _parent_liveness_pipe()
    try:
        launch, options = _command_launch(command, windows=windows, parent_pipe_fd=parent_read_fd)
        process = subprocess.Popen(
            launch,
            cwd=cwd,
            env=None if env is None else dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **options,
        )
    except BaseException:
        if parent_write_fd is not None:
            os.close(parent_write_fd)
        raise
    finally:
        if parent_read_fd is not None:
            os.close(parent_read_fd)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as expired:
        _kill_command(process, windows=windows)
        try:
            stdout, stderr = process.communicate(timeout=_CLEANUP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as cleanup:
            raise OSError(
                "Timed-out command containment did not close its output streams; "
                "stop the remaining external process before retrying"
            ) from cleanup
        raise subprocess.TimeoutExpired(
            list(command), timeout_seconds, output=stdout, stderr=stderr
        ) from expired
    except BaseException:
        _kill_command(process, windows=windows)
        process.wait(timeout=_CLEANUP_TIMEOUT_SECONDS)
        raise
    else:
        if not windows:
            # The watcher still anchors the group even if the command leader
            # exited. Clean it and any remaining child processes synchronously.
            _kill_command(process, windows=False)
    finally:
        if parent_write_fd is not None:
            os.close(parent_write_fd)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    return subprocess.CompletedProcess(list(command), process.wait(timeout=0), stdout, stderr)


def _supervise_command(command: Sequence[str]) -> int:
    try:
        enable_windows_process_containment()
        return subprocess.call(list(command))
    except OSError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 127


def _supervise_posix_command(parent_pipe_fd: int, command: Sequence[str]) -> int:
    if os.name == "nt":
        raise OSError("POSIX process containment is unavailable on this host")
    try:
        # pass_fds temporarily clears CLOEXEC; restore it before target launch
        # so neither the slicer nor its children inherit the control descriptor.
        os.set_inheritable(parent_pipe_fd, False)
        if os.fork() == 0:
            # The watcher must not keep capture pipes or interactive input open.
            for descriptor in (0, 1, 2):
                if descriptor != parent_pipe_fd:
                    with suppress(OSError):
                        os.close(descriptor)
            try:
                while os.read(parent_pipe_fd, 1):
                    pass
            finally:
                # This watcher remains in the group, so the group identifier
                # cannot be recycled to an unrelated process before this signal.
                os.killpg(os.getpgrp(), signal.SIGKILL)
                os._exit(1)
        os.close(parent_pipe_fd)
        # Keep the watcher as the target's sibling. Exec'ing the target here
        # would inject an unexpected child into its waitpid(-1) lifecycle.
        returncode = subprocess.call(list(command))
        if returncode < 0:
            # Preserve Popen's negative POSIX signal status through the wrapper.
            termination_signal = -returncode
            if termination_signal not in {signal.SIGKILL, signal.SIGSTOP}:
                signal.signal(termination_signal, signal.SIG_DFL)
            os.kill(os.getpid(), termination_signal)
        return returncode
    except OSError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 127


if __name__ == "__main__":
    if os.name == "nt":
        raise SystemExit(_supervise_command(sys.argv[1:]))
    raise SystemExit(_supervise_posix_command(int(sys.argv[1]), sys.argv[2:]))
