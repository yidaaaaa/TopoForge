"""Durable isolated local workflow job manager."""

from __future__ import annotations

import contextlib
import itertools
import json
import mimetypes
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from topoforge.exceptions import ConfigurationError
from topoforge.util import sha256_file
from topoforge.validation.slicers import (
    BambuStudioAdapter,
    OrcaSlicerAdapter,
    PrusaSlicerAdapter,
    SlicerAdapter,
    SlicerAvailability,
    SlicerInfo,
    select_slicer,
)
from topoforge.web.models import (
    FileEntry,
    FileListing,
    JobArtifact,
    JobCreateRequest,
    JobError,
    JobEvent,
    JobMaintenanceOverview,
    JobRecord,
    JobState,
    WebAppConfig,
    WorkerResult,
    WorkflowBackupRecord,
    WorkflowRestoreRequest,
    utc_now,
)
from topoforge.workflow import (
    LocalWorkflowStatus,
    WorkflowCleanupResult,
    WorkflowStage,
    apply_workflow_cleanup,
    create_workflow_backup,
    estimate_workflow_storage,
    inspect_workflow_workspace,
    plan_workflow_cleanup,
    read_workflow_launch_config,
    restore_workflow_backup,
    verify_workflow_backup,
)

_SELECTABLE_SUFFIXES = {
    ".geojson",
    ".gpx",
    ".json",
    ".tif",
    ".tiff",
    ".yaml",
    ".yml",
}


def expected_workflow_stages(request: JobCreateRequest) -> tuple[WorkflowStage, ...]:
    """Return the ordered stage set implied by one launch configuration."""
    launch = request.launch
    stages: list[WorkflowStage] = [
        WorkflowStage.ACQUIRE if launch.global_source is not None else WorkflowStage.SOURCE,
        WorkflowStage.BUILD,
    ]
    if launch.overlay is not None:
        stages.append(WorkflowStage.OVERLAY)
    stages.extend(
        (
            WorkflowStage.LAYOUT,
            WorkflowStage.EXTRACT,
            WorkflowStage.MESH,
            WorkflowStage.CONNECT,
        )
    )
    if launch.slicing_enabled:
        stages.append(WorkflowStage.SLICE)
    if launch.project_evidence_enabled:
        stages.append(WorkflowStage.PROJECT)
    return tuple(stages)


def _canonical_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, value: BaseModel | dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(_canonical_bytes(value))
    temporary.replace(path)


def _read_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    try:
        return model_type.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"Web job record is unreadable: {path}") from exc


def _within(root: Path, path: Path) -> bool:
    resolved = path.expanduser().resolve()
    return resolved == root or root in resolved.parents


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _progress(expected: tuple[WorkflowStage, ...], ready: tuple[WorkflowStage, ...]) -> float:
    if not expected:
        return 0.0
    complete = sum(stage in ready for stage in expected)
    return min(0.99, complete / len(expected))


class LocalJobManager:
    """Run, recover, cancel, and inspect local workflows in child processes."""

    def __init__(self, config: WebAppConfig) -> None:
        self.config = config.resolved()
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._slicer_probes: dict[str, SlicerInfo] = {}
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None

    @property
    def jobs_dir(self) -> Path:
        """Return the durable job record directory."""
        return self.config.state_dir / "jobs"

    @property
    def backups_dir(self) -> Path:
        """Return the adapter-owned verified workflow backup directory."""
        return self.config.state_dir / "backups"

    def _slicer_adapter(self, name: str) -> SlicerAdapter:
        if name == "bambu-studio":
            return BambuStudioAdapter(self.config.bambu_studio_executable)
        if name == "orca":
            return OrcaSlicerAdapter()
        if name == "prusa":
            return PrusaSlicerAdapter()
        if name == "auto":
            return select_slicer(bambu_executable=self.config.bambu_studio_executable)
        raise ConfigurationError(f"unsupported slicer name: {name}")

    def probe_slicer(self, name: str, *, refresh: bool = False) -> SlicerInfo:
        """Return one cached executable probe used by validation and workers."""
        with self._lock:
            if not refresh and name in self._slicer_probes:
                return self._slicer_probes[name]
            probe = self._slicer_adapter(name).probe(refresh=refresh)
            self._slicer_probes[name] = probe
            return probe

    def validate_request(
        self,
        request: JobCreateRequest,
    ) -> tuple[JobCreateRequest, SlicerInfo | None]:
        """Normalize one launch and reject unavailable external slicers before queueing."""
        workspace = request.launch.workspace_dir.expanduser().resolve()
        if workspace == self.config.workspace_root or not _within(
            self.config.workspace_root, workspace
        ):
            raise ConfigurationError(f"workspace must be a child of {self.config.workspace_root}")
        launch = request.launch
        if launch.slicing_enabled and launch.slicer_name == "bambu-studio":
            settings = launch.slicer_settings
            filaments = launch.slicer_filaments
            configured_profiles = (
                self.config.bambu_machine_profile,
                self.config.bambu_process_profile,
                self.config.bambu_filament_profile,
            )
            if not settings and all(path is not None for path in configured_profiles):
                settings = (
                    self.config.bambu_machine_profile,
                    self.config.bambu_process_profile,
                )
                filaments = (self.config.bambu_filament_profile,)
            if (
                len(settings) != 2
                or len(filaments) != 1
                or any(path is None for path in (*settings, *filaments))
            ):
                raise ConfigurationError(
                    "Bambu Studio slicing requires one machine, one process, and one "
                    "filament profile. Restart topoforge web with "
                    "--bambu-machine-profile, --bambu-process-profile, and "
                    "--bambu-filament-profile."
                )
            launch = launch.model_copy(
                update={
                    "slicer_settings": settings,
                    "slicer_filaments": filaments,
                }
            )
        slicer: SlicerInfo | None = None
        if launch.slicing_enabled:
            slicer = self.probe_slicer(launch.slicer_name)
            if slicer.status is not SlicerAvailability.AVAILABLE:
                detail = slicer.detail or "the version probe did not succeed"
                option = (
                    " Restart topoforge web with --bambu-studio-executable PATH."
                    if launch.slicer_name == "bambu-studio"
                    else ""
                )
                raise ConfigurationError(f"{slicer.name} is not ready: {detail}.{option}")
        normalized_launch = launch.model_copy(
            update={
                "workspace_dir": workspace,
                "build": launch.build.model_copy(update={"output_dir": workspace}),
            }
        )
        return JobCreateRequest(launch=normalized_launch), slicer

    def start(self) -> None:
        """Create roots, reconcile retained records, and start background polling."""
        with self._lock:
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            self.config.workspace_root.mkdir(parents=True, exist_ok=True)
            self.jobs_dir.mkdir(parents=True, exist_ok=True)
            self.backups_dir.mkdir(parents=True, exist_ok=True)
            self.refresh()
            if self._monitor is None or not self._monitor.is_alive():
                self._stop.clear()
                self._monitor = threading.Thread(
                    target=self._monitor_loop,
                    name="topoforge-web-jobs",
                    daemon=True,
                )
                self._monitor.start()

    def close(self) -> None:
        """Stop polling while leaving isolated workflows running for recovery."""
        self._stop.set()
        monitor = self._monitor
        if monitor is not None:
            monitor.join(timeout=5)
        self._monitor = None

    def _monitor_loop(self) -> None:
        while not self._stop.wait(self.config.poll_interval_seconds):
            try:
                self.refresh()
            except Exception:
                continue

    def _job_dir(self, job_id: str) -> Path:
        if (
            len(job_id) != 32
            or not job_id
            or any(character not in "0123456789abcdef" for character in job_id)
        ):
            raise KeyError(job_id)
        return self.jobs_dir / job_id

    def _record_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"

    def _request_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "request.json"

    def _result_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "result.json"

    def _event_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "events.jsonl"

    def _read_record(self, job_id: str) -> JobRecord:
        path = self._record_path(job_id)
        if not path.is_file():
            raise KeyError(job_id)
        return _read_model(path, JobRecord)

    def _write_record(
        self,
        record: JobRecord,
        *,
        message_key: str | None = None,
    ) -> JobRecord:
        updated = record.model_copy(update={"updated_at": utc_now()})
        _atomic_write(self._record_path(updated.job_id), updated)
        if message_key is not None:
            self._append_event(updated, message_key)
        return updated

    def _append_event(self, record: JobRecord, message_key: str) -> JobEvent:
        events = self.read_events(record.job_id)
        event = JobEvent(
            sequence=(events[-1].sequence + 1 if events else 1),
            job_id=record.job_id,
            occurred_at=record.updated_at,
            state=record.state,
            progress_fraction=record.progress_fraction,
            current_stage=record.current_stage,
            ready_stages=record.ready_stages,
            message_key=message_key,
        )
        path = self._event_path(record.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(_canonical_bytes(event))
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def read_events(self, job_id: str, *, after: int = 0) -> tuple[JobEvent, ...]:
        """Return strictly parsed events after one sequence number."""
        path = self._event_path(job_id)
        if not path.exists():
            return ()
        events: list[JobEvent] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                event = JobEvent.model_validate_json(line)
                if event.sequence > after:
                    events.append(event)
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"Web job event log is unreadable: {path}") from exc
        if any(right.sequence <= left.sequence for left, right in itertools.pairwise(events)):
            raise ConfigurationError(f"Web job event sequence is not monotonic: {path}")
        return tuple(events)

    def submit(self, request: JobCreateRequest) -> JobRecord:
        """Persist and enqueue one complete validated workflow launch."""
        with self._lock:
            normalized, _ = self.validate_request(request)
            workspace = normalized.launch.workspace_dir
            job_id = uuid4().hex
            now = utc_now()
            record = JobRecord(
                job_id=job_id,
                created_at=now,
                updated_at=now,
                state=JobState.QUEUED,
                workspace_dir=workspace,
                expected_stages=expected_workflow_stages(normalized),
                progress_fraction=0.0,
            )
            _atomic_write(self._request_path(job_id), normalized)
            _atomic_write(self._record_path(job_id), record)
            self._append_event(record, "job.queued")
            self._start_queued_jobs()
            return self._read_record(job_id)

    def get(self, job_id: str, *, refresh: bool = True) -> JobRecord:
        """Return one job after optional process/status reconciliation."""
        with self._lock:
            if refresh:
                self.refresh()
            return self._read_record(job_id)

    def list(self) -> tuple[JobRecord, ...]:
        """Return jobs newest first after reconciliation."""
        with self._lock:
            self.refresh()
            records = [
                self._read_record(path.parent.name) for path in self.jobs_dir.glob("*/job.json")
            ]
            return tuple(sorted(records, key=lambda item: item.created_at, reverse=True))

    def _completed_record(self, job_id: str) -> JobRecord:
        record = self.get(job_id)
        if record.state is not JobState.COMPLETED or record.summary is None:
            raise ConfigurationError("workflow maintenance requires a completed job")
        return record

    @staticmethod
    def _backup_record(path: Path) -> WorkflowBackupRecord:
        manifest = verify_workflow_backup(path)
        return WorkflowBackupRecord(
            backup_id=manifest.backup_id,
            workflow_id=manifest.workflow_id,
            original_workspace=manifest.original_workspace,
            archive_size_bytes=path.stat().st_size,
            archive_sha256=sha256_file(path),
            file_count=len(manifest.files),
            download_url=f"/api/v1/backups/{manifest.backup_id}",
            required_checks_passed=manifest.required_checks_passed,
        )

    def list_backups(self) -> tuple[WorkflowBackupRecord, ...]:
        """Strictly reopen adapter-owned backups and return stable records."""
        with self._lock:
            if not self.backups_dir.exists():
                return ()
            records = [
                self._backup_record(path)
                for path in sorted(self.backups_dir.glob("*.zip"), key=lambda item: item.name)
            ]
            return tuple(records)

    def create_backup(self, job_id: str) -> WorkflowBackupRecord:
        """Create or reuse one deterministic verified backup for a completed job."""
        with self._lock:
            record = self._completed_record(job_id)
            self.backups_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.backups_dir / f".{job_id}.creating.zip"
            temporary.unlink(missing_ok=True)
            try:
                result = create_workflow_backup(record.workspace_dir, temporary)
                destination = self.backups_dir / f"{result.manifest.backup_id}.zip"
                if destination.exists():
                    existing = self._backup_record(destination)
                    if existing.archive_sha256 != result.archive_sha256:
                        raise ConfigurationError(
                            "workflow backup id collision has different archive bytes"
                        )
                    temporary.unlink(missing_ok=True)
                    return existing
                temporary.replace(destination)
                return self._backup_record(destination)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise

    def backup_archive_path(
        self,
        backup_id: str,
    ) -> tuple[Path, WorkflowBackupRecord]:
        """Resolve and strictly verify one adapter-owned backup download."""
        if len(backup_id) != 64 or any(
            character not in "0123456789abcdef" for character in backup_id
        ):
            raise KeyError(backup_id)
        path = (self.backups_dir / f"{backup_id}.zip").resolve()
        if self.backups_dir.resolve() not in path.parents or not path.is_file():
            raise KeyError(backup_id)
        record = self._backup_record(path)
        if record.backup_id != backup_id:
            raise ConfigurationError("workflow backup filename does not match its identity")
        return path, record

    def maintenance(self, job_id: str) -> JobMaintenanceOverview:
        """Return measured storage, cleanup, and backup state for one job."""
        record = self._completed_record(job_id)
        summary = record.summary
        if summary is None:
            raise ConfigurationError("completed job summary is missing")
        launch = read_workflow_launch_config(record.workspace_dir / "workflow-launch.yaml")
        storage = estimate_workflow_storage(launch, summary=summary)
        cleanup = plan_workflow_cleanup(record.workspace_dir)
        backups = tuple(
            backup for backup in self.list_backups() if backup.workflow_id == summary.workflow_id
        )
        return JobMaintenanceOverview(
            job_id=record.job_id,
            storage=storage,
            cleanup=cleanup,
            backups=backups,
            required_checks_passed=(
                storage.sufficient_for_estimate
                and cleanup.required_checks_passed
                and all(item.required_checks_passed for item in backups)
            ),
        )

    def cleanup(
        self,
        job_id: str,
        *,
        confirm_workflow_id: str,
    ) -> WorkflowCleanupResult:
        """Apply the core reviewed cleanup contract for one completed job."""
        record = self._completed_record(job_id)
        if record.summary is None or confirm_workflow_id != record.summary.workflow_id:
            raise ConfigurationError(
                "cleanup confirmation does not match the selected completed job"
            )
        return apply_workflow_cleanup(
            record.workspace_dir,
            confirm_workflow_id=confirm_workflow_id,
        )

    def restore_backup(
        self,
        backup_id: str,
        *,
        workspace_name: str | None = None,
    ) -> JobRecord:
        """Restore a verified backup below the workspace root and register it."""
        with self._lock:
            archive, backup = self.backup_archive_path(backup_id)
            request = WorkflowRestoreRequest(workspace_name=workspace_name)
            if request.workspace_name is None:
                raw_name = f"{backup.original_workspace.name}-restored-{backup.backup_id[:8]}"
                name = "".join(
                    character if character.isalnum() or character in "._-" else "-"
                    for character in raw_name
                ).strip(".-")
            else:
                name = request.workspace_name
            if not name:
                raise ConfigurationError("restored workspace name is empty")
            destination = (self.config.workspace_root / name).resolve()
            if not _within(self.config.workspace_root, destination):
                raise ConfigurationError("restored workspace escapes workspace root")
            result = restore_workflow_backup(archive, destination)
            if not result.required_checks_passed:
                raise ConfigurationError("restored workflow did not pass strict checks")
            launch = read_workflow_launch_config(destination / "workflow-launch.yaml")
            create_request = JobCreateRequest(launch=launch)
            summary = inspect_workflow_workspace(destination)
            job_id = uuid4().hex
            now = utc_now()
            base_record = JobRecord(
                job_id=job_id,
                created_at=now,
                updated_at=now,
                state=JobState.COMPLETED,
                workspace_dir=destination,
                expected_stages=expected_workflow_stages(create_request),
                progress_fraction=1.0,
                current_stage=None,
                ready_stages=summary.ready_stages,
                pid=None,
                exit_code=0,
                summary=summary,
            )
            completed = base_record.model_copy(
                update={"artifacts": self._artifacts(base_record, summary.artifacts)}
            )
            _atomic_write(self._request_path(job_id), create_request)
            _atomic_write(self._record_path(job_id), completed)
            self._append_event(completed, "job.restored")
            return self._read_record(job_id)

    def _all_records(self) -> tuple[JobRecord, ...]:
        return tuple(
            self._read_record(path.parent.name) for path in sorted(self.jobs_dir.glob("*/job.json"))
        )

    def _running_count(self) -> int:
        return sum(
            record.state in {JobState.RUNNING, JobState.CANCELLING}
            for record in self._all_records()
        )

    def _start_queued_jobs(self) -> None:
        available = self.config.max_concurrent_jobs - self._running_count()
        if available <= 0:
            return
        queued = sorted(
            (record for record in self._all_records() if record.state is JobState.QUEUED),
            key=lambda item: item.created_at,
        )
        for record in queued[:available]:
            self._start_job(record)

    def _start_job(self, record: JobRecord) -> None:
        job_dir = self._job_dir(record.job_id)
        stdout_path = job_dir / "stdout.log"
        stderr_path = job_dir / "stderr.log"
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        if self.config.bambu_studio_executable is not None:
            env["TOPOFORGE_BAMBU_STUDIO"] = str(self.config.bambu_studio_executable)
        command = [
            sys.executable,
            "-m",
            "topoforge.web.worker",
            "--request",
            str(self._request_path(record.job_id)),
            "--result",
            str(self._result_path(record.job_id)),
        ]
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                command,
                cwd=self.config.workspace_root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
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

    def _status_update(self, record: JobRecord) -> JobRecord:
        status_path = record.workspace_dir / "workflow-status.json"
        if not status_path.is_file():
            return record
        try:
            status = LocalWorkflowStatus.model_validate_json(
                status_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return record
        progress = _progress(record.expected_stages, status.ready_stages)
        if (
            status.current_stage == record.current_stage
            and status.ready_stages == record.ready_stages
            and progress == record.progress_fraction
        ):
            return record
        return self._write_record(
            record.model_copy(
                update={
                    "current_stage": status.current_stage,
                    "ready_stages": status.ready_stages,
                    "progress_fraction": progress,
                }
            ),
            message_key="job.progress",
        )

    def refresh(self) -> None:
        """Reconcile child processes, workflow status, terminal results, and queue."""
        with self._lock:
            if not self.jobs_dir.exists():
                return
            for record in self._all_records():
                if record.state not in {JobState.RUNNING, JobState.CANCELLING}:
                    continue
                record = self._status_update(record)
                process = self._processes.get(record.job_id)
                running = (
                    process.poll() is None
                    if process is not None
                    else record.pid is not None and _pid_is_alive(record.pid)
                )
                if running:
                    continue
                exit_code = process.returncode if process is not None else None
                self._processes.pop(record.job_id, None)
                self._finish_job(record, exit_code=exit_code)
            self._start_queued_jobs()

    def _finish_job(self, record: JobRecord, *, exit_code: int | None) -> JobRecord:
        if record.state is JobState.CANCELLING or record.cancellation_requested:
            return self._write_record(
                record.model_copy(
                    update={
                        "state": JobState.CANCELLED,
                        "exit_code": exit_code,
                        "current_stage": None,
                        "pid": None,
                        "error": None,
                    }
                ),
                message_key="job.cancelled",
            )
        result_path = self._result_path(record.job_id)
        if not result_path.is_file():
            error = JobError(
                code="worker-result-missing",
                message="The isolated worker stopped before publishing a terminal result.",
                corrective_action=(
                    "Inspect the retained job stderr log and resume the saved launch."
                ),
            )
            return self._write_record(
                record.model_copy(
                    update={
                        "state": JobState.FAILED,
                        "exit_code": exit_code,
                        "current_stage": None,
                        "pid": None,
                        "error": error,
                    }
                ),
                message_key="job.failed",
            )
        worker = _read_model(result_path, WorkerResult)
        if not worker.ok:
            return self._write_record(
                record.model_copy(
                    update={
                        "state": JobState.FAILED,
                        "exit_code": worker.exit_code,
                        "current_stage": None,
                        "pid": None,
                        "error": worker.error,
                    }
                ),
                message_key="job.failed",
            )
        summary = inspect_workflow_workspace(record.workspace_dir)
        artifacts = self._artifacts(record, summary.artifacts)
        return self._write_record(
            record.model_copy(
                update={
                    "state": JobState.COMPLETED,
                    "progress_fraction": 1.0,
                    "current_stage": None,
                    "ready_stages": summary.ready_stages,
                    "pid": None,
                    "exit_code": worker.exit_code,
                    "summary": summary,
                    "artifacts": artifacts,
                    "error": None,
                }
            ),
            message_key="job.completed",
        )

    def _artifacts(
        self,
        record: JobRecord,
        values: dict[str, str],
    ) -> tuple[JobArtifact, ...]:
        workspace = record.workspace_dir.resolve()
        artifacts: list[JobArtifact] = []
        for role, raw_path in sorted(values.items()):
            path = Path(raw_path).resolve()
            if not _within(workspace, path):
                raise ConfigurationError(f"workflow artifact escapes workspace: {path}")
            is_file = path.is_file()
            relative = path.relative_to(workspace).as_posix() if path != workspace else "."
            artifacts.append(
                JobArtifact(
                    artifact_id=role,
                    relative_path=relative,
                    filename=path.name or workspace.name,
                    kind="file" if is_file else "directory",
                    media_type=mimetypes.guess_type(path.name)[0] if is_file else None,
                    size_bytes=path.stat().st_size if is_file else None,
                    sha256=sha256_file(path) if is_file else None,
                    download_url=(
                        f"/api/v1/jobs/{record.job_id}/artifacts/{role}" if is_file else None
                    ),
                )
            )
        return tuple(artifacts)

    def cancel(self, job_id: str) -> JobRecord:
        """Cancel a queued job or signal one isolated process group."""
        with self._lock:
            record = self._read_record(job_id)
            if record.state is JobState.QUEUED:
                return self._write_record(
                    record.model_copy(
                        update={
                            "state": JobState.CANCELLED,
                            "cancellation_requested": True,
                        }
                    ),
                    message_key="job.cancelled",
                )
            if record.state not in {JobState.RUNNING, JobState.CANCELLING}:
                return record
            updated = self._write_record(
                record.model_copy(
                    update={
                        "state": JobState.CANCELLING,
                        "cancellation_requested": True,
                    }
                ),
                message_key="job.cancelling",
            )
            if updated.pid is not None:
                threading.Thread(
                    target=self._terminate_process,
                    args=(updated.pid,),
                    name=f"topoforge-cancel-{job_id[:8]}",
                    daemon=True,
                ).start()
            return updated

    @staticmethod
    def _terminate_process(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not _pid_is_alive(pid):
                return
            time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)

    def artifact_path(self, job_id: str, artifact_id: str) -> tuple[Path, JobArtifact]:
        """Reopen and checksum one file artifact before exposing it."""
        record = self.get(job_id)
        artifact = next(
            (item for item in record.artifacts if item.artifact_id == artifact_id),
            None,
        )
        if artifact is None or artifact.kind != "file" or artifact.sha256 is None:
            raise KeyError(artifact_id)
        path = (record.workspace_dir / artifact.relative_path).resolve()
        if not _within(record.workspace_dir.resolve(), path) or not path.is_file():
            raise ConfigurationError("job artifact is missing or escapes its workspace")
        if sha256_file(path) != artifact.sha256:
            raise ConfigurationError("job artifact checksum changed after publication")
        return path, artifact

    def directory_artifact_path(self, job_id: str, artifact_id: str) -> tuple[Path, JobArtifact]:
        """Strictly resolve one published workspace-contained directory artifact."""
        record = self.get(job_id)
        artifact = next(
            (item for item in record.artifacts if item.artifact_id == artifact_id),
            None,
        )
        if artifact is None or artifact.kind != "directory":
            raise KeyError(artifact_id)
        path = (record.workspace_dir / artifact.relative_path).resolve()
        if not _within(record.workspace_dir.resolve(), path) or not path.is_dir():
            raise ConfigurationError("job directory artifact is missing or escapes its workspace")
        return path, artifact

    def _resolve_input(self, raw_path: Path) -> Path:
        path = raw_path.expanduser().resolve()
        if not any(_within(root, path) for root in self.config.input_roots):
            raise ConfigurationError("requested input path is outside configured input roots")
        return path

    def list_files(self, path: Path | None = None) -> FileListing:
        """List non-hidden local inputs while enforcing configured root containment."""
        roots = tuple(str(root) for root in self.config.input_roots)
        if path is None:
            root_entries = tuple(
                FileEntry(
                    name=root.name or str(root),
                    path=str(root),
                    kind="directory",
                    selectable=False,
                )
                for root in self.config.input_roots
                if root.is_dir()
            )
            return FileListing(path=None, parent=None, roots=roots, entries=root_entries)
        directory = self._resolve_input(path)
        if not directory.is_dir():
            raise ConfigurationError(f"input browser path is not a directory: {directory}")
        parent = directory.parent.resolve()
        safe_parent = (
            str(parent) if any(_within(root, parent) for root in self.config.input_roots) else None
        )
        entries: list[FileEntry] = []
        children = sorted(
            directory.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.casefold()),
        )
        for child in children:
            if child.name.startswith("."):
                continue
            resolved = child.resolve()
            if not any(_within(root, resolved) for root in self.config.input_roots):
                continue
            if child.is_dir():
                entries.append(
                    FileEntry(
                        name=child.name,
                        path=str(resolved),
                        kind="directory",
                        selectable=False,
                    )
                )
            elif child.is_file() and child.suffix.lower() in _SELECTABLE_SUFFIXES:
                entries.append(
                    FileEntry(
                        name=child.name,
                        path=str(resolved),
                        kind="file",
                        size_bytes=child.stat().st_size,
                        selectable=True,
                    )
                )
        return FileListing(
            path=str(directory),
            parent=safe_parent,
            roots=roots,
            entries=tuple(entries),
        )

    def iter_events(self, job_id: str, *, after: int = 0) -> Iterator[JobEvent]:
        """Yield current events for adapter-neutral streaming tests."""
        yield from self.read_events(job_id, after=after)
