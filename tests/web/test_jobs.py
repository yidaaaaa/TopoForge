from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from topoforge.exceptions import ConfigurationError
from topoforge.web.jobs import LocalJobManager, expected_workflow_stages
from topoforge.web.models import JobRecord, JobState, WebAppConfig
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
