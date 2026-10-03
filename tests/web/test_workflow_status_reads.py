from __future__ import annotations

import errno
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from topoforge.exceptions import ConfigurationError
from topoforge.web import jobs as jobs_module
from topoforge.web import security as security_module
from topoforge.web.jobs import LocalJobManager
from topoforge.web.models import JobRecord, JobState, WebAppConfig, utc_now
from topoforge.web.security import (
    atomic_write_owned_regular_bytes,
    canonical_json_bytes,
    read_owned_regular_bytes,
)
from topoforge.workflow import LocalWorkflowStatus, WorkflowStage, WorkflowState
from topoforge.workflow import local as workflow_module

from .test_jobs import _PosixWindowsLeaseBackend


@pytest.fixture
def status_manager(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[LocalJobManager]:
    manager = LocalJobManager(web_config)
    monkeypatch.setattr(manager, "_monitor_loop", lambda: None)
    monkeypatch.setattr(manager, "_start_queued_jobs", lambda: None)
    manager.start()
    try:
        yield manager
    finally:
        manager.close()


def _status_record(manager: LocalJobManager) -> tuple[JobRecord, Path, bytes]:
    workspace = manager.config.workspace_root / "status-job"
    workspace.mkdir()
    now = utc_now()
    record = JobRecord(
        job_id="a" * 32,
        created_at=now,
        updated_at=now,
        state=JobState.RUNNING,
        workspace_dir=workspace,
        expected_stages=(WorkflowStage.SOURCE, WorkflowStage.BUILD),
        progress_fraction=0,
        current_stage=WorkflowStage.SOURCE,
    )
    manager._write_record(record)
    payload = canonical_json_bytes(
        LocalWorkflowStatus(
            workflow_id="fixture",
            state=WorkflowState.RUNNING,
            current_stage=WorkflowStage.BUILD,
            ready_stages=(WorkflowStage.SOURCE,),
        )
    )
    return record, workspace / "workflow-status.json", payload


def test_progress_uses_bounded_owned_snapshot_with_atomic_replace_sharing(
    status_manager: LocalJobManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = status_manager
    record, path, payload = _status_record(manager)
    path.write_bytes(payload)
    observed: list[dict[str, Any]] = []
    original_read = jobs_module.read_owned_regular_bytes

    def capture_read(candidate: Path, **kwargs: Any) -> bytes:
        if candidate == path:
            observed.append(kwargs)
        return original_read(candidate, **kwargs)

    monkeypatch.setattr(jobs_module, "read_owned_regular_bytes", capture_read)
    updated = manager._status_update(record)
    assert updated.current_stage is WorkflowStage.BUILD
    assert updated.ready_stages == (WorkflowStage.SOURCE,)
    assert updated.progress_fraction > record.progress_fraction
    assert len(observed) == 1
    assert observed[0]["root"] == manager.config.workspace_root
    assert observed[0]["root_identity"] == manager._owned_identity(manager.config.workspace_root)
    assert observed[0]["max_bytes"] == 1024 * 1024
    assert observed[0]["allow_atomic_replace"] is True


@pytest.mark.parametrize("case", ["missing", "invalid", "oversized", "replaced", "sharing"])
def test_progress_skips_missing_unstable_or_invalid_snapshot_without_changing_job(
    status_manager: LocalJobManager,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    manager = status_manager
    record, path, payload = _status_record(manager)
    before = manager._record_path(record.job_id).read_bytes()
    if case == "invalid":
        path.write_bytes(b"{}\n")
    elif case == "oversized":
        path.write_bytes(b" " * (1024 * 1024 + 1))
    elif case != "missing":
        path.write_bytes(payload)
    if case in {"replaced", "sharing"}:
        original_read = jobs_module.read_owned_regular_bytes

        def unavailable_read(candidate: Path, **kwargs: Any) -> bytes:
            if candidate == path:
                if case == "replaced":
                    raise ValueError("fixture path changed while it was read")
                error = OSError(errno.EACCES, "fixture publisher still owns the new file")
                error.__dict__["winerror"] = 32
                raise error
            return original_read(candidate, **kwargs)

        monkeypatch.setattr(jobs_module, "read_owned_regular_bytes", unavailable_read)
    assert manager._status_update(record) == record
    assert manager._record_path(record.job_id).read_bytes() == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX backend simulates native Windows sharing flags")
@pytest.mark.parametrize("allow_atomic_replace", [False, True])
def test_windows_snapshot_sharing_is_opt_in_and_never_shares_in_place_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_atomic_replace: bool,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "workflow-status.json"
    path.write_bytes(b"original status\n")
    observed: list[int | None] = []

    class CapturingBackend(_PosixWindowsLeaseBackend):
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
            if (
                name == path.name
                and desired_access is not None
                and desired_access & security_module._GENERIC_READ
            ):
                observed.append(share_access)
            return super().open_relative_file(
                parent_handle,
                name,
                create=create,
                directory=directory,
                desired_access=desired_access,
                share_access=share_access,
            )

    backend = CapturingBackend()
    monkeypatch.setattr(security_module, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(security_module, "_WindowsNativeLeaseBackend", lambda: backend)
    options: dict[str, Any] = {"allow_atomic_replace": True} if allow_atomic_replace else {}
    assert (
        read_owned_regular_bytes(
            path,
            root=root,
            root_identity=(root.stat().st_dev, root.stat().st_ino),
            context="progress fixture",
            **options,
        )
        == b"original status\n"
    )
    expected = security_module._FILE_SHARE_READ
    if allow_atomic_replace:
        expected |= security_module._FILE_SHARE_DELETE
    assert observed == [expected]
    assert not expected & security_module._FILE_SHARE_WRITE


@pytest.mark.skipif(os.name == "nt", reason="POSIX backend exercises native no-follow checks")
@pytest.mark.parametrize("allow_atomic_replace", [False, True])
@pytest.mark.parametrize("case", ["symlink", "hardlink", "root-replaced", "file-replaced"])
def test_windows_snapshot_opt_in_preserves_link_and_identity_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_atomic_replace: bool,
    case: str,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "workflow-status.json"
    path.write_bytes(b"original status\n")
    identity = (root.stat().st_dev, root.stat().st_ino)
    external = tmp_path / "external.json"
    external.write_bytes(b"external must not be changed\n")
    if case in {"symlink", "hardlink"}:
        path.unlink()
        if case == "symlink":
            path.symlink_to(external)
        else:
            os.link(external, path)
    moved = tmp_path / "original-workspace"
    switched = False

    class ReplacingBackend(_PosixWindowsLeaseBackend):
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
            nonlocal switched
            if case == "root-replaced" and name == path.name and not switched:
                switched = True
                root.rename(moved)
                root.mkdir()
                path.write_bytes(external.read_bytes())
            return super().open_relative_file(
                parent_handle,
                name,
                create=create,
                directory=directory,
                desired_access=desired_access,
                share_access=share_access,
            )

    fake_os = SimpleNamespace(**{**vars(os), "name": "nt"})
    if case == "file-replaced":

        def replace_during_read(descriptor: int, size: int) -> bytes:
            nonlocal switched
            payload = os.read(descriptor, size)
            if payload and not switched:
                switched = True
                replacement = root / "new-status.json"
                replacement.write_bytes(b"replacement status\n")
                os.replace(replacement, path)
            return payload

        fake_os.read = replace_during_read
    backend = ReplacingBackend()
    monkeypatch.setattr(security_module, "os", fake_os)
    monkeypatch.setattr(security_module, "_WindowsNativeLeaseBackend", lambda: backend)
    with pytest.raises((OSError, ValueError)):
        read_owned_regular_bytes(
            path,
            root=root,
            root_identity=identity,
            context="progress fixture",
            allow_atomic_replace=allow_atomic_replace,
        )
    assert external.read_bytes() == b"external must not be changed\n"
    if case == "root-replaced":
        assert (moved / path.name).read_bytes() == b"original status\n"
        assert path.read_bytes() == external.read_bytes()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows delete-sharing enforcement")
@pytest.mark.parametrize("allow_atomic_replace", [False, True])
def test_native_windows_reader_sharing_controls_atomic_status_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_atomic_replace: bool,
) -> None:
    root = tmp_path / "workspace with spaces"
    root.mkdir()
    path = root / "workflow-status.json"
    path.write_bytes(b"original status\n")
    identity = security_module.real_directory_tree_identity(root, context="fixture workspace")
    reader_opened = threading.Event()
    writer_done = threading.Event()
    reader_released = threading.Event()
    writer_errors: list[BaseException] = []
    writer_finished_while_reader_held: list[bool] = []
    original_open = security_module._open_windows_owned_entry
    replacement_payload = canonical_json_bytes(
        LocalWorkflowStatus(
            workflow_id="updated",
            state=WorkflowState.RUNNING,
            current_stage=WorkflowStage.BUILD,
            ready_stages=(),
        )
    )

    def publish_status() -> None:
        workflow_module._status(
            root,
            workspace=workflow_module._WorkspaceLease(root, identity),
            workflow_id="updated",
            state=WorkflowState.RUNNING,
            current_stage=WorkflowStage.BUILD,
            records=[],
        )

    def pause_after_open(
        candidate_root: Path,
        candidate: Path,
        **kwargs: Any,
    ) -> security_module._WindowsOwnedEntry:
        opened = original_open(candidate_root, candidate, **kwargs)
        if candidate == path:
            reader_opened.set()
            if not writer_done.wait(5):
                opened.parent.backend.close_handle(opened.handle)
                security_module._close_windows_handles(opened.parent.backend, opened.parent.handles)
                raise AssertionError("atomic writer did not finish while reader was held")
        return opened

    def publish() -> None:
        try:
            if not reader_opened.wait(5):
                raise AssertionError("reader did not acquire its file handle")
            publish_status()
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_finished_while_reader_held.append(not reader_released.is_set())
            writer_done.set()

    monkeypatch.setattr(security_module, "_open_windows_owned_entry", pause_after_open)
    publisher = threading.Thread(target=publish, daemon=True)
    publisher.start()
    payload: bytes | None = None
    read_error: Exception | None = None
    try:
        options: dict[str, Any] = {"allow_atomic_replace": True} if allow_atomic_replace else {}
        payload = read_owned_regular_bytes(
            path,
            root=root,
            root_identity=identity,
            context="concurrent progress snapshot",
            **options,
        )
    except (OSError, ValueError) as exc:
        read_error = exc
    finally:
        reader_released.set()
        publisher.join(timeout=5)
    assert not publisher.is_alive()
    assert writer_done.is_set()
    assert writer_finished_while_reader_held == [True]
    if allow_atomic_replace:
        assert writer_errors == []
        assert read_error is not None
        assert payload is None
        assert path.read_bytes() == replacement_payload
    else:
        assert len(writer_errors) == 1
        assert isinstance(writer_errors[0], ConfigurationError)
        assert isinstance(writer_errors[0].__cause__, OSError)
        assert not getattr(writer_errors[0].__cause__, "committed", False)
        assert read_error is None
        assert payload == b"original status\n"
        assert path.read_bytes() == b"original status\n"
        # Releasing the default reader removes the sharing conflict.
        publish_status()
        assert path.read_bytes() == replacement_payload


@pytest.mark.parametrize("name", ["x", "地形😀.json"])
@pytest.mark.parametrize(
    ("replace", "allow_open_destination", "expected_class", "expected_flags"),
    [(False, False, 10, 0), (True, False, 10, 1), (True, True, 65, 3)],
)
def test_windows_rename_open_destination_uses_explicit_class_flags_and_utf16(
    name: str,
    replace: bool,
    allow_open_destination: bool,
    expected_class: int,
    expected_flags: int,
) -> None:
    calls: list[tuple[int, bytes]] = []

    def capture(
        _handle: object,
        _io_status: object,
        buffer: Any,
        length: int,
        information_class: int,
    ) -> int:
        calls.append((information_class, security_module.ctypes.string_at(buffer, length)))
        return 0

    backend = object.__new__(security_module._WindowsNativeLeaseBackend)
    backend._nt_set_information_file = capture
    options: dict[str, Any] = {"allow_open_destination": True} if allow_open_destination else {}
    backend.rename_relative(11, 22, name, replace=replace, **options)
    assert len(calls) == 1
    information_class, payload = calls[0]
    assert information_class == expected_class
    header_type = (
        security_module._FileRenameInfoExHeader
        if allow_open_destination
        else security_module._FileRenameInfoHeader
    )
    header_size = security_module.ctypes.sizeof(header_type)
    header = header_type.from_buffer_copy(payload[:header_size])
    encoded_name = name.encode("utf-16-le")
    assert int.from_bytes(payload[:4], "little") == expected_flags
    assert header.RootDirectory == 22
    assert header.FileNameLength == len(encoded_name)
    filename_offset = header_type.FileNameLength.offset + 4
    assert payload[filename_offset : filename_offset + len(encoded_name)] == encoded_name
    assert len(payload) == header_size + len(encoded_name)
    if security_module.ctypes.sizeof(security_module.ctypes.c_void_p) == 8:
        assert header_size == 24
        assert header_type.RootDirectory.offset == 8
        assert filename_offset == 20


@pytest.mark.parametrize("entry", ["public", "windows-helper", "native-backend"])
def test_open_destination_publication_requires_replace_before_filesystem_access(
    tmp_path: Path,
    entry: str,
) -> None:
    root = tmp_path / "absent-workspace"
    path = root / "workflow-status.json"
    with pytest.raises(ValueError, match="requires replace=True"):
        if entry == "native-backend":
            backend = object.__new__(security_module._WindowsNativeLeaseBackend)
            backend.rename_relative(11, 22, path.name, replace=False, allow_open_destination=True)
        else:
            write = (
                atomic_write_owned_regular_bytes
                if entry == "public"
                else security_module._write_atomic_owned_regular_bytes_windows
            )
            write(
                path,
                b"status",
                root=root,
                root_identity=(1, 2),
                context="invalid publication fixture",
                replace=False,
                allow_open_destination=True,
            )
    assert not root.exists()


@pytest.mark.parametrize(
    "name", ["", ".", "..", "../status", "nested\\status", "status:stream", "a\x00b"]
)
def test_windows_open_destination_rename_rejects_unsafe_names(name: str) -> None:
    backend = object.__new__(security_module._WindowsNativeLeaseBackend)
    with pytest.raises(RuntimeError, match="unsafe Windows filesystem basename"):
        backend.rename_relative(11, 22, name, replace=True, allow_open_destination=True)


@pytest.mark.parametrize(
    ("ntstatus", "winerror"),
    [(0xC0000022, 5), (0xC0000003, 87), (0xC00000BB, 50)],
)
def test_windows_open_destination_rename_propagates_failure_without_fallback(
    ntstatus: int,
    winerror: int,
) -> None:
    calls: list[int] = []

    def fail(
        _handle: object,
        _io_status: object,
        _buffer: object,
        _length: int,
        information_class: int,
    ) -> int:
        calls.append(information_class)
        return security_module.ctypes.c_int32(ntstatus).value

    backend = object.__new__(security_module._WindowsNativeLeaseBackend)
    backend._nt_set_information_file = fail
    backend._rtl_status_to_error = lambda _status: winerror
    with pytest.raises(OSError, match=f"NTSTATUS 0x{ntstatus:08x}") as caught:
        backend.rename_relative(11, 22, "status.json", replace=True, allow_open_destination=True)
    reported_error = getattr(caught.value, "winerror", None)
    if reported_error is None:
        reported_error = vars(caught.value).get("winerror")
    assert reported_error == winerror
    assert calls == [65]


@pytest.mark.skipif(os.name == "nt", reason="POSIX backend exercises Windows publisher selection")
@pytest.mark.parametrize("allow_open_destination", [False, True])
def test_windows_owned_publication_preserves_explicit_open_destination_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_open_destination: bool,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "workflow-status.json"
    path.write_bytes(b"original")
    observed: list[bool] = []

    class CapturingBackend(_PosixWindowsLeaseBackend):
        def rename_relative(
            self,
            handle: int,
            parent_handle: int,
            name: str,
            *,
            replace: bool,
            allow_open_destination: bool = False,
        ) -> None:
            observed.append(allow_open_destination)
            super().rename_relative(
                handle,
                parent_handle,
                name,
                replace=replace,
                allow_open_destination=allow_open_destination,
            )

    backend = CapturingBackend()
    monkeypatch.setattr(security_module, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(security_module, "_WindowsNativeLeaseBackend", lambda: backend)
    options: dict[str, Any] = {"allow_open_destination": True} if allow_open_destination else {}
    atomic_write_owned_regular_bytes(
        path,
        b"replacement",
        root=root,
        root_identity=(root.stat().st_dev, root.stat().st_ino),
        context="progress publication",
        **options,
    )
    assert path.read_bytes() == b"replacement"
    assert observed == [allow_open_destination]


@pytest.mark.skipif(os.name == "nt", reason="POSIX backend exercises no-follow publisher checks")
@pytest.mark.parametrize("case", ["symlink", "hardlink", "root-replaced", "rename-failed"])
def test_windows_open_destination_publication_preserves_path_guards_and_failed_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "workflow-status.json"
    path.write_bytes(b"original")
    identity = (root.stat().st_dev, root.stat().st_ino)
    external = tmp_path / "external.json"
    external.write_bytes(b"external")
    if case in {"symlink", "hardlink"}:
        path.unlink()
        if case == "symlink":
            path.symlink_to(external)
        else:
            os.link(external, path)
    elif case == "root-replaced":
        root.rename(tmp_path / "original-workspace")
        root.mkdir()
        path.write_bytes(b"replacement-root")
    before = path.read_bytes()

    def before_rename() -> None:
        if case == "rename-failed":
            raise PermissionError(errno.EACCES, "injected native publication failure")

    backend = _PosixWindowsLeaseBackend(before_rename=before_rename)
    monkeypatch.setattr(security_module, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(security_module, "_WindowsNativeLeaseBackend", lambda: backend)
    with pytest.raises((OSError, ValueError)):
        atomic_write_owned_regular_bytes(
            path,
            b"must not publish",
            root=root,
            root_identity=identity,
            context="unsafe progress publication",
            allow_open_destination=True,
        )
    assert path.read_bytes() == before
    assert external.read_bytes() == b"external"
    assert list(root.iterdir()) == [path]
    if case == "root-replaced":
        assert (tmp_path / "original-workspace" / path.name).read_bytes() == b"original"
