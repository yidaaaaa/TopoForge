#!/usr/bin/env python3
"""Shared, verifier-only Windows host and candidate evidence helpers."""

from __future__ import annotations

import ctypes
import hashlib
import importlib
import json
import os
import platform
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

TARGET_WINDOWS_10_22H2 = "win10-22h2"
TARGET_WINDOWS_11 = "win11"
WINDOWS_TARGETS = (TARGET_WINDOWS_10_22H2, TARGET_WINDOWS_11)
WINDOWS_TARGET_IDS = {
    TARGET_WINDOWS_10_22H2: "windows-10-22h2-x64",
    TARGET_WINDOWS_11: "windows-11-x64",
}


class EvidencePublicationError(OSError):
    """Report whether atomic evidence publication committed before an I/O failure."""

    def __init__(
        self,
        *,
        destination: Path,
        temporary: Path,
        committed: bool | None,
        cause: BaseException,
    ) -> None:
        state = {
            True: "committed, but directory durability is uncertain",
            False: "not committed",
            None: "in an uncertain state",
        }[committed]
        super().__init__(f"evidence publication is {state} at {destination}: {cause}")
        self.destination = destination
        self.temporary = temporary
        self.committed = committed


def sha256_file(path: Path) -> str:
    """Return the SHA-256 of one evidence input."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_WINDOWS_MACHINE_NAMES = {
    0x0000: "UNKNOWN",
    0x014C: "I386",
    0x01C4: "ARMNT",
    0x8664: "AMD64",
    0xAA64: "ARM64",
}


def _windows_architecture_record() -> dict[str, Any]:
    """Use IsWow64Process2 to distinguish native x64 from ARM64 emulation."""
    win_dll_factory = getattr(ctypes, "WinDLL", None)
    if not callable(win_dll_factory):  # pragma: no cover - available only on Windows
        raise RuntimeError("native Windows architecture verification requires ctypes.WinDLL")
    try:
        kernel32: Any = win_dll_factory("kernel32", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.argtypes = []
        get_current_process.restype = ctypes.c_void_p
        is_wow64_process2 = kernel32.IsWow64Process2
        is_wow64_process2.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ushort),
            ctypes.POINTER(ctypes.c_ushort),
        ]
        is_wow64_process2.restype = ctypes.c_int
    except (AttributeError, OSError, TypeError) as exc:
        raise RuntimeError(
            "native Windows x64 verification requires the IsWow64Process2 API"
        ) from exc

    process_machine = ctypes.c_ushort()
    native_machine = ctypes.c_ushort()
    succeeded = bool(
        is_wow64_process2(
            get_current_process(),
            ctypes.byref(process_machine),
            ctypes.byref(native_machine),
        )
    )
    if not succeeded:
        get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
        raise RuntimeError(f"IsWow64Process2 failed with Windows error {int(get_last_error())}")
    process_code = int(process_machine.value)
    native_code = int(native_machine.value)
    return {
        "process_machine_code": process_code,
        "process_machine": _WINDOWS_MACHINE_NAMES.get(process_code, f"0x{process_code:04X}"),
        "native_machine_code": native_code,
        "native_machine": _WINDOWS_MACHINE_NAMES.get(native_code, f"0x{native_code:04X}"),
        "native_x64_verified": process_code == 0x0000 and native_code == 0x8664,
    }


def _registry_values() -> dict[str, Any]:
    """Read the Windows client identity used by the clean-host gate."""
    try:
        winreg: Any = importlib.import_module("winreg")
    except ImportError as exc:  # pragma: no cover - available only on Windows
        raise RuntimeError("Windows target verification requires the winreg module") from exc

    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    names = (
        "ProductName",
        "DisplayVersion",
        "CurrentBuildNumber",
        "UBR",
        "InstallationType",
    )
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
        values: dict[str, Any] = {}
        for name in names:
            try:
                values[name] = winreg.QueryValueEx(key, name)[0]
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"Windows registry value {name} is missing; use an unmodified "
                    "Windows 10 22H2 or Windows 11 client installation"
                ) from exc
    return values


def windows_host_record(*, require_windows: bool) -> dict[str, Any]:
    """Record a Windows host without treating hosted Server as a client target."""
    system = platform.system()
    machine = platform.machine()
    if system != "Windows":
        if require_windows:
            raise RuntimeError("Windows host verification requires a native Windows host")
        return {
            "system": system,
            "machine": machine,
            "native_windows_verified": False,
            "target_verified": False,
            "evidence_scope": "contract-only non-Windows host",
        }
    architecture = _windows_architecture_record()
    if (
        machine.casefold() not in {"amd64", "x86_64"}
        or architecture["native_x64_verified"] is not True
    ):
        raise RuntimeError(
            "Windows host verification requires native Windows x64; "
            f"observed process={architecture['process_machine']}, "
            f"native={architecture['native_machine']}"
        )
    values = _registry_values()
    try:
        build = int(str(values["CurrentBuildNumber"]).strip())
        ubr = int(values["UBR"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Windows build identity is invalid") from exc
    if build < 1 or ubr < 0:
        raise RuntimeError("Windows build identity is invalid")
    return {
        "product_name": str(values["ProductName"]).strip(),
        "display_version": str(values["DisplayVersion"]).strip(),
        "current_build_number": build,
        "ubr": ubr,
        "full_build": f"{build}.{ubr}",
        "installation_type": str(values["InstallationType"]).strip(),
        "system": system,
        "machine": machine,
        **architecture,
        "native_windows_verified": True,
        "target_verified": False,
        "evidence_scope": "hosted/unclassified Windows; not clean-client target evidence",
    }


def windows_target_record(
    expected_target: str,
    *,
    require_windows: bool,
) -> dict[str, Any]:
    """Strictly identify one declared Windows x64 client target."""
    if expected_target not in WINDOWS_TARGETS:
        raise ValueError(f"expected Windows target must be one of {WINDOWS_TARGETS}")
    system = platform.system()
    machine = platform.machine()
    if system != "Windows":
        if require_windows:
            raise RuntimeError("Windows target verification requires a native Windows host")
        return {
            "expected_target": expected_target,
            "target_id": WINDOWS_TARGET_IDS[expected_target],
            "system": system,
            "machine": machine,
            "native_windows_verified": False,
            "target_verified": False,
            "evidence_scope": "contract-only non-Windows host",
        }
    architecture = _windows_architecture_record()
    if (
        machine.casefold() not in {"amd64", "x86_64"}
        or architecture["native_x64_verified"] is not True
    ):
        raise RuntimeError(
            "Windows target verification requires native Windows x64; "
            f"observed process={architecture['process_machine']}, "
            f"native={architecture['native_machine']}"
        )

    values = _registry_values()
    product_name = str(values["ProductName"]).strip()
    display_version = str(values["DisplayVersion"]).strip()
    build_text = str(values["CurrentBuildNumber"]).strip()
    installation_type = str(values["InstallationType"]).strip()
    try:
        build = int(build_text)
        ubr = int(values["UBR"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "Windows build identity is invalid; repair the CurrentVersion registry values"
        ) from exc
    if build < 1 or ubr < 0:
        raise RuntimeError(
            "Windows build identity is invalid; repair the CurrentVersion registry values"
        )
    if installation_type.casefold() != "client":
        raise RuntimeError(
            f"{expected_target} acceptance requires InstallationType=Client, got "
            f"{installation_type!r}; hosted Windows Server is not clean-client evidence"
        )

    product_lower = product_name.casefold()
    if expected_target == TARGET_WINDOWS_10_22H2:
        matches = (
            "windows 10" in product_lower
            and "windows 11" not in product_lower
            and display_version.casefold() == "22h2"
            and build == 19045
        )
        expectation = "Windows 10, DisplayVersion 22H2, build 19045, Client"
    else:
        # ProductName can remain "Windows 10 ..." on Windows 11 for compatibility.
        matches = (
            ("windows 10" in product_lower or "windows 11" in product_lower)
            and "server" not in product_lower
            and build >= 22000
        )
        expectation = "Windows 10/11 compatibility ProductName and client build 22000 or newer"
    if not matches:
        raise RuntimeError(
            f"host does not match --expected-target {expected_target}: expected {expectation}; "
            f"observed ProductName={product_name!r}, DisplayVersion={display_version!r}, "
            f"build={build}.{ubr}. Run the candidate on the declared clean client target."
        )
    return {
        "expected_target": expected_target,
        "target_id": WINDOWS_TARGET_IDS[expected_target],
        "product_name": product_name,
        "display_version": display_version,
        "current_build_number": build,
        "ubr": ubr,
        "full_build": f"{build}.{ubr}",
        "installation_type": installation_type,
        "system": system,
        "machine": machine,
        **architecture,
        "native_windows_verified": True,
        "target_verified": True,
    }


def _absolute_lexical_path(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return Path(os.path.abspath(os.fspath(candidate)))


def _link_like(information: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(information.st_mode) or bool(
        getattr(information, "st_file_attributes", 0) & reparse_flag
    )


def _object_identity(information: os.stat_result) -> tuple[int, int, int]:
    return (
        information.st_dev,
        information.st_ino,
        stat.S_IFMT(information.st_mode),
    )


def _checked_directory_chain(path: Path) -> tuple[os.stat_result, ...]:
    absolute = _absolute_lexical_path(path)
    current = Path(absolute.anchor)
    result: list[os.stat_result] = []
    for index, part in enumerate(absolute.parts):
        if index:
            current /= part
        information = current.lstat()
        if _link_like(information) or not stat.S_ISDIR(information.st_mode):
            raise ValueError(f"evidence directory path is not a real directory: {current}")
        result.append(information)
    return tuple(result)


def _same_objects(
    before: tuple[os.stat_result, ...],
    after: tuple[os.stat_result, ...],
) -> bool:
    return len(before) == len(after) and all(
        _object_identity(left) == _object_identity(right)
        for left, right in zip(before, after, strict=True)
    )


def _ensure_plain_directory_tree(path: Path) -> tuple[os.stat_result, ...]:
    absolute = _absolute_lexical_path(path)
    missing: list[Path] = []
    cursor = absolute
    while True:
        try:
            cursor.lstat()
        except FileNotFoundError:
            if cursor == Path(cursor.anchor):
                raise
            missing.append(cursor)
            cursor = cursor.parent
            continue
        break
    existing = _checked_directory_chain(cursor)
    for directory in reversed(missing):
        parent_before = _checked_directory_chain(directory.parent)
        directory.mkdir()
        if not _same_objects(parent_before, _checked_directory_chain(directory.parent)):
            raise ValueError("evidence directory parent changed during creation")
        created = directory.lstat()
        if _link_like(created) or not stat.S_ISDIR(created.st_mode):
            raise ValueError(f"evidence directory is not a real directory: {directory}")
    return _checked_directory_chain(absolute) if missing else existing


def _unlink_if_same_object(
    path: Path,
    expected_identity: tuple[int, int, int],
) -> None:
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return
    if (
        _link_like(observed)
        or not stat.S_ISREG(observed.st_mode)
        or _object_identity(observed) != expected_identity
    ):
        return
    try:
        path.unlink()
    except OSError:
        return


def _publication_state(
    *,
    destination: Path,
    temporary: Path,
    temporary_identity: tuple[int, int, int],
    payload_size: int,
    parent_before: tuple[os.stat_result, ...],
) -> bool | None:
    temporary_information: os.stat_result | None = None
    destination_information: os.stat_result | None = None
    try:
        parent_after = _checked_directory_chain(destination.parent)
        if not _same_objects(parent_before, parent_after):
            return None
        try:
            temporary_information = temporary.lstat()
        except FileNotFoundError:
            temporary_information = None
        destination_information = destination.lstat()
    except FileNotFoundError:
        destination_information = None
    except (OSError, ValueError):
        return None
    temporary_matches = temporary_information is not None and (
        not _link_like(temporary_information)
        and stat.S_ISREG(temporary_information.st_mode)
        and temporary_information.st_nlink == 1
        and temporary_information.st_size == payload_size
        and _object_identity(temporary_information) == temporary_identity
    )
    destination_matches = destination_information is not None and (
        not _link_like(destination_information)
        and stat.S_ISREG(destination_information.st_mode)
        and destination_information.st_nlink == 1
        and destination_information.st_size == payload_size
        and _object_identity(destination_information) == temporary_identity
    )
    if temporary_information is None and destination_matches:
        return True
    if temporary_matches and not destination_matches:
        return False
    return None


def source_repository_record(
    repository_root: Path,
    *,
    expected_commit: str | None,
    require_clean: bool,
) -> dict[str, Any]:
    """Bind verification to one observed Git commit and complete working-tree state."""
    root = repository_root.resolve()
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            f"candidate source identity is unavailable at {root}; use the exact clean Git checkout"
        ) from exc
    dirty = bool(status.strip())
    commit_matches = expected_commit is None or commit.casefold() == expected_commit.casefold()
    if not commit_matches:
        raise RuntimeError(
            f"candidate source commit is {commit}, expected {expected_commit}; "
            "check out the exact candidate commit"
        )
    if require_clean and dirty:
        status_lines = status.splitlines()
        bounded_status = "\n".join(status_lines[:20])[:2000]
        if len(status_lines) > 20:
            bounded_status += f"\n... {len(status_lines) - 20} additional entries omitted"
        raise RuntimeError(
            "candidate source checkout has modifications or untracked files; commit, remove, "
            "or revert them before collecting release evidence; bounded Git status:\n"
            f"{bounded_status}"
        )
    return {
        "repository_root": str(root),
        "commit": commit,
        "expected_commit": expected_commit,
        "tracked_dirty": dirty,
        "clean_required": require_clean,
        "required_checks_passed": commit_matches and (not require_clean or not dirty),
    }


def write_canonical_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write stable verifier evidence JSON."""
    destination = _absolute_lexical_path(path)
    parent_before = _ensure_plain_directory_tree(destination.parent)
    try:
        previous = destination.lstat()
    except FileNotFoundError:
        pass
    else:
        if _link_like(previous) or not stat.S_ISREG(previous.st_mode) or previous.st_nlink != 1:
            raise ValueError(
                f"evidence destination must be absent or a real single-link file: {destination}"
            )
    serialized = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temporary: Path | None = None
    temporary_identity: tuple[int, int, int] | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            opened = os.fstat(handle.fileno())
            temporary_identity = _object_identity(opened)
            if (
                _link_like(opened)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size != 0
            ):
                raise ValueError("evidence temporary is not a new single-link regular file")
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
            written = os.fstat(handle.fileno())
            if (
                _object_identity(written) != temporary_identity
                or written.st_nlink != 1
                or written.st_size != len(serialized)
            ):
                raise ValueError("evidence temporary changed while it was written")
        if temporary is None or temporary_identity is None:
            raise RuntimeError("evidence temporary creation did not return an identity")
        temporary_information = temporary.lstat()
        if (
            _link_like(temporary_information)
            or temporary_information.st_nlink != 1
            or temporary_information.st_size != len(serialized)
            or _object_identity(temporary_information) != temporary_identity
            or not _same_objects(
                parent_before,
                _checked_directory_chain(destination.parent),
            )
        ):
            raise ValueError("evidence temporary path changed before publication")
        try:
            os.replace(temporary, destination)
        except OSError as exc:
            state = _publication_state(
                destination=destination,
                temporary=temporary,
                temporary_identity=temporary_identity,
                payload_size=len(serialized),
                parent_before=parent_before,
            )
            if state is not True:
                raise EvidencePublicationError(
                    destination=destination,
                    temporary=temporary,
                    committed=state,
                    cause=exc,
                ) from exc
        state = _publication_state(
            destination=destination,
            temporary=temporary,
            temporary_identity=temporary_identity,
            payload_size=len(serialized),
            parent_before=parent_before,
        )
        if state is not True:
            raise EvidencePublicationError(
                destination=destination,
                temporary=temporary,
                committed=None,
                cause=RuntimeError("replacement could not be reconciled to the written object"),
            )
        if os.name != "nt":
            directory_fd = -1
            try:
                directory_fd = os.open(
                    destination.parent,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
                )
                if _object_identity(os.fstat(directory_fd)) != _object_identity(parent_before[-1]):
                    raise OSError("evidence directory changed before durability synchronization")
                os.fsync(directory_fd)
            except OSError as exc:
                committed = _publication_state(
                    destination=destination,
                    temporary=temporary,
                    temporary_identity=temporary_identity,
                    payload_size=len(serialized),
                    parent_before=parent_before,
                )
                raise EvidencePublicationError(
                    destination=destination,
                    temporary=temporary,
                    committed=True if committed is True else None,
                    cause=exc,
                ) from exc
            finally:
                if directory_fd >= 0:
                    os.close(directory_fd)
    finally:
        if temporary is not None and temporary_identity is not None:
            _unlink_if_same_object(temporary, temporary_identity)


def load_candidate_binding(
    path: Path,
    *,
    verifier_role: str,
    verifier_path: Path,
    expected_target: str,
) -> dict[str, Any]:
    """Strictly reopen a portable candidate binding inside a nested verifier."""
    resolved = path.resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"candidate binding is unreadable: {resolved}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("candidate binding root must be an object")
    if payload.get("schema_version") != "topoforge-windows-candidate-binding-v1":
        raise RuntimeError("candidate binding schema is invalid")
    if payload.get("expected_target") != expected_target:
        raise RuntimeError("candidate binding target differs from this verifier target")
    expected_target_id = WINDOWS_TARGET_IDS.get(expected_target)
    if (
        payload.get("target_id") != expected_target_id
        or payload.get("required_checks_passed") is not True
    ):
        raise RuntimeError("candidate binding target ID or pass state is invalid")
    archive = payload.get("archive")
    source = payload.get("source_repository")
    verifier_hashes = payload.get("verifier_sha256")
    if (
        not isinstance(archive, dict)
        or not isinstance(source, dict)
        or not isinstance(verifier_hashes, dict)
    ):
        raise RuntimeError("candidate binding is missing archive, source, or verifier identity")
    if set(verifier_hashes) != {"builder", "portable", "system", "bambu", "helper"}:
        raise RuntimeError("candidate binding verifier role set is invalid")
    archive_sha256 = archive.get("sha256")
    archive_bytes = archive.get("bytes")
    source_commit = source.get("commit")
    config_sha256 = payload.get("config_sha256")
    build_constraints_sha256 = payload.get("build_constraints_sha256")
    expected_verifier = verifier_hashes.get(verifier_role)
    digests = (
        archive_sha256,
        config_sha256,
        build_constraints_sha256,
        expected_verifier,
    )
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value.casefold())
        for value in digests
    ):
        raise RuntimeError("candidate binding contains an invalid SHA-256")
    if not isinstance(archive_bytes, int) or isinstance(archive_bytes, bool) or archive_bytes < 1:
        raise RuntimeError("candidate binding archive byte count is invalid")
    if (
        source.get("clean_required") is not True
        or source.get("required_checks_passed") is not True
        or (expected_target in WINDOWS_TARGETS and source.get("expected_commit") != source_commit)
        or source.get("tracked_dirty") is not False
        or not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit.casefold())
    ):
        raise RuntimeError("candidate binding does not identify one clean source commit")
    build_constraints_path = (
        Path(__file__).resolve().parents[1] / "packaging" / "build-constraints.txt"
    )
    if sha256_file(build_constraints_path) != build_constraints_sha256:
        raise RuntimeError("candidate binding build constraints differ from this verifier")
    actual_verifier = sha256_file(verifier_path.resolve())
    if expected_verifier != actual_verifier:
        raise RuntimeError(
            f"{verifier_role} verifier SHA-256 differs from the portable candidate binding"
        )
    return {
        "binding_path": str(resolved),
        "binding_sha256": sha256_file(resolved),
        "archive_sha256": archive_sha256,
        "archive_bytes": archive_bytes,
        "source_commit": source_commit,
        "source_tracked_dirty": False,
        "config_sha256": payload.get("config_sha256"),
        "build_constraints_sha256": build_constraints_sha256,
        "verifier_role": verifier_role,
        "verifier_sha256": actual_verifier,
        "expected_target": expected_target,
        "target_id": WINDOWS_TARGET_IDS.get(expected_target),
        "required_checks_passed": True,
    }


def runtime_platform_record(*, require_windows: bool) -> dict[str, Any]:
    """Return the common runtime identity without implying a target pass."""
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "native_windows_required": require_windows,
        "native_windows_verified": platform.system() == "Windows",
    }
