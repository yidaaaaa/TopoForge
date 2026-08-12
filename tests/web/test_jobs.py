from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from topoforge.exceptions import ConfigurationError
from topoforge.util import sha256_file
from topoforge.web import jobs as jobs_module
from topoforge.web import security as security_module
from topoforge.web import worker as worker_module
from topoforge.web.jobs import LocalJobManager, expected_workflow_stages
from topoforge.web.models import (
    JobBatchDeleteApplyRequest,
    JobBatchDeleteMode,
    JobBatchDeletePlanRequest,
    JobDeleteRequest,
    JobDeletionInventory,
    JobError,
    JobRecord,
    JobState,
    JobTrashActionRequest,
    JobTrashActionTransaction,
    JobTrashJobInventory,
    JobTrashPurgeEntry,
    JobTrashRecord,
    JobTrashTransaction,
    JobTrashTransactionMove,
    JobTrashWorkspace,
    WebAppConfig,
    WorkerReady,
    WorkerResult,
    utc_now,
)
from topoforge.web.processes import (
    process_group_id,
    process_identity,
    worker_process_options,
)
from topoforge.web.security import WebManagerLease, write_exclusive_regular_bytes
from topoforge.web.server import is_loopback_host
from topoforge.workflow import WorkflowRunSummary, WorkflowStage, WorkflowState

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
        process = cast(
            "subprocess.Popen[bytes]",
            subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **worker_process_options(),
            ),
        )
        self._processes[record.job_id] = process
        identity = process_identity(process.pid)
        group = process_group_id(process.pid)
        assert identity is not None
        assert group is not None
        self._write_record(
            record.model_copy(
                update={
                    "state": JobState.RUNNING,
                    "pid": process.pid,
                    "process_identity": identity,
                    "process_group_id": group,
                    "current_stage": record.expected_stages[0],
                }
            ),
            message_key="job.started",
        )


def prepare_interrupted_trash_transaction(
    manager: LocalJobManager,
    *,
    job_id: str,
    workspace: Path,
    batch_id: str,
) -> JobTrashTransaction:
    request = JobBatchDeletePlanRequest(
        job_ids=(job_id,),
        mode=JobBatchDeleteMode.QUARANTINE_WORKSPACE,
    )
    plan = manager.plan_batch_delete(request)
    state_temporary = manager.trash_dir / f".{batch_id}.creating"
    state_destination = manager._trash_batch_dir(batch_id)
    workspace_temporary = manager.workspace_trash_dir / f".{batch_id}.creating"
    workspace_destination = manager._workspace_trash_batch_dir(batch_id)
    quarantined = workspace_destination / f"0000-{workspace.name}"
    created_at = utc_now()
    plan_item = plan.items[0]
    assert plan_item.job_record_inventory is not None
    assert plan_item.workspace_inventory is not None
    transaction = JobTrashTransaction(
        batch_id=batch_id,
        state_temporary=state_temporary,
        state_destination=state_destination,
        workspace_temporary=workspace_temporary,
        workspace_destination=workspace_destination,
        job_moves=(
            JobTrashTransactionMove(
                source=manager._job_dir(job_id),
                temporary=state_temporary / "jobs" / job_id,
                destination=state_destination / "jobs" / job_id,
            ),
        ),
        workspace_moves=(
            JobTrashTransactionMove(
                source=workspace,
                temporary=workspace_temporary / quarantined.name,
                destination=quarantined,
            ),
        ),
        trash_record=JobTrashRecord(
            batch_id=batch_id,
            plan_id=plan.plan_id,
            mode=plan.mode,
            created_at=created_at,
            purge_after=created_at + timedelta(days=7),
            job_ids=plan.job_ids,
            job_record_bytes=plan.job_record_bytes,
            job_inventories=(
                JobTrashJobInventory(
                    job_id=job_id,
                    inventory=plan_item.job_record_inventory,
                ),
            ),
            workspaces=(
                JobTrashWorkspace(
                    original_workspace=workspace,
                    quarantined_workspace=quarantined,
                    workspace_existed=True,
                    size_bytes=plan.workspace_bytes,
                    inventory=plan_item.workspace_inventory,
                ),
            ),
            total_quarantined_bytes=plan.total_target_bytes,
            backups_preserved=True,
            required_checks_passed=True,
        ),
    )
    transaction_path = manager._trash_transaction_path(batch_id)
    transaction_path.parent.mkdir(parents=True)
    jobs_module._atomic_write(transaction_path, transaction)
    (state_temporary / "jobs").mkdir(parents=True)
    workspace_temporary.mkdir(parents=True)
    return transaction


def create_cancelled_trash(
    manager: LocalJobManager,
    *,
    job_id: str,
    workspace_name: str,
) -> tuple[JobTrashRecord, Path, Path]:
    """Create one inventory-bound quarantined job/workspace test batch."""
    workspace = manager.config.workspace_root / workspace_name
    workspace.mkdir(parents=True)
    marker = workspace / "payload.bin"
    marker.write_bytes(b"inventory-bound-payload")
    now = utc_now()
    manager._write_record(
        JobRecord(
            job_id=job_id,
            created_at=now,
            updated_at=now,
            state=JobState.CANCELLED,
            workspace_dir=workspace,
            expected_stages=(),
            progress_fraction=0.0,
            cancellation_requested=True,
        )
    )
    request = JobBatchDeletePlanRequest(
        job_ids=(job_id,),
        mode=JobBatchDeleteMode.QUARANTINE_WORKSPACE,
    )
    plan = manager.plan_batch_delete(request)
    trash = manager.apply_batch_delete(
        JobBatchDeleteApplyRequest(
            job_ids=plan.job_ids,
            mode=plan.mode,
            confirm_plan_id=plan.plan_id,
        )
    )
    return trash, workspace, marker


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
    manager.start()
    try:
        listing = manager.list_files(root)
        assert [entry.name for entry in listing.entries] == ["folder", "terrain.tif"]
        assert listing.entries[1].selectable is True
        with pytest.raises(ConfigurationError, match="outside configured input roots"):
            manager.list_files(tmp_path)
    finally:
        manager.close()


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
    monkeypatch.setattr(jobs_module, "inspect_process_identity", lambda _pid: "fixture-process")
    manager = LocalJobManager(configured)
    ready = WorkerReady(
        job_id="0" * 32,
        launch_nonce="0" * 32,
        request_sha256="0" * 64,
        pid=FakeProcess.pid,
        process_identity="fixture-worker",
        process_group_id=FakeProcess.pid,
        jobs_root_device=0,
        jobs_root_inode=0,
    )

    def ready_after_start(
        record: JobRecord,
        _process: subprocess.Popen[bytes],
    ) -> tuple[WorkerReady, str]:
        assert record.state is JobState.STARTING
        assert not manager._launch_gate_path(record.job_id).exists()
        return (
            ready.model_copy(
                update={
                    "job_id": record.job_id,
                    "launch_nonce": record.launch_nonce,
                    "request_sha256": record.request_sha256,
                }
            ),
            "a" * 64,
        )

    monkeypatch.setattr(manager, "_wait_for_worker_ready", ready_after_start)
    monkeypatch.setattr(manager, "_verify_worker_ready_live", lambda _ready: None)
    monkeypatch.setattr(manager, "_publish_launch_gate", lambda _record: None)
    manager.start()
    try:
        manager.submit(make_job_request(configured, name="worker-bambu-environment"))
        assert captured_environments[0]["TOPOFORGE_BAMBU_STUDIO"] == str(executable.resolve())
    finally:
        manager.close()


def test_worker_start_fails_closed_when_process_identity_is_unavailable(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated: list[tuple[int, str | None, int | None]] = []

    class FakeProcess:
        pid = 543_210

        def __init__(self) -> None:
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = -9
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    def capturing_terminator(
        pid: int,
        *,
        expected_identity: str | None = None,
        process_group: int | None = None,
    ) -> None:
        terminated.append((pid, expected_identity, process_group))
        fake_process.returncode = -9

    fake_process = FakeProcess()
    parent_pid = os.getpid()
    parent_identity = process_identity(parent_pid)
    assert parent_identity is not None
    monkeypatch.setattr(
        jobs_module,
        "inspect_process_identity",
        lambda pid: parent_identity if pid == parent_pid else None,
    )
    monkeypatch.setattr(jobs_module.subprocess, "Popen", lambda *_args, **_kwargs: fake_process)
    monkeypatch.setattr(jobs_module, "terminate_process_tree", capturing_terminator)
    manager = LocalJobManager(web_config)

    def reject_missing_ready(
        record: JobRecord,
        _process: subprocess.Popen[bytes],
    ) -> tuple[WorkerReady, str]:
        assert record.state is JobState.STARTING
        assert not manager._launch_gate_path(record.job_id).exists()
        raise RuntimeError("worker did not publish containment-ready evidence")

    monkeypatch.setattr(manager, "_wait_for_worker_ready", reject_missing_ready)
    manager.start()
    try:
        failed = manager.submit(make_job_request(web_config, name="worker-without-identity"))
        assert failed.state is JobState.FAILED
        assert failed.pid is None
        assert failed.process_identity is None
        assert failed.process_group_id is None
        assert failed.error is not None
        assert failed.error.code == "worker-start-failed"
        assert failed.error.exception_type == "RuntimeError"
        assert terminated == [(FakeProcess.pid, None, None)]

        manager.refresh()
        assert len(terminated) == 1
        assert manager.get(failed.job_id, refresh=False).state is JobState.FAILED
    finally:
        manager.close()


def test_bambu_project_manifest_publishes_recommended_project_artifacts(
    web_config: WebAppConfig,
) -> None:
    manager = LocalJobManager(web_config)
    workspace = web_config.workspace_root / "project-artifacts"
    project_root = workspace / "stages" / "70-project" / "fixture"
    tile_root = project_root / "tiles" / "tile-r0000-c0000"
    tile_root.mkdir(parents=True)
    core = workspace / "model.3mf"
    core.write_bytes(b"core-geometry")
    project = tile_root / "model.bambu-p2s.3mf"
    project.write_bytes(b"bambu-project")
    validation = tile_root / "project_validation.json"
    validation.write_text("{}\n", encoding="utf-8")
    manifest = project_root / "bambu-tile-project-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "topoforge-bambu-tile-project-assembly-v1",
                "required_checks_passed": True,
                "tile_count": 1,
                "tiles": [
                    {
                        "tile_id": "tile-r0000-c0000",
                        "required_checks_passed": True,
                        "files": {
                            "bambu_project_3mf": "tiles/tile-r0000-c0000/model.bambu-p2s.3mf"
                        },
                        "sha256": {"bambu_project_3mf": sha256_file(project)},
                        "validation_path": ("tiles/tile-r0000-c0000/project_validation.json"),
                        "validation_sha256": sha256_file(validation),
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    now = utc_now()
    record = JobRecord(
        job_id="d" * 32,
        created_at=now,
        updated_at=now,
        state=JobState.COMPLETED,
        workspace_dir=workspace,
        expected_stages=(),
        progress_fraction=1.0,
        exit_code=0,
    )

    artifacts = manager._artifacts(
        record,
        {
            "model_3mf": str(core),
            "project_manifest": str(manifest),
        },
    )
    by_id = {artifact.artifact_id: artifact for artifact in artifacts}

    assert by_id["model_3mf"].filename == "model.3mf"
    assert by_id["bambu_project_3mf"].filename == "model.bambu-p2s.3mf"
    assert by_id["bambu_project_3mf"].sha256 == sha256_file(project)
    assert by_id["bambu_project_3mf"].download_url == (
        f"/api/v1/jobs/{record.job_id}/artifacts/bambu_project_3mf"
    )
    assert by_id["bambu_project_validation"].sha256 == sha256_file(validation)

    retained = record.model_copy(
        update={
            "summary": WorkflowRunSummary(
                workflow_id="local-project-artifacts",
                state=WorkflowState.COMPLETED,
                source_mode="local",
                final_stage=WorkflowStage.PROJECT,
                ready_stages=(WorkflowStage.PROJECT,),
                metrics={},
                artifacts={
                    "model_3mf": str(core),
                    "project_manifest": str(manifest),
                },
                required_checks_passed=True,
            ),
            "artifacts": (),
        }
    )
    manager.start()
    try:
        manager._write_record(retained)
        manager.refresh()
        backfilled = manager.get(record.job_id, refresh=False)
        assert any(artifact.artifact_id == "bambu_project_3mf" for artifact in backfilled.artifacts)
    finally:
        manager.close()


def test_bambu_project_manifest_rejects_workspace_escape(
    web_config: WebAppConfig,
) -> None:
    manager = LocalJobManager(web_config)
    workspace = web_config.workspace_root / "escaping-project-artifacts"
    project_root = workspace / "project"
    project_root.mkdir(parents=True)
    outside = web_config.workspace_root / "outside-project.3mf"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"outside")
    validation = project_root / "project_validation.json"
    validation.write_text("{}\n", encoding="utf-8")
    manifest = project_root / "bambu-tile-project-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "topoforge-bambu-tile-project-assembly-v1",
                "required_checks_passed": True,
                "tile_count": 1,
                "tiles": [
                    {
                        "tile_id": "tile-r0000-c0000",
                        "required_checks_passed": True,
                        "files": {"bambu_project_3mf": "../../outside-project.3mf"},
                        "sha256": {"bambu_project_3mf": sha256_file(outside)},
                        "validation_path": "project_validation.json",
                        "validation_sha256": sha256_file(validation),
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    now = utc_now()
    record = JobRecord(
        job_id="e" * 32,
        created_at=now,
        updated_at=now,
        state=JobState.COMPLETED,
        workspace_dir=workspace,
        expected_stages=(),
        progress_fraction=1.0,
        exit_code=0,
    )

    with pytest.raises(ConfigurationError, match="escapes the workflow workspace"):
        manager._artifacts(record, {"project_manifest": str(manifest)})


def test_isolated_worker_completes_and_publishes_checksum_artifacts(
    web_config: WebAppConfig,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    try:
        submitted = manager.submit(make_job_request(web_config, name="complete"))
        completed = wait_for_terminal(manager, submitted.job_id)
        assert completed.state is JobState.COMPLETED, completed.error
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


def test_running_job_recovers_and_cancels_after_manager_restart(
    web_config: WebAppConfig,
) -> None:
    first = SlowJobManager(web_config)
    recovered: LocalJobManager | None = None
    process: subprocess.Popen[bytes] | None = None
    first.start()
    try:
        submitted = first.submit(make_job_request(web_config, name="restart-recovery"))
        process = first._processes[submitted.job_id]
        assert first.get(submitted.job_id).state is JobState.RUNNING
        first.close()

        recovered = LocalJobManager(web_config)
        recovered.start()
        running = recovered.get(submitted.job_id)
        assert running.state is JobState.RUNNING
        assert running.pid == process.pid

        cancelling = recovered.cancel(submitted.job_id)
        assert cancelling.state is JobState.CANCELLING
        cancelled = wait_for_terminal(recovered, submitted.job_id, timeout_seconds=15)
        assert cancelled.state is JobState.CANCELLED
        assert cancelled.cancellation_requested is True
        assert cancelled.pid is None
    finally:
        if recovered is not None:
            recovered.close()
        first.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


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


def test_batch_delete_plan_is_deterministic_and_reports_active_and_shared_blockers(
    web_config: WebAppConfig,
) -> None:
    manager = SlowJobManager(web_config)
    manager.start()
    try:
        running = manager.submit(make_job_request(web_config, name="batch-shared"))
        queued = manager.submit(make_job_request(web_config, name="batch-shared"))
        active_plan = manager.plan_batch_delete(
            JobBatchDeletePlanRequest(
                job_ids=(running.job_id,),
                mode=JobBatchDeleteMode.QUARANTINE_WORKSPACE,
            )
        )
        assert active_plan.required_checks_passed is False
        assert any("job is active" in blocker for blocker in active_plan.blockers)
        assert any("unselected jobs" in blocker for blocker in active_plan.blockers)

        manager.cancel(queued.job_id)
        manager.cancel(running.job_id)
        assert wait_for_terminal(manager, running.job_id, timeout_seconds=15).state is (
            JobState.CANCELLED
        )

        shared_plan = manager.plan_batch_delete(
            JobBatchDeletePlanRequest(
                job_ids=(running.job_id,),
                mode=JobBatchDeleteMode.QUARANTINE_WORKSPACE,
            )
        )
        assert shared_plan.required_checks_passed is False
        assert shared_plan.items[0].unselected_reference_job_ids == (queued.job_id,)

        request = JobBatchDeletePlanRequest(
            job_ids=(queued.job_id, running.job_id),
            mode=JobBatchDeleteMode.QUARANTINE_WORKSPACE,
        )
        first = manager.plan_batch_delete(request)
        second = manager.plan_batch_delete(request)
        assert first == second
        assert first.required_checks_passed is True
        assert first.job_ids == tuple(sorted((running.job_id, queued.job_id)))
        assert first.unique_workspace_count == 1

        record_only = manager.plan_batch_delete(
            JobBatchDeletePlanRequest(
                job_ids=(running.job_id,),
                mode=JobBatchDeleteMode.RECORD_ONLY,
            )
        )
        assert record_only.required_checks_passed is True
        assert record_only.workspace_bytes == 0
    finally:
        manager.close()


def test_batch_workspace_quarantine_restore_and_permanent_purge(
    web_config: WebAppConfig,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    try:
        now = utc_now()
        job_ids = ("d" * 32, "e" * 32)
        markers: dict[str, Path] = {}
        for index, job_id in enumerate(job_ids):
            workspace = web_config.workspace_root / f"batch-project-{index}"
            workspace.mkdir(parents=True)
            marker = workspace / "retained.bin"
            marker.write_bytes(f"workspace-{index}".encode())
            markers[job_id] = marker
            manager._write_record(
                JobRecord(
                    job_id=job_id,
                    created_at=now,
                    updated_at=now,
                    state=JobState.CANCELLED,
                    workspace_dir=workspace,
                    expected_stages=(),
                    progress_fraction=0.0,
                    cancellation_requested=True,
                )
            )
        backup_marker = manager.backups_dir / "must-survive.bin"
        backup_marker.write_bytes(b"backup evidence")

        plan_request = JobBatchDeletePlanRequest(
            job_ids=job_ids,
            mode=JobBatchDeleteMode.QUARANTINE_WORKSPACE,
        )
        plan = manager.plan_batch_delete(plan_request)
        applied = manager.apply_batch_delete(
            JobBatchDeleteApplyRequest(
                **plan_request.model_dump(),
                confirm_plan_id=plan.plan_id,
            )
        )
        assert applied.job_ids == job_ids
        assert applied.total_quarantined_bytes == plan.total_target_bytes
        assert not any(manager.trash_transactions_dir.glob("*/transaction.json"))
        assert len(applied.workspaces) == 2
        assert all(workspace.quarantined_workspace is not None for workspace in applied.workspaces)
        assert manager.list() == ()
        assert all(not marker.exists() for marker in markers.values())
        assert manager.list_trash() == (applied,)

        with pytest.raises(ConfigurationError, match="confirmation"):
            manager.restore_trash(
                applied.batch_id,
                JobTrashActionRequest(confirm_batch_id="0" * 32),
            )
        restored = manager.restore_trash(
            applied.batch_id,
            JobTrashActionRequest(confirm_batch_id=applied.batch_id),
        )
        assert restored.action == "restored"
        assert restored.required_checks_passed is True
        assert {record.job_id for record in manager.list()} == set(job_ids)
        assert all(marker.is_file() for marker in markers.values())
        assert manager.list_trash() == ()

        repeat_plan = manager.plan_batch_delete(plan_request)
        repeat = manager.apply_batch_delete(
            JobBatchDeleteApplyRequest(
                **plan_request.model_dump(),
                confirm_plan_id=repeat_plan.plan_id,
            )
        )
        purged = manager.purge_trash(
            repeat.batch_id,
            JobTrashActionRequest(confirm_batch_id=repeat.batch_id),
        )
        assert purged.action == "purged"
        assert purged.affected_bytes > 0
        assert manager.list_trash() == ()
        assert manager.list() == ()
        assert all(not marker.exists() for marker in markers.values())
        assert backup_marker.read_bytes() == b"backup evidence"
        assert (manager.deletion_audit_dir / f"{applied.batch_id}-restored.json").is_file()
        assert (manager.deletion_audit_dir / f"{repeat.batch_id}-purged.json").is_file()
    finally:
        manager.close()


def test_start_rolls_back_unpublished_trash_transaction(
    web_config: WebAppConfig,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    job_id = "a" * 32
    workspace = web_config.workspace_root / "interrupted-rollback"
    workspace.mkdir(parents=True)
    marker = workspace / "retained.bin"
    marker.write_bytes(b"rollback evidence")
    now = utc_now()
    manager._write_record(
        JobRecord(
            job_id=job_id,
            created_at=now,
            updated_at=now,
            state=JobState.CANCELLED,
            workspace_dir=workspace,
            expected_stages=(),
            progress_fraction=0.0,
            cancellation_requested=True,
        )
    )
    transaction = prepare_interrupted_trash_transaction(
        manager,
        job_id=job_id,
        workspace=workspace,
        batch_id="1" * 32,
    )
    manager.close()

    transaction.workspace_moves[0].source.replace(transaction.workspace_moves[0].temporary)
    transaction.job_moves[0].source.replace(transaction.job_moves[0].temporary)
    assert not marker.exists()
    assert not manager._job_dir(job_id).exists()

    recovered = LocalJobManager(web_config)
    recovered.start()
    try:
        assert recovered.get(job_id).state is JobState.CANCELLED
        assert marker.read_bytes() == b"rollback evidence"
        assert recovered.list_trash() == ()
        assert not recovered._trash_transaction_path(transaction.batch_id).exists()
        assert not transaction.state_temporary.exists()
        assert not transaction.workspace_temporary.exists()
    finally:
        recovered.close()


def test_start_completes_published_workspace_trash_transaction(
    web_config: WebAppConfig,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    job_id = "b" * 32
    workspace = web_config.workspace_root / "interrupted-publish"
    workspace.mkdir(parents=True)
    marker = workspace / "retained.bin"
    marker.write_bytes(b"publish evidence")
    now = utc_now()
    manager._write_record(
        JobRecord(
            job_id=job_id,
            created_at=now,
            updated_at=now,
            state=JobState.CANCELLED,
            workspace_dir=workspace,
            expected_stages=(),
            progress_fraction=0.0,
            cancellation_requested=True,
        )
    )
    transaction = prepare_interrupted_trash_transaction(
        manager,
        job_id=job_id,
        workspace=workspace,
        batch_id="2" * 32,
    )
    manager.close()

    transaction.workspace_moves[0].source.replace(transaction.workspace_moves[0].temporary)
    transaction.job_moves[0].source.replace(transaction.job_moves[0].temporary)
    jobs_module._atomic_write(transaction.state_temporary / "trash.json", transaction.trash_record)
    transaction.workspace_temporary.replace(transaction.workspace_destination)
    assert transaction.workspace_destination.is_dir()
    assert transaction.state_temporary.is_dir()
    assert not transaction.state_destination.exists()

    recovered = LocalJobManager(web_config)
    recovered.start()
    try:
        assert recovered.list() == ()
        assert recovered.list_trash() == (transaction.trash_record,)
        assert transaction.state_destination.is_dir()
        assert not transaction.state_temporary.exists()
        assert not recovered._trash_transaction_path(transaction.batch_id).exists()
        restored = recovered.restore_trash(
            transaction.batch_id,
            JobTrashActionRequest(confirm_batch_id=transaction.batch_id),
        )
        assert restored.action == "restored"
        assert recovered.get(job_id).state is JobState.CANCELLED
        assert marker.read_bytes() == b"publish evidence"
    finally:
        recovered.close()


def test_completed_job_maintenance_backup_cleanup_and_restore(
    web_config: WebAppConfig,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    try:
        submitted = manager.submit(make_job_request(web_config, name="maintained"))
        completed = wait_for_terminal(manager, submitted.job_id)
        assert completed.state is JobState.COMPLETED, completed.error
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
            manager.cleanup(
                completed.job_id,
                confirm_workflow_id="wrong",
                confirm_plan_id=overview.cleanup.plan_id,
            )
        cleanup = manager.cleanup(
            completed.job_id,
            confirm_workflow_id=completed.summary.workflow_id,
            confirm_plan_id=overview.cleanup.plan_id,
        )
        assert cleanup.required_checks_passed is True
        assert cleanup.removed_paths == ("stages/99-unused/stale",)
        assert not unused.exists()
        assert manager.maintenance(completed.job_id).cleanup.reclaimable_bytes == 0

        plan_request = JobBatchDeletePlanRequest(
            job_ids=(restored.job_id,),
            mode=JobBatchDeleteMode.BACKUP_AND_QUARANTINE,
        )
        plan = manager.plan_batch_delete(plan_request)
        assert plan.required_checks_passed is True
        assert plan.backup_job_ids == (restored.job_id,)
        trashed = manager.apply_batch_delete(
            JobBatchDeleteApplyRequest(
                **plan_request.model_dump(),
                confirm_plan_id=plan.plan_id,
            )
        )
        assert len(trashed.backup_ids) == 1
        assert manager.backup_archive_path(trashed.backup_ids[0])[0].is_file()
        assert not restored.workspace_dir.exists()
        manager.restore_trash(
            trashed.batch_id,
            JobTrashActionRequest(confirm_batch_id=trashed.batch_id),
        )
        assert manager.get(restored.job_id).state is JobState.COMPLETED
        assert restored.workspace_dir.is_dir()
    finally:
        manager.close()


def test_recovered_pid_identity_mismatch_fails_closed(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalJobManager(web_config)
    now = utc_now()
    job_id = "1" * 32
    record = JobRecord(
        job_id=job_id,
        created_at=now,
        updated_at=now,
        state=JobState.RUNNING,
        workspace_dir=web_config.workspace_root / "reused-pid",
        expected_stages=(),
        progress_fraction=0.0,
        pid=os.getpid(),
        process_identity="linux:stale-worker:1",
        process_group_id=os.getpgrp(),
    )
    manager._write_record(record)
    monkeypatch.setattr(
        jobs_module,
        "terminate_process_tree",
        lambda *_args, **_kwargs: pytest.fail("recovery must not signal a reused PID"),
    )

    manager.start()
    try:
        failed = manager.get(job_id)
        assert failed.state is JobState.FAILED
        assert failed.error is not None
        assert failed.error.code == "worker-result-missing"
        assert failed.pid is None
        assert failed.process_identity is None
        assert failed.process_group_id is None
    finally:
        manager.close()


def test_recovered_worker_inspection_failure_blocks_queue(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = web_config.model_copy(update={"poll_interval_seconds": 5.0})
    manager = LocalJobManager(configured)
    now = utc_now()
    running_id = "6" * 32
    manager._write_record(
        JobRecord(
            job_id=running_id,
            created_at=now,
            updated_at=now,
            state=JobState.RUNNING,
            workspace_dir=web_config.workspace_root / "inspection-unavailable",
            expected_stages=(),
            progress_fraction=0.0,
            pid=424_243,
            process_identity="linux:expected-worker:123",
            process_group_id=424_243,
        )
    )
    queued_id = "7" * 32
    manager._write_record(
        JobRecord(
            job_id=queued_id,
            created_at=now + timedelta(microseconds=1),
            updated_at=now + timedelta(microseconds=1),
            state=JobState.QUEUED,
            workspace_dir=web_config.workspace_root / "after-inspection-failure",
            expected_stages=(),
            progress_fraction=0.0,
        )
    )
    inspection: bool | OSError = OSError("simulated process inspection refusal")
    started: list[str] = []

    def inspect_containment(*_args: object, **_kwargs: object) -> bool:
        if isinstance(inspection, OSError):
            raise inspection
        return inspection

    monkeypatch.setattr(jobs_module, "process_containment_is_alive", inspect_containment)
    monkeypatch.setattr(
        manager,
        "_start_job",
        lambda queued_record: started.append(queued_record.job_id),
    )

    manager.start()
    try:
        manager.refresh()
        blocked = manager.get(running_id, refresh=False)
        assert blocked.state is JobState.RUNNING
        assert blocked.pid == 424_243
        assert blocked.process_identity == "linux:expected-worker:123"
        assert blocked.process_group_id == 424_243
        assert blocked.error is not None
        assert blocked.error.code == "worker-inspection-unavailable"
        assert manager.get(queued_id, refresh=False).state is JobState.QUEUED
        assert started == []

        inspection = True
        manager.refresh()
        recovered = manager.get(running_id, refresh=False)
        assert recovered.state is JobState.RUNNING
        assert recovered.error is None
        assert recovered.pid == 424_243
        assert manager.get(queued_id, refresh=False).state is JobState.QUEUED
        assert started == []

        inspection = False
        manager.refresh()
        terminal = manager.get(running_id, refresh=False)
        assert terminal.state is JobState.FAILED
        assert terminal.error is not None
        assert terminal.error.code == "worker-result-missing"
        assert terminal.pid is None
        assert terminal.process_identity is None
        assert terminal.process_group_id is None
        assert started == [queued_id]
    finally:
        manager.close()


def test_job_record_accepts_deployed_v1_and_rejects_unknown_schema(
    tmp_path: Path,
) -> None:
    now = utc_now()
    current = JobRecord(
        job_id="f" * 32,
        created_at=now,
        updated_at=now,
        state=JobState.QUEUED,
        workspace_dir=tmp_path / "schema-record",
        expected_stages=(),
        progress_fraction=0.0,
    )
    payload = current.model_dump(mode="json")

    legacy = dict(payload)
    legacy["schema_version"] = "topoforge-web-job-v1"
    assert JobRecord.model_validate(legacy).schema_version == "topoforge-web-job-v1"

    unknown = dict(payload)
    unknown["schema_version"] = "topoforge-web-job-v999"
    with pytest.raises(ValueError, match="schema_version"):
        JobRecord.model_validate(unknown)


def test_legacy_live_worker_without_identity_blocks_queue_until_pid_is_gone(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = web_config.model_copy(update={"poll_interval_seconds": 5.0})
    manager = LocalJobManager(configured)
    now = utc_now()
    legacy_id = "4" * 32
    fake_pid = 424_242
    manager._write_record(
        JobRecord(
            schema_version="topoforge-web-job-v1",
            job_id=legacy_id,
            created_at=now,
            updated_at=now,
            state=JobState.RUNNING,
            workspace_dir=web_config.workspace_root / "legacy-running",
            expected_stages=(),
            progress_fraction=0.0,
            pid=fake_pid,
        )
    )
    queued_id = "5" * 32
    manager._write_record(
        JobRecord(
            job_id=queued_id,
            created_at=now + timedelta(microseconds=1),
            updated_at=now + timedelta(microseconds=1),
            state=JobState.QUEUED,
            workspace_dir=web_config.workspace_root / "after-legacy",
            expected_stages=(),
            progress_fraction=0.0,
        )
    )
    pid_alive: bool | None = None
    started: list[str] = []

    def inspect_legacy_pid(_pid: int) -> bool:
        if pid_alive is None:
            raise OSError("simulated process inspection refusal")
        return pid_alive

    monkeypatch.setattr(jobs_module, "process_is_alive", inspect_legacy_pid)
    monkeypatch.setattr(
        manager,
        "_start_job",
        lambda queued_record: started.append(queued_record.job_id),
    )

    manager.start()
    try:
        manager.refresh()
        inspection_blocked = manager.get(legacy_id, refresh=False)
        assert inspection_blocked.state is JobState.RUNNING
        assert inspection_blocked.pid == fake_pid
        assert inspection_blocked.error is not None
        assert inspection_blocked.error.code == "worker-identity-unavailable"
        assert inspection_blocked.error.exception_type == "OSError"
        assert manager.get(queued_id, refresh=False).state is JobState.QUEUED
        assert started == []

        pid_alive = True
        manager.refresh()
        blocked = manager.get(legacy_id, refresh=False)
        assert blocked.state is JobState.RUNNING
        assert blocked.pid == fake_pid
        assert blocked.error is not None
        assert blocked.error.code == "worker-identity-unavailable"
        assert manager.get(queued_id, refresh=False).state is JobState.QUEUED
        assert started == []

        cancelling = manager.cancel(legacy_id)
        assert cancelling.state is JobState.CANCELLING
        assert cancelling.pid == fake_pid
        assert cancelling.error is not None
        assert cancelling.error.code == "worker-identity-unavailable"
        assert started == []

        pid_alive = False
        manager.refresh()
        terminal = manager.get(legacy_id, refresh=False)
        assert terminal.state is JobState.CANCELLED
        assert terminal.pid is None
        assert terminal.process_identity is None
        assert terminal.process_group_id is None
        assert started == [queued_id]
    finally:
        manager.close()


def test_invalid_terminal_result_fails_job_and_starts_next_queue_entry(
    web_config: WebAppConfig,
) -> None:
    manager = LocalJobManager(web_config)
    now = utc_now()
    broken_id = "2" * 32
    broken = JobRecord(
        job_id=broken_id,
        created_at=now,
        updated_at=now,
        state=JobState.RUNNING,
        workspace_dir=web_config.workspace_root / "broken-result",
        expected_stages=(),
        progress_fraction=0.0,
        pid=999_999,
        process_identity="linux:missing-worker:1",
        process_group_id=999_999,
    )
    manager._write_record(broken)
    manager._result_path(broken_id).write_text("{not-json\n", encoding="utf-8")

    queued_id = "3" * 32
    request = make_job_request(web_config, name="after-broken-result", missing_source=True)
    queued = JobRecord(
        job_id=queued_id,
        created_at=now + timedelta(microseconds=1),
        updated_at=now + timedelta(microseconds=1),
        state=JobState.QUEUED,
        workspace_dir=request.launch.workspace_dir,
        expected_stages=expected_workflow_stages(request),
        progress_fraction=0.0,
    )
    manager._write_record(queued)
    manager._request_path(queued_id).write_text(request.model_dump_json(), encoding="utf-8")

    manager.start()
    try:
        failed = manager.get(broken_id)
        assert failed.state is JobState.FAILED
        assert failed.error is not None
        assert failed.error.code == "worker-result-invalid"
        assert failed.error.exception_type == "ConfigurationError"

        next_terminal = wait_for_terminal(manager, queued_id, timeout_seconds=15)
        assert next_terminal.state is JobState.FAILED
        assert next_terminal.error is not None
        assert next_terminal.error.code == "workflow-execution-failed"
    finally:
        manager.close()


def test_cancellation_failure_is_persisted_and_retryable(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SlowJobManager(web_config)
    original_terminator = jobs_module.terminate_process_tree
    manager.start()
    try:
        submitted = manager.submit(make_job_request(web_config, name="cancel-retry"))

        def fail_termination(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("simulated safe termination refusal")

        monkeypatch.setattr(jobs_module, "terminate_process_tree", fail_termination)
        manager.cancel(submitted.job_id)
        deadline = time.monotonic() + 5.0
        observed = manager.get(submitted.job_id, refresh=False)
        while observed.error is None and time.monotonic() < deadline:
            time.sleep(0.01)
            observed = manager.get(submitted.job_id, refresh=False)
        assert observed.state is JobState.CANCELLING
        assert observed.error is not None
        assert observed.error.code == "worker-termination-failed"

        monkeypatch.setattr(jobs_module, "terminate_process_tree", original_terminator)
        retried = manager.cancel(submitted.job_id)
        assert retried.error is None
        assert wait_for_terminal(manager, submitted.job_id, timeout_seconds=15).state is (
            JobState.CANCELLED
        )
    finally:
        monkeypatch.setattr(jobs_module, "terminate_process_tree", original_terminator)
        manager.close()
        for process in manager._processes.values():
            if process.poll() is None:
                process.kill()


def test_starting_spawn_crash_never_requeues_or_releases_worker(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedManagerCrash(BaseException):
        pass

    class FakeProcess:
        pid = 876_543

    parent_pid = os.getpid()
    parent_identity = process_identity(parent_pid)
    assert parent_identity is not None

    monkeypatch.setattr(
        jobs_module,
        "inspect_process_identity",
        lambda pid: parent_identity if pid == parent_pid else None,
    )
    monkeypatch.setattr(
        jobs_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        jobs_module,
        "terminate_process_tree",
        lambda *_args, **_kwargs: pytest.fail("simulated manager crash must not signal a PID"),
    )

    configured = web_config.model_copy(update={"poll_interval_seconds": 5.0})
    manager = LocalJobManager(configured)
    monkeypatch.setattr(
        manager,
        "_wait_for_worker_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SimulatedManagerCrash()),
    )
    manager.start()
    try:
        with pytest.raises(SimulatedManagerCrash):
            manager.submit(make_job_request(web_config, name="starting-crash"))
        starting = manager._all_records()[0]
        assert starting.state is JobState.STARTING
        assert starting.launch_nonce is not None
        assert starting.launch_gate_deadline is not None
        assert not manager._launch_gate_path(starting.job_id).exists()
    finally:
        manager.close()

    monkeypatch.setattr(
        jobs_module,
        "inspect_process_identity",
        lambda pid: parent_identity if pid == parent_pid else None,
    )
    recovered = LocalJobManager(configured)
    started: list[str] = []
    monkeypatch.setattr(
        recovered,
        "_start_job",
        lambda record: started.append(record.job_id),
    )
    recovered.start()
    try:
        queued = recovered.submit(make_job_request(web_config, name="after-starting-crash"))
        assert queued.state is JobState.QUEUED
        blocked = recovered.get(starting.job_id)
        assert blocked.state is JobState.STARTING
        assert blocked.error is not None
        assert blocked.error.code == "worker-start-recovery-blocked"
        assert started == []

        monkeypatch.setattr(jobs_module, "inspect_process_identity", lambda _pid: None)
        recovered.refresh()
        failed = recovered.get(starting.job_id, refresh=False)
        assert failed.state is JobState.FAILED
        assert failed.error is not None
        assert failed.error.code == "worker-start-interrupted"
        assert started == [queued.job_id]
    finally:
        recovered.close()


def test_starting_crash_after_ready_never_releases_or_replaces_worker(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedManagerCrash(BaseException):
        pass

    class FakeProcess:
        pid = 876_544

        @staticmethod
        def poll() -> None:
            return None

    parent_identity = process_identity(os.getpid())
    assert parent_identity is not None
    monkeypatch.setattr(jobs_module, "inspect_process_identity", lambda _pid: parent_identity)
    monkeypatch.setattr(
        jobs_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: FakeProcess(),
    )
    configured = web_config.model_copy(update={"poll_interval_seconds": 5.0})
    manager = LocalJobManager(configured)

    def publish_ready(
        record: JobRecord,
        _process: subprocess.Popen[bytes],
    ) -> tuple[WorkerReady, str]:
        jobs_identity = manager._owned_identity(manager.jobs_dir)
        assert record.launch_nonce is not None
        assert record.request_sha256 is not None
        ready = WorkerReady(
            job_id=record.job_id,
            launch_nonce=record.launch_nonce,
            request_sha256=record.request_sha256,
            pid=FakeProcess.pid,
            process_identity="fixture-ready-worker",
            process_group_id=FakeProcess.pid,
            jobs_root_device=jobs_identity[0],
            jobs_root_inode=jobs_identity[1],
        )
        payload = jobs_module._canonical_bytes(ready)
        security_module.write_exclusive_owned_regular_bytes(
            manager._worker_ready_path(record.job_id),
            payload,
            root=manager.jobs_dir,
            root_identity=jobs_identity,
            context="test worker containment-ready record",
        )
        return ready, hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(manager, "_wait_for_worker_ready", publish_ready)
    monkeypatch.setattr(
        manager,
        "_verify_worker_ready_live",
        lambda _ready: (_ for _ in ()).throw(SimulatedManagerCrash()),
    )
    manager.start()
    try:
        with pytest.raises(SimulatedManagerCrash):
            manager.submit(make_job_request(configured, name="crash-after-ready"))
        starting = manager._all_records()[0]
        assert starting.state is JobState.STARTING
        assert starting.worker_ready_sha256 is not None
        assert starting.pid == FakeProcess.pid
        assert not manager._launch_gate_path(starting.job_id).exists()
    finally:
        manager.close()

    monkeypatch.setattr(
        jobs_module,
        "process_containment_is_alive",
        lambda *_args, **_kwargs: True,
    )
    recovered = LocalJobManager(configured)
    started: list[str] = []
    monkeypatch.setattr(
        recovered,
        "_start_job",
        lambda queued_record: started.append(queued_record.job_id),
    )
    recovered.start()
    try:
        blocked = recovered.get(starting.job_id)
        assert blocked.state is JobState.STARTING
        assert blocked.worker_ready_sha256 == starting.worker_ready_sha256
        assert blocked.error is not None
        assert blocked.error.code == "worker-start-recovery-blocked"
        assert not recovered._launch_gate_path(starting.job_id).exists()

        queued = recovered.submit(make_job_request(configured, name="queued-after-ready-crash"))
        assert queued.state is JobState.QUEUED
        assert started == []
        assert not recovered._launch_gate_path(starting.job_id).exists()
    finally:
        recovered.close()


def test_starting_cancellation_waits_for_identity_bound_ready_record(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = web_config.model_copy(update={"poll_interval_seconds": 5.0})
    manager = LocalJobManager(configured)
    now = utc_now()
    parent_identity = process_identity(os.getpid())
    assert parent_identity is not None
    job_id = "f" * 32
    starting = manager._write_record(
        JobRecord(
            job_id=job_id,
            created_at=now,
            updated_at=now,
            state=JobState.STARTING,
            workspace_dir=configured.workspace_root / "cancel-starting-ready",
            expected_stages=(),
            progress_fraction=0.0,
            request_sha256="1" * 64,
            launch_nonce="2" * 32,
            launch_gate_deadline=now + timedelta(seconds=30),
            launch_parent_pid=os.getpid(),
            launch_parent_identity=parent_identity,
        )
    )
    manager.start()
    try:
        blocked = manager.cancel(job_id)
        assert blocked.state is JobState.STARTING
        assert blocked.cancellation_requested is True
        assert blocked.pid is None
        assert blocked.error is not None
        assert blocked.error.code == "worker-start-cancellation-blocked"
        assert manager._running_count() == 1
        assert not manager._launch_gate_path(job_id).exists()

        jobs_identity = manager._owned_identity(manager.jobs_dir)
        ready = WorkerReady(
            job_id=job_id,
            launch_nonce=starting.launch_nonce or "",
            request_sha256=starting.request_sha256 or "",
            pid=654_321,
            process_identity="fixture-cancellable-worker",
            process_group_id=654_321,
            jobs_root_device=jobs_identity[0],
            jobs_root_inode=jobs_identity[1],
        )
        security_module.write_exclusive_owned_regular_bytes(
            manager._worker_ready_path(job_id),
            jobs_module._canonical_bytes(ready),
            root=manager.jobs_dir,
            root_identity=jobs_identity,
            context="test cancellable worker-ready record",
        )
        monkeypatch.setattr(
            jobs_module,
            "process_containment_is_alive",
            lambda *_args, **_kwargs: True,
        )
        termination_args: list[tuple[object, ...]] = []

        class FakeThread:
            def __init__(self, *, target: object, args: tuple[object, ...], **_kwargs: object):
                del target
                termination_args.append(args)

            @staticmethod
            def start() -> None:
                return None

        monkeypatch.setattr(jobs_module.threading, "Thread", FakeThread)
        manager.refresh()

        cancelling = manager.get(job_id, refresh=False)
        assert cancelling.state is JobState.CANCELLING
        assert cancelling.cancellation_requested is True
        assert cancelling.pid == ready.pid
        assert cancelling.process_identity == ready.process_identity
        assert cancelling.process_group_id == ready.process_group_id
        assert termination_args == [
            (
                job_id,
                ready.pid,
                ready.process_identity,
                ready.process_group_id,
            )
        ]
        assert not manager._launch_gate_path(job_id).exists()
    finally:
        manager.close()


@pytest.mark.parametrize("unexpected_gate", [False, True])
def test_starting_protocol_corruption_retains_a_live_worker_and_slot(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
    unexpected_gate: bool,
) -> None:
    configured = web_config.model_copy(update={"poll_interval_seconds": 5.0})
    manager = LocalJobManager(configured)
    manager.start()
    try:
        now = utc_now()
        job_id = ("a" if unexpected_gate else "b") * 32
        jobs_identity = manager._owned_identity(manager.jobs_dir)
        ready = WorkerReady(
            job_id=job_id,
            launch_nonce="c" * 32,
            request_sha256="d" * 64,
            pid=765_433,
            process_identity="fixture-live-starting-worker",
            process_group_id=765_433,
            jobs_root_device=jobs_identity[0],
            jobs_root_inode=jobs_identity[1],
        )
        ready_payload = jobs_module._canonical_bytes(ready)
        ready_sha256 = hashlib.sha256(ready_payload).hexdigest()
        starting = manager._write_record(
            JobRecord(
                job_id=job_id,
                created_at=now,
                updated_at=now,
                state=JobState.STARTING,
                workspace_dir=configured.workspace_root / f"corrupt-starting-{unexpected_gate}",
                expected_stages=(),
                progress_fraction=0.0,
                request_sha256=ready.request_sha256,
                launch_nonce=ready.launch_nonce,
                worker_ready_sha256=ready_sha256,
                pid=ready.pid,
                process_identity=ready.process_identity,
                process_group_id=ready.process_group_id,
            )
        )
        if unexpected_gate:
            security_module.write_exclusive_owned_regular_bytes(
                manager._worker_ready_path(job_id),
                ready_payload,
                root=manager.jobs_dir,
                root_identity=jobs_identity,
                context="test retained ready record",
            )
            manager._launch_gate_path(job_id).write_bytes(b'{"unexpected":true}\n')
        monkeypatch.setattr(
            jobs_module,
            "process_containment_is_alive",
            lambda *_args, **_kwargs: True,
        )

        blocked = manager._reconcile_starting(starting)

        assert blocked.state is JobState.STARTING
        assert blocked.pid == ready.pid
        assert blocked.process_identity == ready.process_identity
        assert blocked.process_group_id == ready.process_group_id
        assert blocked.error is not None
        assert blocked.error.code == "worker-start-recovery-blocked"
        assert manager._running_count() == 1
    finally:
        manager.close()


def test_recovered_starting_job_absorbs_existing_worker_result(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = web_config.model_copy(update={"poll_interval_seconds": 5.0})
    manager = LocalJobManager(configured)
    now = utc_now()
    job_id = "8" * 32
    request_sha256 = "a" * 64
    error = JobError(
        code="workflow-execution-failed",
        message="launch parent exited before gate release",
        corrective_action="Submit the retained request again.",
        exception_type="RuntimeError",
    )
    starting = manager._write_record(
        JobRecord(
            job_id=job_id,
            created_at=now,
            updated_at=now,
            state=JobState.STARTING,
            workspace_dir=web_config.workspace_root / "absorbed-starting-result",
            expected_stages=(),
            progress_fraction=0.0,
            request_sha256=request_sha256,
            launch_nonce="9" * 32,
            launch_gate_deadline=now + timedelta(seconds=30),
            launch_parent_pid=os.getpid(),
            launch_parent_identity="fixture-parent",
        )
    )
    jobs_identity = manager.jobs_dir.stat().st_dev, manager.jobs_dir.stat().st_ino
    ready = WorkerReady(
        job_id=job_id,
        launch_nonce="9" * 32,
        request_sha256=request_sha256,
        pid=987_650,
        process_identity="fixture-worker",
        process_group_id=987_650,
        jobs_root_device=jobs_identity[0],
        jobs_root_inode=jobs_identity[1],
    )
    jobs_module._atomic_write(manager._worker_ready_path(job_id), ready)
    worker_ready_sha256 = sha256_file(manager._worker_ready_path(job_id))
    running_candidate = starting.model_copy(
        update={
            "state": JobState.RUNNING,
            "worker_ready_sha256": worker_ready_sha256,
            "pid": ready.pid,
            "process_identity": ready.process_identity,
            "process_group_id": ready.process_group_id,
        }
    )
    launch_gate_sha256 = hashlib.sha256(
        jobs_module._canonical_bytes(manager._launch_gate_payload(running_candidate))
    ).hexdigest()
    jobs_module._atomic_write(
        manager._result_path(job_id),
        WorkerResult(
            job_id=job_id,
            launch_nonce="9" * 32,
            request_sha256=request_sha256,
            worker_ready_sha256=worker_ready_sha256,
            launch_gate_sha256=launch_gate_sha256,
            ok=False,
            exit_code=2,
            completed_at=now,
            error=error,
        ),
    )
    monkeypatch.setattr(
        jobs_module,
        "process_containment_is_alive",
        lambda *_args, **_kwargs: False,
    )

    manager.start()
    try:
        manager.refresh()
        failed = manager.get(job_id, refresh=False)
        assert failed.state is JobState.FAILED
        assert failed.exit_code == 2
        assert failed.error == error
        assert [event.message_key for event in manager.read_events(job_id)] == ["job.failed"]
    finally:
        manager.close()


def test_worker_parent_death_before_gate_never_executes_workflow(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = web_config.state_dir / "jobs" / ("c" * 32) / "request.json"
    result_path = request_path.with_name("result.json")
    gate_path = request_path.with_name("launch-gate.json")
    ready_path = request_path.with_name("worker-ready.json")
    jobs_root = request_path.parent.parent
    jobs_module._atomic_write(
        request_path,
        make_job_request(web_config, name="parent-death-gate"),
    )
    request_sha256 = sha256_file(request_path)
    jobs_root_identity = jobs_root.stat().st_dev, jobs_root.stat().st_ino
    worker_pid = os.getpid()
    monkeypatch.setattr(worker_module, "enable_current_process_containment", lambda: None)
    monkeypatch.setattr(
        worker_module,
        "process_identity",
        lambda pid: "fixture-worker" if pid == worker_pid else None,
    )
    monkeypatch.setattr(worker_module, "process_group_id", lambda _pid: worker_pid)
    monkeypatch.setattr(
        worker_module,
        "execute_workflow_launch",
        lambda *_args, **_kwargs: pytest.fail("workflow must remain behind the launch gate"),
    )

    exit_code = worker_module.run_worker(
        request_path,
        result_path,
        gate_path=gate_path,
        ready_path=ready_path,
        jobs_root=jobs_root,
        jobs_root_identity=jobs_root_identity,
        launch_nonce="d" * 32,
        request_sha256=request_sha256,
        parent_pid=worker_pid + 100_000,
        parent_identity="fixture-parent",
        gate_timeout_seconds=1.0,
    )

    result = WorkerResult.model_validate_json(result_path.read_bytes())
    assert exit_code == 2
    assert result.ok is False
    assert result.error is not None
    assert "parent exited or changed identity" in result.error.message
    assert WorkerReady.model_validate_json(ready_path.read_bytes()).pid == worker_pid
    assert not gate_path.exists()


@pytest.mark.parametrize("linked_name", ["request.json", "worker-ready.json"])
def test_worker_never_follows_request_or_ready_links(
    web_config: WebAppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    linked_name: str,
) -> None:
    jobs_root = tmp_path / "worker-owned-jobs"
    job_dir = jobs_root / ("1" * 32)
    job_dir.mkdir(parents=True)
    request_path = job_dir / "request.json"
    ready_path = job_dir / "worker-ready.json"
    gate_path = job_dir / "launch-gate.json"
    result_path = job_dir / "result.json"
    request_payload = jobs_module._canonical_bytes(
        make_job_request(web_config, name=f"linked-{linked_name}")
    )
    external = tmp_path / f"external-{linked_name}"
    if linked_name == "request.json":
        external.write_bytes(request_payload)
        request_path.symlink_to(external)
    else:
        request_path.write_bytes(request_payload)
        external.write_bytes(b"must remain unchanged")
        ready_path.symlink_to(external)
    original_external = external.read_bytes()
    worker_pid = os.getpid()
    monkeypatch.setattr(worker_module, "enable_current_process_containment", lambda: None)
    monkeypatch.setattr(worker_module, "process_identity", lambda _pid: "fixture-worker")
    monkeypatch.setattr(worker_module, "process_group_id", lambda _pid: worker_pid)
    monkeypatch.setattr(
        worker_module,
        "execute_workflow_launch",
        lambda *_args, **_kwargs: pytest.fail("unsafe worker path must not execute"),
    )

    exit_code = worker_module.run_worker(
        request_path,
        result_path,
        gate_path=gate_path,
        ready_path=ready_path,
        jobs_root=jobs_root,
        jobs_root_identity=(jobs_root.stat().st_dev, jobs_root.stat().st_ino),
        launch_nonce="2" * 32,
        request_sha256=hashlib.sha256(request_payload).hexdigest(),
        parent_pid=worker_pid,
        parent_identity="fixture-parent",
        gate_timeout_seconds=0.1,
    )

    assert exit_code == 2
    assert external.read_bytes() == original_external
    assert not result_path.exists()


def test_worker_never_follows_gate_or_result_links(
    web_config: WebAppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs_root = tmp_path / "worker-gate-jobs"
    job_dir = jobs_root / ("3" * 32)
    job_dir.mkdir(parents=True)
    request_path = job_dir / "request.json"
    ready_path = job_dir / "worker-ready.json"
    gate_path = job_dir / "launch-gate.json"
    result_path = job_dir / "result.json"
    request_payload = jobs_module._canonical_bytes(make_job_request(web_config, name="linked-gate"))
    request_path.write_bytes(request_payload)
    external_gate = tmp_path / "external-gate.json"
    external_gate.write_bytes(b"must remain unchanged")
    gate_path.symlink_to(external_gate)
    worker_pid = os.getpid()
    monkeypatch.setattr(worker_module, "enable_current_process_containment", lambda: None)
    monkeypatch.setattr(worker_module, "process_identity", lambda _pid: "fixture-worker")
    monkeypatch.setattr(worker_module, "process_group_id", lambda _pid: worker_pid)
    monkeypatch.setattr(
        worker_module,
        "execute_workflow_launch",
        lambda *_args, **_kwargs: pytest.fail("unsafe gate must not release workflow"),
    )

    exit_code = worker_module.run_worker(
        request_path,
        result_path,
        gate_path=gate_path,
        ready_path=ready_path,
        jobs_root=jobs_root,
        jobs_root_identity=(jobs_root.stat().st_dev, jobs_root.stat().st_ino),
        launch_nonce="4" * 32,
        request_sha256=hashlib.sha256(request_payload).hexdigest(),
        parent_pid=worker_pid,
        parent_identity="fixture-parent",
        gate_timeout_seconds=0.1,
    )
    assert exit_code == 2
    assert external_gate.read_bytes() == b"must remain unchanged"
    assert WorkerReady.model_validate_json(ready_path.read_bytes()).pid == worker_pid
    assert WorkerResult.model_validate_json(result_path.read_bytes()).ok is False

    second_jobs_root = tmp_path / "worker-result-jobs"
    second_job_dir = second_jobs_root / ("5" * 32)
    second_job_dir.mkdir(parents=True)
    second_request = second_job_dir / "request.json"
    second_ready = second_job_dir / "worker-ready.json"
    second_gate = second_job_dir / "launch-gate.json"
    second_result = second_job_dir / "result.json"
    second_request.write_bytes(request_payload)
    external_result = tmp_path / "external-result.json"
    external_result.write_bytes(b"must remain unchanged")
    second_result.symlink_to(external_result)
    monkeypatch.setattr(worker_module, "_wait_for_launch_gate", lambda **_kwargs: None)
    monkeypatch.setattr(
        worker_module,
        "execute_workflow_launch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture failure")),
    )

    with pytest.raises((FileExistsError, ValueError)):
        worker_module.run_worker(
            second_request,
            second_result,
            gate_path=second_gate,
            ready_path=second_ready,
            jobs_root=second_jobs_root,
            jobs_root_identity=(
                second_jobs_root.stat().st_dev,
                second_jobs_root.stat().st_ino,
            ),
            launch_nonce="6" * 32,
            request_sha256=hashlib.sha256(request_payload).hexdigest(),
            parent_pid=worker_pid,
            parent_identity="fixture-parent",
            gate_timeout_seconds=0.1,
        )
    assert external_result.read_bytes() == b"must remain unchanged"


def test_worker_rejects_replaced_jobs_root_identity(
    web_config: WebAppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs_root = tmp_path / "replaceable-jobs"
    job_id = "7" * 32
    job_dir = jobs_root / job_id
    job_dir.mkdir(parents=True)
    request_payload = jobs_module._canonical_bytes(
        make_job_request(web_config, name="original-root")
    )
    (job_dir / "request.json").write_bytes(request_payload)
    root_identity = jobs_root.stat().st_dev, jobs_root.stat().st_ino
    original_root = tmp_path / "original-jobs"
    jobs_root.rename(original_root)
    replacement_job = jobs_root / job_id
    replacement_job.mkdir(parents=True)
    replacement_payload = jobs_module._canonical_bytes(
        make_job_request(web_config, name="replacement-root")
    )
    replacement_request = replacement_job / "request.json"
    replacement_request.write_bytes(replacement_payload)
    monkeypatch.setattr(worker_module, "enable_current_process_containment", lambda: None)
    monkeypatch.setattr(
        worker_module,
        "execute_workflow_launch",
        lambda *_args, **_kwargs: pytest.fail("replaced root must not execute"),
    )

    exit_code = worker_module.run_worker(
        replacement_request,
        replacement_job / "result.json",
        gate_path=replacement_job / "launch-gate.json",
        ready_path=replacement_job / "worker-ready.json",
        jobs_root=jobs_root,
        jobs_root_identity=root_identity,
        launch_nonce="8" * 32,
        request_sha256=hashlib.sha256(request_payload).hexdigest(),
        parent_pid=os.getpid(),
        parent_identity="fixture-parent",
        gate_timeout_seconds=0.1,
    )

    assert exit_code == 2
    assert replacement_request.read_bytes() == replacement_payload
    assert not (replacement_job / "worker-ready.json").exists()
    assert not (replacement_job / "result.json").exists()


def test_worker_ready_rejects_foreign_root_identity_and_legacy_schema(
    web_config: WebAppConfig,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    now = utc_now()
    job_id = "a" * 32
    starting = manager._write_record(
        JobRecord(
            job_id=job_id,
            created_at=now,
            updated_at=now,
            state=JobState.STARTING,
            workspace_dir=web_config.workspace_root / "foreign-ready-root",
            expected_stages=(),
            progress_fraction=0.0,
            request_sha256="b" * 64,
            launch_nonce="c" * 32,
        )
    )
    jobs_identity = manager.jobs_dir.stat().st_dev, manager.jobs_dir.stat().st_ino
    ready = WorkerReady(
        job_id=job_id,
        launch_nonce="c" * 32,
        request_sha256="b" * 64,
        pid=765_432,
        process_identity="fixture-worker",
        process_group_id=765_432,
        jobs_root_device=jobs_identity[0],
        jobs_root_inode=jobs_identity[1] + 1,
    )
    jobs_module._atomic_write(manager._worker_ready_path(job_id), ready)

    with pytest.raises(ConfigurationError, match="durable launch identity"):
        manager._read_worker_ready(starting)

    manager._worker_ready_path(job_id).unlink()
    legacy = ready.model_copy(
        update={
            "jobs_root_inode": jobs_identity[1],
        }
    ).model_dump(mode="json")
    legacy["schema_version"] = "topoforge-web-worker-ready-v0"
    manager._worker_ready_path(job_id).write_bytes(jobs_module._canonical_bytes(legacy))
    with pytest.raises(ConfigurationError, match="invalid"):
        manager._read_worker_ready(starting)
    manager.close()


def test_legacy_worker_result_schema_is_rejected() -> None:
    current = WorkerResult(
        job_id="d" * 32,
        launch_nonce="e" * 32,
        request_sha256="f" * 64,
        worker_ready_sha256="1" * 64,
        launch_gate_sha256="2" * 64,
        ok=False,
        exit_code=2,
        completed_at=utc_now(),
        error=JobError(
            code="legacy-result",
            message="legacy",
            corrective_action="Submit again.",
        ),
    ).model_dump(mode="json")
    current["schema_version"] = "topoforge-web-worker-result-v2"

    with pytest.raises(ValueError, match="worker-result-v3"):
        WorkerResult.model_validate(current)


def test_trash_restore_rejects_noncanonical_record_and_workspace_escape(
    web_config: WebAppConfig,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    try:
        workspace = web_config.workspace_root / "trash-escape-source"
        workspace.mkdir(parents=True)
        (workspace / "payload.bin").write_bytes(b"retained trash payload")
        now = utc_now()
        job_id = "e" * 32
        manager._write_record(
            JobRecord(
                job_id=job_id,
                created_at=now,
                updated_at=now,
                state=JobState.CANCELLED,
                workspace_dir=workspace,
                expected_stages=(),
                progress_fraction=0.0,
                cancellation_requested=True,
            )
        )
        plan_request = JobBatchDeletePlanRequest(
            job_ids=(job_id,),
            mode=JobBatchDeleteMode.QUARANTINE_WORKSPACE,
        )
        plan = manager.plan_batch_delete(plan_request)
        applied = manager.apply_batch_delete(
            JobBatchDeleteApplyRequest(
                job_ids=plan.job_ids,
                mode=plan.mode,
                confirm_plan_id=plan.plan_id,
            )
        )
        record_path = manager._trash_record_path(applied.batch_id)
        record_path.write_text(
            applied.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigurationError, match="not canonical"):
            manager.restore_trash(
                applied.batch_id,
                JobTrashActionRequest(confirm_batch_id=applied.batch_id),
            )
        assert not workspace.exists()

        jobs_module._atomic_write(record_path, applied)
        outside_parent = web_config.workspace_root.parent / "must-not-create-on-restore"
        assert not outside_parent.exists()
        escaped_workspace = outside_parent / workspace.name
        tampered = applied.model_copy(
            update={
                "workspaces": (
                    applied.workspaces[0].model_copy(
                        update={"original_workspace": escaped_workspace}
                    ),
                )
            }
        )
        jobs_module._atomic_write(record_path, tampered)

        with pytest.raises(ConfigurationError, match="outside its configured root"):
            manager.restore_trash(
                applied.batch_id,
                JobTrashActionRequest(confirm_batch_id=applied.batch_id),
            )
        assert not outside_parent.exists()
        assert not workspace.exists()
        assert applied.workspaces[0].quarantined_workspace is not None
        assert applied.workspaces[0].quarantined_workspace.is_dir()
    finally:
        manager.close()


def test_batch_delete_inventory_rejects_same_size_and_move_time_changes(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    try:
        workspace = web_config.workspace_root / "inventory-bound-delete"
        workspace.mkdir(parents=True)
        payload = workspace / "payload.bin"
        payload.write_bytes(b"AAAAAA")
        now = utc_now()
        job_id = "f" * 32
        manager._write_record(
            JobRecord(
                job_id=job_id,
                created_at=now,
                updated_at=now,
                state=JobState.CANCELLED,
                workspace_dir=workspace,
                expected_stages=(),
                progress_fraction=0.0,
                cancellation_requested=True,
            )
        )
        request = JobBatchDeletePlanRequest(
            job_ids=(job_id,),
            mode=JobBatchDeleteMode.QUARANTINE_WORKSPACE,
        )
        reviewed = manager.plan_batch_delete(request)
        reviewed_item = reviewed.items[0]
        assert reviewed_item.workspace_inventory is not None
        assert reviewed_item.job_record_inventory is not None

        original_stat = payload.stat()
        payload.write_bytes(b"BBBBBB")
        os.utime(
            payload,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        changed = manager.plan_batch_delete(request)
        assert changed.workspace_bytes == reviewed.workspace_bytes
        assert changed.items[0].workspace_inventory is not None
        assert (
            changed.items[0].workspace_inventory.inventory_sha256
            != reviewed_item.workspace_inventory.inventory_sha256
        )
        assert changed.plan_id != reviewed.plan_id
        with pytest.raises(ConfigurationError, match="plan changed"):
            manager.apply_batch_delete(
                JobBatchDeleteApplyRequest(
                    job_ids=reviewed.job_ids,
                    mode=reviewed.mode,
                    confirm_plan_id=reviewed.plan_id,
                )
            )
        assert workspace.is_dir()
        assert manager._job_dir(job_id).is_dir()

        current = manager.plan_batch_delete(request)
        original_uuid4 = jobs_module.uuid4

        def mutate_after_review() -> object:
            current_stat = payload.stat()
            payload.write_bytes(b"CCCCCC")
            os.utime(
                payload,
                ns=(current_stat.st_atime_ns, current_stat.st_mtime_ns),
            )
            return original_uuid4()

        monkeypatch.setattr(jobs_module, "uuid4", mutate_after_review)
        with pytest.raises(ConfigurationError, match="workspace changed during batch apply"):
            manager.apply_batch_delete(
                JobBatchDeleteApplyRequest(
                    job_ids=current.job_ids,
                    mode=current.mode,
                    confirm_plan_id=current.plan_id,
                )
            )

        assert payload.read_bytes() == b"CCCCCC"
        assert workspace.is_dir()
        assert manager._job_dir(job_id).is_dir()
        assert not any(manager.trash_transactions_dir.glob("*/transaction.json"))
        assert manager.list_trash() == ()
    finally:
        manager.close()


def test_manager_lease_and_adapter_roots_fail_closed(
    web_config: WebAppConfig,
    tmp_path: Path,
) -> None:
    first = LocalJobManager(web_config)
    second = LocalJobManager(web_config)
    first.start()
    try:
        with pytest.raises(ConfigurationError, match="another TopoForge Web manager"):
            second.start()
    finally:
        first.close()
    second.start()
    try:
        before = tuple(second.jobs_dir.iterdir())
        stale_request = make_job_request(web_config, name="stale-manager")
        with pytest.raises(ConfigurationError, match="does not own its state roots"):
            first.submit(stale_request)
        with pytest.raises(ConfigurationError, match="does not own its state roots"):
            first.cancel("0" * 32)
        with pytest.raises(ConfigurationError, match="does not own its state roots"):
            first.refresh()
        assert tuple(second.jobs_dir.iterdir()) == before
    finally:
        second.close()

    external = tmp_path / "external-root"
    external.mkdir()
    marker = external / "must-survive.bin"
    marker.write_bytes(b"external evidence")
    linked_state = tmp_path / "linked-state"
    linked_state.symlink_to(external, target_is_directory=True)
    linked_config = WebAppConfig(
        state_dir=linked_state,
        workspace_root=tmp_path / "linked-workspaces",
        input_roots=web_config.input_roots,
    )
    with pytest.raises(ConfigurationError, match="real directory"):
        LocalJobManager(linked_config).start()
    assert marker.read_bytes() == b"external evidence"

    state = tmp_path / "safe-state"
    workspace_root = tmp_path / "safe-workspaces"
    state.mkdir()
    workspace_root.mkdir()
    (workspace_root / ".topoforge-trash").symlink_to(
        external,
        target_is_directory=True,
    )
    child_link_config = WebAppConfig(
        state_dir=state,
        workspace_root=workspace_root,
        input_roots=web_config.input_roots,
    )
    with pytest.raises(ConfigurationError, match="real directory"):
        LocalJobManager(child_link_config).start()
    assert marker.read_bytes() == b"external evidence"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX os.open race injection does not intercept the native Windows backend",
)
def test_lease_open_race_never_truncates_swapped_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_path = tmp_path / "manager.lock"
    lease_path.write_bytes(b"prior lease\n")
    external = tmp_path / "external.txt"
    external.write_bytes(b"must remain unchanged")
    real_open = security_module.os.open
    swapped = False

    def swapping_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if Path(path) == lease_path and not swapped:
            swapped = True
            lease_path.unlink()
            lease_path.symlink_to(external)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(security_module.os, "O_NOFOLLOW", 0)
    monkeypatch.setattr(security_module.os, "open", swapping_open)
    with pytest.raises(RuntimeError, match="changed before its handle"):
        WebManagerLease.acquire(
            lease_path,
            {"schema_version": "test-lease-v1"},
        )
    assert external.read_bytes() == b"must remain unchanged"


def test_exclusive_publication_is_complete_and_never_replaces(tmp_path: Path) -> None:
    destination = tmp_path / "gate.json"
    payload = b'{"schema_version":"test-gate-v1"}\n'
    write_exclusive_regular_bytes(
        destination,
        payload,
        context="test exclusive publication",
    )
    assert destination.read_bytes() == payload
    assert destination.stat().st_nlink == 1
    assert not any(path.name.endswith(".publishing") for path in tmp_path.iterdir())
    with pytest.raises(FileExistsError):
        write_exclusive_regular_bytes(
            destination,
            b"replacement\n",
            context="test exclusive publication",
        )
    assert destination.read_bytes() == payload


def test_running_recovery_never_republishes_missing_or_v1_gate(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = web_config.model_copy(update={"poll_interval_seconds": 5.0})
    manager = LocalJobManager(configured)
    manager.start()
    try:
        job_id = "1" * 32
        request = make_job_request(configured, name="missing-recovery-gate")
        jobs_module._atomic_write(manager._request_path(job_id), request)
        request_sha256 = sha256_file(manager._request_path(job_id))
        now = utc_now()
        jobs_identity = manager.jobs_dir.stat().st_dev, manager.jobs_dir.stat().st_ino
        ready = WorkerReady(
            job_id=job_id,
            launch_nonce="2" * 32,
            request_sha256=request_sha256,
            pid=987_654,
            process_identity="fixture-worker",
            process_group_id=987_654,
            jobs_root_device=jobs_identity[0],
            jobs_root_inode=jobs_identity[1],
        )
        jobs_module._atomic_write(manager._worker_ready_path(job_id), ready)
        worker_ready_sha256 = sha256_file(manager._worker_ready_path(job_id))
        candidate = JobRecord(
            job_id=job_id,
            created_at=now,
            updated_at=now,
            state=JobState.RUNNING,
            workspace_dir=request.launch.workspace_dir,
            expected_stages=expected_workflow_stages(request),
            progress_fraction=0.0,
            request_sha256=request_sha256,
            launch_nonce="2" * 32,
            worker_ready_sha256=worker_ready_sha256,
            launch_gate_deadline=now + timedelta(seconds=30),
            launch_parent_pid=os.getpid(),
            launch_parent_identity="fixture-parent",
            pid=987_654,
            process_identity="fixture-worker",
            process_group_id=987_654,
        )
        gate_sha256 = hashlib.sha256(
            jobs_module._canonical_bytes(manager._launch_gate_payload(candidate))
        ).hexdigest()
        running = manager._write_record(
            candidate.model_copy(update={"launch_gate_sha256": gate_sha256})
        )
        monkeypatch.setattr(
            jobs_module,
            "process_containment_is_alive",
            lambda *_args, **_kwargs: True,
        )

        manager.refresh()

        assert not manager._launch_gate_path(job_id).exists()
        blocked = manager.get(job_id, refresh=False)
        assert blocked.error is not None
        assert blocked.error.code == "worker-launch-gate-invalid"

        legacy_gate = manager._launch_gate_payload(running)
        legacy_gate["schema_version"] = "topoforge-web-worker-launch-gate-v1"
        manager._launch_gate_path(job_id).write_bytes(jobs_module._canonical_bytes(legacy_gate))
        with pytest.raises(ConfigurationError, match="does not match"):
            manager._verify_launch_gate(running)
    finally:
        manager.close()


def test_worker_result_rejects_symlink_and_foreign_identity(
    web_config: WebAppConfig,
    tmp_path: Path,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    try:
        now = utc_now()
        job_id = "3" * 32
        running = manager._write_record(
            JobRecord(
                job_id=job_id,
                created_at=now,
                updated_at=now,
                state=JobState.RUNNING,
                workspace_dir=web_config.workspace_root / "result-identity",
                expected_stages=(),
                progress_fraction=0.0,
                request_sha256="4" * 64,
                launch_nonce="5" * 32,
                worker_ready_sha256="8" * 64,
                launch_gate_sha256="6" * 64,
                pid=999_991,
                process_identity="fixture-worker",
                process_group_id=999_991,
            )
        )
        external = tmp_path / "external-result.json"
        external.write_bytes(b"external result must survive")
        result_path = manager._result_path(job_id)
        result_path.symlink_to(external)
        with pytest.raises(ConfigurationError, match="real, non-hard-linked"):
            manager._finish_job(running, exit_code=2)
        assert external.read_bytes() == b"external result must survive"
        result_path.unlink()
        jobs_module._atomic_write(
            result_path,
            WorkerResult(
                job_id="7" * 32,
                launch_nonce="5" * 32,
                request_sha256="4" * 64,
                worker_ready_sha256="8" * 64,
                launch_gate_sha256="6" * 64,
                ok=False,
                exit_code=2,
                completed_at=now,
                error=JobError(
                    code="foreign-result",
                    message="foreign",
                    corrective_action="none",
                ),
            ),
        )
        with pytest.raises(ConfigurationError, match="does not match"):
            manager._finish_job(running, exit_code=2)
    finally:
        manager.close()


def test_trash_v2_detects_post_quarantine_same_size_tamper(
    web_config: WebAppConfig,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    try:
        trash, workspace, _ = create_cancelled_trash(
            manager,
            job_id="8" * 32,
            workspace_name="post-quarantine-tamper",
        )
        quarantined = trash.workspaces[0].quarantined_workspace
        assert quarantined is not None
        payload = quarantined / "payload.bin"
        metadata = payload.stat()
        payload.write_bytes(b"X" * metadata.st_size)
        os.utime(payload, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))

        with pytest.raises(ConfigurationError, match="changed after publication"):
            manager.restore_trash(
                trash.batch_id,
                JobTrashActionRequest(confirm_batch_id=trash.batch_id),
            )
        assert not manager._trash_action_path(trash.batch_id).exists()
        assert not workspace.exists()
        assert payload.is_file()
    finally:
        manager.close()


def test_legacy_trash_v1_is_preserved_and_fails_closed(
    web_config: WebAppConfig,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    try:
        trash, workspace, _ = create_cancelled_trash(
            manager,
            job_id="9" * 32,
            workspace_name="legacy-v1-preservation",
        )
        legacy = trash.model_dump(mode="json")
        legacy["schema_version"] = "topoforge-web-job-trash-v1"
        legacy.pop("job_inventories")
        for workspace_record in legacy["workspaces"]:
            workspace_record.pop("inventory")
        record_path = manager._trash_record_path(trash.batch_id)
        record_path.write_bytes(jobs_module._canonical_bytes(legacy))

        with pytest.raises(ConfigurationError, match="legacy trash v1 is preserved"):
            manager.purge_trash(
                trash.batch_id,
                JobTrashActionRequest(confirm_batch_id=trash.batch_id),
            )
        assert record_path.is_file()
        assert not workspace.exists()
        assert trash.workspaces[0].quarantined_workspace is not None
        assert trash.workspaces[0].quarantined_workspace.is_dir()
    finally:
        manager.close()


def test_restore_action_recovers_after_hard_crash(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalJobManager(web_config)
    recovered: LocalJobManager | None = None
    manager.start()
    try:
        trash, workspace, marker = create_cancelled_trash(
            manager,
            job_id="a" * 32,
            workspace_name="restore-hard-crash",
        )

        class SimulatedCrash(BaseException):
            pass

        original_move = manager._restore_inventory_move
        calls = 0

        def crashing_move(
            *,
            source: Path,
            destination: Path,
            destination_root: Path,
            inventory: JobDeletionInventory,
            context: str,
        ) -> None:
            nonlocal calls
            original_move(
                source=source,
                destination=destination,
                destination_root=destination_root,
                inventory=inventory,
                context=context,
            )
            calls += 1
            if calls == 1:
                raise SimulatedCrash

        monkeypatch.setattr(manager, "_restore_inventory_move", crashing_move)
        with pytest.raises(SimulatedCrash):
            manager.restore_trash(
                trash.batch_id,
                JobTrashActionRequest(confirm_batch_id=trash.batch_id),
            )
        assert manager._trash_action_path(trash.batch_id).is_file()
        manager.close()

        recovered = LocalJobManager(web_config)
        recovered.start()
        assert marker.read_bytes() == b"inventory-bound-payload"
        assert recovered.get("a" * 32).state is JobState.CANCELLED
        assert not recovered._trash_action_path(trash.batch_id).exists()
        assert (recovered.deletion_audit_dir / f"{trash.batch_id}-restored.json").is_file()
        assert workspace.is_dir()
    finally:
        manager.close()
        if recovered is not None:
            recovered.close()


def test_purge_action_recovers_after_hard_crash_and_rejects_injection(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalJobManager(web_config)
    recovered: LocalJobManager | None = None
    manager.start()
    try:
        trash, workspace, _ = create_cancelled_trash(
            manager,
            job_id="b" * 32,
            workspace_name="purge-hard-crash",
        )

        class SimulatedCrash(BaseException):
            pass

        original_remove = manager._remove_purge_entry
        calls = 0

        def crashing_remove(
            transaction: JobTrashActionTransaction,
            entry: JobTrashPurgeEntry,
        ) -> None:
            nonlocal calls
            original_remove(transaction, entry)
            calls += 1
            if calls == 1:
                raise SimulatedCrash

        monkeypatch.setattr(manager, "_remove_purge_entry", crashing_remove)
        with pytest.raises(SimulatedCrash):
            manager.purge_trash(
                trash.batch_id,
                JobTrashActionRequest(confirm_batch_id=trash.batch_id),
            )
        assert manager._trash_action_path(trash.batch_id).is_file()
        manager.close()

        recovered = LocalJobManager(web_config)
        recovered.start()
        assert not workspace.exists()
        with pytest.raises(KeyError):
            recovered.get("b" * 32)
        assert not recovered._trash_action_path(trash.batch_id).exists()
        assert (recovered.deletion_audit_dir / f"{trash.batch_id}-purged.json").is_file()

        injected, _, _ = create_cancelled_trash(
            recovered,
            job_id="c" * 32,
            workspace_name="purge-injection",
        )
        original_recovered_remove = recovered._remove_purge_entry
        injected_path: Path | None = None

        def injecting_remove(
            transaction: JobTrashActionTransaction,
            entry: JobTrashPurgeEntry,
        ) -> None:
            nonlocal injected_path
            if injected_path is None:
                state_purging = transaction.state_purging
                injected_path = state_purging / "unexpected.bin"
                injected_path.write_bytes(b"must not be deleted")
            original_recovered_remove(transaction, entry)

        monkeypatch.setattr(recovered, "_remove_purge_entry", injecting_remove)
        with pytest.raises(ConfigurationError, match="unexpected remaining entry"):
            recovered.purge_trash(
                injected.batch_id,
                JobTrashActionRequest(confirm_batch_id=injected.batch_id),
            )
        assert injected_path is not None
        assert injected_path.read_bytes() == b"must not be deleted"
        injected_path.unlink()
        monkeypatch.setattr(
            recovered,
            "_remove_purge_entry",
            original_recovered_remove,
        )
        result = recovered.purge_trash(
            injected.batch_id,
            JobTrashActionRequest(confirm_batch_id=injected.batch_id),
        )
        assert result.required_checks_passed is True
    finally:
        manager.close()
        if recovered is not None:
            recovered.close()


def test_darwin_exclusive_publication_uses_bound_parent_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeRenameAtX:
        argtypes: object = None
        restype: object = None

        def __call__(self, *args: object) -> int:
            calls.append(args)
            return 0

    class FakeLibC:
        renameatx_np = FakeRenameAtX()

    monkeypatch.setattr(security_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        security_module.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: FakeLibC(),
    )
    security_module._publish_posix_noreplace(
        parent_descriptor=47,
        temporary_name=".gate.publishing",
        destination_name="gate.json",
        destination=tmp_path / "gate.json",
    )
    assert calls == [(47, b".gate.publishing", 47, b"gate.json", 4)]

    monkeypatch.setattr(
        security_module.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: object(),
    )
    with pytest.raises(RuntimeError, match="renameatx_np is unavailable"):
        security_module._publish_posix_noreplace(
            parent_descriptor=47,
            temporary_name=".gate.publishing",
            destination_name="gate.json",
            destination=tmp_path / "gate.json",
        )


@pytest.mark.parametrize(
    "relative_path",
    (
        "/absolute",
        "D:drive-relative",
        "a\\b",
        "a//b",
        "a/./b",
        "a/../b",
        "a/",
        "../a",
    ),
)
def test_purge_entry_rejects_noncanonical_cross_platform_paths(
    relative_path: str,
) -> None:
    with pytest.raises(ValueError, match="canonical relative POSIX path"):
        JobTrashPurgeEntry(
            root="state",
            relative_path=relative_path,
            kind="directory",
            size_bytes=0,
            device=1,
            inode=1,
            link_count=1,
            modified_time_ns=1,
        )


@pytest.mark.parametrize(
    ("limit_name", "error_pattern"),
    (
        ("_MAX_TRASH_ACTION_BYTES", "8 MiB safety limit"),
        ("_MAX_PURGE_MANIFEST_ENTRIES", "entry.*safety limit"),
    ),
)
def test_purge_preflight_limits_fail_before_intent_or_move(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    error_pattern: str,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    try:
        trash, _, _ = create_cancelled_trash(
            manager,
            job_id="d" * 32,
            workspace_name=f"purge-limit-{limit_name.lower()}",
        )
        monkeypatch.setattr(jobs_module, limit_name, 1)

        with pytest.raises(ConfigurationError, match=error_pattern):
            manager.purge_trash(
                trash.batch_id,
                JobTrashActionRequest(confirm_batch_id=trash.batch_id),
            )

        assert not manager._trash_action_path(trash.batch_id).exists()
        assert manager._trash_batch_dir(trash.batch_id).is_dir()
        assert manager._workspace_trash_batch_dir(trash.batch_id).is_dir()
        assert not manager._state_purging_path(trash.batch_id).exists()
        assert not manager._workspace_purging_path(trash.batch_id).exists()
    finally:
        manager.close()


@pytest.mark.parametrize("link_kind", ("symlink", "hardlink"))
def test_web_durable_replace_rejects_linked_destination_without_touching_external(
    tmp_path: Path,
    link_kind: str,
) -> None:
    parent = tmp_path / "jobs" / ("a" * 32)
    parent.mkdir(parents=True)
    external = tmp_path / "external.json"
    external.write_bytes(b"must remain unchanged")
    destination = parent / "job.json"
    if link_kind == "symlink":
        destination.symlink_to(external)
    else:
        os.link(external, destination)

    with pytest.raises(ValueError, match="real non-linked file"):
        jobs_module._atomic_write(destination, {"safe": True})
    assert external.read_bytes() == b"must remain unchanged"


@pytest.mark.parametrize("link_kind", ("symlink", "hardlink"))
def test_job_event_log_rejects_link_without_touching_external(
    web_config: WebAppConfig,
    tmp_path: Path,
    link_kind: str,
) -> None:
    manager = LocalJobManager(web_config)
    manager.start()
    try:
        now = utc_now()
        job_id = "b" * 32
        record = manager._write_record(
            JobRecord(
                job_id=job_id,
                created_at=now,
                updated_at=now,
                state=JobState.QUEUED,
                workspace_dir=web_config.workspace_root / "linked-event",
                expected_stages=(),
                progress_fraction=0.0,
            )
        )
        external = tmp_path / "external-events.jsonl"
        external.write_bytes(b"must remain unchanged")
        event_path = manager._event_path(job_id)
        if link_kind == "symlink":
            event_path.symlink_to(external)
        else:
            os.link(external, event_path)

        with pytest.raises(ConfigurationError, match="event log is unreadable"):
            manager._append_event(record, "job.queued")
        assert external.read_bytes() == b"must remain unchanged"
    finally:
        manager.close()


@pytest.mark.parametrize("log_name", ("stdout.log", "stderr.log"))
@pytest.mark.parametrize("link_kind", ("symlink", "hardlink"))
def test_worker_log_creation_rejects_link_without_truncating_external(
    web_config: WebAppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    log_name: str,
    link_kind: str,
) -> None:
    manager = LocalJobManager(web_config)
    monkeypatch.setattr(manager, "_monitor_loop", lambda: None)
    manager.start()
    try:
        monkeypatch.setattr(manager, "_start_queued_jobs", lambda: None)
        queued = manager.submit(make_job_request(web_config, name=f"linked-{log_name}-{link_kind}"))
        external = tmp_path / f"external-{log_name}-{link_kind}.txt"
        external.write_bytes(b"must remain unchanged")
        log_path = manager._job_dir(queued.job_id) / log_name
        if link_kind == "symlink":
            log_path.symlink_to(external)
        else:
            os.link(external, log_path)

        manager._start_job(queued)

        failed = manager.get(queued.job_id, refresh=False)
        assert failed.state is JobState.FAILED
        assert failed.error is not None
        assert failed.error.code == "worker-start-failed"
        assert external.read_bytes() == b"must remain unchanged"
    finally:
        manager.close()


class _PosixWindowsLeaseBackend:
    def __init__(
        self,
        before_relative: Callable[[], None] | None = None,
        *,
        before_rename: Callable[[], None] | None = None,
        before_delete: Callable[[], None] | None = None,
    ) -> None:
        self._before_relative = before_relative or (lambda: None)
        self._before_rename = before_rename or (lambda: None)
        self._before_delete = before_delete or (lambda: None)
        self._names: dict[int, tuple[int, str]] = {}

    def open_parent(self, path: Path) -> int:
        return os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )

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
        del share_access
        self._before_relative()
        access = (
            security_module._GENERIC_READ | security_module._GENERIC_WRITE
            if desired_access is None
            else desired_access
        )
        if directory:
            if create:
                os.mkdir(name, 0o700, dir_fd=parent_handle)
            handle = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_handle,
            )
        else:
            flags = (
                os.O_RDWR
                if access & (security_module._GENERIC_WRITE | security_module._DELETE)
                else os.O_RDONLY
            )
            flags |= os.O_NOFOLLOW
            if create:
                flags |= os.O_CREAT | os.O_EXCL
            handle = os.open(name, flags, 0o600, dir_fd=parent_handle)
        self._names[handle] = (parent_handle, name)
        return handle

    def information(
        self,
        handle: int,
    ) -> security_module._WindowsFileInformation:
        result = os.fstat(handle)
        attributes = (
            security_module._FILE_ATTRIBUTE_DIRECTORY if stat.S_ISDIR(result.st_mode) else 0
        )
        return security_module._WindowsFileInformation(
            attributes=attributes,
            link_count=result.st_nlink,
            volume_serial_number=result.st_dev,
            file_id=result.st_ino,
        )

    @staticmethod
    def adopt_file_handle(handle: int, *, flags: int | None = None) -> int:
        del flags
        return handle

    def close_handle(self, handle: int) -> None:
        self._names.pop(handle, None)
        os.close(handle)

    def rename_relative(
        self,
        handle: int,
        parent_handle: int,
        name: str,
        *,
        replace: bool,
    ) -> None:
        self._before_rename()
        source_parent, source_name = self._names[handle]
        source_metadata = os.fstat(handle)
        if stat.S_ISDIR(source_metadata.st_mode):
            if replace:
                os.replace(
                    source_name,
                    name,
                    src_dir_fd=source_parent,
                    dst_dir_fd=parent_handle,
                )
            else:
                try:
                    os.stat(name, dir_fd=parent_handle, follow_symlinks=False)
                except FileNotFoundError:
                    os.rename(
                        source_name,
                        name,
                        src_dir_fd=source_parent,
                        dst_dir_fd=parent_handle,
                    )
                else:
                    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), name)
        elif replace:
            os.replace(
                source_name,
                name,
                src_dir_fd=source_parent,
                dst_dir_fd=parent_handle,
            )
        else:
            os.link(
                source_name,
                name,
                src_dir_fd=source_parent,
                dst_dir_fd=parent_handle,
                follow_symlinks=False,
            )
            os.unlink(source_name, dir_fd=source_parent)
        self._names[handle] = (parent_handle, name)

    def delete_file(self, handle: int) -> None:
        self._before_delete()
        parent_handle, name = self._names[handle]
        if stat.S_ISDIR(os.fstat(handle).st_mode):
            os.rmdir(name, dir_fd=parent_handle)
        else:
            os.unlink(name, dir_fd=parent_handle)


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX fake backend exercises Windows manager-lease sharing",
)
def test_windows_manager_lease_path_verification_shares_with_locked_writer(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    candidate = state / "manager.lock"
    candidate.write_bytes(b"locked lease\n")
    observed_shares: list[int] = []

    class ShareCheckingBackend(_PosixWindowsLeaseBackend):
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
            if name == candidate.name:
                observed_shares.append(0 if share_access is None else share_access)
                if share_access is None or not share_access & security_module._FILE_SHARE_WRITE:
                    raise BrokenPipeError(
                        errno.EPIPE,
                        "fixture writer rejects a verification handle without write sharing",
                        name,
                    )
            return super().open_relative_file(
                parent_handle,
                name,
                create=create,
                directory=directory,
                desired_access=desired_access,
                share_access=share_access,
            )

    metadata = candidate.stat()
    security_module._verify_owned_regular_path_with_open_writer(
        candidate,
        root=state,
        root_identity=(state.stat().st_dev, state.stat().st_ino),
        expected_identity=(metadata.st_dev, metadata.st_ino),
        context="fixture manager lease verification",
        windows_backend=ShareCheckingBackend(),
    )

    assert observed_shares
    assert all(share & security_module._FILE_SHARE_WRITE for share in observed_shares)


def test_windows_lease_ctypes_x64_abi_layout() -> None:
    if security_module.ctypes.sizeof(security_module.ctypes.c_void_p) != 8:
        pytest.skip("Windows portable support targets x64")
    assert security_module.ctypes.sizeof(security_module._UnicodeString) == 16
    assert security_module.ctypes.sizeof(security_module._ObjectAttributes) == 48
    assert security_module.ctypes.sizeof(security_module._IoStatusBlock) == 16
    assert security_module.ctypes.sizeof(security_module._FileIdInfo) == 24
    assert security_module.ctypes.sizeof(security_module._FileRenameInfoHeader) == 24
    assert security_module._FileRenameInfoHeader.ReplaceIfExists.offset == 0
    assert security_module._FileRenameInfoHeader.RootDirectory.offset == 8
    assert security_module._FileRenameInfoHeader.FileNameLength.offset == 16
    assert (
        security_module._FileRenameInfoHeader.FileNameLength.offset
        + security_module.ctypes.sizeof(security_module.ctypes.c_uint32)
        == 20
    )
    assert security_module.ctypes.sizeof(security_module._FileDispositionInfo) == 1


def test_windows_rename_and_disposition_buffers_match_x64_abi() -> None:
    calls: list[tuple[int, bytes, int]] = []

    def capture(
        _handle: object,
        information_class: int,
        buffer: Any,
        length: int,
    ) -> int:
        calls.append(
            (
                int(information_class),
                security_module.ctypes.string_at(buffer, int(length)),
                int(length),
            )
        )
        return 1

    backend = object.__new__(security_module._WindowsNativeLeaseBackend)
    backend._set_file_information = capture
    backend.rename_relative(11, 22, "x", replace=True)
    rename_class, rename_payload, rename_length = calls.pop(0)
    encoded_name = "x".encode("utf-16-le")
    header_size = security_module.ctypes.sizeof(security_module._FileRenameInfoHeader)
    header = security_module._FileRenameInfoHeader.from_buffer_copy(rename_payload[:header_size])
    assert rename_class == security_module._FILE_RENAME_INFO_CLASS
    assert rename_length == header_size + len(encoded_name)
    assert header.ReplaceIfExists == 1
    assert header.RootDirectory == 22
    assert header.FileNameLength == len(encoded_name)
    filename_offset = (
        security_module._FileRenameInfoHeader.FileNameLength.offset
        + security_module.ctypes.sizeof(security_module.ctypes.c_uint32)
    )
    assert rename_payload[filename_offset : filename_offset + len(encoded_name)] == encoded_name

    backend.delete_file(11)
    disposition_class, disposition_payload, disposition_length = calls.pop(0)
    assert disposition_class == security_module._FILE_DISPOSITION_INFO_CLASS
    assert disposition_length == 1
    assert disposition_payload == b"\x01"


@pytest.mark.parametrize(
    ("extended_available", "extended_file_id", "expected_volume", "expected_file_id"),
    (
        (True, (1 << 100) + 17, 0x123456789ABCDEF0, (1 << 100) + 17),
        (False, 0, 0xA1B2C3D4, 0x1122334455667788),
        (True, 0, 0x123456789ABCDEF0, 0x1122334455667788),
    ),
)
def test_windows_file_information_matches_cpython_312_fallback(
    extended_available: bool,
    extended_file_id: int,
    expected_volume: int,
    expected_file_id: int,
) -> None:
    def basic_information(_handle: object, output: Any) -> int:
        result = security_module.ctypes.cast(
            output,
            security_module.ctypes.POINTER(security_module._ByHandleFileInformation),
        ).contents
        result.dwFileAttributes = security_module._FILE_ATTRIBUTE_NORMAL
        result.dwVolumeSerialNumber = 0xA1B2C3D4
        result.nNumberOfLinks = 1
        result.nFileIndexHigh = 0x11223344
        result.nFileIndexLow = 0x55667788
        return 1

    def extended_information(
        _handle: object,
        _information_class: int,
        output: Any,
        _length: int,
    ) -> int:
        if not extended_available:
            return 0
        result = security_module.ctypes.cast(
            output,
            security_module.ctypes.POINTER(security_module._FileIdInfo),
        ).contents
        result.VolumeSerialNumber = 0x123456789ABCDEF0
        for index, value in enumerate(extended_file_id.to_bytes(16, byteorder="little")):
            result.FileId[index] = value
        return 1

    backend = object.__new__(security_module._WindowsNativeLeaseBackend)
    backend._get_information = basic_information
    backend._get_information_ex = extended_information

    information = backend.information(123)

    assert information.attributes == security_module._FILE_ATTRIBUTE_NORMAL
    assert information.link_count == 1
    assert information.volume_serial_number == expected_volume
    assert information.file_id == expected_file_id


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX fake backend exercises the Windows relative-open contract",
)
def test_windows_relative_lease_open_never_follows_replaced_parent(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    original = tmp_path / "original-state"
    external = tmp_path / "external"
    external.mkdir()
    victim = external / "manager.lock"
    victim.write_bytes(b"must remain unchanged")
    candidate = state / "manager.lock"
    swapped = False

    def replace_parent() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        state.rename(original)
        state.symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeError, match="parent changed"):
        security_module._open_windows_lease_relative(
            candidate,
            backend=_PosixWindowsLeaseBackend(replace_parent),
        )
    assert victim.read_bytes() == b"must remain unchanged"
    assert (original / "manager.lock").read_bytes() == b""


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX fake backend exercises the Windows relative-open contract",
)
def test_windows_relative_lease_create_race_never_follows_final_link(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    external = tmp_path / "external.txt"
    external.write_bytes(b"must remain unchanged")
    candidate = state / "manager.lock"

    def insert_link() -> None:
        candidate.symlink_to(external)

    with pytest.raises(RuntimeError, match="appeared during exclusive creation"):
        security_module._open_windows_lease_relative(
            candidate,
            backend=_PosixWindowsLeaseBackend(insert_link),
        )
    assert external.read_bytes() == b"must remain unchanged"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX fake backend exercises the Windows relative-publication contract",
)
def test_windows_relative_publication_never_writes_through_replaced_parent(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    original = tmp_path / "original-state"
    external = tmp_path / "external"
    external.mkdir()
    destination = state / "gate.json"
    swapped = False

    def replace_parent() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        state.rename(original)
        state.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="temporary could not be safely created"):
        security_module._write_atomic_regular_bytes_windows(
            destination,
            b"safe payload\n",
            context="test publication",
            replace=False,
            backend=_PosixWindowsLeaseBackend(replace_parent),
        )
    assert tuple(external.iterdir()) == ()
    assert tuple(original.iterdir()) == ()


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX fake backend exercises the Windows relative-publication contract",
)
@pytest.mark.parametrize("replace", (False, True))
def test_windows_relative_publication_never_follows_racing_destination_link(
    tmp_path: Path,
    replace: bool,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    external = tmp_path / "external.txt"
    external.write_bytes(b"must remain unchanged")
    destination = state / "gate.json"

    def insert_link() -> None:
        destination.symlink_to(external)

    backend = _PosixWindowsLeaseBackend(before_rename=insert_link)
    if replace:
        security_module._write_atomic_regular_bytes_windows(
            destination,
            b"safe payload\n",
            context="test publication",
            replace=True,
            backend=backend,
        )
        assert destination.read_bytes() == b"safe payload\n"
        assert not destination.is_symlink()
    else:
        with pytest.raises(FileExistsError):
            security_module._write_atomic_regular_bytes_windows(
                destination,
                b"safe payload\n",
                context="test publication",
                replace=False,
                backend=backend,
            )
        assert destination.is_symlink()
    assert external.read_bytes() == b"must remain unchanged"
    assert not any(path.name.endswith(".publishing") for path in state.iterdir())


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX fake backend exercises the Windows relative-read contract",
)
def test_windows_relative_stable_read_never_accepts_replaced_parent(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    candidate = state / "record.json"
    candidate.write_bytes(b"original\n")
    before = candidate.lstat()
    original = tmp_path / "original-state"
    external = tmp_path / "external"
    external.mkdir()
    external_record = external / candidate.name
    external_record.write_bytes(b"external secret\n")

    def replace_parent() -> None:
        state.rename(original)
        state.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="unreadable without following links"):
        security_module._read_stable_regular_bytes_windows(
            candidate,
            before,
            context="test record",
            max_bytes=1024,
            backend=_PosixWindowsLeaseBackend(replace_parent),
        )
    assert external_record.read_bytes() == b"external secret\n"
    assert (original / candidate.name).read_bytes() == b"original\n"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX fake backend exercises the Windows anchored-publication contract",
)
def test_windows_anchored_publication_commits_to_pinned_replaced_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "jobs"
    parent = root / ("a" * 32)
    parent.mkdir(parents=True)
    root_identity = (root.stat().st_dev, root.stat().st_ino)
    destination = parent / "request.json"
    original_root = tmp_path / "original-jobs"
    external = tmp_path / "external"
    external_parent = external / parent.name
    external_parent.mkdir(parents=True)
    external_marker = external_parent / destination.name
    external_marker.write_bytes(b"must remain unchanged")

    def replace_root() -> None:
        root.rename(original_root)
        root.symlink_to(external, target_is_directory=True)

    security_module._write_atomic_owned_regular_bytes_windows(
        destination,
        b"safe payload\n",
        root=root,
        root_identity=root_identity,
        context="anchored request",
        replace=False,
        backend=_PosixWindowsLeaseBackend(before_rename=replace_root),
    )
    assert external_marker.read_bytes() == b"must remain unchanged"
    assert (original_root / parent.name / destination.name).read_bytes() == b"safe payload\n"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX fake backend exercises the Windows anchored-directory-move contract",
)
def test_windows_anchored_directory_move_commits_to_pinned_destination_root(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "jobs"
    source = source_root / ("b" * 32)
    source.mkdir(parents=True)
    (source / "job.json").write_bytes(b"source record\n")
    destination_root = tmp_path / "trash"
    destination_parent = destination_root / ".batch.creating" / "jobs"
    destination_parent.mkdir(parents=True)
    destination = destination_parent / source.name
    original_destination_root = tmp_path / "original-trash"
    external = tmp_path / "external-trash"
    external_parent = external / ".batch.creating" / "jobs"
    external_parent.mkdir(parents=True)
    external_marker = external_parent / "must-survive.bin"
    external_marker.write_bytes(b"external evidence")
    source_identity = (source.stat().st_dev, source.stat().st_ino)

    def replace_destination_root() -> None:
        destination_root.rename(original_destination_root)
        destination_root.symlink_to(external, target_is_directory=True)

    security_module._move_owned_path_windows(
        source,
        destination,
        source_root=source_root,
        source_root_identity=(source_root.stat().st_dev, source_root.stat().st_ino),
        destination_root=destination_root,
        destination_root_identity=(
            destination_root.stat().st_dev,
            destination_root.stat().st_ino,
        ),
        expected_identity=source_identity,
        directory=True,
        context="anchored quarantine",
        backend=_PosixWindowsLeaseBackend(before_rename=replace_destination_root),
    )
    assert external_marker.read_bytes() == b"external evidence"
    assert not (external_parent / source.name).exists()
    moved = original_destination_root / ".batch.creating" / "jobs" / source.name
    assert (moved / "job.json").read_bytes() == b"source record\n"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX fake backend exercises the Windows anchored-delete contract",
)
@pytest.mark.parametrize("directory", (False, True))
def test_windows_anchored_delete_commits_through_pinned_replaced_parent(
    tmp_path: Path,
    directory: bool,
) -> None:
    root = tmp_path / "trash"
    parent = root / ".batch.purging"
    parent.mkdir(parents=True)
    victim = parent / "victim"
    if directory:
        victim.mkdir()
    else:
        victim.write_bytes(b"internal evidence")
    victim_identity = (victim.stat().st_dev, victim.stat().st_ino)
    original_parent = root / ".batch.original"
    external = tmp_path / "external-purge"
    external.mkdir()
    external_victim = external / victim.name
    if directory:
        external_victim.mkdir()
        (external_victim / "marker.bin").write_bytes(b"must remain unchanged")
    else:
        external_victim.write_bytes(b"must remain unchanged")

    def replace_parent() -> None:
        parent.rename(original_parent)
        parent.symlink_to(external, target_is_directory=True)

    security_module._remove_owned_path_windows(
        victim,
        root=root,
        root_identity=(root.stat().st_dev, root.stat().st_ino),
        expected_identity=victim_identity,
        directory=directory,
        context="anchored purge",
        backend=_PosixWindowsLeaseBackend(before_delete=replace_parent),
    )
    if directory:
        assert (external_victim / "marker.bin").read_bytes() == b"must remain unchanged"
    else:
        assert external_victim.read_bytes() == b"must remain unchanged"
    assert not (original_parent / victim.name).exists()


@pytest.mark.parametrize(
    ("native_code", "expected_type", "expected_errno"),
    (
        (2, FileNotFoundError, errno.ENOENT),
        (3, FileNotFoundError, errno.ENOENT),
        (5, PermissionError, errno.EACCES),
        (80, FileExistsError, errno.EEXIST),
        (183, FileExistsError, errno.EEXIST),
        (1234, OSError, 1234),
    ),
)
def test_windows_native_errors_are_normalized_for_portable_control_flow(
    native_code: int,
    expected_type: type[OSError],
    expected_errno: int,
) -> None:
    error = security_module._WindowsNativeLeaseBackend._normalized_error(
        native_code,
        "native operation failed",
        "entry.bin",
    )

    assert type(error) is expected_type
    assert error.errno == expected_errno
    assert error.filename == "entry.bin"
    assert vars(error)["winerror"] == native_code
    assert "native operation failed" in str(error)


class _NormalizedWindowsDirectoryBackend(_PosixWindowsLeaseBackend):
    def __init__(self, target_name: str, *, collide_on_create: bool) -> None:
        super().__init__()
        self._target_name = target_name
        self._collide_on_create = collide_on_create
        self._collision_injected = False

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
            name == self._target_name
            and create
            and self._collide_on_create
            and not self._collision_injected
        ):
            self._collision_injected = True
            os.mkdir(name, 0o700, dir_fd=parent_handle)
            raise security_module._WindowsNativeLeaseBackend._normalized_error(
                183,
                "NtCreateFile failed",
                name,
            )
        try:
            return super().open_relative_file(
                parent_handle,
                name,
                create=create,
                directory=directory,
                desired_access=desired_access,
                share_access=share_access,
            )
        except FileNotFoundError as exc:
            raise security_module._WindowsNativeLeaseBackend._normalized_error(
                3,
                "NtCreateFile failed",
                name,
            ) from exc


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX fake backend exercises Windows normalized directory errors",
)
def test_windows_directory_walk_treats_native_exists_as_create_collision(
    tmp_path: Path,
) -> None:
    target = tmp_path / "windows-normalized-create" / "leaf"
    target.parent.mkdir()
    backend = _NormalizedWindowsDirectoryBackend(
        target.name,
        collide_on_create=True,
    )

    identity = security_module._walk_windows_directory_tree(
        target,
        context="normalized create",
        create_missing=True,
        backend=backend,
    )

    assert identity == (target.stat().st_dev, target.stat().st_ino)


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX fake backend exercises Windows normalized directory errors",
)
def test_windows_directory_walk_preserves_native_missing_subclass(
    tmp_path: Path,
) -> None:
    target = tmp_path / "windows-normalized-missing" / "absent"
    target.parent.mkdir()
    backend = _NormalizedWindowsDirectoryBackend(
        target.name,
        collide_on_create=False,
    )

    with pytest.raises(FileNotFoundError) as captured:
        security_module._walk_windows_directory_tree(
            target,
            context="normalized missing",
            create_missing=False,
            backend=backend,
        )

    assert captured.value.errno == errno.ENOENT
    assert vars(captured.value)["winerror"] == 3


@pytest.mark.skipif(os.name == "nt", reason="POSIX openat race regression")
def test_owned_entry_identity_rejects_links_and_replacement_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "owned"
    root.mkdir()
    root_identity = (root.stat().st_dev, root.stat().st_ino)
    regular = root / "regular.bin"
    regular.write_bytes(b"original")
    directory = root / "directory"
    directory.mkdir()

    assert security_module.owned_entry_identity(
        regular,
        root=root,
        root_identity=root_identity,
        directory=False,
        context="regular identity",
    ) == (regular.stat().st_dev, regular.stat().st_ino)
    assert security_module.owned_entry_identity(
        directory,
        root=root,
        root_identity=root_identity,
        directory=True,
        context="directory identity",
    ) == (directory.stat().st_dev, directory.stat().st_ino)
    assert (
        security_module.owned_entry_identity(
            root / "missing" / "entry.bin",
            root=root,
            root_identity=root_identity,
            directory=False,
            context="missing identity",
        )
        is None
    )

    external = tmp_path / "external.bin"
    external.write_bytes(b"must remain unchanged")
    linked = root / "linked.bin"
    linked.symlink_to(external)
    with pytest.raises(ValueError, match="unsafe"):
        security_module.owned_entry_identity(
            linked,
            root=root,
            root_identity=root_identity,
            directory=False,
            context="linked identity",
        )

    hardlinked = root / "hardlinked.bin"
    os.link(external, hardlinked)
    with pytest.raises(ValueError, match="unsafe"):
        security_module.owned_entry_identity(
            hardlinked,
            root=root,
            root_identity=root_identity,
            directory=False,
            context="hardlinked identity",
        )

    racing = root / "racing.bin"
    racing.write_bytes(b"before")
    retained = root / "retained.bin"
    real_open = security_module.os.open
    swapped = False

    def replace_before_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == racing.name and dir_fd is not None and not swapped:
            swapped = True
            racing.rename(retained)
            racing.write_bytes(b"replacement")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(security_module.os, "open", replace_before_open)
    with pytest.raises(ValueError, match="unsafe or changed"):
        security_module.owned_entry_identity(
            racing,
            root=root,
            root_identity=root_identity,
            directory=False,
            context="racing identity",
        )
    assert retained.read_bytes() == b"before"
    assert racing.read_bytes() == b"replacement"
    assert external.read_bytes() == b"must remain unchanged"


@pytest.mark.skipif(os.name == "nt", reason="POSIX openat race regression")
def test_real_directory_tree_rejects_parent_swap_during_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "bootstrap-parent"
    target = parent / "child"
    target.mkdir(parents=True)
    retained_parent = tmp_path / "retained-parent"
    external = tmp_path / "external-tree"
    external.mkdir()
    real_open = security_module.os.open
    swapped = False

    def swap_after_child_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == target.name and dir_fd is not None and not swapped:
            swapped = True
            parent.rename(retained_parent)
            parent.symlink_to(external, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(security_module.os, "open", swap_after_child_open)
    with pytest.raises((OSError, ValueError)):
        security_module.real_directory_tree_identity(
            target,
            context="bootstrap tree",
        )

    assert (retained_parent / target.name).is_dir()
    assert tuple(external.iterdir()) == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX openat race regression")
def test_owned_atomic_write_commits_to_pinned_parent_after_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cache"
    parent = root / "tiles"
    parent.mkdir(parents=True)
    root_identity = (root.stat().st_dev, root.stat().st_ino)
    destination = parent / "tile.png"
    retained_parent = root / "retained-tiles"
    external = tmp_path / "external-cache"
    external.mkdir()
    real_replace = security_module.os.replace
    swapped = False

    def swap_then_replace(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            parent.rename(retained_parent)
            parent.symlink_to(external, target_is_directory=True)
        real_replace(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(security_module.os, "replace", swap_then_replace)
    security_module.atomic_write_owned_regular_bytes(
        destination,
        b"safe tile",
        root=root,
        root_identity=root_identity,
        context="cache tile",
    )

    assert (retained_parent / destination.name).read_bytes() == b"safe tile"
    assert tuple(external.iterdir()) == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX openat race regression")
def test_owned_move_returns_success_after_committed_parent_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "jobs"
    source_root.mkdir()
    source = source_root / "job"
    source.write_bytes(b"job record")
    destination_root = tmp_path / "trash"
    destination_parent = destination_root / "batch"
    destination_parent.mkdir(parents=True)
    destination = destination_parent / source.name
    retained_parent = destination_root / "retained-batch"
    external = tmp_path / "external-trash-parent"
    external.mkdir()
    real_publish = security_module._publish_posix_noreplace
    swapped = False

    def publish_then_swap(
        *,
        parent_descriptor: int,
        temporary_name: str,
        destination_name: str,
        destination: Path,
        destination_parent_descriptor: int | None = None,
    ) -> None:
        nonlocal swapped
        real_publish(
            parent_descriptor=parent_descriptor,
            temporary_name=temporary_name,
            destination_name=destination_name,
            destination=destination,
            destination_parent_descriptor=destination_parent_descriptor,
        )
        if not swapped:
            swapped = True
            destination_parent.rename(retained_parent)
            destination_parent.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(
        security_module,
        "_publish_posix_noreplace",
        publish_then_swap,
    )
    security_module.move_owned_path(
        source,
        destination,
        source_root=source_root,
        source_root_identity=(source_root.stat().st_dev, source_root.stat().st_ino),
        destination_root=destination_root,
        destination_root_identity=(
            destination_root.stat().st_dev,
            destination_root.stat().st_ino,
        ),
        expected_identity=(source.stat().st_dev, source.stat().st_ino),
        directory=False,
        context="quarantine move",
    )

    assert not source.exists()
    assert (retained_parent / destination.name).read_bytes() == b"job record"
    assert tuple(external.iterdir()) == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX durability fault injection")
def test_owned_atomic_write_reports_committed_state_when_parent_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    destination = root / "record.json"
    destination.write_bytes(b"old")
    root_identity = (root.stat().st_dev, root.stat().st_ino)
    real_fsync = security_module.os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(security_module.os, "fsync", fail_directory_fsync)
    with pytest.raises(security_module.CommittedStateUncertainError) as captured:
        security_module.atomic_write_owned_regular_bytes(
            destination,
            b"new",
            root=root,
            root_identity=root_identity,
            context="state record",
        )

    assert captured.value.committed is True
    assert captured.value.operation == "publication"
    assert captured.value.path == destination
    assert destination.read_bytes() == b"new"
    assert not any(path.name.endswith(".publishing") for path in root.iterdir())

    monkeypatch.setattr(security_module.os, "fsync", real_fsync)
    assert security_module.owned_entry_identity(
        destination,
        root=root,
        root_identity=root_identity,
        directory=False,
        context="state record reconciliation",
    ) == (destination.stat().st_dev, destination.stat().st_ino)
    security_module.atomic_write_owned_regular_bytes(
        destination,
        b"new",
        root=root,
        root_identity=root_identity,
        context="state record replay",
    )
    assert destination.read_bytes() == b"new"


@pytest.mark.skipif(os.name == "nt", reason="POSIX verification fault injection")
def test_owned_atomic_write_reports_committed_state_when_postcheck_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    destination = root / "record.json"
    root_identity = (root.stat().st_dev, root.stat().st_ino)
    real_stat = security_module.os.stat
    destination_stats = 0

    def fail_postcommit_stat(
        path: str | Path,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal destination_stats
        if path == destination.name and dir_fd is not None:
            destination_stats += 1
            if destination_stats == 2:
                raise OSError(errno.EIO, "injected post-publication stat failure")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(security_module.os, "stat", fail_postcommit_stat)
    with pytest.raises(security_module.CommittedStateUncertainError) as captured:
        security_module.atomic_write_owned_regular_bytes(
            destination,
            b"published",
            root=root,
            root_identity=root_identity,
            context="state record",
        )

    assert captured.value.operation == "publication"
    assert destination.read_bytes() == b"published"
    assert not any(path.name.endswith(".publishing") for path in root.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="POSIX durability fault injection")
def test_owned_removal_reports_committed_state_when_parent_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "trash"
    root.mkdir()
    victim = root / "victim.bin"
    victim.write_bytes(b"payload")
    root_identity = (root.stat().st_dev, root.stat().st_ino)
    victim_identity = (victim.stat().st_dev, victim.stat().st_ino)
    real_fsync = security_module.os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(security_module.os, "fsync", fail_directory_fsync)
    with pytest.raises(security_module.CommittedStateUncertainError) as captured:
        security_module.remove_owned_path(
            victim,
            root=root,
            root_identity=root_identity,
            expected_identity=victim_identity,
            directory=False,
            context="trash purge",
        )

    assert captured.value.committed is True
    assert captured.value.operation == "removal"
    assert captured.value.path == victim
    assert not victim.exists()

    monkeypatch.setattr(security_module.os, "fsync", real_fsync)
    security_module.remove_owned_path(
        victim,
        root=root,
        root_identity=root_identity,
        expected_identity=victim_identity,
        directory=False,
        context="trash purge reconciliation",
        missing_ok=True,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX durability fault injection")
def test_owned_move_reports_committed_state_when_parent_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "jobs"
    source_root.mkdir()
    source = source_root / "job.bin"
    source.write_bytes(b"payload")
    source_identity = (source.stat().st_dev, source.stat().st_ino)
    destination_root = tmp_path / "trash"
    destination_root.mkdir()
    destination = destination_root / source.name
    real_fsync = security_module.os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(security_module.os, "fsync", fail_directory_fsync)
    with pytest.raises(security_module.CommittedStateUncertainError) as captured:
        security_module.move_owned_path(
            source,
            destination,
            source_root=source_root,
            source_root_identity=(source_root.stat().st_dev, source_root.stat().st_ino),
            destination_root=destination_root,
            destination_root_identity=(
                destination_root.stat().st_dev,
                destination_root.stat().st_ino,
            ),
            expected_identity=source_identity,
            directory=False,
            context="trash quarantine",
        )

    assert captured.value.committed is True
    assert captured.value.operation == "move"
    assert captured.value.path == destination
    assert not source.exists()
    assert destination.read_bytes() == b"payload"


@pytest.mark.skipif(os.name == "nt", reason="POSIX close fault injection")
def test_owned_atomic_write_reports_committed_state_when_descriptor_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    destination = root / "record.json"
    root_identity = (root.stat().st_dev, root.stat().st_ino)
    real_close = security_module.os.close
    injected = False

    def close_then_fail(descriptor: int) -> None:
        nonlocal injected
        is_regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
        real_close(descriptor)
        if is_regular and not injected:
            injected = True
            raise OSError(errno.EIO, "injected descriptor close failure")

    monkeypatch.setattr(security_module.os, "close", close_then_fail)
    try:
        raise LookupError("unrelated caller exception")
    except LookupError as unrelated:
        with pytest.raises(security_module.CommittedStateUncertainError) as captured:
            security_module.atomic_write_owned_regular_bytes(
                destination,
                b"published",
                root=root,
                root_identity=root_identity,
                context="state record",
            )
        assert getattr(unrelated, "__notes__", ()) == ()

    assert captured.value.committed is True
    assert captured.value.operation == "publication"
    assert captured.value.path == destination
    assert destination.read_bytes() == b"published"
    assert not any(path.name.endswith(".publishing") for path in root.iterdir())
