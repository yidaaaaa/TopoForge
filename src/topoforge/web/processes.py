"""Explicit POSIX and Windows worker-process lifecycle adapters."""

from __future__ import annotations

import ctypes
import errno
import math
import ntpath
import os
import signal
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

_WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
_WINDOWS_PROCESS_TERMINATE = 0x0001
_WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WINDOWS_SYNCHRONIZE = 0x00100000
_WINDOWS_ERROR_ACCESS_DENIED = 5
_WINDOWS_ERROR_INVALID_PARAMETER = 87
_WINDOWS_WAIT_OBJECT_0 = 0
_WINDOWS_WAIT_TIMEOUT = 258
_WINDOWS_WAIT_FAILED = 0xFFFFFFFF
_WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_WINDOWS_JOB_HANDLE: Any | None = None

_DARWIN_PROC_PGRP_ONLY = 2
_DARWIN_PROC_PIDTBSDINFO = 3
_DARWIN_STATUS_IDLE = 1
_DARWIN_STATUS_RUNNING = 2
_DARWIN_STATUS_SLEEPING = 3
_DARWIN_STATUS_STOPPED = 4
_DARWIN_STATUS_ZOMBIE = 5
_DARWIN_LIVE_STATUSES = {
    _DARWIN_STATUS_IDLE,
    _DARWIN_STATUS_RUNNING,
    _DARWIN_STATUS_SLEEPING,
    _DARWIN_STATUS_STOPPED,
}


def _windows_last_error() -> int:
    """Return the calling thread's Win32 error without importing Windows-only stubs."""
    return int(ctypes.get_last_error())  # pyright: ignore[reportAttributeAccessIssue]


class ProcessIdentityMismatchError(RuntimeError):
    """A live PID no longer identifies the worker recorded by the manager."""


class ProcessInspectionUnavailableError(OSError):
    """The operating system could not verify a live worker's identity."""


class ProcessPlatform(StrEnum):
    """Supported worker-process lifecycle families."""

    POSIX = "posix"
    WINDOWS = "windows"


class _DarwinProcBsdInfo(ctypes.Structure):
    """Exact 64-bit Darwin ``proc_bsdinfo`` layout used by PROC_PIDTBSDINFO."""

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


@dataclass(frozen=True, slots=True)
class _DarwinProcessSnapshot:
    pid: int
    status: int
    process_group_id: int
    start_seconds: int
    start_microseconds: int

    @property
    def alive(self) -> bool:
        if self.status == _DARWIN_STATUS_ZOMBIE:
            return False
        if self.status not in _DARWIN_LIVE_STATUSES:
            raise ProcessInspectionUnavailableError(
                f"libproc returned unknown process status {self.status} for PID {self.pid}"
            )
        return True

    @property
    def identity(self) -> str:
        return f"darwin:{self.pid}:{self.start_seconds}:{self.start_microseconds:06d}"


class _WindowsProcessBackend(Protocol):
    def open_process(self, pid: int, access: int) -> int | None: ...

    def creation_identity(self, handle: int, pid: int) -> str: ...

    def wait(self, handle: int, timeout_seconds: float) -> bool: ...

    def terminate(self, handle: int, pid: int) -> None: ...

    def close(self, handle: int) -> None: ...


class _NativeWindowsProcessBackend:
    """Small exact-handle Win32 process API used by inspection and termination."""

    _open_process: Any
    _get_process_times: Any
    _wait_for_single_object: Any
    _terminate_process: Any
    _close_handle: Any
    _file_time_type: Any

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows process inspection is unavailable on this host")
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._open_process = kernel32.OpenProcess
        self._open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self._open_process.restype = wintypes.HANDLE
        self._get_process_times = kernel32.GetProcessTimes
        self._get_process_times.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        self._get_process_times.restype = wintypes.BOOL
        self._wait_for_single_object = kernel32.WaitForSingleObject
        self._wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self._wait_for_single_object.restype = wintypes.DWORD
        self._terminate_process = kernel32.TerminateProcess
        self._terminate_process.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self._terminate_process.restype = wintypes.BOOL
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = (wintypes.HANDLE,)
        self._close_handle.restype = wintypes.BOOL
        self._file_time_type = wintypes.FILETIME

    @staticmethod
    def _error(context: str, pid: int) -> OSError:
        error = _windows_last_error()
        return OSError(error, f"{context} for pid {pid}")

    def open_process(self, pid: int, access: int) -> int | None:
        handle = self._open_process(access, False, pid)
        if not handle:
            error = _windows_last_error()
            if error == _WINDOWS_ERROR_INVALID_PARAMETER:
                return None
            if error == _WINDOWS_ERROR_ACCESS_DENIED:
                raise ProcessInspectionUnavailableError(
                    f"OpenProcess denied access to live or protected PID {pid}"
                )
            raise OSError(error, f"OpenProcess failed for pid {pid}")
        return int(handle)

    def creation_identity(self, handle: int, pid: int) -> str:
        creation = self._file_time_type()
        exit_time = self._file_time_type()
        kernel_time = self._file_time_type()
        user_time = self._file_time_type()
        if not self._get_process_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            raise self._error("GetProcessTimes failed", pid)
        value = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        return f"windows:{value}"

    def wait(self, handle: int, timeout_seconds: float) -> bool:
        milliseconds = min(
            max(math.ceil(max(timeout_seconds, 0.0) * 1000.0), 0),
            0xFFFFFFFE,
        )
        result = int(self._wait_for_single_object(handle, milliseconds))
        if result == _WINDOWS_WAIT_OBJECT_0:
            return True
        if result == _WINDOWS_WAIT_TIMEOUT:
            return False
        if result == _WINDOWS_WAIT_FAILED:
            error = _windows_last_error()
            raise OSError(error, "WaitForSingleObject failed for a Web worker")
        raise OSError(f"WaitForSingleObject returned unexpected status {result}")

    def terminate(self, handle: int, pid: int) -> None:
        if not self._terminate_process(handle, 2):
            raise self._error("TerminateProcess failed", pid)

    def close(self, handle: int) -> None:
        if not self._close_handle(handle):
            error = _windows_last_error()
            raise OSError(error, "CloseHandle failed for a Web worker")


def _windows_process_api() -> _WindowsProcessBackend:
    return _NativeWindowsProcessBackend()


def _load_darwin_libproc() -> Any:
    if sys.platform != "darwin":
        raise OSError("Darwin libproc inspection is unavailable on this host")
    return ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)


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


def worker_interpreter_launch(
    platform_family: ProcessPlatform | None = None,
    *,
    executable: str | None = None,
    base_executable: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Return an interpreter that makes ``Popen.pid`` the actual worker PID.

    CPython's Windows virtual-environment ``python.exe`` is a redirector: it
    creates the base interpreter as a child and waits for it. Launching that
    redirector would bind the manager to the wrapper PID instead of the worker
    that publishes containment evidence. Invoke the base interpreter directly
    and reproduce CPython's redirector handoff so the child still starts in the
    selected virtual environment.
    """
    family = platform_family or current_process_platform()
    selected = sys.executable if executable is None else executable
    if not selected or "\x00" in selected:
        raise ProcessInspectionUnavailableError("worker interpreter path is invalid")
    if family is not ProcessPlatform.WINDOWS:
        return selected, {}

    raw_base = (
        getattr(sys, "_base_executable", None) if base_executable is None else base_executable
    )
    if not isinstance(raw_base, str) or not raw_base or "\x00" in raw_base:
        raise ProcessInspectionUnavailableError(
            "Windows did not expose the base interpreter behind the virtual-environment launcher"
        )
    if ntpath.normcase(ntpath.abspath(raw_base)) == ntpath.normcase(ntpath.abspath(selected)):
        return selected, {}
    return raw_base, {"__PYVENV_LAUNCHER__": selected}


def _linux_process_stat(pid: int) -> tuple[str, int, str] | None:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return None
    try:
        stat = (proc_root / str(pid) / "stat").read_text(encoding="ascii")
    except (FileNotFoundError, NotADirectoryError, OSError):
        return None
    closing_parenthesis = stat.rfind(")")
    if closing_parenthesis < 0:
        return None
    fields = stat[closing_parenthesis + 1 :].strip().split()
    if len(fields) < 20:
        return None
    try:
        process_group_id = int(fields[2])
    except ValueError:
        return None
    return fields[0], process_group_id, fields[19]


def _darwin_process_snapshot(
    pid: int,
    *,
    library: Any | None = None,
) -> _DarwinProcessSnapshot | None:
    """Read one process identity, group, and status in a single libproc snapshot."""
    active = _load_darwin_libproc() if library is None else library
    proc_pidinfo = active.proc_pidinfo
    proc_pidinfo.argtypes = (
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    )
    proc_pidinfo.restype = ctypes.c_int
    information = _DarwinProcBsdInfo()
    ctypes.set_errno(0)
    observed_bytes = int(
        proc_pidinfo(
            pid,
            _DARWIN_PROC_PIDTBSDINFO,
            0,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
    )
    if observed_bytes == 0:
        error = ctypes.get_errno()
        if error in {errno.ENOENT, errno.ESRCH}:
            return None
        raise ProcessInspectionUnavailableError(
            error or errno.EIO,
            f"proc_pidinfo could not inspect PID {pid}",
        )
    if observed_bytes != ctypes.sizeof(information):
        raise ProcessInspectionUnavailableError(
            f"proc_pidinfo returned {observed_bytes} of {ctypes.sizeof(information)} "
            f"bytes for PID {pid}"
        )
    if (
        int(information.pbi_pid) != pid
        or int(information.pbi_pgid) < 1
        or int(information.pbi_start_tvusec) >= 1_000_000
    ):
        raise ProcessInspectionUnavailableError(
            f"proc_pidinfo returned invalid identity fields for PID {pid}"
        )
    return _DarwinProcessSnapshot(
        pid=int(information.pbi_pid),
        status=int(information.pbi_status),
        process_group_id=int(information.pbi_pgid),
        start_seconds=int(information.pbi_start_tvsec),
        start_microseconds=int(information.pbi_start_tvusec),
    )


def _darwin_process_group_is_alive(
    process_group: int,
    *,
    library: Any | None = None,
) -> bool:
    """Return whether libproc reports a non-zombie member of one process group."""
    active = _load_darwin_libproc() if library is None else library
    proc_listpids = active.proc_listpids
    proc_listpids.argtypes = (
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_int,
    )
    proc_listpids.restype = ctypes.c_int
    ctypes.set_errno(0)
    required_bytes = int(proc_listpids(_DARWIN_PROC_PGRP_ONLY, process_group, None, 0))
    if required_bytes <= 0:
        error = ctypes.get_errno()
        if error in {0, errno.ENOENT, errno.ESRCH}:
            return False
        raise ProcessInspectionUnavailableError(
            error,
            f"proc_listpids could not inspect process group {process_group}",
        )
    integer_size = ctypes.sizeof(ctypes.c_int)
    capacity = max(required_bytes // integer_size + 32, 32)
    maximum_capacity = 1_000_000
    while True:
        if capacity > maximum_capacity:
            raise ProcessInspectionUnavailableError(
                f"proc_listpids process group {process_group} exceeded the bounded PID buffer"
            )
        values = (ctypes.c_int * capacity)()
        ctypes.set_errno(0)
        observed_bytes = int(
            proc_listpids(
                _DARWIN_PROC_PGRP_ONLY,
                process_group,
                ctypes.byref(values),
                ctypes.sizeof(values),
            )
        )
        if observed_bytes < 0 or observed_bytes > ctypes.sizeof(values):
            raise ProcessInspectionUnavailableError(
                f"proc_listpids returned an invalid byte count for group {process_group}"
            )
        if observed_bytes == 0:
            error = ctypes.get_errno()
            if error in {0, errno.ENOENT, errno.ESRCH}:
                return False
            raise ProcessInspectionUnavailableError(
                error,
                f"proc_listpids could not read process group {process_group}",
            )
        if observed_bytes % integer_size != 0:
            raise ProcessInspectionUnavailableError(
                f"proc_listpids returned a partial PID for group {process_group}"
            )
        if observed_bytes < ctypes.sizeof(values):
            break
        capacity *= 2
    count = observed_bytes // integer_size
    for pid in values[:count]:
        if pid < 1:
            continue
        snapshot = _darwin_process_snapshot(pid, library=active)
        if snapshot is not None and snapshot.process_group_id == process_group and snapshot.alive:
            return True
    return False


def _posix_process_is_alive(pid: int) -> bool:
    if sys.platform == "darwin":
        snapshot = _darwin_process_snapshot(pid)
        return snapshot is not None and snapshot.alive
    stat = _linux_process_stat(pid)
    if stat is not None:
        return stat[0] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_process_is_alive(pid: int) -> bool:
    active = _windows_process_api()
    handle = active.open_process(
        pid,
        _WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION | _WINDOWS_SYNCHRONIZE,
    )
    if handle is None:
        return False
    try:
        return not active.wait(handle, 0.0)
    finally:
        active.close(handle)


def _windows_process_identity(pid: int) -> str | None:
    active = _windows_process_api()
    handle = active.open_process(
        pid,
        _WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION | _WINDOWS_SYNCHRONIZE,
    )
    if handle is None:
        return None
    try:
        if active.wait(handle, 0.0):
            return None
        return active.creation_identity(handle, pid)
    finally:
        active.close(handle)


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


def process_identity(
    pid: int,
    *,
    platform_family: ProcessPlatform | None = None,
) -> str | None:
    """Return an OS-issued process creation identity, or None when it is unavailable."""
    if pid < 1:
        return None
    family = platform_family or current_process_platform()
    if family is ProcessPlatform.WINDOWS:
        return _windows_process_identity(pid)
    if sys.platform == "darwin":
        snapshot = _darwin_process_snapshot(pid)
        if snapshot is None or not snapshot.alive:
            return None
        return snapshot.identity
    stat = _linux_process_stat(pid)
    if stat is not None:
        state, _, start_ticks = stat
        if state == "Z":
            return None
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except OSError:
            return None
        if not boot_id:
            return None
        return f"linux:{boot_id}:{start_ticks}"
    return None


def process_matches_identity(
    pid: int,
    expected_identity: str | None,
    *,
    platform_family: ProcessPlatform | None = None,
) -> bool:
    """Fail closed unless a live PID retains the recorded creation identity."""
    if expected_identity is None:
        return False
    try:
        observed = process_identity(pid, platform_family=platform_family)
    except OSError:
        return False
    return observed is not None and observed == expected_identity


def process_group_id(
    pid: int,
    *,
    platform_family: ProcessPlatform | None = None,
) -> int | None:
    """Return the worker containment group identifier without guessing."""
    if pid < 1:
        return None
    family = platform_family or current_process_platform()
    if family is ProcessPlatform.WINDOWS:
        return pid if process_is_alive(pid, platform_family=family) else None
    if sys.platform == "darwin":
        snapshot = _darwin_process_snapshot(pid)
        if snapshot is None or not snapshot.alive:
            return None
        return snapshot.process_group_id
    stat = _linux_process_stat(pid)
    if stat is not None:
        return stat[1] if stat[0] != "Z" else None
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return None


def process_containment_is_alive(
    pid: int,
    expected_identity: str | None,
    process_group: int | None,
    *,
    platform_family: ProcessPlatform | None = None,
) -> bool:
    """Return whether a live worker retains its recorded identity and containment group."""
    family = platform_family or current_process_platform()
    if family is ProcessPlatform.POSIX and sys.platform == "darwin":
        snapshot = _darwin_process_snapshot(pid)
        if snapshot is None or not snapshot.alive:
            if (
                process_group is not None
                and process_group == pid
                and _darwin_process_group_is_alive(process_group)
            ):
                raise ProcessInspectionUnavailableError(
                    f"Worker PID {pid} exited while recorded process group {process_group} "
                    "still has live members; the original group identity can no longer "
                    "be proven."
                )
            return False
        if expected_identity is None:
            raise ProcessInspectionUnavailableError(
                f"Could not verify identity for live worker PID {pid} because no process "
                "identity was recorded."
            )
        if snapshot.identity != expected_identity:
            return False
        if process_group is None:
            raise ProcessInspectionUnavailableError(
                f"Could not verify containment for live worker PID {pid} because no process "
                "group was recorded."
            )
        if process_group != pid or snapshot.process_group_id != process_group:
            raise ProcessInspectionUnavailableError(
                f"Live worker PID {pid} no longer matches its recorded process group "
                f"{process_group}."
            )
        return True
    leader_alive = process_is_alive(pid, platform_family=family)
    if leader_alive:
        inspection_error: OSError | None = None
        observed_identity: str | None = None
        if expected_identity is not None:
            try:
                observed_identity = process_identity(pid, platform_family=family)
            except OSError as exc:
                inspection_error = exc
        if observed_identity is not None:
            if observed_identity != expected_identity:
                return False
            if family is ProcessPlatform.WINDOWS:
                if process_group is None:
                    raise ProcessInspectionUnavailableError(
                        f"Could not verify containment for live worker PID {pid} because no "
                        "process group was recorded."
                    )
                if process_group != pid:
                    raise ProcessInspectionUnavailableError(
                        f"Live Windows worker PID {pid} is not bound to its recorded process "
                        f"group {process_group}."
                    )
                return True
            if family is ProcessPlatform.POSIX:
                if process_group is None:
                    raise ProcessInspectionUnavailableError(
                        f"Could not verify containment for live worker PID {pid} because "
                        "no process group was recorded."
                    )
                if process_group != pid:
                    raise ProcessInspectionUnavailableError(
                        f"Live worker PID {pid} is not bound to its recorded process group "
                        f"{process_group}."
                    )
                observed_group = process_group_id(pid, platform_family=family)
                if observed_group is not None:
                    if observed_group != process_group:
                        raise ProcessInspectionUnavailableError(
                            f"Live worker PID {pid} belongs to process group {observed_group}, "
                            f"not recorded group {process_group}."
                        )
                    return True
                try:
                    leader_still_alive = process_is_alive(pid, platform_family=family)
                except OSError as exc:
                    raise ProcessInspectionUnavailableError(
                        f"Could not recheck worker PID {pid} after process-group inspection failed."
                    ) from exc
                if leader_still_alive:
                    raise ProcessInspectionUnavailableError(
                        f"Could not verify process group for live worker PID {pid}."
                    )
                if _posix_process_group_is_alive(process_group):
                    raise ProcessInspectionUnavailableError(
                        f"Worker PID {pid} exited while recorded process group "
                        f"{process_group} still has live members; the original group "
                        "identity can no longer be proven."
                    )
                return False
            return True
        try:
            leader_still_alive = process_is_alive(pid, platform_family=family)
        except OSError as exc:
            raise ProcessInspectionUnavailableError(
                f"Could not recheck worker PID {pid} after identity inspection failed."
            ) from exc
        if leader_still_alive:
            detail = (
                f": {inspection_error}"
                if inspection_error is not None
                else " because no process identity was available"
            )
            raise ProcessInspectionUnavailableError(
                f"Could not verify identity for live worker PID {pid}{detail}."
            ) from inspection_error
    if (
        family is ProcessPlatform.POSIX
        and process_group is not None
        and process_group == pid
        and _posix_process_group_is_alive(process_group)
    ):
        raise ProcessInspectionUnavailableError(
            f"Worker PID {pid} exited while recorded process group {process_group} still "
            "has live members; the original group identity can no longer be proven."
        )
    return False


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


def _posix_process_group_is_alive(process_group: int) -> bool:
    if sys.platform == "darwin":
        return _darwin_process_group_is_alive(process_group)
    proc_root = Path("/proc")
    if proc_root.is_dir():
        try:
            entries = tuple(proc_root.iterdir())
        except OSError:
            entries = ()
        if entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                stat = _linux_process_stat(int(entry.name))
                if stat is not None and stat[1] == process_group and stat[0] != "Z":
                    return True
            return False
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until_posix_group_stopped(
    process_group: int,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> bool:
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while _posix_process_group_is_alive(process_group):
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(poll_interval_seconds, 0.001))
    return True


def _terminate_posix_process_group(
    process_group: int,
    *,
    leader_pid: int,
    expected_identity: str | None,
    graceful_timeout_seconds: float,
    force_timeout_seconds: float,
    poll_interval_seconds: float,
) -> None:
    if (
        expected_identity is not None
        and process_is_alive(leader_pid, platform_family=ProcessPlatform.POSIX)
        and not process_matches_identity(
            leader_pid,
            expected_identity,
            platform_family=ProcessPlatform.POSIX,
        )
    ):
        raise ProcessIdentityMismatchError(
            f"Worker PID {leader_pid} changed identity; refusing to signal its process group"
        )
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    if _wait_until_posix_group_stopped(
        process_group,
        timeout_seconds=graceful_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    ):
        return
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return
    if not _wait_until_posix_group_stopped(
        process_group,
        timeout_seconds=force_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    ):
        raise RuntimeError(
            f"POSIX worker process group {process_group} remained alive after forced termination"
        )


def _terminate_windows_process_tree(
    pid: int,
    *,
    expected_identity: str | None,
    process_group: int | None,
    graceful_timeout_seconds: float,
    force_timeout_seconds: float,
    poll_interval_seconds: float,
) -> None:
    del graceful_timeout_seconds, poll_interval_seconds
    if process_group is not None and process_group != pid:
        raise ProcessIdentityMismatchError(
            f"Worker PID {pid} is not bound to recorded Windows process group "
            f"{process_group}; refusing to terminate it"
        )
    active = _windows_process_api()
    handle = active.open_process(
        pid,
        _WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION
        | _WINDOWS_PROCESS_TERMINATE
        | _WINDOWS_SYNCHRONIZE,
    )
    if handle is None:
        return
    try:
        if active.wait(handle, 0.0):
            return
        if expected_identity is None:
            raise ProcessInspectionUnavailableError(
                f"Refusing to terminate live Windows worker PID {pid} without a recorded "
                "creation identity"
            )
        observed_identity = active.creation_identity(handle, pid)
        if observed_identity != expected_identity:
            raise ProcessIdentityMismatchError(
                f"Worker PID {pid} changed identity; refusing to signal it"
            )
        # CTRL_BREAK targets a numeric process-group id, not this verified
        # handle. A just-exited leader could have its PID reused before that
        # signal call. TerminateProcess remains bound to the exact open handle;
        # the worker's kill-on-close Job Object contains its descendants.
        active.terminate(handle, pid)
        if not active.wait(handle, force_timeout_seconds):
            raise RuntimeError(
                f"Windows worker process {pid} remained alive after exact-handle termination"
            )
    finally:
        active.close(handle)


def enable_current_process_containment(
    platform_family: ProcessPlatform | None = None,
) -> None:
    """Put a Windows worker in a kill-on-close Job Object before it spawns children."""
    global _WINDOWS_JOB_HANDLE
    family = platform_family or current_process_platform()
    if family is not ProcessPlatform.WINDOWS:
        return
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
        raise OSError(error, "CreateJobObjectW failed for the Web worker")
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
        raise OSError(error, "SetInformationJobObject failed for the Web worker")
    if not assign_process(handle, get_current_process()):
        error = ctypes.get_last_error()
        close_handle(handle)
        raise OSError(
            error,
            "AssignProcessToJobObject failed; run TopoForge outside a restrictive parent job",
        )
    _WINDOWS_JOB_HANDLE = handle


def terminate_process_tree(
    pid: int,
    *,
    expected_identity: str | None = None,
    process_group: int | None = None,
    graceful_timeout_seconds: float = 5.0,
    force_timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.05,
    platform_family: ProcessPlatform | None = None,
) -> None:
    """Terminate one verified isolated worker and its complete process containment."""
    if pid < 1:
        raise ValueError("worker pid must be positive")
    family = platform_family or current_process_platform()
    if family is ProcessPlatform.WINDOWS:
        _terminate_windows_process_tree(
            pid,
            expected_identity=expected_identity,
            process_group=process_group,
            graceful_timeout_seconds=graceful_timeout_seconds,
            force_timeout_seconds=force_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        return
    leader_alive = process_is_alive(pid, platform_family=family)
    containment = process_group if process_group is not None else pid
    if containment < 1:
        raise ValueError("worker process group must be positive")
    if containment != pid:
        raise ProcessIdentityMismatchError(
            f"Worker PID {pid} is not bound to recorded process group {containment}; "
            "refusing to terminate it"
        )
    if leader_alive and expected_identity is None:
        raise ProcessInspectionUnavailableError(
            f"Refusing to terminate live worker PID {pid} without a recorded creation identity"
        )
    if (
        leader_alive
        and expected_identity is not None
        and not process_matches_identity(
            pid,
            expected_identity,
            platform_family=family,
        )
    ):
        raise ProcessIdentityMismatchError(
            f"Worker PID {pid} changed identity; refusing to terminate it"
        )
    if not leader_alive:
        if _posix_process_group_is_alive(containment):
            raise ProcessInspectionUnavailableError(
                f"Worker PID {pid} exited while recorded process group {containment} still "
                "has live members; refusing to signal a group whose original identity can "
                "no longer be proven"
            )
        return
    observed_group = process_group_id(pid, platform_family=family)
    if observed_group is None:
        try:
            leader_still_alive = process_is_alive(pid, platform_family=family)
        except OSError as exc:
            raise ProcessInspectionUnavailableError(
                f"Could not recheck worker PID {pid} after process-group inspection failed."
            ) from exc
        if leader_still_alive:
            raise ProcessInspectionUnavailableError(
                f"Could not verify process group for live worker PID {pid}."
            )
        return
    if observed_group != containment:
        raise ProcessIdentityMismatchError(
            f"Worker PID {pid} belongs to process group {observed_group}, not recorded "
            f"group {containment}; refusing to terminate it"
        )
    _terminate_posix_process_group(
        containment,
        leader_pid=pid,
        expected_identity=expected_identity,
        graceful_timeout_seconds=graceful_timeout_seconds,
        force_timeout_seconds=force_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
