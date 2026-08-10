"""Explicit POSIX and Windows worker-process lifecycle adapters."""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import subprocess
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

_WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
_WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WINDOWS_STILL_ACTIVE = 259
_WINDOWS_ERROR_ACCESS_DENIED = 5
_WINDOWS_ERROR_INVALID_PARAMETER = 87


class ProcessPlatform(StrEnum):
    """Supported worker-process lifecycle families."""

    POSIX = "posix"
    WINDOWS = "windows"


def current_process_platform() -> ProcessPlatform:
    """Return the process lifecycle family for the running interpreter."""
    return ProcessPlatform.WINDOWS if os.name == "nt" else ProcessPlatform.POSIX


def worker_process_options(
    platform_family: ProcessPlatform | None = None,
) -> dict[str, Any]:
    """Return explicit subprocess isolation options for one worker."""
    family = platform_family or current_process_platform()
    if family is ProcessPlatform.WINDOWS:
        return {"creationflags": _WINDOWS_CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _posix_process_is_alive(pid: int) -> bool:
    proc_root = Path("/proc")
    if proc_root.is_dir():
        try:
            stat = (proc_root / str(pid) / "stat").read_text(encoding="ascii")
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            stat = ""
        if stat:
            closing_parenthesis = stat.rfind(")")
            if closing_parenthesis >= 0:
                remainder = stat[closing_parenthesis + 1 :].strip()
                if remainder and remainder[0] == "Z":
                    return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_process_is_alive(pid: int) -> bool:
    if os.name != "nt":
        raise OSError("Windows process inspection is unavailable on this host")
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = open_process(_WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == _WINDOWS_ERROR_INVALID_PARAMETER:
            return False
        if error == _WINDOWS_ERROR_ACCESS_DENIED:
            return True
        raise OSError(error, f"OpenProcess failed for pid {pid}")
    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            error = ctypes.get_last_error()
            raise OSError(error, f"GetExitCodeProcess failed for pid {pid}")
        return exit_code.value == _WINDOWS_STILL_ACTIVE
    finally:
        close_handle(handle)


def process_is_alive(
    pid: int,
    *,
    platform_family: ProcessPlatform | None = None,
) -> bool:
    """Return whether a recorded worker PID still identifies a live process."""
    if pid < 1:
        return False
    family = platform_family or current_process_platform()
    if family is ProcessPlatform.WINDOWS:
        return _windows_process_is_alive(pid)
    return _posix_process_is_alive(pid)


def _wait_until_stopped(
    pid: int,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    platform_family: ProcessPlatform,
) -> bool:
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while process_is_alive(pid, platform_family=platform_family):
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(poll_interval_seconds, 0.001))
    return True


def _terminate_posix_process_group(
    pid: int,
    *,
    graceful_timeout_seconds: float,
    force_timeout_seconds: float,
    poll_interval_seconds: float,
) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if _wait_until_stopped(
        pid,
        timeout_seconds=graceful_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        platform_family=ProcessPlatform.POSIX,
    ):
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    if not _wait_until_stopped(
        pid,
        timeout_seconds=force_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        platform_family=ProcessPlatform.POSIX,
    ):
        raise RuntimeError(
            f"POSIX worker process group {pid} remained alive after forced termination"
        )


def _terminate_windows_process_tree(
    pid: int,
    *,
    graceful_timeout_seconds: float,
    force_timeout_seconds: float,
    poll_interval_seconds: float,
) -> None:
    ctrl_break_event = getattr(signal, "CTRL_BREAK_EVENT", 1)
    with contextlib.suppress(OSError, ValueError):
        os.kill(pid, ctrl_break_event)
    if _wait_until_stopped(
        pid,
        timeout_seconds=graceful_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        platform_family=ProcessPlatform.WINDOWS,
    ):
        return
    completed = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0 and process_is_alive(pid, platform_family=ProcessPlatform.WINDOWS):
        detail = completed.stderr.strip() or completed.stdout.strip() or "no taskkill detail"
        raise RuntimeError(f"taskkill could not terminate worker tree {pid}: {detail}")
    if not _wait_until_stopped(
        pid,
        timeout_seconds=force_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        platform_family=ProcessPlatform.WINDOWS,
    ):
        raise RuntimeError(f"Windows worker process tree {pid} remained alive after taskkill")


def terminate_process_tree(
    pid: int,
    *,
    graceful_timeout_seconds: float = 5.0,
    force_timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.05,
    platform_family: ProcessPlatform | None = None,
) -> None:
    """Terminate one isolated worker and its descendants on the active platform."""
    if pid < 1:
        raise ValueError("worker pid must be positive")
    family = platform_family or current_process_platform()
    if not process_is_alive(pid, platform_family=family):
        return
    if family is ProcessPlatform.WINDOWS:
        _terminate_windows_process_tree(
            pid,
            graceful_timeout_seconds=graceful_timeout_seconds,
            force_timeout_seconds=force_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        return
    _terminate_posix_process_group(
        pid,
        graceful_timeout_seconds=graceful_timeout_seconds,
        force_timeout_seconds=force_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
