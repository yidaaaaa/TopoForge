from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from topoforge.exceptions import ConfigurationError
from topoforge.web.jobs import LocalJobManager, expected_workflow_stages
from topoforge.web.models import JobDeleteRequest, JobRecord, JobState, WebAppConfig, utc_now
from topoforge.web.server import is_loopback_host
from topoforge.workflow import WorkflowStage

from .conftest import make_job_request


def wait_for_terminal(
    manager: LocalJobManager,
    job_id: str,
    *,
    timeout_seconds: float = 90,
) -> JobRecord:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        record = manager.get(job_id)
        if record.state in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}:
            return record
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout_seconds} seconds")


class SlowJobManager(LocalJobManager):
    def _start_job(self, record: JobRecord) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._processes[record.job_id] = process
        self._write_record(
            record.model_copy(
                update={
                    "state": JobState.RUNNING,
                    "pid": process.pid,
                    "current_stage": record.expected_stages[0],
                }
            ),
            message_key="job.started",
        )


def test_loopback_policy_and_stage_contract(web_config: WebAppConfig) -> None:
    request = make_job_request(web_config)
    assert expected_workflow_stages(request) == (
        WorkflowStage.SOURCE,
        WorkflowStage.BUILD,
        WorkflowStage.LAYOUT,
        WorkflowStage.EXTRACT,
        WorkflowStage.MESH,
        WorkflowStage.CONNECT,
    )
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert is_loopback_host("[::1]")
    assert is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("192.168.1.10")
    assert not is_loopback_host("example.test")


def test_web_config_rejects_overlapping_state_and_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        WebAppConfig(
            state_dir=tmp_path / "root" / "state",
            workspace_root=tmp_path / "root",
            input_roots=(tmp_path,),
        )


def test_web_config_rejects_partial_bambu_profiles(tmp_path: Path) -> None:
    machine = tmp_path / "machine.json"
    machine.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be configured together"):
        WebAppConfig(
            state_dir=tmp_path / "state",
            workspace_root=tmp_path / "workspaces",
            input_roots=(tmp_path,),
            bambu_machine_profile=machine,
        )


def test_input_browser_enforces_roots_and_filters_files(
    web_config: WebAppConfig,
    tmp_path: Path,
) -> None:
    root = web_config.input_roots[0]
    (root / "terrain.tif").write_bytes(b"fixture")
    (root / "notes.txt").write_text("hidden by suffix policy\n", encoding="utf-8")
    (root / ".secret.yaml").write_text("hidden: true\n", encoding="utf-8")
    (root / "folder").mkdir()
    manager = LocalJobManager(web_config)

    listing = manager.list_files(root)
    assert [entry.name for entry in listing.entries] == ["folder", "terrain.tif"]
    assert listing.entries[1].selectable is True
    with pytest.raises(ConfigurationError, match="outside configured input roots"):
        manager.list_files(tmp_path)


def test_isolated_worker_inherits_configured_bambu_executable(
    web_config: WebAppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "BambuStudio.AppImage"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    configured = WebAppConfig.model_validate(
        {
            **web_config.model_dump(),
            "bambu_studio_executable": executable,
        }
    )
    captured_environments: list[dict[str, str]] = []

    class FakeProcess:
        pid = 43210

        @staticmethod
        def poll() -> None:
            return None

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        del command
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        assert all(
            isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
        )
        captured_environments.append(environment)
        return FakeProcess()

    monkeypatch.setattr("topoforge.web.jobs.subprocess.Popen", fake_popen)
    manager = LocalJobManager(configured)
    manager.start()
    try:
        manager.submit(make_job_request(configured, name="worker-bambu-environment"))
        assert captured_environments[0]["TOPOFORGE_BAMBU_STUDIO"] == str(executable.resolve())
    finally:
        manager.close()


def test_isolated_worker_completes_and_publishes_checksum_artifacts(
    web_config: WebAppConfig,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    try:
        submitted = manager.submit(make_job_request(web_config, name="complete"))
        completed = wait_for_terminal(manager, submitted.job_id)
        assert completed.state is JobState.COMPLETED
        assert completed.exit_code == 0
        assert completed.progress_fraction == 1.0
        assert completed.summary is not None
        assert completed.summary.required_checks_passed is True
        assert completed.ready_stages == completed.expected_stages
        model = next(
            artifact for artifact in completed.artifacts if artifact.artifact_id == "model_3mf"
        )
        assert model.sha256 is not None
        path, reopened = manager.artifact_path(completed.job_id, "model_3mf")
        assert path.is_file()
        assert reopened.sha256 == model.sha256
        events = manager.read_events(completed.job_id)
        assert events[0].message_key == "job.queued"
        assert events[-1].message_key == "job.completed"
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    finally:
        manager.close()


def test_worker_failure_is_structured_and_retained(web_config: WebAppConfig) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    try:
        submitted = manager.submit(
            make_job_request(web_config, name="failure", missing_source=True)
        )
        failed = wait_for_terminal(manager, submitted.job_id)
        assert failed.state is JobState.FAILED
        assert failed.exit_code == 2
        assert failed.error is not None
        assert failed.error.code == "workflow-execution-failed"
        assert failed.error.exception_type is not None
        assert (manager.jobs_dir / failed.job_id / "result.json").is_file()
        assert (manager.jobs_dir / failed.job_id / "stderr.log").is_file()
    finally:
        manager.close()


def test_running_and_queued_jobs_cancel_without_touching_other_records(
    web_config: WebAppConfig,
) -> None:
    manager = SlowJobManager(web_config)
    manager.start()
    try:
        running = manager.submit(make_job_request(web_config, name="running"))
        queued = manager.submit(make_job_request(web_config, name="queued"))
        assert manager.get(running.job_id).state is JobState.RUNNING
        assert manager.get(queued.job_id).state is JobState.QUEUED

        queued_cancelled = manager.cancel(queued.job_id)
        assert queued_cancelled.state is JobState.CANCELLED
        assert queued_cancelled.pid is None

        cancelling = manager.cancel(running.job_id)
        assert cancelling.state is JobState.CANCELLING
        cancelled = wait_for_terminal(manager, running.job_id, timeout_seconds=15)
        assert cancelled.state is JobState.CANCELLED
        assert cancelled.cancellation_requested is True
        assert manager.get(queued.job_id).state is JobState.CANCELLED
    finally:
        manager.close()


def test_terminal_job_deletion_protects_shared_workspaces_and_backups(
    web_config: WebAppConfig,
) -> None:
    manager = SlowJobManager(web_config)
    manager.start()
    try:
        running = manager.submit(make_job_request(web_config, name="shared-delete"))
        queued = manager.submit(make_job_request(web_config, name="shared-delete"))
        workspace = running.workspace_dir
        assert queued.workspace_dir == workspace
        workspace.mkdir(parents=True, exist_ok=True)
        marker = workspace / "retained-artifact.bin"
        marker.write_bytes(b"workspace evidence")
        backup_marker = manager.backups_dir / "retained-backup.tar.gz"
        backup_marker.write_bytes(b"backup evidence")

        with pytest.raises(ConfigurationError, match="cancel the selected job first"):
            manager.delete(
                running.job_id,
                JobDeleteRequest(confirm_job_id=running.job_id),
            )

        manager.cancel(queued.job_id)
        manager.cancel(running.job_id)
        assert wait_for_terminal(manager, running.job_id, timeout_seconds=15).state is (
            JobState.CANCELLED
        )

        with pytest.raises(ConfigurationError, match="confirmation does not match"):
            manager.delete(
                running.job_id,
                JobDeleteRequest(confirm_job_id="0" * 32),
            )
        with pytest.raises(ConfigurationError, match="referenced by other jobs"):
            manager.delete(
                running.job_id,
                JobDeleteRequest(
                    confirm_job_id=running.job_id,
                    delete_workspace=True,
                ),
            )

        record_only = manager.delete(
            running.job_id,
            JobDeleteRequest(confirm_job_id=running.job_id),
        )
        assert record_only.previous_state is JobState.CANCELLED
        assert record_only.workspace_removed is False
        assert record_only.workspace_retained is True
        assert record_only.deleted_job_record_bytes > 0
        assert record_only.deleted_workspace_bytes == 0
        assert record_only.reclaimed_bytes == record_only.deleted_job_record_bytes
        assert record_only.backups_preserved is True
        assert record_only.required_checks_passed is True
        assert marker.read_bytes() == b"workspace evidence"
        with pytest.raises(KeyError):
            manager.get(running.job_id)

        removed = manager.delete(
            queued.job_id,
            JobDeleteRequest(
                confirm_job_id=queued.job_id,
                delete_workspace=True,
            ),
        )
        assert removed.previous_state is JobState.CANCELLED
        assert removed.workspace_existed is True
        assert removed.workspace_removed is True
        assert removed.workspace_retained is False
        assert removed.deleted_job_record_bytes > 0
        assert removed.deleted_workspace_bytes >= len(b"workspace evidence")
        assert removed.reclaimed_bytes == (
            removed.deleted_job_record_bytes + removed.deleted_workspace_bytes
        )
        assert removed.backups_preserved is True
        assert removed.required_checks_passed is True
        assert not workspace.exists()
        assert backup_marker.read_bytes() == b"backup evidence"
        with pytest.raises(KeyError):
            manager.get(queued.job_id)
    finally:
        manager.close()


def test_workspace_deletion_rejects_root_and_symlink_records(
    web_config: WebAppConfig,
    tmp_path: Path,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    try:
        job_id = "c" * 32
        now = utc_now()
        root_record = JobRecord(
            job_id=job_id,
            created_at=now,
            updated_at=now,
            state=JobState.CANCELLED,
            workspace_dir=web_config.workspace_root.resolve(),
            expected_stages=(),
            progress_fraction=0.0,
            cancellation_requested=True,
        )
        manager._write_record(root_record)
        with pytest.raises(ConfigurationError, match="outside the configured workspace root"):
            manager.delete(
                job_id,
                JobDeleteRequest(confirm_job_id=job_id, delete_workspace=True),
            )

        outside = tmp_path / "outside-workspace"
        outside.mkdir()
        outside_marker = outside / "must-survive.bin"
        outside_marker.write_bytes(b"outside evidence")
        linked_workspace = web_config.workspace_root / "linked-workspace"
        linked_workspace.symlink_to(outside, target_is_directory=True)
        manager._write_record(root_record.model_copy(update={"workspace_dir": linked_workspace}))
        with pytest.raises(ConfigurationError, match="workspace is a symlink"):
            manager.delete(
                job_id,
                JobDeleteRequest(confirm_job_id=job_id, delete_workspace=True),
            )

        record_only = manager.delete(
            job_id,
            JobDeleteRequest(confirm_job_id=job_id),
        )
        assert record_only.workspace_removed is False
        assert record_only.workspace_retained is True
        assert outside_marker.read_bytes() == b"outside evidence"
        assert linked_workspace.is_symlink()
        with pytest.raises(KeyError):
            manager.get(job_id)
    finally:
        manager.close()


def test_completed_job_maintenance_backup_cleanup_and_restore(
    web_config: WebAppConfig,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    try:
        submitted = manager.submit(make_job_request(web_config, name="maintained"))
        completed = wait_for_terminal(manager, submitted.job_id)
        assert completed.state is JobState.COMPLETED
        assert completed.summary is not None

        unused = completed.workspace_dir / "stages" / "99-unused" / "stale"
        unused.mkdir(parents=True)
        (unused / "payload.bin").write_bytes(b"unreferenced-stage")

        overview = manager.maintenance(completed.job_id)
        assert overview.required_checks_passed is True
        assert overview.storage.current_workspace_bytes > 0
        assert overview.storage.sufficient_for_estimate is True
        assert overview.cleanup.reclaimable_bytes >= len(b"unreferenced-stage")
        assert overview.cleanup.candidates[0].path == "stages/99-unused/stale"
        assert overview.backups == ()

        backup = manager.create_backup(completed.job_id)
        archive, reopened = manager.backup_archive_path(backup.backup_id)
        assert archive.is_file()
        assert reopened == backup
        assert backup.archive_size_bytes == archive.stat().st_size
        assert manager.list_backups() == (backup,)

        restored = manager.restore_backup(backup.backup_id)
        assert restored.state is JobState.COMPLETED
        assert restored.exit_code == 0
        assert restored.workspace_dir != completed.workspace_dir
        assert restored.workspace_dir.parent == web_config.workspace_root.resolve()
        assert restored.summary is not None
        assert restored.summary.workflow_id == completed.summary.workflow_id
        original_model = next(
            item for item in completed.artifacts if item.artifact_id == "model_3mf"
        )
        restored_model = next(
            item for item in restored.artifacts if item.artifact_id == "model_3mf"
        )
        assert restored_model.sha256 == original_model.sha256
        assert manager.read_events(restored.job_id)[-1].message_key == "job.restored"

        with pytest.raises(ConfigurationError, match="confirmation"):
            manager.cleanup(completed.job_id, confirm_workflow_id="wrong")
        cleanup = manager.cleanup(
            completed.job_id,
            confirm_workflow_id=completed.summary.workflow_id,
        )
        assert cleanup.required_checks_passed is True
        assert cleanup.removed_paths == ("stages/99-unused/stale",)
        assert not unused.exists()
        assert manager.maintenance(completed.job_id).cleanup.reclaimable_bytes == 0
    finally:
        manager.close()
