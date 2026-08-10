from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from topoforge.exceptions import ConfigurationError
from topoforge.util import sha256_file
from topoforge.web.jobs import LocalJobManager, expected_workflow_stages
from topoforge.web.models import (
    JobBatchDeleteApplyRequest,
    JobBatchDeleteMode,
    JobBatchDeletePlanRequest,
    JobDeleteRequest,
    JobRecord,
    JobState,
    JobTrashActionRequest,
    JobTrashRecord,
    JobTrashTransaction,
    JobTrashTransactionMove,
    JobTrashWorkspace,
    WebAppConfig,
    utc_now,
)
from topoforge.web.processes import terminate_process_tree, worker_process_options
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
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **worker_process_options(),
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
            workspaces=(
                JobTrashWorkspace(
                    original_workspace=workspace,
                    quarantined_workspace=quarantined,
                    workspace_existed=True,
                    size_bytes=plan.workspace_bytes,
                ),
            ),
            total_quarantined_bytes=plan.total_target_bytes,
            backups_preserved=True,
            required_checks_passed=True,
        ),
    )
    transaction_path = manager._trash_transaction_path(batch_id)
    transaction_path.parent.mkdir(parents=True)
    transaction_path.write_text(
        transaction.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    (state_temporary / "jobs").mkdir(parents=True)
    workspace_temporary.mkdir(parents=True)
    return transaction


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
    manager._write_record(retained)
    manager.refresh()
    backfilled = manager.get(record.job_id, refresh=False)
    assert any(artifact.artifact_id == "bambu_project_3mf" for artifact in backfilled.artifacts)


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
                terminate_process_tree(process.pid)
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
    (transaction.state_temporary / "trash.json").write_text(
        transaction.trash_record.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
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
