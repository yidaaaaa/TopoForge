from __future__ import annotations

import errno
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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
            atomic_write_owned_regular_bytes(
                path,
                b"replacement status\n",
                root=root,
                root_identity=identity,
                context="concurrent progress publication",
                replace=True,
            )
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
        assert path.read_bytes() == b"replacement status\n"
    else:
        assert len(writer_errors) == 1
        assert isinstance(writer_errors[0], OSError)
        assert read_error is None
        assert payload == b"original status\n"
        assert path.read_bytes() == b"original status\n"
        # Releasing the default reader removes the sharing conflict.
        atomic_write_owned_regular_bytes(
            path,
            b"replacement status\n",
            root=root,
            root_identity=identity,
            context="publication after reader close",
            replace=True,
        )
        assert path.read_bytes() == b"replacement status\n"
