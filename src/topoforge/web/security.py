"""Loopback HTTP and race-stable local Web filesystem security helpers."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any, BinaryIO, Protocol, TypeVar
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel
from starlette.requests import Request

from topoforge.platforms import stat_result_is_link_like

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_DEFAULT_MAX_RECORD_BYTES = 8 * 1024 * 1024

_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_DELETE = 0x00000004
_DELETE = 0x00010000
_FILE_SHARE_WRITE = 0x00000002
_FILE_LIST_DIRECTORY = 0x00000001
_FILE_TRAVERSE = 0x00000020
_FILE_READ_ATTRIBUTES = 0x00000080
_SYNCHRONIZE = 0x00100000
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_OPEN = 1
_FILE_CREATE = 2
_FILE_OPENED = 1
_FILE_CREATED = 2
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_NON_DIRECTORY_FILE = 0x00000040
_OBJ_CASE_INSENSITIVE = 0x00000040
_OBJ_DONT_REPARSE = 0x00001000
_FILE_ID_INFO_CLASS = 0x12
_FILE_RENAME_INFO_CLASS = 3
_FILE_DISPOSITION_INFO_CLASS = 4
_HANDLE_FLAG_INHERIT = 0x00000001
_WINDOWS_CTYPES: Any = ctypes


class CommittedStateUncertainError(OSError):
    """A filesystem mutation committed but its durable final state is uncertain."""

    committed: bool = True
    operation: str
    path: Path

    def __init__(
        self,
        *,
        operation: str,
        path: Path,
        context: str,
        cause: Exception,
    ) -> None:
        self.operation = operation
        self.path = path
        super().__init__(
            errno.EIO,
            f"{context} {operation} committed, but durable final-state verification "
            f"is uncertain; reopen the owned path before retrying: {cause}",
            path,
        )


def _finish_mutation_finalization(
    errors: list[OSError],
    *,
    active_error: BaseException | None,
    committed: bool,
    operation: str,
    path: Path,
    context: str,
) -> None:
    """Preserve primary failures and classify finalization after a committed mutation."""
    if not errors:
        return
    if active_error is not None:
        for error in errors:
            active_error.add_note(f"{context} finalization also failed: {error}")
        return
    primary, *additional = errors
    if committed:
        result = CommittedStateUncertainError(
            operation=operation,
            path=path,
            context=context,
            cause=primary,
        )
        for error in additional:
            result.add_note(f"additional finalization failure: {error}")
        raise result from primary
    for error in additional:
        primary.add_note(f"additional finalization failure: {error}")
    raise primary


@dataclass(frozen=True, slots=True)
class _WindowsFileInformation:
    attributes: int
    link_count: int
    volume_serial_number: int
    file_id: int


class _WindowsLeaseBackend(Protocol):
    def open_parent(self, path: Path) -> int: ...

    def open_relative_file(
        self,
        parent_handle: int,
        name: str,
        *,
        create: bool,
        directory: bool = False,
        desired_access: int | None = None,
        share_access: int | None = None,
    ) -> int: ...

    def information(self, handle: int) -> _WindowsFileInformation: ...

    def adopt_file_handle(self, handle: int, *, flags: int | None = None) -> int: ...

    def close_handle(self, handle: int) -> None: ...

    def rename_relative(
        self,
        handle: int,
        parent_handle: int,
        name: str,
        *,
        replace: bool,
    ) -> None: ...

    def delete_file(self, handle: int) -> None: ...


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_uint16),
        ("MaximumLength", ctypes.c_uint16),
        ("Buffer", ctypes.c_void_p),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.c_void_p),
        ("Attributes", ctypes.c_uint32),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [
        ("StatusOrPointer", ctypes.c_void_p),
        ("Information", ctypes.c_size_t),
    ]


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("LowDateTime", ctypes.c_uint32),
        ("HighDateTime", ctypes.c_uint32),
    ]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", _FileTime),
        ("ftLastAccessTime", _FileTime),
        ("ftLastWriteTime", _FileTime),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_uint64),
        ("FileId", ctypes.c_ubyte * 16),
    ]


class _FileRenameInfoHeader(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_ubyte),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
    ]


class _FileDispositionInfo(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


def _extended_windows_path(path: Path) -> str:
    value = os.fspath(path)
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


class _WindowsNativeLeaseBackend:
    _create_file: Any
    _get_information: Any
    _get_information_ex: Any
    _set_handle_information: Any
    _close_handle: Any
    _set_file_information: Any
    _nt_create_file: Any
    _rtl_status_to_error: Any

    def __init__(self) -> None:
        if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise RuntimeError("the native Web lease backend requires Windows x64")
        win_dll = _WINDOWS_CTYPES.WinDLL
        kernel32 = win_dll("kernel32", use_last_error=True)
        ntdll = win_dll("ntdll", use_last_error=True)
        self._create_file: Any = kernel32.CreateFileW
        self._create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self._create_file.restype = ctypes.c_void_p
        self._get_information: Any = kernel32.GetFileInformationByHandle
        self._get_information.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        self._get_information.restype = ctypes.c_int
        self._get_information_ex: Any = kernel32.GetFileInformationByHandleEx
        self._get_information_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._get_information_ex.restype = ctypes.c_int
        self._set_handle_information: Any = kernel32.SetHandleInformation
        self._set_handle_information.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self._set_handle_information.restype = ctypes.c_int
        self._close_handle: Any = kernel32.CloseHandle
        self._close_handle.argtypes = [ctypes.c_void_p]
        self._close_handle.restype = ctypes.c_int
        self._set_file_information: Any = kernel32.SetFileInformationByHandle
        self._set_file_information.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._set_file_information.restype = ctypes.c_int
        self._nt_create_file: Any = ntdll.NtCreateFile
        self._nt_create_file.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint32,
            ctypes.POINTER(_ObjectAttributes),
            ctypes.POINTER(_IoStatusBlock),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._nt_create_file.restype = ctypes.c_int32
        self._rtl_status_to_error: Any = ntdll.RtlNtStatusToDosError
        self._rtl_status_to_error.argtypes = [ctypes.c_int32]
        self._rtl_status_to_error.restype = ctypes.c_uint32

    @staticmethod
    def _normalized_error(error: int, context: str, path: Path | str) -> OSError:
        """Translate native Windows errors into Python's portable subclasses."""
        formatter = getattr(_WINDOWS_CTYPES, "FormatError", None)
        try:
            detail = str(formatter(error)).strip() if formatter is not None else ""
        except (OSError, ValueError):
            detail = ""
        if not detail:
            detail = f"Windows error {error}"
        error_type: type[OSError]
        normalized_errno: int
        if error in {2, 3}:
            error_type = FileNotFoundError
            normalized_errno = errno.ENOENT
        elif error in {80, 183}:
            error_type = FileExistsError
            normalized_errno = errno.EEXIST
        elif error == 5:
            error_type = PermissionError
            normalized_errno = errno.EACCES
        else:
            error_type = OSError
            normalized_errno = error
        result = error_type(
            normalized_errno,
            f"{context}: {detail}",
            os.fspath(path),
        )
        result.__dict__["winerror"] = error
        return result

    @classmethod
    def _last_error(cls, context: str, path: Path | str) -> OSError:
        return cls._normalized_error(
            int(_WINDOWS_CTYPES.get_last_error()),
            context,
            path,
        )

    def _make_noninheritable(self, handle: int, path: Path | str) -> None:
        if not self._set_handle_information(
            ctypes.c_void_p(handle),
            _HANDLE_FLAG_INHERIT,
            0,
        ):
            raise self._last_error("SetHandleInformation failed", path)

    @staticmethod
    def _validate_basename(name: str) -> None:
        if (
            not name
            or name in {".", ".."}
            or any(character in name for character in ("\x00", "/", "\\", ":"))
        ):
            raise RuntimeError(f"unsafe Windows filesystem basename: {name!r}")

    def open_parent(self, path: Path) -> int:
        raw = self._create_file(
            _extended_windows_path(path),
            _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if raw is None or raw == ctypes.c_void_p(-1).value:
            raise self._last_error("CreateFileW parent open failed", path)
        handle = int(raw)
        try:
            self._make_noninheritable(handle, path)
        except BaseException:
            self.close_handle(handle)
            raise
        return handle

    def open_relative_file(
        self,
        parent_handle: int,
        name: str,
        *,
        create: bool,
        directory: bool = False,
        desired_access: int | None = None,
        share_access: int | None = None,
    ) -> int:
        self._validate_basename(name)
        name_buffer = ctypes.create_unicode_buffer(name)
        encoded_length = len(name.encode("utf-16-le"))
        unicode_name = _UnicodeString(
            encoded_length,
            encoded_length + 2,
            ctypes.cast(name_buffer, ctypes.c_void_p),
        )
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            ctypes.c_void_p(parent_handle),
            ctypes.cast(ctypes.pointer(unicode_name), ctypes.c_void_p),
            _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE,
            None,
            None,
        )
        io_status = _IoStatusBlock()
        raw = ctypes.c_void_p()
        status = int(
            self._nt_create_file(
                ctypes.byref(raw),
                _GENERIC_READ | _GENERIC_WRITE | _SYNCHRONIZE
                if desired_access is None
                else desired_access,
                ctypes.byref(attributes),
                ctypes.byref(io_status),
                None,
                _FILE_ATTRIBUTE_NORMAL,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE if share_access is None else share_access,
                _FILE_CREATE if create else _FILE_OPEN,
                _FILE_SYNCHRONOUS_IO_NONALERT
                | (_FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE)
                | _FILE_FLAG_OPEN_REPARSE_POINT,
                None,
                0,
            )
        )
        if status < 0:
            error = int(self._rtl_status_to_error(status))
            raise self._normalized_error(
                error,
                f"NtCreateFile failed with NTSTATUS 0x{status & 0xFFFFFFFF:08x}",
                name,
            )
        if raw.value is None:
            raise RuntimeError("NtCreateFile returned success without a file handle")
        handle = int(raw.value)
        expected_information = _FILE_CREATED if create else _FILE_OPENED
        if int(io_status.Information) != expected_information:
            self.close_handle(handle)
            raise RuntimeError(
                f"NtCreateFile returned an unexpected create/open disposition for {name!r}"
            )
        try:
            self._make_noninheritable(handle, name)
        except BaseException:
            self.close_handle(handle)
            raise
        return handle

    def information(self, handle: int) -> _WindowsFileInformation:
        basic = _ByHandleFileInformation()
        if not self._get_information(ctypes.c_void_p(handle), ctypes.byref(basic)):
            raise self._last_error(
                "GetFileInformationByHandle failed",
                "manager.lock",
            )
        identity = _FileIdInfo()
        has_extended_identity = bool(
            self._get_information_ex(
                ctypes.c_void_p(handle),
                _FILE_ID_INFO_CLASS,
                ctypes.byref(identity),
                ctypes.sizeof(identity),
            )
        )
        fallback_file_id = (int(basic.nFileIndexHigh) << 32) | int(basic.nFileIndexLow)
        extended_file_id = int.from_bytes(bytes(identity.FileId), byteorder="little")
        return _WindowsFileInformation(
            attributes=int(basic.dwFileAttributes),
            link_count=int(basic.nNumberOfLinks),
            volume_serial_number=(
                int(identity.VolumeSerialNumber)
                if has_extended_identity
                else int(basic.dwVolumeSerialNumber)
            ),
            file_id=(
                extended_file_id
                if has_extended_identity and extended_file_id != 0
                else fallback_file_id
            ),
        )

    @staticmethod
    def adopt_file_handle(handle: int, *, flags: int | None = None) -> int:
        import msvcrt

        descriptor_flags = os.O_RDWR if flags is None else flags
        descriptor_flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        windows_runtime: Any = msvcrt
        descriptor = windows_runtime.open_osfhandle(handle, descriptor_flags)
        if descriptor < 0:
            raise OSError("msvcrt.open_osfhandle rejected the Windows file handle")
        return descriptor

    def rename_relative(
        self,
        handle: int,
        parent_handle: int,
        name: str,
        *,
        replace: bool,
    ) -> None:
        self._validate_basename(name)
        encoded_name = name.encode("utf-16-le")
        header_bytes = _FileRenameInfoHeader.FileNameLength.offset + ctypes.sizeof(ctypes.c_uint32)
        buffer = ctypes.create_string_buffer(
            ctypes.sizeof(_FileRenameInfoHeader) + len(encoded_name)
        )
        header = ctypes.cast(buffer, ctypes.POINTER(_FileRenameInfoHeader)).contents
        header.ReplaceIfExists = int(replace)
        header.RootDirectory = ctypes.c_void_p(parent_handle)
        header.FileNameLength = len(encoded_name)
        ctypes.memmove(ctypes.addressof(buffer) + header_bytes, encoded_name, len(encoded_name))
        if not self._set_file_information(
            ctypes.c_void_p(handle),
            _FILE_RENAME_INFO_CLASS,
            ctypes.byref(buffer),
            ctypes.sizeof(buffer),
        ):
            raise self._last_error("relative atomic rename failed", name)

    def delete_file(self, handle: int) -> None:
        disposition = _FileDispositionInfo(1)
        if not self._set_file_information(
            ctypes.c_void_p(handle),
            _FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise self._last_error("handle-relative temporary cleanup failed", "publishing file")

    def close_handle(self, handle: int) -> None:
        if not self._close_handle(ctypes.c_void_p(handle)):
            raise self._last_error("CloseHandle failed", "Web manager lease")


def _windows_identity_matches(
    information: _WindowsFileInformation,
    result: os.stat_result,
) -> bool:
    return information.volume_serial_number == int(result.st_dev) and information.file_id == int(
        result.st_ino
    )


def _lexical_relative_parts(root: Path, path: Path, *, context: str) -> tuple[str, ...]:
    lexical_root = Path(os.path.abspath(root.expanduser()))
    candidate = Path(os.path.abspath(path.expanduser()))
    if candidate != lexical_root and lexical_root not in candidate.parents:
        raise ValueError(f"{context} is outside its trusted root: {candidate}")
    return candidate.relative_to(lexical_root).parts


def _identity_tuple(result: os.stat_result) -> tuple[int, int]:
    return int(result.st_dev), int(result.st_ino)


def _open_posix_directory_chain(path: Path, *, context: str) -> tuple[int, ...]:
    """Pin every absolute ancestor with no-follow openat operations."""
    candidate = Path(os.path.abspath(path.expanduser()))
    anchor = Path(candidate.anchor or os.path.sep)
    parts = candidate.relative_to(anchor).parts
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    handles: list[int] = []
    try:
        descriptor = os.open(anchor, flags)
        handles.append(descriptor)
        opened = os.fstat(descriptor)
        if stat_result_is_link_like(opened) or not stat.S_ISDIR(opened.st_mode):
            raise ValueError(f"{context} filesystem anchor is unsafe: {anchor}")
        for part in parts:
            descriptor = os.open(part, flags, dir_fd=handles[-1])
            handles.append(descriptor)
            opened = os.fstat(descriptor)
            if stat_result_is_link_like(opened) or not stat.S_ISDIR(opened.st_mode):
                raise ValueError(f"{context} contains a link-like or non-directory component")
        return tuple(handles)
    except BaseException:
        with contextlib.suppress(OSError):
            _close_posix_handles(tuple(handles))
        raise


def _walk_posix_directory_tree(
    path: Path,
    *,
    context: str,
    create_missing: bool,
) -> tuple[int, int]:
    """Walk an absolute POSIX directory tree from `/` using only openat calls."""
    candidate = Path(os.path.abspath(path.expanduser()))
    anchor = Path(candidate.anchor or os.path.sep)
    parts = candidate.relative_to(anchor).parts
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    handles: list[int] = []
    try:
        descriptor = os.open(anchor, flags)
        handles.append(descriptor)
        opened = os.fstat(descriptor)
        if stat_result_is_link_like(opened) or not stat.S_ISDIR(opened.st_mode):
            raise ValueError(f"{context} filesystem anchor is unsafe: {anchor}")
        for part in parts:
            try:
                descriptor = os.open(part, flags, dir_fd=handles[-1])
            except FileNotFoundError:
                if not create_missing:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=handles[-1])
                    os.fsync(handles[-1])
                except FileExistsError:
                    pass
                descriptor = os.open(part, flags, dir_fd=handles[-1])
            handles.append(descriptor)
            opened = os.fstat(descriptor)
            if stat_result_is_link_like(opened) or not stat.S_ISDIR(opened.st_mode):
                raise ValueError(f"{context} contains a link-like or non-directory component")
        final_opened = os.fstat(handles[-1])
        verification = _open_posix_directory_chain(
            candidate,
            context=f"{context} verification",
        )
        try:
            if tuple(_identity_tuple(os.fstat(item)) for item in verification) != tuple(
                _identity_tuple(os.fstat(item)) for item in handles
            ):
                raise ValueError(
                    f"{context} changed while its directory chain was opened: {candidate}"
                )
        finally:
            _close_posix_handles(verification)
        return _identity_tuple(final_opened)
    finally:
        with contextlib.suppress(OSError):
            _close_posix_handles(tuple(handles))


def _open_windows_directory_chain(
    path: Path,
    *,
    context: str,
    create_missing: bool,
    backend: _WindowsLeaseBackend,
) -> tuple[tuple[int, ...], _WindowsFileInformation]:
    """Pin every Windows ancestor relative to a volume/share handle."""
    candidate = Path(os.path.abspath(path.expanduser()))
    anchor = Path(candidate.anchor)
    if not candidate.anchor:
        raise ValueError(f"{context} has no absolute Windows anchor: {candidate}")
    parts = candidate.relative_to(anchor).parts
    handles: list[int] = []
    try:
        handle = backend.open_parent(anchor)
        handles.append(handle)
        information = backend.information(handle)
        if (
            information.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or not information.attributes & _FILE_ATTRIBUTE_DIRECTORY
        ):
            raise ValueError(f"{context} filesystem anchor is unsafe: {anchor}")
        for part in parts:
            try:
                handle = backend.open_relative_file(
                    handles[-1],
                    part,
                    create=False,
                    directory=True,
                    desired_access=(
                        _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
                    ),
                    share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                )
            except FileNotFoundError:
                if not create_missing:
                    raise
                try:
                    handle = backend.open_relative_file(
                        handles[-1],
                        part,
                        create=True,
                        directory=True,
                        desired_access=(
                            _FILE_LIST_DIRECTORY
                            | _FILE_TRAVERSE
                            | _FILE_READ_ATTRIBUTES
                            | _SYNCHRONIZE
                        ),
                        share_access=(_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE),
                    )
                except FileExistsError:
                    handle = backend.open_relative_file(
                        handles[-1],
                        part,
                        create=False,
                        directory=True,
                        desired_access=(
                            _FILE_LIST_DIRECTORY
                            | _FILE_TRAVERSE
                            | _FILE_READ_ATTRIBUTES
                            | _SYNCHRONIZE
                        ),
                        share_access=(_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE),
                    )
            handles.append(handle)
            information = backend.information(handle)
            if (
                information.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                or not information.attributes & _FILE_ATTRIBUTE_DIRECTORY
            ):
                raise ValueError(f"{context} contains a reparse or non-directory component")
        return tuple(handles), information
    except BaseException:
        with contextlib.suppress(OSError):
            _close_windows_handles(backend, tuple(handles))
        raise


def _walk_windows_directory_tree(
    path: Path,
    *,
    context: str,
    create_missing: bool,
    backend: _WindowsLeaseBackend | None = None,
) -> tuple[int, int]:
    """Walk a Windows directory tree from its volume/share root without reparses."""
    active = _WindowsNativeLeaseBackend() if backend is None else backend
    candidate = Path(os.path.abspath(path.expanduser()))
    anchor = Path(candidate.anchor)
    if not candidate.anchor:
        raise ValueError(f"{context} has no absolute Windows anchor: {candidate}")
    parts = candidate.relative_to(anchor).parts
    handles: list[int] = []
    try:
        handle = active.open_parent(anchor)
        handles.append(handle)
        information = active.information(handle)
        if (
            information.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or not information.attributes & _FILE_ATTRIBUTE_DIRECTORY
        ):
            raise ValueError(f"{context} filesystem anchor is unsafe: {anchor}")
        for part in parts:
            try:
                handle = active.open_relative_file(
                    handles[-1],
                    part,
                    create=False,
                    directory=True,
                    desired_access=(
                        _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
                    ),
                    share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                )
            except FileNotFoundError:
                if not create_missing:
                    raise
                try:
                    handle = active.open_relative_file(
                        handles[-1],
                        part,
                        create=True,
                        directory=True,
                        desired_access=(
                            _FILE_LIST_DIRECTORY
                            | _FILE_TRAVERSE
                            | _FILE_READ_ATTRIBUTES
                            | _SYNCHRONIZE
                        ),
                        share_access=(_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE),
                    )
                except FileExistsError:
                    handle = active.open_relative_file(
                        handles[-1],
                        part,
                        create=False,
                        directory=True,
                        desired_access=(
                            _FILE_LIST_DIRECTORY
                            | _FILE_TRAVERSE
                            | _FILE_READ_ATTRIBUTES
                            | _SYNCHRONIZE
                        ),
                        share_access=(_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE),
                    )
            handles.append(handle)
            information = active.information(handle)
            if (
                information.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                or not information.attributes & _FILE_ATTRIBUTE_DIRECTORY
            ):
                raise ValueError(f"{context} contains a reparse or non-directory component")
        verification_handles, verification_information = _open_windows_directory_chain(
            candidate,
            context=f"{context} verification",
            create_missing=False,
            backend=active,
        )
        try:
            if (
                verification_information.volume_serial_number,
                verification_information.file_id,
            ) != (information.volume_serial_number, information.file_id):
                raise ValueError(
                    f"{context} changed while its directory chain was opened: {candidate}"
                )
        finally:
            _close_windows_handles(active, verification_handles)
        return information.volume_serial_number, information.file_id
    finally:
        with contextlib.suppress(OSError):
            _close_windows_handles(active, tuple(handles))


def ensure_real_directory_tree(path: Path, *, context: str) -> tuple[int, int]:
    """Create a directory tree through no-follow handles and return its identity."""
    if os.name == "nt":
        return _walk_windows_directory_tree(path, context=context, create_missing=True)
    return _walk_posix_directory_tree(path, context=context, create_missing=True)


def real_directory_tree_identity(path: Path, *, context: str) -> tuple[int, int]:
    """Open every ancestor without following links and return the final identity."""
    if os.name == "nt":
        return _walk_windows_directory_tree(path, context=context, create_missing=False)
    return _walk_posix_directory_tree(path, context=context, create_missing=False)


@dataclass(frozen=True, slots=True)
class _WindowsPinnedDirectory:
    backend: _WindowsLeaseBackend
    handles: tuple[int, ...]
    information: _WindowsFileInformation

    @property
    def handle(self) -> int:
        return self.handles[-1]


@dataclass(frozen=True, slots=True)
class _WindowsOwnedEntry:
    parent: _WindowsPinnedDirectory
    handle: int
    information: _WindowsFileInformation


def _close_windows_handles(backend: _WindowsLeaseBackend, handles: tuple[int, ...]) -> None:
    first_error: OSError | None = None
    for handle in reversed(handles):
        try:
            backend.close_handle(handle)
        except OSError as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def _open_windows_pinned_directory(
    root: Path,
    path: Path,
    *,
    expected_root_identity: tuple[int, int],
    context: str,
    backend: _WindowsLeaseBackend | None = None,
) -> _WindowsPinnedDirectory:
    """Open a directory by walking only from one identity-bound root handle."""
    active = _WindowsNativeLeaseBackend() if backend is None else backend
    lexical_root = Path(os.path.abspath(root.expanduser()))
    candidate = Path(os.path.abspath(path.expanduser()))
    parts = _lexical_relative_parts(lexical_root, candidate, context=context)
    root_handles, information = _open_windows_directory_chain(
        lexical_root,
        context=f"{context} trusted root",
        create_missing=False,
        backend=active,
    )
    handles = list(root_handles)
    try:
        if (information.volume_serial_number, information.file_id) != expected_root_identity:
            raise ValueError(f"{context} trusted root handle is unsafe: {lexical_root}")
        for part in parts:
            child_handle = active.open_relative_file(
                handles[-1],
                part,
                create=False,
                directory=True,
                desired_access=(
                    _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
                ),
                share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            )
            handles.append(child_handle)
            information = active.information(child_handle)
            if (
                information.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                or not information.attributes & _FILE_ATTRIBUTE_DIRECTORY
            ):
                raise ValueError(f"{context} contains a reparse or non-directory component")
        verification_handles, verification_information = _open_windows_directory_chain(
            candidate,
            context=f"{context} verification",
            create_missing=False,
            backend=active,
        )
        try:
            if (
                verification_information.volume_serial_number,
                verification_information.file_id,
            ) != (information.volume_serial_number, information.file_id):
                raise ValueError(
                    f"{context} directory changed while its handle was opened: {candidate}"
                )
        finally:
            _close_windows_handles(active, verification_handles)
        return _WindowsPinnedDirectory(
            backend=active,
            handles=tuple(handles),
            information=information,
        )
    except BaseException:
        with contextlib.suppress(OSError):
            _close_windows_handles(active, tuple(handles))
        raise


def _open_windows_owned_entry(
    root: Path,
    path: Path,
    *,
    expected_root_identity: tuple[int, int],
    expected_identity: tuple[int, int] | None,
    directory: bool,
    create: bool,
    desired_access: int,
    share_access: int,
    context: str,
    backend: _WindowsLeaseBackend | None = None,
) -> _WindowsOwnedEntry:
    """Open one entry relative to an identity-bound, no-follow directory chain."""
    active = _WindowsNativeLeaseBackend() if backend is None else backend
    candidate = Path(os.path.abspath(path.expanduser()))
    parts = _lexical_relative_parts(root, candidate, context=context)
    if not parts:
        raise ValueError(f"{context} may not target the trusted root itself")
    parent = _open_windows_pinned_directory(
        root,
        candidate.parent,
        expected_root_identity=expected_root_identity,
        context=f"{context} parent",
        backend=active,
    )
    handle: int | None = None
    keep_open = False
    try:
        handle = active.open_relative_file(
            parent.handle,
            candidate.name,
            create=create,
            directory=directory,
            desired_access=desired_access,
            share_access=share_access,
        )
        information = active.information(handle)
        is_directory = bool(information.attributes & _FILE_ATTRIBUTE_DIRECTORY)
        if (
            information.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or is_directory != directory
            or (not directory and information.link_count != 1)
            or (
                expected_identity is not None
                and (
                    information.volume_serial_number,
                    information.file_id,
                )
                != expected_identity
            )
        ):
            raise ValueError(f"{context} entry handle is unsafe: {candidate}")
        verification_handle: int | None = None
        try:
            verification_handle = active.open_relative_file(
                parent.handle,
                candidate.name,
                create=False,
                directory=directory,
                desired_access=_FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            )
            verification = active.information(verification_handle)
            if (
                verification.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                or bool(verification.attributes & _FILE_ATTRIBUTE_DIRECTORY) != directory
                or (not directory and verification.link_count != 1)
                or (
                    verification.volume_serial_number,
                    verification.file_id,
                )
                != (information.volume_serial_number, information.file_id)
            ):
                raise ValueError(
                    f"{context} entry changed while its handle was opened: {candidate}"
                )
        except OSError as exc:
            raise ValueError(
                f"{context} entry changed while its handle was opened: {candidate}"
            ) from exc
        finally:
            if verification_handle is not None:
                active.close_handle(verification_handle)
        keep_open = True
        return _WindowsOwnedEntry(parent=parent, handle=handle, information=information)
    finally:
        if not keep_open:
            if create and handle is not None:
                with contextlib.suppress(OSError):
                    active.delete_file(handle)
            try:
                if handle is not None:
                    with contextlib.suppress(OSError):
                        active.close_handle(handle)
            finally:
                with contextlib.suppress(OSError):
                    _close_windows_handles(active, parent.handles)


@dataclass(frozen=True, slots=True)
class _PosixPinnedDirectory:
    handles: tuple[int, ...]

    @property
    def descriptor(self) -> int:
        return self.handles[-1]


def _close_posix_handles(handles: tuple[int, ...]) -> None:
    first_error: OSError | None = None
    for descriptor in reversed(handles):
        try:
            os.close(descriptor)
        except OSError as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def _open_posix_pinned_directory(
    root: Path,
    path: Path,
    *,
    expected_root_identity: tuple[int, int],
    context: str,
) -> _PosixPinnedDirectory:
    """Open a POSIX directory chain exclusively with no-follow relative opens."""
    lexical_root = Path(os.path.abspath(root.expanduser()))
    candidate = Path(os.path.abspath(path.expanduser()))
    parts = _lexical_relative_parts(lexical_root, candidate, context=context)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    handles = list(
        _open_posix_directory_chain(
            lexical_root,
            context=f"{context} trusted root",
        )
    )
    try:
        if _identity_tuple(os.fstat(handles[-1])) != expected_root_identity:
            raise ValueError(f"{context} trusted root changed while it was opened")
        for part in parts:
            descriptor = os.open(part, flags, dir_fd=handles[-1])
            handles.append(descriptor)
            opened = os.fstat(descriptor)
            if stat_result_is_link_like(opened) or not stat.S_ISDIR(opened.st_mode):
                raise ValueError(f"{context} contains a link-like directory component")
        opened = os.fstat(handles[-1])
        verification = _open_posix_directory_chain(candidate, context=f"{context} verification")
        try:
            if _identity_tuple(os.fstat(verification[-1])) != _identity_tuple(opened):
                raise ValueError(f"{context} directory changed while it was opened: {candidate}")
        finally:
            _close_posix_handles(verification)
        return _PosixPinnedDirectory(handles=tuple(handles))
    except BaseException:
        with contextlib.suppress(OSError):
            _close_posix_handles(tuple(handles))
        raise


def owned_directory_identity(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
) -> tuple[int, int]:
    """Open a directory below an identity-bound root and return its handle identity."""
    candidate = Path(os.path.abspath(path.expanduser()))
    if os.name == "nt":
        pinned = _open_windows_pinned_directory(
            root,
            candidate,
            expected_root_identity=root_identity,
            context=context,
        )
        try:
            return (
                pinned.information.volume_serial_number,
                pinned.information.file_id,
            )
        finally:
            _close_windows_handles(pinned.backend, pinned.handles)

    pinned = _open_posix_pinned_directory(
        root,
        candidate,
        expected_root_identity=root_identity,
        context=context,
    )
    try:
        return _identity_tuple(os.fstat(pinned.descriptor))
    finally:
        _close_posix_handles(pinned.handles)


def _validate_owned_entry_stat(
    result: os.stat_result,
    *,
    directory: bool,
    expected_identity: tuple[int, int] | None,
    context: str,
) -> None:
    if (
        stat_result_is_link_like(result)
        or stat.S_ISDIR(result.st_mode) != directory
        or (not directory and (not stat.S_ISREG(result.st_mode) or result.st_nlink != 1))
        or (expected_identity is not None and _identity_tuple(result) != expected_identity)
    ):
        raise ValueError(f"{context} entry is unsafe or changed")


def owned_entry_identity(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    directory: bool,
    context: str,
) -> tuple[int, int] | None:
    """Return an owned entry identity, ``None`` for absence, or reject unsafe state."""
    candidate = Path(os.path.abspath(path.expanduser()))
    _lexical_relative_parts(root, candidate, context=context)

    if os.name == "nt":
        try:
            entry = _open_windows_owned_entry(
                root,
                candidate,
                expected_root_identity=root_identity,
                expected_identity=None,
                directory=directory,
                create=False,
                desired_access=_FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                context=context,
            )
        except OSError as exc:
            if int(exc.errno or -1) not in {errno.ENOENT, 2, 3}:
                raise
            try:
                trusted = _open_windows_pinned_directory(
                    root,
                    root,
                    expected_root_identity=root_identity,
                    context=f"{context} missing-entry root check",
                )
            except (OSError, ValueError) as root_exc:
                raise ValueError(f"{context} trusted root became unsafe") from root_exc
            _close_windows_handles(trusted.backend, trusted.handles)
            return None
        try:
            return (
                entry.information.volume_serial_number,
                entry.information.file_id,
            )
        finally:
            try:
                entry.parent.backend.close_handle(entry.handle)
            finally:
                _close_windows_handles(entry.parent.backend, entry.parent.handles)

    try:
        parent = _open_posix_pinned_directory(
            root,
            candidate.parent,
            expected_root_identity=root_identity,
            context=f"{context} parent",
        )
    except FileNotFoundError:
        try:
            trusted = _open_posix_pinned_directory(
                root,
                root,
                expected_root_identity=root_identity,
                context=f"{context} missing-entry root check",
            )
        except (OSError, ValueError) as root_exc:
            raise ValueError(f"{context} trusted root became unsafe") from root_exc
        _close_posix_handles(trusted.handles)
        return None

    descriptor = -1
    try:
        try:
            before = os.stat(
                candidate.name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        _validate_owned_entry_stat(
            before,
            directory=directory,
            expected_identity=None,
            context=context,
        )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(candidate.name, flags, dir_fd=parent.descriptor)
        opened = os.fstat(descriptor)
        _validate_owned_entry_stat(
            opened,
            directory=directory,
            expected_identity=_identity_tuple(before),
            context=context,
        )
        try:
            after = os.stat(
                candidate.name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise ValueError(f"{context} changed while its identity was inspected") from exc
        _validate_owned_entry_stat(
            after,
            directory=directory,
            expected_identity=_identity_tuple(opened),
            context=context,
        )
        return _identity_tuple(opened)
    finally:
        try:
            if descriptor >= 0:
                os.close(descriptor)
        finally:
            _close_posix_handles(parent.handles)


def create_owned_directory(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
    exist_ok: bool = False,
) -> None:
    """Create one directory relative to an identity-bound trusted root."""
    candidate = Path(os.path.abspath(path.expanduser()))
    _lexical_relative_parts(root, candidate, context=context)
    if os.name == "nt":
        try:
            entry = _open_windows_owned_entry(
                root,
                candidate,
                expected_root_identity=root_identity,
                expected_identity=None,
                directory=True,
                create=True,
                desired_access=(
                    _FILE_LIST_DIRECTORY
                    | _FILE_TRAVERSE
                    | _FILE_READ_ATTRIBUTES
                    | _DELETE
                    | _SYNCHRONIZE
                ),
                share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                context=context,
            )
        except FileExistsError:
            if not exist_ok:
                raise
            entry = _open_windows_owned_entry(
                root,
                candidate,
                expected_root_identity=root_identity,
                expected_identity=None,
                directory=True,
                create=False,
                desired_access=_FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES,
                share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                context=context,
            )
        try:
            entry.parent.backend.close_handle(entry.handle)
        finally:
            _close_windows_handles(entry.parent.backend, entry.parent.handles)
        return

    parent = _open_posix_pinned_directory(
        root,
        candidate.parent,
        expected_root_identity=root_identity,
        context=f"{context} parent",
    )
    created = False
    try:
        try:
            prior = os.stat(candidate.name, dir_fd=parent.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            os.mkdir(candidate.name, 0o700, dir_fd=parent.descriptor)
            created = True
        else:
            if not exist_ok:
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), candidate)
            _validate_owned_entry_stat(
                prior, directory=True, expected_identity=None, context=context
            )
        observed = os.stat(candidate.name, dir_fd=parent.descriptor, follow_symlinks=False)
        _validate_owned_entry_stat(
            observed,
            directory=True,
            expected_identity=None,
            context=context,
        )
        os.fsync(parent.descriptor)
    except BaseException:
        if created:
            with contextlib.suppress(OSError):
                os.rmdir(candidate.name, dir_fd=parent.descriptor)
        raise
    finally:
        _close_posix_handles(parent.handles)


def _move_owned_path_windows(
    source: Path,
    destination: Path,
    *,
    source_root: Path,
    source_root_identity: tuple[int, int],
    destination_root: Path,
    destination_root_identity: tuple[int, int],
    expected_identity: tuple[int, int],
    directory: bool,
    context: str,
    backend: _WindowsLeaseBackend | None = None,
) -> None:
    """Move one entry using only identity-bound Windows directory handles."""
    active = _WindowsNativeLeaseBackend() if backend is None else backend
    opened = _open_windows_owned_entry(
        source_root,
        source,
        expected_root_identity=source_root_identity,
        expected_identity=expected_identity,
        directory=directory,
        create=False,
        desired_access=_DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        context=context,
        backend=active,
    )
    destination_parent: _WindowsPinnedDirectory | None = None
    moved = False
    body_succeeded = False
    try:
        destination_parent = _open_windows_pinned_directory(
            destination_root,
            destination.parent,
            expected_root_identity=destination_root_identity,
            context=f"{context} destination parent",
            backend=active,
        )
        active.rename_relative(
            opened.handle,
            destination_parent.handle,
            destination.name,
            replace=False,
        )
        moved = True
        try:
            verification_handle: int | None = None
            try:
                verification_handle = active.open_relative_file(
                    destination_parent.handle,
                    destination.name,
                    create=False,
                    directory=directory,
                    desired_access=_FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                    share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                )
                verification = active.information(verification_handle)
                if (
                    verification.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                    or bool(verification.attributes & _FILE_ATTRIBUTE_DIRECTORY) != directory
                    or (not directory and verification.link_count != 1)
                    or (verification.volume_serial_number, verification.file_id)
                    != (opened.information.volume_serial_number, opened.information.file_id)
                ):
                    raise ValueError(f"{context} destination changed during move: {destination}")
            finally:
                if verification_handle is not None:
                    active.close_handle(verification_handle)
        except Exception as exc:
            raise CommittedStateUncertainError(
                operation="move",
                path=destination,
                context=context,
                cause=exc,
            ) from exc
        body_succeeded = True
    finally:
        active_error = None if body_succeeded else sys.exception()
        finalization_errors: list[OSError] = []
        try:
            active.close_handle(opened.handle)
        except OSError as exc:
            finalization_errors.append(exc)
        try:
            _close_windows_handles(active, opened.parent.handles)
        except OSError as exc:
            finalization_errors.append(exc)
        if destination_parent is not None:
            try:
                _close_windows_handles(active, destination_parent.handles)
            except OSError as exc:
                finalization_errors.append(exc)
        _finish_mutation_finalization(
            finalization_errors,
            active_error=active_error,
            committed=moved,
            operation="move",
            path=destination,
            context=context,
        )


def move_owned_path(
    source: Path,
    destination: Path,
    *,
    source_root: Path,
    source_root_identity: tuple[int, int],
    destination_root: Path,
    destination_root_identity: tuple[int, int],
    expected_identity: tuple[int, int],
    directory: bool,
    context: str,
) -> None:
    """Move one unchanged entry without following any parent-directory link."""
    source_path = Path(os.path.abspath(source.expanduser()))
    destination_path = Path(os.path.abspath(destination.expanduser()))
    _lexical_relative_parts(source_root, source_path, context=context)
    _lexical_relative_parts(destination_root, destination_path, context=context)
    if os.name == "nt":
        _move_owned_path_windows(
            source_path,
            destination_path,
            source_root=source_root,
            source_root_identity=source_root_identity,
            destination_root=destination_root,
            destination_root_identity=destination_root_identity,
            expected_identity=expected_identity,
            directory=directory,
            context=context,
        )
        return

    source_parent = _open_posix_pinned_directory(
        source_root,
        source_path.parent,
        expected_root_identity=source_root_identity,
        context=f"{context} source parent",
    )
    destination_parent: _PosixPinnedDirectory | None = None
    moved = False
    body_succeeded = False
    try:
        before = os.stat(
            source_path.name,
            dir_fd=source_parent.descriptor,
            follow_symlinks=False,
        )
        _validate_owned_entry_stat(
            before,
            directory=directory,
            expected_identity=expected_identity,
            context=context,
        )
        destination_parent = _open_posix_pinned_directory(
            destination_root,
            destination_path.parent,
            expected_root_identity=destination_root_identity,
            context=f"{context} destination parent",
        )
        _publish_posix_noreplace(
            parent_descriptor=source_parent.descriptor,
            temporary_name=source_path.name,
            destination_name=destination_path.name,
            destination=destination_path,
            destination_parent_descriptor=destination_parent.descriptor,
        )
        moved = True
        try:
            after = os.stat(
                destination_path.name,
                dir_fd=destination_parent.descriptor,
                follow_symlinks=False,
            )
            _validate_owned_entry_stat(
                after,
                directory=directory,
                expected_identity=expected_identity,
                context=context,
            )
            os.fsync(source_parent.descriptor)
            if destination_parent.descriptor != source_parent.descriptor:
                os.fsync(destination_parent.descriptor)
        except Exception as exc:
            raise CommittedStateUncertainError(
                operation="move",
                path=destination_path,
                context=context,
                cause=exc,
            ) from exc
        body_succeeded = True
    finally:
        active_error = None if body_succeeded else sys.exception()
        finalization_errors: list[OSError] = []
        try:
            _close_posix_handles(source_parent.handles)
        except OSError as exc:
            finalization_errors.append(exc)
        if destination_parent is not None:
            try:
                _close_posix_handles(destination_parent.handles)
            except OSError as exc:
                finalization_errors.append(exc)
        _finish_mutation_finalization(
            finalization_errors,
            active_error=active_error,
            committed=moved,
            operation="move",
            path=destination_path,
            context=context,
        )


def _remove_owned_path_windows(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    expected_identity: tuple[int, int],
    directory: bool,
    context: str,
    backend: _WindowsLeaseBackend | None = None,
) -> None:
    """Remove one exact entry through its Windows handle."""
    opened = _open_windows_owned_entry(
        root,
        path,
        expected_root_identity=root_identity,
        expected_identity=expected_identity,
        directory=directory,
        create=False,
        desired_access=_DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        context=context,
        backend=backend,
    )
    deleted = False
    body_succeeded = False
    try:
        opened.parent.backend.delete_file(opened.handle)
        deleted = True
        body_succeeded = True
    finally:
        active_error = None if body_succeeded else sys.exception()
        finalization_errors: list[OSError] = []
        try:
            opened.parent.backend.close_handle(opened.handle)
        except OSError as exc:
            finalization_errors.append(exc)
        try:
            _close_windows_handles(opened.parent.backend, opened.parent.handles)
        except OSError as exc:
            finalization_errors.append(exc)
        _finish_mutation_finalization(
            finalization_errors,
            active_error=active_error,
            committed=deleted,
            operation="removal",
            path=path,
            context=context,
        )


def remove_owned_path(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    expected_identity: tuple[int, int],
    directory: bool,
    context: str,
    missing_ok: bool = False,
) -> None:
    """Remove one identity-bound entry without following parent-directory links."""
    candidate = Path(os.path.abspath(path.expanduser()))
    _lexical_relative_parts(root, candidate, context=context)
    if os.name == "nt":
        try:
            _remove_owned_path_windows(
                candidate,
                root=root,
                root_identity=root_identity,
                expected_identity=expected_identity,
                directory=directory,
                context=context,
            )
        except FileNotFoundError:
            if not missing_ok:
                raise
        return

    try:
        parent = _open_posix_pinned_directory(
            root,
            candidate.parent,
            expected_root_identity=root_identity,
            context=f"{context} parent",
        )
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    removed = False
    body_succeeded = False
    try:
        try:
            before = os.stat(candidate.name, dir_fd=parent.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                body_succeeded = True
                return
            raise
        _validate_owned_entry_stat(
            before,
            directory=directory,
            expected_identity=expected_identity,
            context=context,
        )
        if directory:
            os.rmdir(candidate.name, dir_fd=parent.descriptor)
        else:
            os.unlink(candidate.name, dir_fd=parent.descriptor)
        removed = True
        try:
            os.fsync(parent.descriptor)
        except OSError as exc:
            raise CommittedStateUncertainError(
                operation="removal",
                path=candidate,
                context=context,
                cause=exc,
            ) from exc
        body_succeeded = True
    finally:
        active_error = None if body_succeeded else sys.exception()
        finalization_errors: list[OSError] = []
        try:
            _close_posix_handles(parent.handles)
        except OSError as exc:
            finalization_errors.append(exc)
        _finish_mutation_finalization(
            finalization_errors,
            active_error=active_error,
            committed=removed,
            operation="removal",
            path=candidate,
            context=context,
        )


def _write_atomic_owned_regular_bytes_windows(
    destination: Path,
    payload: bytes,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
    replace: bool,
    backend: _WindowsLeaseBackend | None = None,
) -> None:
    """Atomically publish a file below one identity-bound Windows root."""
    active = _WindowsNativeLeaseBackend() if backend is None else backend
    parent = _open_windows_pinned_directory(
        root,
        destination.parent,
        expected_root_identity=root_identity,
        context=f"{context} parent",
        backend=active,
    )
    temporary_name = f".{destination.name}.{uuid4().hex}.publishing"
    native_handle: int | None = None
    descriptor: int | None = None
    published = False
    body_succeeded = False
    try:
        if replace:
            prior_handle: int | None = None
            try:
                prior_handle = active.open_relative_file(
                    parent.handle,
                    destination.name,
                    create=False,
                    directory=False,
                    desired_access=_FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                    share_access=(_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE),
                )
            except OSError as exc:
                if int(exc.errno or -1) not in {errno.ENOENT, 2, 3}:
                    raise
            if prior_handle is not None:
                try:
                    prior = active.information(prior_handle)
                    if (
                        prior.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                        or prior.attributes & _FILE_ATTRIBUTE_DIRECTORY
                        or prior.link_count != 1
                    ):
                        raise ValueError(
                            f"{context} destination must be a real non-linked file: {destination}"
                        )
                finally:
                    active.close_handle(prior_handle)
        native_handle = active.open_relative_file(
            parent.handle,
            temporary_name,
            create=True,
            directory=False,
            desired_access=_GENERIC_READ | _GENERIC_WRITE | _DELETE | _SYNCHRONIZE,
            share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        )
        information = active.information(native_handle)
        if (
            information.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or information.attributes & _FILE_ATTRIBUTE_DIRECTORY
            or information.link_count != 1
        ):
            raise ValueError(f"{context} temporary entry is unsafe")
        descriptor = active.adopt_file_handle(native_handle, flags=os.O_RDWR)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(f"short write while publishing {context}")
            view = view[written:]
        os.fsync(descriptor)
        active.rename_relative(
            native_handle,
            parent.handle,
            destination.name,
            replace=replace,
        )
        published = True
        try:
            verification_handle: int | None = None
            try:
                verification_handle = active.open_relative_file(
                    parent.handle,
                    destination.name,
                    create=False,
                    directory=False,
                    desired_access=_FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                    share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                )
                verification = active.information(verification_handle)
                if (
                    verification.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                    or verification.attributes & _FILE_ATTRIBUTE_DIRECTORY
                    or verification.link_count != 1
                    or (verification.volume_serial_number, verification.file_id)
                    != (information.volume_serial_number, information.file_id)
                ):
                    raise ValueError(
                        f"{context} destination changed during publication: {destination}"
                    )
            finally:
                if verification_handle is not None:
                    active.close_handle(verification_handle)
        except Exception as exc:
            raise CommittedStateUncertainError(
                operation="publication",
                path=destination,
                context=context,
                cause=exc,
            ) from exc
        body_succeeded = True
    except OSError as exc:
        if not replace and int(exc.errno or -1) in {errno.EEXIST, 80, 183}:
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), destination) from exc
        raise
    finally:
        active_error = None if body_succeeded else sys.exception()
        finalization_errors: list[OSError] = []
        if not published and native_handle is not None:
            try:
                active.delete_file(native_handle)
            except OSError as exc:
                finalization_errors.append(exc)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                finalization_errors.append(exc)
        elif native_handle is not None:
            try:
                active.close_handle(native_handle)
            except OSError as exc:
                finalization_errors.append(exc)
        try:
            _close_windows_handles(active, parent.handles)
        except OSError as exc:
            finalization_errors.append(exc)
        _finish_mutation_finalization(
            finalization_errors,
            active_error=active_error,
            committed=published,
            operation="publication",
            path=destination,
            context=context,
        )


def atomic_write_owned_regular_bytes(
    path: Path,
    payload: bytes,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
    replace: bool = True,
) -> None:
    """Atomically publish below an identity-bound root using relative syscalls only."""
    destination = Path(os.path.abspath(path.expanduser()))
    _lexical_relative_parts(root, destination, context=context)
    if os.name == "nt":
        _write_atomic_owned_regular_bytes_windows(
            destination,
            payload,
            root=root,
            root_identity=root_identity,
            context=context,
            replace=replace,
        )
        return

    parent = _open_posix_pinned_directory(
        root,
        destination.parent,
        expected_root_identity=root_identity,
        context=f"{context} parent",
    )
    temporary_name = f".{destination.name}.{uuid4().hex}.publishing"
    descriptor = -1
    published = False
    body_succeeded = False
    try:
        if replace:
            try:
                prior = os.stat(
                    destination.name,
                    dir_fd=parent.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                prior = None
            if prior is not None:
                _validate_owned_entry_stat(
                    prior,
                    directory=False,
                    expected_identity=None,
                    context=context,
                )
        descriptor = os.open(
            temporary_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent.descriptor,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(f"short write while publishing {context}")
            view = view[written:]
        os.fsync(descriptor)
        if replace:
            os.replace(
                temporary_name,
                destination.name,
                src_dir_fd=parent.descriptor,
                dst_dir_fd=parent.descriptor,
            )
        else:
            _publish_posix_noreplace(
                parent_descriptor=parent.descriptor,
                temporary_name=temporary_name,
                destination_name=destination.name,
                destination=destination,
            )
        published = True
        try:
            opened = os.fstat(descriptor)
            observed = os.stat(
                destination.name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
            _validate_owned_entry_stat(
                observed,
                directory=False,
                expected_identity=_identity_tuple(opened),
                context=context,
            )
            if _stable_file_fields(observed) != _stable_file_fields(
                opened
            ) or opened.st_size != len(payload):
                raise ValueError(f"{context} destination changed during publication: {destination}")
            os.fsync(parent.descriptor)
        except Exception as exc:
            raise CommittedStateUncertainError(
                operation="publication",
                path=destination,
                context=context,
                cause=exc,
            ) from exc
        body_succeeded = True
    finally:
        active_error = None if body_succeeded else sys.exception()
        finalization_errors: list[OSError] = []
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                finalization_errors.append(exc)
        if not published:
            try:
                os.unlink(temporary_name, dir_fd=parent.descriptor)
            except FileNotFoundError:
                pass
            except OSError as exc:
                finalization_errors.append(exc)
        try:
            _close_posix_handles(parent.handles)
        except OSError as exc:
            finalization_errors.append(exc)
        _finish_mutation_finalization(
            finalization_errors,
            active_error=active_error,
            committed=published,
            operation="publication",
            path=destination,
            context=context,
        )


def write_exclusive_owned_regular_bytes(
    path: Path,
    payload: bytes,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
) -> None:
    """Publish one new file below an identity-bound root without replacement."""
    atomic_write_owned_regular_bytes(
        path,
        payload,
        root=root,
        root_identity=root_identity,
        context=context,
        replace=False,
    )


def read_owned_regular_bytes(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
    max_bytes: int = _DEFAULT_MAX_RECORD_BYTES,
) -> bytes:
    """Read a bounded file through an identity-bound directory chain."""
    if max_bytes < 1:
        raise ValueError("maximum record size must be positive")
    candidate = Path(os.path.abspath(path.expanduser()))
    _lexical_relative_parts(root, candidate, context=context)
    if os.name == "nt":
        opened = _open_windows_owned_entry(
            root,
            candidate,
            expected_root_identity=root_identity,
            expected_identity=None,
            directory=False,
            create=False,
            desired_access=_GENERIC_READ | _SYNCHRONIZE,
            share_access=_FILE_SHARE_READ,
            context=context,
        )
        descriptor: int | None = None
        try:
            descriptor = opened.parent.backend.adopt_file_handle(
                opened.handle,
                flags=os.O_RDONLY,
            )
            before = os.fstat(descriptor)
            if before.st_size > max_bytes:
                raise ValueError(f"{context} exceeds the {max_bytes}-byte safety limit")
            chunks: list[bytes] = []
            observed = 0
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - observed))
                if not chunk:
                    break
                chunks.append(chunk)
                observed += len(chunk)
                if observed > max_bytes:
                    raise ValueError(f"{context} exceeds the {max_bytes}-byte safety limit")
            finished = os.fstat(descriptor)
            if _stable_file_fields(finished) != _stable_file_fields(before):
                raise ValueError(f"{context} changed while it was read: {candidate}")
            parent_after = candidate.parent.lstat()
            candidate_after = candidate.lstat()
            if (
                stat_result_is_link_like(parent_after)
                or not stat.S_ISDIR(parent_after.st_mode)
                or not _windows_identity_matches(opened.parent.information, parent_after)
                or not _windows_identity_matches(opened.information, candidate_after)
                or _stable_file_fields(candidate_after) != _stable_file_fields(before)
            ):
                raise ValueError(f"{context} path changed while it was read: {candidate}")
            payload = b"".join(chunks)
            if len(payload) != before.st_size:
                raise ValueError(f"{context} size changed while it was read: {candidate}")
            return payload
        finally:
            try:
                if descriptor is not None:
                    os.close(descriptor)
                else:
                    opened.parent.backend.close_handle(opened.handle)
            finally:
                _close_windows_handles(opened.parent.backend, opened.parent.handles)

    parent = _open_posix_pinned_directory(
        root,
        candidate.parent,
        expected_root_identity=root_identity,
        context=f"{context} parent",
    )
    descriptor = -1
    try:
        before = os.stat(candidate.name, dir_fd=parent.descriptor, follow_symlinks=False)
        _validate_owned_entry_stat(
            before,
            directory=False,
            expected_identity=None,
            context=context,
        )
        if before.st_size > max_bytes:
            raise ValueError(f"{context} exceeds the {max_bytes}-byte safety limit")
        descriptor = os.open(
            candidate.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
            dir_fd=parent.descriptor,
        )
        opened = os.fstat(descriptor)
        if _stable_file_fields(opened) != _stable_file_fields(before):
            raise ValueError(f"{context} changed while it was opened: {candidate}")
        chunks = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - observed))
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
            if observed > max_bytes:
                raise ValueError(f"{context} exceeds the {max_bytes}-byte safety limit")
        finished = os.fstat(descriptor)
        after = os.stat(candidate.name, dir_fd=parent.descriptor, follow_symlinks=False)
        if _stable_file_fields(finished) != _stable_file_fields(before) or _stable_file_fields(
            after
        ) != _stable_file_fields(before):
            raise ValueError(f"{context} changed while it was read: {candidate}")
        parent_after = candidate.parent.lstat()
        opened_parent = os.fstat(parent.descriptor)
        if (
            stat_result_is_link_like(parent_after)
            or not stat.S_ISDIR(parent_after.st_mode)
            or _identity_tuple(parent_after) != _identity_tuple(opened_parent)
        ):
            raise ValueError(f"{context} parent changed while it was read: {candidate.parent}")
        payload = b"".join(chunks)
        if len(payload) != before.st_size:
            raise ValueError(f"{context} size changed while it was read: {candidate}")
        return payload
    finally:
        try:
            if descriptor >= 0:
                os.close(descriptor)
        finally:
            _close_posix_handles(parent.handles)


@contextlib.contextmanager
def open_owned_regular_binary(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
    expected_identity: tuple[int, int] | None = None,
) -> Iterator[BinaryIO]:
    """Yield one pinned regular-file stream opened below an identity-bound root."""
    candidate = Path(os.path.abspath(path.expanduser()))
    _lexical_relative_parts(root, candidate, context=context)
    descriptor = -1
    if os.name == "nt":
        opened = _open_windows_owned_entry(
            root,
            candidate,
            expected_root_identity=root_identity,
            expected_identity=expected_identity,
            directory=False,
            create=False,
            desired_access=_GENERIC_READ | _SYNCHRONIZE,
            share_access=_FILE_SHARE_READ,
            context=context,
        )
        adopted = False
        parents_closed = False
        try:
            descriptor = opened.parent.backend.adopt_file_handle(
                opened.handle,
                flags=os.O_RDONLY,
            )
            adopted = True
            metadata = os.fstat(descriptor)
            _validate_owned_entry_stat(
                metadata,
                directory=False,
                expected_identity=expected_identity,
                context=context,
            )
            _close_windows_handles(opened.parent.backend, opened.parent.handles)
            parents_closed = True
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                yield stream
        finally:
            try:
                if descriptor >= 0:
                    os.close(descriptor)
                elif not adopted:
                    with contextlib.suppress(OSError):
                        opened.parent.backend.close_handle(opened.handle)
            finally:
                if not parents_closed:
                    with contextlib.suppress(OSError):
                        _close_windows_handles(opened.parent.backend, opened.parent.handles)
        return

    parent = _open_posix_pinned_directory(
        root,
        candidate.parent,
        expected_root_identity=root_identity,
        context=f"{context} parent",
    )
    parents_closed = False
    try:
        before = os.stat(candidate.name, dir_fd=parent.descriptor, follow_symlinks=False)
        _validate_owned_entry_stat(
            before,
            directory=False,
            expected_identity=expected_identity,
            context=context,
        )
        descriptor = os.open(
            candidate.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
            dir_fd=parent.descriptor,
        )
        opened = os.fstat(descriptor)
        if _stable_file_fields(opened) != _stable_file_fields(before):
            raise ValueError(f"{context} changed while it was opened: {candidate}")
        _close_posix_handles(parent.handles)
        parents_closed = True
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            yield stream
    finally:
        try:
            if descriptor >= 0:
                os.close(descriptor)
        finally:
            if not parents_closed:
                with contextlib.suppress(OSError):
                    _close_posix_handles(parent.handles)


def _open_exclusive_owned_regular_descriptor(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
) -> int:
    candidate = Path(os.path.abspath(path.expanduser()))
    _lexical_relative_parts(root, candidate, context=context)
    if os.name == "nt":
        opened = _open_windows_owned_entry(
            root,
            candidate,
            expected_root_identity=root_identity,
            expected_identity=None,
            directory=False,
            create=True,
            desired_access=_GENERIC_READ | _GENERIC_WRITE | _SYNCHRONIZE,
            share_access=_FILE_SHARE_READ,
            context=context,
        )
        descriptor: int | None = None
        try:
            descriptor = opened.parent.backend.adopt_file_handle(
                opened.handle,
                flags=os.O_RDWR,
            )
            _close_windows_handles(opened.parent.backend, opened.parent.handles)
            return descriptor
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            else:
                with contextlib.suppress(OSError):
                    opened.parent.backend.delete_file(opened.handle)
                with contextlib.suppress(OSError):
                    opened.parent.backend.close_handle(opened.handle)
            with contextlib.suppress(OSError):
                _close_windows_handles(opened.parent.backend, opened.parent.handles)
            raise

    parent = _open_posix_pinned_directory(
        root,
        candidate.parent,
        expected_root_identity=root_identity,
        context=f"{context} parent",
    )
    descriptor = -1
    try:
        descriptor = os.open(
            candidate.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent.descriptor,
        )
        opened = os.fstat(descriptor)
        _validate_owned_entry_stat(
            opened,
            directory=False,
            expected_identity=None,
            context=context,
        )
        _close_posix_handles(parent.handles)
        return descriptor
    except BaseException:
        try:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(OSError):
                os.unlink(candidate.name, dir_fd=parent.descriptor)
        finally:
            with contextlib.suppress(OSError):
                _close_posix_handles(parent.handles)
        raise


@contextlib.contextmanager
def open_exclusive_owned_regular_binary(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
) -> Iterator[BinaryIO]:
    """Yield a new binary stream created below an identity-bound root."""
    descriptor = _open_exclusive_owned_regular_descriptor(
        path,
        root=root,
        root_identity=root_identity,
        context=context,
    )
    try:
        with os.fdopen(descriptor, "w+b", closefd=True) as stream:
            descriptor = -1
            yield stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class _WindowsRelativeOpen:
    backend: _WindowsLeaseBackend
    descriptor: int
    native_handle: int
    parent_handle: int
    information: _WindowsFileInformation
    parent_information: _WindowsFileInformation


def _open_windows_relative_regular(
    candidate: Path,
    *,
    context: str,
    create: bool | None,
    desired_access: int,
    share_access: int,
    descriptor_flags: int,
    delete_created_on_failure: bool = False,
    backend: _WindowsLeaseBackend | None = None,
) -> _WindowsRelativeOpen:
    """Open one real regular file relative to a pinned real Windows parent."""
    active = _WindowsNativeLeaseBackend() if backend is None else backend
    parent = candidate.parent
    parent_before = parent.lstat()
    if stat_result_is_link_like(parent_before) or not stat.S_ISDIR(parent_before.st_mode):
        raise RuntimeError(f"{context} parent is link-like or not a directory: {parent}")
    try:
        before = candidate.lstat()
    except FileNotFoundError:
        if create is False:
            raise
        before = None
        should_create = True
    else:
        if create is True:
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), candidate)
        should_create = False
        if stat_result_is_link_like(before) or not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"{context} path is link-like or not regular: {candidate}")
        if before.st_nlink != 1:
            raise RuntimeError(f"{context} path is hard-linked: {candidate}")
    parent_confirm = parent.lstat()
    if (
        stat_result_is_link_like(parent_confirm)
        or not stat.S_ISDIR(parent_confirm.st_mode)
        or (parent_confirm.st_dev, parent_confirm.st_ino)
        != (parent_before.st_dev, parent_before.st_ino)
    ):
        raise RuntimeError(f"{context} parent changed during inspection: {parent}")

    parent_handle: int | None = None
    child_handle: int | None = None
    native_handle: int | None = None
    descriptor: int | None = None
    keep_open = False
    try:
        parent_handle = active.open_parent(parent)
        parent_information = active.information(parent_handle)
        if (
            parent_information.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or not parent_information.attributes & _FILE_ATTRIBUTE_DIRECTORY
            or not _windows_identity_matches(parent_information, parent_before)
        ):
            raise RuntimeError(f"{context} parent changed or is a reparse point: {parent}")
        child_handle = active.open_relative_file(
            parent_handle,
            candidate.name,
            create=should_create,
            desired_access=desired_access,
            share_access=share_access,
        )
        native_handle = child_handle
        child_information = active.information(child_handle)
        if (
            child_information.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or child_information.attributes & _FILE_ATTRIBUTE_DIRECTORY
            or child_information.link_count != 1
        ):
            raise RuntimeError(f"{context} is link-like, non-regular, or hard-linked: {candidate}")
        if before is not None and not _windows_identity_matches(child_information, before):
            raise RuntimeError(f"{context} changed before its relative handle opened: {candidate}")
        parent_after = parent.lstat()
        if (
            stat_result_is_link_like(parent_after)
            or not stat.S_ISDIR(parent_after.st_mode)
            or not _windows_identity_matches(parent_information, parent_after)
        ):
            raise RuntimeError(f"{context} parent changed during relative open: {parent}")
        candidate_after = candidate.lstat()
        if (
            stat_result_is_link_like(candidate_after)
            or not stat.S_ISREG(candidate_after.st_mode)
            or candidate_after.st_nlink != 1
            or not _windows_identity_matches(child_information, candidate_after)
        ):
            raise RuntimeError(f"{context} path changed during relative open: {candidate}")
        descriptor = active.adopt_file_handle(child_handle, flags=descriptor_flags)
        child_handle = None
        opened = os.fstat(descriptor)
        if (
            stat_result_is_link_like(opened)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not _windows_identity_matches(child_information, opened)
        ):
            raise RuntimeError(f"{context} descriptor identity is unsafe: {candidate}")
        keep_open = True
        return _WindowsRelativeOpen(
            backend=active,
            descriptor=descriptor,
            native_handle=native_handle,
            parent_handle=parent_handle,
            information=child_information,
            parent_information=parent_information,
        )
    finally:
        if not keep_open:
            cleanup_handle = native_handle if native_handle is not None else child_handle
            if delete_created_on_failure and should_create and cleanup_handle is not None:
                with contextlib.suppress(OSError):
                    active.delete_file(cleanup_handle)
            try:
                if descriptor is not None:
                    os.close(descriptor)
                elif child_handle is not None:
                    with contextlib.suppress(OSError):
                        active.close_handle(child_handle)
            finally:
                if parent_handle is not None:
                    with contextlib.suppress(OSError):
                        active.close_handle(parent_handle)


def _open_windows_lease_relative(
    candidate: Path,
    *,
    backend: _WindowsLeaseBackend | None = None,
) -> tuple[int, int]:
    """Safely open/create one lease relative to a pinned real parent handle."""
    try:
        opened = _open_windows_relative_regular(
            candidate,
            context="Web manager lease",
            create=None,
            desired_access=_GENERIC_READ | _GENERIC_WRITE | _SYNCHRONIZE,
            share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
            descriptor_flags=os.O_RDWR,
            backend=backend,
        )
    except OSError as exc:
        error = int(exc.errno or -1)
        if error in {errno.EEXIST, 80, 183}:
            detail = "appeared during exclusive creation"
        elif error in {2, 3}:
            detail = "disappeared before the existing file could be opened"
        elif error in {5, 32, 33}:
            detail = "could not be safely opened because access or sharing changed"
        else:
            detail = f"could not be safely opened (Windows error {error})"
        raise RuntimeError(f"Web manager lease {detail}: {candidate}") from exc
    return opened.descriptor, opened.parent_handle


def canonical_json_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    """Return the canonical JSON encoding used by durable Web records."""
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _stable_file_fields(result: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        result.st_mode,
        result.st_dev,
        result.st_ino,
        result.st_nlink,
        result.st_size,
        result.st_mtime_ns,
    )


def _read_stable_regular_bytes_windows(
    candidate: Path,
    before: os.stat_result,
    *,
    context: str,
    max_bytes: int,
    backend: _WindowsLeaseBackend | None = None,
) -> bytes:
    """Read one Windows file through a pinned parent and relative no-follow handle."""
    try:
        opened_file = _open_windows_relative_regular(
            candidate,
            context=context,
            create=False,
            desired_access=_GENERIC_READ | _SYNCHRONIZE,
            share_access=_FILE_SHARE_READ,
            descriptor_flags=os.O_RDONLY,
            backend=backend,
        )
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{context} is unreadable without following links: {candidate}") from exc

    chunks: list[bytes] = []
    observed = 0
    try:
        opened = os.fstat(opened_file.descriptor)
        if _stable_file_fields(opened) != _stable_file_fields(before):
            raise ValueError(f"{context} changed while it was opened: {candidate}")
        while True:
            chunk = os.read(
                opened_file.descriptor,
                min(1024 * 1024, max_bytes + 1 - observed),
            )
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
            if observed > max_bytes:
                raise ValueError(
                    f"{context} exceeds the {max_bytes}-byte safety limit: {candidate}"
                )
        finished = os.fstat(opened_file.descriptor)
        parent_after = candidate.parent.lstat()
        if (
            stat_result_is_link_like(parent_after)
            or not stat.S_ISDIR(parent_after.st_mode)
            or not _windows_identity_matches(opened_file.parent_information, parent_after)
        ):
            raise ValueError(f"{context} parent changed while it was read: {candidate.parent}")
        after = candidate.lstat()
        if (
            stat_result_is_link_like(after)
            or _stable_file_fields(finished) != _stable_file_fields(before)
            or _stable_file_fields(after) != _stable_file_fields(before)
        ):
            raise ValueError(f"{context} changed while it was read: {candidate}")
    except OSError as exc:
        raise ValueError(f"{context} is unreadable without following links: {candidate}") from exc
    finally:
        try:
            os.close(opened_file.descriptor)
        finally:
            with contextlib.suppress(OSError):
                opened_file.backend.close_handle(opened_file.parent_handle)

    payload = b"".join(chunks)
    if len(payload) != before.st_size:
        raise ValueError(f"{context} size changed while it was read: {candidate}")
    return payload


def read_stable_regular_bytes(
    path: Path,
    *,
    context: str,
    max_bytes: int = _DEFAULT_MAX_RECORD_BYTES,
) -> bytes:
    """Read a bounded regular file without following links or accepting path races."""
    if max_bytes < 1:
        raise ValueError("maximum record size must be positive")
    candidate = Path(os.path.abspath(path.expanduser()))
    try:
        before = candidate.lstat()
    except OSError as exc:
        raise ValueError(f"{context} is missing or unreadable: {candidate}") from exc
    if stat_result_is_link_like(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{context} must be a real regular file: {candidate}")
    if before.st_nlink != 1:
        raise ValueError(f"{context} must not be hard-linked: {candidate}")
    if before.st_size > max_bytes:
        raise ValueError(f"{context} exceeds the {max_bytes}-byte safety limit: {candidate}")

    if os.name == "nt":
        return _read_stable_regular_bytes_windows(
            candidate,
            before,
            context=context,
            max_bytes=max_bytes,
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(candidate, flags)
        opened = os.fstat(descriptor)
        if (
            stat_result_is_link_like(opened)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _stable_file_fields(opened) != _stable_file_fields(before)
        ):
            raise ValueError(f"{context} changed while it was opened: {candidate}")
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - observed))
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
            if observed > max_bytes:
                raise ValueError(
                    f"{context} exceeds the {max_bytes}-byte safety limit: {candidate}"
                )
        finished = os.fstat(descriptor)
    except OSError as exc:
        raise ValueError(f"{context} is unreadable without following links: {candidate}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    try:
        after = candidate.lstat()
    except OSError as exc:
        raise ValueError(f"{context} changed while it was read: {candidate}") from exc
    if (
        stat_result_is_link_like(after)
        or _stable_file_fields(finished) != _stable_file_fields(before)
        or _stable_file_fields(after) != _stable_file_fields(before)
    ):
        raise ValueError(f"{context} changed while it was read: {candidate}")
    payload = b"".join(chunks)
    if len(payload) != before.st_size:
        raise ValueError(f"{context} size changed while it was read: {candidate}")
    return payload


def read_canonical_model(
    path: Path,
    model_type: type[_ModelT],
    *,
    context: str,
    max_bytes: int = _DEFAULT_MAX_RECORD_BYTES,
) -> _ModelT:
    """Read, validate, and require canonical bytes for one Pydantic Web record."""
    payload = read_stable_regular_bytes(path, context=context, max_bytes=max_bytes)
    result = model_type.model_validate_json(payload)
    if payload != canonical_json_bytes(result):
        raise ValueError(f"{context} is not canonical: {path}")
    return result


def _publish_posix_noreplace(
    *,
    parent_descriptor: int,
    temporary_name: str,
    destination_name: str,
    destination: Path,
    destination_parent_descriptor: int | None = None,
) -> None:
    """Use an atomic POSIX no-replace rename or fail closed."""
    target_parent = (
        parent_descriptor
        if destination_parent_descriptor is None
        else destination_parent_descriptor
    )
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise RuntimeError("renameat2 is unavailable for safe exclusive publication")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            parent_descriptor,
            os.fsencode(temporary_name),
            target_parent,
            os.fsencode(destination_name),
            1,
        )
    elif sys.platform == "darwin":
        renameatx_np = getattr(libc, "renameatx_np", None)
        if renameatx_np is None:
            raise RuntimeError("renameatx_np is unavailable for safe exclusive publication")
        renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx_np.restype = ctypes.c_int
        result = renameatx_np(
            parent_descriptor,
            os.fsencode(temporary_name),
            target_parent,
            os.fsencode(destination_name),
            0x00000004,
        )
    else:
        raise RuntimeError(
            "this POSIX platform lacks a verified atomic no-replace publication primitive"
        )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), destination)
    raise OSError(error, os.strerror(error), destination)


def _write_atomic_regular_bytes_windows(
    destination: Path,
    payload: bytes,
    *,
    context: str,
    replace: bool,
    backend: _WindowsLeaseBackend | None = None,
) -> None:
    """Publish via a pinned Windows parent and a handle-relative atomic rename."""
    temporary = destination.parent / f".{destination.name}.{uuid4().hex}.publishing"
    if replace:
        try:
            prior = destination.lstat()
        except FileNotFoundError:
            prior = None
        if prior is not None and (
            stat_result_is_link_like(prior)
            or not stat.S_ISREG(prior.st_mode)
            or prior.st_nlink != 1
        ):
            raise ValueError(f"{context} destination must be a real non-linked file: {destination}")
    try:
        opened_file = _open_windows_relative_regular(
            temporary,
            context=f"{context} temporary",
            create=True,
            desired_access=_GENERIC_READ | _GENERIC_WRITE | _DELETE | _SYNCHRONIZE,
            share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            descriptor_flags=os.O_RDWR,
            delete_created_on_failure=True,
            backend=backend,
        )
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{context} temporary could not be safely created: {temporary}") from exc

    published = False
    try:
        view = memoryview(payload)
        while view:
            written = os.write(opened_file.descriptor, view)
            if written <= 0:
                raise OSError(f"short write while publishing {context}")
            view = view[written:]
        os.fsync(opened_file.descriptor)
        opened_file.backend.rename_relative(
            opened_file.native_handle,
            opened_file.parent_handle,
            destination.name,
            replace=replace,
        )
        published = True
        parent_after = destination.parent.lstat()
        if (
            stat_result_is_link_like(parent_after)
            or not stat.S_ISDIR(parent_after.st_mode)
            or not _windows_identity_matches(opened_file.parent_information, parent_after)
        ):
            raise ValueError(f"{context} parent changed during publication: {destination.parent}")
        destination_after = destination.lstat()
        if (
            stat_result_is_link_like(destination_after)
            or not stat.S_ISREG(destination_after.st_mode)
            or destination_after.st_nlink != 1
            or not _windows_identity_matches(opened_file.information, destination_after)
        ):
            raise ValueError(f"{context} destination changed during publication: {destination}")
    except OSError as exc:
        error = int(exc.errno or -1)
        if not replace and error in {errno.EEXIST, 80, 183}:
            raise FileExistsError(error, os.strerror(error), destination) from exc
        raise
    finally:
        if not published:
            with contextlib.suppress(OSError):
                opened_file.backend.delete_file(opened_file.native_handle)
        try:
            os.close(opened_file.descriptor)
        finally:
            with contextlib.suppress(OSError):
                opened_file.backend.close_handle(opened_file.parent_handle)


def atomic_write_regular_bytes(
    path: Path,
    payload: bytes,
    *,
    context: str,
    replace: bool = True,
) -> None:
    """Atomically publish a complete durable regular file within one pinned parent."""
    destination = Path(os.path.abspath(path.expanduser()))
    parent = destination.parent
    try:
        parent_before = parent.lstat()
    except OSError as exc:
        raise ValueError(f"{context} parent is missing or unreadable: {parent}") from exc
    if stat_result_is_link_like(parent_before) or not stat.S_ISDIR(parent_before.st_mode):
        raise ValueError(f"{context} parent must be a real directory: {parent}")
    if os.name == "nt":
        _write_atomic_regular_bytes_windows(
            destination,
            payload,
            context=context,
            replace=replace,
        )
        return

    temporary_name = f".{destination.name}.{uuid4().hex}.publishing"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    parent_descriptor = -1
    published = False
    try:
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_parent = os.fstat(parent_descriptor)
        if _stable_file_fields(opened_parent) != _stable_file_fields(parent_before):
            raise ValueError(f"{context} parent changed while it was opened: {parent}")
        if replace:
            try:
                prior = os.stat(destination.name, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                prior = None
            if prior is not None and (
                stat_result_is_link_like(prior)
                or not stat.S_ISREG(prior.st_mode)
                or prior.st_nlink != 1
            ):
                raise ValueError(
                    f"{context} destination must be a real non-linked file: {destination}"
                )
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_descriptor)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(f"short write while publishing {context}")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if replace:
            os.replace(
                temporary_name,
                destination.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        else:
            _publish_posix_noreplace(
                parent_descriptor=parent_descriptor,
                temporary_name=temporary_name,
                destination_name=destination.name,
                destination=destination,
            )
        published = True
        os.fsync(parent_descriptor)
    finally:
        try:
            if descriptor >= 0:
                os.close(descriptor)
        finally:
            try:
                if not published and parent_descriptor >= 0:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(temporary_name, dir_fd=parent_descriptor)
            finally:
                if parent_descriptor >= 0:
                    os.close(parent_descriptor)
    try:
        parent_after = parent.lstat()
    except OSError as exc:
        raise ValueError(f"{context} parent changed after publication: {parent}") from exc
    if (
        stat_result_is_link_like(parent_after)
        or not stat.S_ISDIR(parent_after.st_mode)
        or parent_after.st_dev != parent_before.st_dev
        or parent_after.st_ino != parent_before.st_ino
    ):
        raise ValueError(f"{context} parent changed during publication: {parent}")


def write_exclusive_regular_bytes(path: Path, payload: bytes, *, context: str) -> None:
    """Atomically publish a complete durable file without replacing any path."""
    atomic_write_regular_bytes(path, payload, context=context, replace=False)


def _open_exclusive_regular_descriptor(path: Path, *, context: str) -> int:
    """Create one new regular file without following a path race."""
    candidate = Path(os.path.abspath(path.expanduser()))
    parent = candidate.parent
    try:
        parent_before = parent.lstat()
    except OSError as exc:
        raise ValueError(f"{context} parent is missing or unreadable: {parent}") from exc
    if stat_result_is_link_like(parent_before) or not stat.S_ISDIR(parent_before.st_mode):
        raise ValueError(f"{context} parent must be a real directory: {parent}")
    if os.name == "nt":
        try:
            opened_file = _open_windows_relative_regular(
                candidate,
                context=context,
                create=True,
                desired_access=_GENERIC_READ | _GENERIC_WRITE | _SYNCHRONIZE,
                share_access=_FILE_SHARE_READ,
                descriptor_flags=os.O_WRONLY,
                delete_created_on_failure=True,
            )
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"{context} could not be safely created: {candidate}") from exc
        try:
            opened_file.backend.close_handle(opened_file.parent_handle)
        except BaseException:
            os.close(opened_file.descriptor)
            raise
        return opened_file.descriptor

    parent_descriptor = -1
    descriptor = -1
    keep_file = False
    try:
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_parent = os.fstat(parent_descriptor)
        if _stable_file_fields(opened_parent) != _stable_file_fields(parent_before):
            raise ValueError(f"{context} parent changed while it was opened: {parent}")
        descriptor = os.open(
            candidate.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        if (
            stat_result_is_link_like(opened)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            raise ValueError(f"{context} is not a real non-linked regular file: {candidate}")
        parent_after = parent.lstat()
        if (
            stat_result_is_link_like(parent_after)
            or not stat.S_ISDIR(parent_after.st_mode)
            or (parent_after.st_dev, parent_after.st_ino)
            != (opened_parent.st_dev, opened_parent.st_ino)
        ):
            raise ValueError(f"{context} parent changed during creation: {parent}")
        candidate_after = candidate.lstat()
        if (
            stat_result_is_link_like(candidate_after)
            or not stat.S_ISREG(candidate_after.st_mode)
            or candidate_after.st_nlink != 1
            or (candidate_after.st_dev, candidate_after.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError(f"{context} path changed during creation: {candidate}")
        keep_file = True
        return descriptor
    finally:
        if keep_file:
            if parent_descriptor >= 0:
                try:
                    os.close(parent_descriptor)
                except BaseException:
                    if descriptor >= 0:
                        os.close(descriptor)
                    raise
        else:
            try:
                if descriptor >= 0:
                    os.close(descriptor)
            finally:
                try:
                    if parent_descriptor >= 0:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(candidate.name, dir_fd=parent_descriptor)
                finally:
                    if parent_descriptor >= 0:
                        os.close(parent_descriptor)


@contextlib.contextmanager
def open_exclusive_regular_binary(path: Path, *, context: str) -> Iterator[BinaryIO]:
    """Yield a new binary output stream backed by a safely created regular file."""
    descriptor = _open_exclusive_regular_descriptor(path, context=context)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            yield stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_owned_regular_readwrite_descriptor(
    path: Path,
    *,
    root: Path,
    root_identity: tuple[int, int],
    context: str,
) -> int:
    """Open or exclusively create one read/write file below a pinned root."""
    candidate = Path(os.path.abspath(path.expanduser()))
    _lexical_relative_parts(root, candidate, context=context)
    if os.name == "nt":
        try:
            opened = _open_windows_owned_entry(
                root,
                candidate,
                expected_root_identity=root_identity,
                expected_identity=None,
                directory=False,
                create=False,
                desired_access=_GENERIC_READ | _GENERIC_WRITE | _SYNCHRONIZE,
                share_access=_FILE_SHARE_READ,
                context=context,
            )
        except FileNotFoundError:
            try:
                opened = _open_windows_owned_entry(
                    root,
                    candidate,
                    expected_root_identity=root_identity,
                    expected_identity=None,
                    directory=False,
                    create=True,
                    desired_access=_GENERIC_READ | _GENERIC_WRITE | _SYNCHRONIZE,
                    share_access=_FILE_SHARE_READ,
                    context=context,
                )
            except FileExistsError as exc:
                raise RuntimeError(f"{context} appeared during acquisition: {candidate}") from exc
        descriptor: int | None = None
        try:
            descriptor = opened.parent.backend.adopt_file_handle(
                opened.handle,
                flags=os.O_RDWR,
            )
            _close_windows_handles(opened.parent.backend, opened.parent.handles)
            return descriptor
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            else:
                with contextlib.suppress(OSError):
                    opened.parent.backend.close_handle(opened.handle)
            with contextlib.suppress(OSError):
                _close_windows_handles(opened.parent.backend, opened.parent.handles)
            raise

    parent = _open_posix_pinned_directory(
        root,
        candidate.parent,
        expected_root_identity=root_identity,
        context=f"{context} parent",
    )
    descriptor = -1
    try:
        try:
            before = os.stat(candidate.name, dir_fd=parent.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            before = None
        else:
            _validate_owned_entry_stat(
                before,
                directory=False,
                expected_identity=None,
                context=context,
            )
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if before is None:
            flags |= os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(candidate.name, flags, 0o600, dir_fd=parent.descriptor)
        except FileExistsError as exc:
            raise RuntimeError(f"{context} appeared during acquisition: {candidate}") from exc
        opened = os.fstat(descriptor)
        _validate_owned_entry_stat(
            opened,
            directory=False,
            expected_identity=None,
            context=context,
        )
        if before is not None and _stable_file_fields(opened) != _stable_file_fields(before):
            raise RuntimeError(f"{context} changed while it was opened: {candidate}")
        _close_posix_handles(parent.handles)
        return descriptor
    except BaseException:
        try:
            if descriptor >= 0:
                os.close(descriptor)
        finally:
            with contextlib.suppress(OSError):
                _close_posix_handles(parent.handles)
        raise


@dataclass
class WebManagerLease:
    """Lifetime OS file lock proving exclusive ownership of one Web state root."""

    path: Path
    descriptor: int
    windows_parent_handle: int | None = None

    @classmethod
    def acquire(
        cls,
        path: Path,
        metadata: dict[str, Any],
        *,
        root: Path | None = None,
        root_identity: tuple[int, int] | None = None,
    ) -> WebManagerLease:
        """Acquire a non-blocking lock and publish owner metadata."""
        if (root is None) != (root_identity is None):
            raise ValueError("an anchored Web manager lease requires both root and root identity")
        candidate = Path(os.path.abspath(path.expanduser()))
        descriptor = -1
        windows_parent_handle: int | None = None
        before: os.stat_result | None = None
        anchored = root is not None and root_identity is not None
        if anchored:
            assert root is not None
            assert root_identity is not None
            try:
                descriptor = _open_owned_regular_readwrite_descriptor(
                    candidate,
                    root=root,
                    root_identity=root_identity,
                    context="Web manager lease",
                )
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"Web manager lease could not be opened safely: {candidate}"
                ) from exc
        elif os.name == "nt":
            descriptor, windows_parent_handle = _open_windows_lease_relative(candidate)
        else:
            try:
                before = candidate.lstat()
            except FileNotFoundError:
                before = None
            else:
                if stat_result_is_link_like(before) or not stat.S_ISREG(before.st_mode):
                    raise RuntimeError(
                        f"Web manager lease path is link-like or not regular: {candidate}"
                    )
                if before.st_nlink != 1:
                    raise RuntimeError(f"Web manager lease path is hard-linked: {candidate}")
            flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            if before is None:
                flags |= os.O_CREAT | os.O_EXCL
            try:
                descriptor = os.open(candidate, flags, 0o600)
            except FileExistsError as exc:
                raise RuntimeError(
                    f"Web manager lease path appeared during acquisition: {candidate}"
                ) from exc
        acquired = False
        try:
            os.set_inheritable(descriptor, False)
            opened = os.fstat(descriptor)
            if stat_result_is_link_like(opened) or not stat.S_ISREG(opened.st_mode):
                raise RuntimeError(f"Web manager lease is not a real regular file: {candidate}")
            if opened.st_nlink != 1:
                raise RuntimeError(f"Web manager lease is hard-linked: {candidate}")
            if before is not None and (
                opened.st_dev != before.st_dev or opened.st_ino != before.st_ino
            ):
                raise RuntimeError(
                    f"Web manager lease changed before its handle was opened: {candidate}"
                )
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise RuntimeError(
                        "another TopoForge Web manager owns the state directory: "
                        f"{candidate.parent}"
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise RuntimeError(
                        "another TopoForge Web manager owns the state directory: "
                        f"{candidate.parent}"
                    ) from exc
            acquired = True
            payload = canonical_json_bytes(metadata)
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while publishing Web manager lease metadata")
                view = view[written:]
            os.fsync(descriptor)
            if anchored:
                try:
                    assert root is not None
                    assert root_identity is not None
                    with open_owned_regular_binary(
                        candidate,
                        root=root,
                        root_identity=root_identity,
                        context="Web manager lease verification",
                        expected_identity=_identity_tuple(opened),
                    ) as verification:
                        verified = os.fstat(verification.fileno())
                    if _identity_tuple(verified) != _identity_tuple(opened):
                        raise RuntimeError(
                            f"Web manager lease path changed during acquisition: {candidate}"
                        )
                except (OSError, ValueError) as exc:
                    raise RuntimeError(
                        f"Web manager lease path changed during acquisition: {candidate}"
                    ) from exc
            else:
                after = candidate.lstat()
                if (
                    stat_result_is_link_like(after)
                    or not stat.S_ISREG(after.st_mode)
                    or after.st_nlink != 1
                    or after.st_dev != opened.st_dev
                    or after.st_ino != opened.st_ino
                ):
                    raise RuntimeError(
                        f"Web manager lease path changed during acquisition: {candidate}"
                    )
            return cls(
                path=candidate,
                descriptor=descriptor,
                windows_parent_handle=windows_parent_handle,
            )
        except BaseException:
            if acquired:
                if os.name == "nt":
                    with contextlib.suppress(OSError):
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    with contextlib.suppress(OSError):
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            try:
                os.close(descriptor)
            finally:
                if windows_parent_handle is not None:
                    with contextlib.suppress(OSError):
                        _WindowsNativeLeaseBackend().close_handle(windows_parent_handle)
            raise

    def release(self) -> None:
        """Release this manager's lifetime lease; the evidence file remains durable."""
        descriptor = self.descriptor
        parent_handle = self.windows_parent_handle
        self.descriptor = -1
        self.windows_parent_handle = None
        try:
            if descriptor >= 0:
                if os.name == "nt":
                    with contextlib.suppress(OSError):
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    with contextlib.suppress(OSError):
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
        finally:
            if parent_handle is not None:
                with contextlib.suppress(OSError):
                    _WindowsNativeLeaseBackend().close_handle(parent_handle)


def host_header_is_allowed(value: str | None, *, allow_testserver: bool = False) -> bool:
    """Return whether one Host value names only an explicit loopback authority."""
    if value is None or not value or value != value.strip():
        return False
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False
    if any(character in value for character in (",", "@", "/", "\\", "?", "#")):
        return False
    if any(ord(character) <= 32 or ord(character) == 127 for character in value):
        return False

    host: str
    port: str | None = None
    if value.startswith("["):
        closing = value.find("]")
        if closing <= 1 or value.find("[", 1) != -1:
            return False
        host = value[1:closing]
        suffix = value[closing + 1 :]
        if "[" in host or "]" in host or "%" in host:
            return False
        if suffix:
            if not suffix.startswith(":") or suffix.count(":") != 1:
                return False
            port = suffix[1:]
        try:
            parsed = ip_address(host)
        except ValueError:
            return False
        if parsed.version != 6:
            return False
    elif "[" in value or "]" in value:
        return False
    elif value.count(":") > 1:
        host = value
        try:
            parsed = ip_address(host)
        except ValueError:
            return False
        if parsed.version != 6:
            return False
    elif ":" in value:
        host, port = value.rsplit(":", 1)
        if not host:
            return False
    else:
        host = value

    if port is not None:
        if not port or len(port) > 5 or not port.isascii() or not port.isdecimal():
            return False
        numeric_port = int(port, 10)
        if numeric_port < 1 or numeric_port > 65535:
            return False

    normalized_host = host.casefold()
    if normalized_host == "localhost":
        return True
    if allow_testserver and normalized_host == "testserver":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def request_host_is_allowed(request: Request, *, allow_testserver: bool = False) -> bool:
    """Validate the request's single raw Host header without merging duplicates."""
    raw_headers = request.scope.get("headers", ())
    host_values = [
        value for name, value in raw_headers if isinstance(name, bytes) and name.lower() == b"host"
    ]
    if len(host_values) != 1 or not isinstance(host_values[0], bytes):
        return False
    try:
        value = host_values[0].decode("ascii")
    except UnicodeDecodeError:
        return False
    return host_header_is_allowed(value, allow_testserver=allow_testserver)


def _single_ascii_request_header(request: Request, name: bytes) -> str | None:
    """Return one raw ASCII request header, rejecting duplicates and malformed bytes."""
    values = [
        value
        for raw_name, value in request.scope.get("headers", ())
        if isinstance(raw_name, bytes) and raw_name.lower() == name
    ]
    if not values:
        return None
    if len(values) != 1 or not isinstance(values[0], bytes):
        raise ValueError(f"duplicate {name.decode('ascii')} header")
    try:
        value = values[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError(f"non-ASCII {name.decode('ascii')} header") from exc
    if not value or value != value.strip():
        raise ValueError(f"malformed {name.decode('ascii')} header")
    return value


def _normalized_authority(
    host: str,
    port: int | None,
    *,
    scheme: str,
) -> tuple[str, int]:
    """Normalize one already-validated HTTP authority for exact origin comparison."""
    try:
        normalized_host = ip_address(host).compressed
    except ValueError:
        normalized_host = host.casefold()
    return normalized_host, port if port is not None else (443 if scheme == "https" else 80)


def _request_authority(request: Request, host_value: str) -> tuple[str, int]:
    scheme = request.url.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise ValueError("unsupported request scheme")
    if host_value.startswith("["):
        closing = host_value.find("]")
        host = host_value[1:closing]
        suffix = host_value[closing + 1 :]
        port = int(suffix[1:]) if suffix else None
    elif host_value.count(":") > 1:
        host = host_value
        port = None
    elif ":" in host_value:
        host, raw_port = host_value.rsplit(":", 1)
        port = int(raw_port)
    else:
        host = host_value
        port = None
    return _normalized_authority(host, port, scheme=scheme)


def request_mutation_is_allowed(request: Request) -> bool:
    """Reject browser cross-origin mutations while preserving headerless local clients."""
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return True
    try:
        origin = _single_ascii_request_header(request, b"origin")
        fetch_site = _single_ascii_request_header(request, b"sec-fetch-site")
        host_value = _single_ascii_request_header(request, b"host")
    except ValueError:
        return False
    if host_value is None:
        return False
    if fetch_site is not None and fetch_site != "same-origin":
        return False
    if origin is None:
        return fetch_site in {None, "same-origin"}
    if origin == "null" or any(ord(character) <= 32 for character in origin):
        return False
    try:
        parsed = urlsplit(origin)
        scheme = parsed.scheme.casefold()
        if (
            scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.hostname is None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return False
        origin_authority = _normalized_authority(
            parsed.hostname,
            parsed.port,
            scheme=scheme,
        )
        return scheme == request.url.scheme.casefold() and origin_authority == _request_authority(
            request,
            host_value,
        )
    except (ValueError, UnicodeError):
        return False
