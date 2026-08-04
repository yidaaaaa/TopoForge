"""Durable isolated local workflow job manager."""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import mimetypes
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
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
    JobBatchDeleteApplyRequest,
    JobBatchDeleteMode,
    JobBatchDeletePlan,
    JobBatchDeletePlanItem,
    JobBatchDeletePlanRequest,
    JobCreateRequest,
    JobDeleteRequest,
    JobDeleteResult,
    JobError,
    JobEvent,
    JobMaintenanceOverview,
    JobRecord,
    JobState,
    JobTrashActionRequest,
    JobTrashActionResult,
    JobTrashRecord,
    JobTrashTransaction,
    JobTrashTransactionMove,
    JobTrashWorkspace,
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


def _manifest_relative_file(
    *,
    workspace: Path,
    project_root: Path,
    relative: Any,
    role: str,
) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ConfigurationError(f"Bambu project {role} path must be relative")
    path = (project_root / relative).resolve()
    if not _within(workspace, path) or not _within(project_root, path):
        raise ConfigurationError(f"Bambu project {role} escapes the workflow workspace")
    if not path.is_file():
        raise ConfigurationError(f"Bambu project {role} is missing: {path}")
    return path


def _bambu_project_artifact_paths(workspace: Path, manifest_path: Path) -> dict[str, Path]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"Bambu project manifest is unreadable: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise ConfigurationError("Bambu project manifest root must be an object")
    if (
        manifest.get("schema_version") != "topoforge-bambu-tile-project-assembly-v1"
        or manifest.get("required_checks_passed") is not True
    ):
        raise ConfigurationError("Bambu project manifest has not passed its required checks")
    tiles = manifest.get("tiles")
    tile_count = manifest.get("tile_count")
    if (
        not isinstance(tiles, list)
        or not tiles
        or not isinstance(tile_count, int)
        or tile_count != len(tiles)
    ):
        raise ConfigurationError("Bambu project manifest has an invalid tile set")

    project_root = manifest_path.parent.resolve()
    resolved: dict[str, Path] = {}
    seen_tile_ids: set[str] = set()
    single_tile = tile_count == 1
    for tile in tiles:
        if not isinstance(tile, dict) or tile.get("required_checks_passed") is not True:
            raise ConfigurationError("Bambu project tile has not passed its required checks")
        tile_id = tile.get("tile_id")
        if not isinstance(tile_id, str):
            raise ConfigurationError("Bambu project manifest has an invalid tile id")
        parts = tile_id.split("-")
        if (
            len(parts) != 3
            or parts[0] != "tile"
            or len(parts[1]) != 5
            or not parts[1].startswith("r")
            or not parts[1][1:].isdigit()
            or len(parts[2]) != 5
            or not parts[2].startswith("c")
            or not parts[2][1:].isdigit()
            or tile_id in seen_tile_ids
        ):
            raise ConfigurationError("Bambu project manifest has an invalid tile id")
        seen_tile_ids.add(tile_id)
        files = tile.get("files")
        hashes = tile.get("sha256")
        if not isinstance(files, dict) or not isinstance(hashes, dict):
            raise ConfigurationError(f"Bambu project tile roles are invalid: {tile_id}")

        suffix = "" if single_tile else f"_{tile_id.replace('-', '_')}"
        project = _manifest_relative_file(
            workspace=workspace,
            project_root=project_root,
            relative=files.get("bambu_project_3mf"),
            role=f"{tile_id} project 3MF",
        )
        expected_project_hash = hashes.get("bambu_project_3mf")
        if (
            not isinstance(expected_project_hash, str)
            or sha256_file(project) != expected_project_hash
        ):
            raise ConfigurationError(f"Bambu project checksum mismatch: {tile_id}")
        resolved[f"bambu_project_3mf{suffix}"] = project

        validation = _manifest_relative_file(
            workspace=workspace,
            project_root=project_root,
            relative=tile.get("validation_path"),
            role=f"{tile_id} validation",
        )
        expected_validation_hash = tile.get("validation_sha256")
        if (
            not isinstance(expected_validation_hash, str)
            or sha256_file(validation) != expected_validation_hash
        ):
            raise ConfigurationError(f"Bambu project validation checksum mismatch: {tile_id}")
        resolved[f"bambu_project_validation{suffix}"] = validation
    return resolved


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


def _path_size_bytes(path: Path) -> int:
    """Measure one path without following symlinks outside its tree."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return 0
    if path.is_symlink() or not path.is_dir():
        return metadata.st_size
    return metadata.st_size + sum(_path_size_bytes(child) for child in path.iterdir())


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

    @property
    def trash_dir(self) -> Path:
        """Return the durable state root for recoverable task batches."""
        return self.config.state_dir / "trash"

    @property
    def deletion_audit_dir(self) -> Path:
        """Return the append-only result root for completed trash actions."""
        return self.config.state_dir / "deletion-audit"

    @property
    def trash_transactions_dir(self) -> Path:
        """Return the durable intent root for interrupted trash operations."""
        return self.config.state_dir / "trash-transactions"

    @property
    def workspace_trash_dir(self) -> Path:
        """Return the same-filesystem quarantine root below the workspace root."""
        return self.config.workspace_root / ".topoforge-trash"

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
            self.trash_dir.mkdir(parents=True, exist_ok=True)
            self.deletion_audit_dir.mkdir(parents=True, exist_ok=True)
            self.workspace_trash_dir.mkdir(parents=True, exist_ok=True)
            self.trash_transactions_dir.mkdir(parents=True, exist_ok=True)
            self._recover_trash_transactions()
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

    @staticmethod
    def _validate_batch_id(batch_id: str) -> str:
        if len(batch_id) != 32 or any(
            character not in "0123456789abcdef" for character in batch_id
        ):
            raise KeyError(batch_id)
        return batch_id

    def _trash_batch_dir(self, batch_id: str) -> Path:
        return self.trash_dir / self._validate_batch_id(batch_id)

    def _workspace_trash_batch_dir(self, batch_id: str) -> Path:
        return self.workspace_trash_dir / self._validate_batch_id(batch_id)

    def _trash_record_path(self, batch_id: str) -> Path:
        return self._trash_batch_dir(batch_id) / "trash.json"

    def _trash_transaction_dir(self, batch_id: str) -> Path:
        return self.trash_transactions_dir / self._validate_batch_id(batch_id)

    def _trash_transaction_path(self, batch_id: str) -> Path:
        return self._trash_transaction_dir(batch_id) / "transaction.json"

    def _read_trash_record(self, batch_id: str) -> JobTrashRecord:
        path = self._trash_record_path(batch_id)
        if not path.is_file():
            raise KeyError(batch_id)
        return _read_model(path, JobTrashRecord)

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        return left.expanduser().resolve() == right.expanduser().resolve()

    def _validate_trash_transaction(
        self,
        transaction: JobTrashTransaction,
    ) -> JobTrashTransaction:
        batch_id = self._validate_batch_id(transaction.batch_id)
        expected_paths = (
            (transaction.state_temporary, self.trash_dir / f".{batch_id}.creating"),
            (transaction.state_destination, self._trash_batch_dir(batch_id)),
            (
                transaction.workspace_temporary,
                self.workspace_trash_dir / f".{batch_id}.creating",
            ),
            (
                transaction.workspace_destination,
                self._workspace_trash_batch_dir(batch_id),
            ),
        )
        if any(not self._same_path(actual, expected) for actual, expected in expected_paths):
            raise ConfigurationError("trash transaction contains an unexpected batch path")
        if transaction.trash_record.batch_id != batch_id:
            raise ConfigurationError("trash transaction batch identity does not match its record")

        expected_job_ids = set(transaction.trash_record.job_ids)
        actual_job_ids: set[str] = set()
        for move in transaction.job_moves:
            job_id = move.source.name
            self._job_dir(job_id)
            expected = JobTrashTransactionMove(
                source=self._job_dir(job_id),
                temporary=transaction.state_temporary / "jobs" / job_id,
                destination=transaction.state_destination / "jobs" / job_id,
            )
            if move != expected:
                raise ConfigurationError("trash transaction contains an unexpected job path")
            actual_job_ids.add(job_id)
        if actual_job_ids != expected_job_ids or len(actual_job_ids) != len(transaction.job_moves):
            raise ConfigurationError("trash transaction job identities do not match its record")

        expected_workspace_moves = {
            (
                workspace.original_workspace.expanduser().resolve(),
                workspace.quarantined_workspace.expanduser().resolve(),
            )
            for workspace in transaction.trash_record.workspaces
            if workspace.quarantined_workspace is not None
        }
        actual_workspace_moves: set[tuple[Path, Path]] = set()
        workspace_root = self.config.workspace_root.resolve()
        for move in transaction.workspace_moves:
            source = move.source.expanduser().resolve()
            temporary = move.temporary.expanduser().resolve()
            destination = move.destination.expanduser().resolve()
            if source == workspace_root or workspace_root not in source.parents:
                raise ConfigurationError("trash transaction workspace escaped its configured root")
            if (
                transaction.workspace_temporary.resolve() not in temporary.parents
                or transaction.workspace_destination.resolve() not in destination.parents
                or temporary.relative_to(transaction.workspace_temporary.resolve())
                != destination.relative_to(transaction.workspace_destination.resolve())
            ):
                raise ConfigurationError(
                    "trash transaction contains an unexpected workspace quarantine path"
                )
            actual_workspace_moves.add((source, destination))
        if actual_workspace_moves != expected_workspace_moves or len(actual_workspace_moves) != len(
            transaction.workspace_moves
        ):
            raise ConfigurationError(
                "trash transaction workspaces do not match its recovery record"
            )
        return transaction

    @staticmethod
    def _transaction_move_location(move: JobTrashTransactionMove) -> Path:
        locations = tuple(
            path
            for path in (move.source, move.temporary, move.destination)
            if path.exists() or path.is_symlink()
        )
        if len(locations) != 1:
            raise ConfigurationError(
                "trash transaction recovery requires exactly one copy of every moved path"
            )
        if locations[0].is_symlink():
            raise ConfigurationError("trash transaction moved path must not be a symlink")
        return locations[0]

    def _remove_trash_transaction(self, transaction: JobTrashTransaction) -> None:
        path = self._trash_transaction_path(transaction.batch_id)
        path.unlink()
        with contextlib.suppress(OSError):
            path.parent.rmdir()

    def _rollback_trash_transaction(self, transaction: JobTrashTransaction) -> None:
        for move in reversed((*transaction.job_moves, *transaction.workspace_moves)):
            current = self._transaction_move_location(move)
            if current != move.source:
                move.source.parent.mkdir(parents=True, exist_ok=True)
                current.replace(move.source)
        for record_path in (
            transaction.state_temporary / "trash.json",
            transaction.state_destination / "trash.json",
        ):
            if record_path.exists():
                record_path.unlink()
        for path in (
            transaction.state_temporary / "jobs",
            transaction.state_temporary,
            transaction.state_destination / "jobs",
            transaction.state_destination,
            transaction.workspace_temporary,
            transaction.workspace_destination,
        ):
            if path.exists():
                path.rmdir()
        self._remove_trash_transaction(transaction)

    def _publish_trash_transaction(
        self,
        transaction: JobTrashTransaction,
    ) -> JobTrashRecord:
        for move in (*transaction.job_moves, *transaction.workspace_moves):
            self._transaction_move_location(move)
        temporary_record = transaction.state_temporary / "trash.json"
        published_record = transaction.state_destination / "trash.json"
        if transaction.state_temporary.exists() and transaction.state_destination.exists():
            raise ConfigurationError("trash transaction has duplicate state batch directories")
        if published_record.is_file():
            reopened = _read_model(published_record, JobTrashRecord)
        elif temporary_record.is_file():
            reopened = _read_model(temporary_record, JobTrashRecord)
            if transaction.workspace_moves:
                if transaction.workspace_destination.exists():
                    if transaction.workspace_temporary.exists():
                        raise ConfigurationError(
                            "trash transaction has duplicate workspace batch directories"
                        )
                elif transaction.workspace_temporary.exists():
                    transaction.workspace_temporary.replace(transaction.workspace_destination)
                else:
                    raise ConfigurationError(
                        "trash transaction workspace batch is missing during publication"
                    )
            transaction.state_temporary.replace(transaction.state_destination)
        else:
            raise ConfigurationError("trash transaction is not ready for publication")
        if reopened != transaction.trash_record:
            raise ConfigurationError("trash transaction record changed before publication")
        verified = self._verify_trash_record(reopened)
        self._remove_trash_transaction(transaction)
        return verified

    def _recover_trash_transactions(self) -> None:
        for path in sorted(self.trash_transactions_dir.glob("*/transaction.json")):
            if path.is_symlink() or path.parent.is_symlink():
                raise ConfigurationError("trash transaction path must not be a symlink")
            transaction = self._validate_trash_transaction(_read_model(path, JobTrashTransaction))
            if path != self._trash_transaction_path(transaction.batch_id):
                raise ConfigurationError("trash transaction is stored under the wrong batch id")
            if (transaction.state_temporary / "trash.json").is_file() or (
                transaction.state_destination / "trash.json"
            ).is_file():
                self._publish_trash_transaction(transaction)
            else:
                self._rollback_trash_transaction(transaction)

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
                if (
                    record.state is JobState.COMPLETED
                    and record.summary is not None
                    and "project_manifest" in record.summary.artifacts
                    and not any(
                        artifact.artifact_id.startswith("bambu_project_3mf")
                        for artifact in record.artifacts
                    )
                ):
                    record = self._write_record(
                        record.model_copy(
                            update={
                                "artifacts": self._artifacts(
                                    record,
                                    record.summary.artifacts,
                                )
                            }
                        )
                    )
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
        expanded_values = dict(values)
        raw_manifest = expanded_values.get("project_manifest")
        if raw_manifest is not None:
            manifest_path = Path(raw_manifest).resolve()
            if not _within(workspace, manifest_path):
                raise ConfigurationError(f"workflow artifact escapes workspace: {manifest_path}")
            derived = _bambu_project_artifact_paths(workspace, manifest_path)
            duplicates = sorted(set(expanded_values).intersection(derived))
            if duplicates:
                raise ConfigurationError(
                    "workflow artifact roles collide with Bambu project roles: "
                    + ", ".join(duplicates)
                )
            expanded_values.update({role: str(path) for role, path in derived.items()})

        artifacts: list[JobArtifact] = []
        for role, raw_path in sorted(expanded_values.items()):
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

    def plan_batch_delete(self, request: JobBatchDeletePlanRequest) -> JobBatchDeletePlan:
        """Measure one terminal-job batch without changing records or workspaces."""
        with self._lock:
            self.refresh()
            job_ids = tuple(sorted(request.job_ids))
            records = {job_id: self._read_record(job_id) for job_id in job_ids}
            all_records = self._all_records()
            selected = set(job_ids)
            terminal_states = {
                JobState.CANCELLED,
                JobState.COMPLETED,
                JobState.FAILED,
            }
            backups_by_workflow: dict[str, list[str]] = {}
            for backup in self.list_backups():
                backups_by_workflow.setdefault(backup.workflow_id, []).append(backup.backup_id)

            selected_by_workspace: dict[Path, list[JobRecord]] = {}
            references_by_workspace: dict[Path, list[str]] = {}
            source_paths: dict[Path, Path] = {}
            for record in all_records:
                source_path = record.workspace_dir.expanduser()
                workspace = source_path.resolve()
                references_by_workspace.setdefault(workspace, []).append(record.job_id)
                if record.job_id in selected:
                    selected_by_workspace.setdefault(workspace, []).append(record)
                    source_paths[workspace] = source_path

            workspace_blockers: dict[Path, tuple[str, ...]] = {}
            workspace_sizes: dict[Path, int] = {}
            backup_job_by_workspace: dict[Path, str] = {}
            for workspace, workspace_records in selected_by_workspace.items():
                source_path = source_paths[workspace]
                blockers: list[str] = []
                exists = source_path.exists() or source_path.is_symlink()
                workspace_sizes[workspace] = _path_size_bytes(source_path) if exists else 0
                if request.mode is not JobBatchDeleteMode.RECORD_ONLY:
                    workspace_root = self.config.workspace_root.resolve()
                    if source_path.is_symlink():
                        blockers.append(
                            "workspace is a symlink and cannot be moved into quarantine"
                        )
                    if workspace == workspace_root or workspace_root not in workspace.parents:
                        blockers.append("workspace is outside the configured workspace root")
                    unselected = sorted(set(references_by_workspace.get(workspace, ())) - selected)
                    if unselected:
                        blockers.append(
                            "workspace is still referenced by unselected jobs: "
                            + ", ".join(unselected)
                        )
                    if request.mode is JobBatchDeleteMode.BACKUP_AND_QUARANTINE:
                        if not exists:
                            blockers.append(
                                "workspace is missing and cannot be verified for backup"
                            )
                        candidates = sorted(
                            record.job_id
                            for record in workspace_records
                            if record.state is JobState.COMPLETED and record.summary is not None
                        )
                        if candidates:
                            backup_job_by_workspace[workspace] = candidates[0]
                        else:
                            blockers.append(
                                "verified workflow backup requires a selected completed job"
                            )
                workspace_blockers[workspace] = tuple(blockers)

            items: list[JobBatchDeletePlanItem] = []
            aggregate_blockers: list[str] = []
            for job_id in job_ids:
                record = records[job_id]
                workspace = record.workspace_dir.expanduser().resolve()
                blockers = list(workspace_blockers[workspace])
                if record.state not in terminal_states:
                    blockers.insert(0, "job is active; cancel it before batch removal")
                workflow_id = record.summary.workflow_id if record.summary is not None else None
                verified_backups = tuple(
                    sorted(backups_by_workflow.get(workflow_id, ()))
                    if workflow_id is not None
                    else ()
                )
                references = tuple(sorted(references_by_workspace.get(workspace, ())))
                unselected_references = tuple(sorted(set(references) - selected))
                item = JobBatchDeletePlanItem(
                    job_id=job_id,
                    state=record.state,
                    workspace=workspace,
                    workspace_existed=(
                        record.workspace_dir.expanduser().exists()
                        or record.workspace_dir.expanduser().is_symlink()
                    ),
                    job_record_bytes=_path_size_bytes(self._job_dir(job_id)),
                    workspace_bytes=(
                        0
                        if request.mode is JobBatchDeleteMode.RECORD_ONLY
                        else workspace_sizes[workspace]
                    ),
                    workspace_reference_job_ids=references,
                    unselected_reference_job_ids=unselected_references,
                    verified_backup_ids=verified_backups,
                    eligible=not blockers,
                    blockers=tuple(blockers),
                )
                items.append(item)
                aggregate_blockers.extend(f"{job_id}: {blocker}" for blocker in blockers)

            job_record_bytes = sum(item.job_record_bytes for item in items)
            workspace_bytes = (
                0
                if request.mode is JobBatchDeleteMode.RECORD_ONLY
                else sum(workspace_sizes.values())
            )
            provisional = JobBatchDeletePlan(
                plan_id="0" * 64,
                mode=request.mode,
                job_ids=job_ids,
                items=tuple(items),
                selected_job_count=len(job_ids),
                eligible_job_count=sum(item.eligible for item in items),
                unique_workspace_count=len(selected_by_workspace),
                job_record_bytes=job_record_bytes,
                workspace_bytes=workspace_bytes,
                total_target_bytes=job_record_bytes + workspace_bytes,
                backup_job_ids=tuple(sorted(backup_job_by_workspace.values())),
                blockers=tuple(aggregate_blockers),
                required_checks_passed=not aggregate_blockers,
            )
            identity = provisional.model_dump(mode="json", exclude={"plan_id"})
            plan_id = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
            return provisional.model_copy(update={"plan_id": plan_id})

    def _verify_trash_record(self, record: JobTrashRecord) -> JobTrashRecord:
        batch_dir = self._trash_batch_dir(record.batch_id)
        if not record.required_checks_passed:
            raise ConfigurationError("job trash record did not pass its creation checks")
        for job_id in record.job_ids:
            quarantined_job = batch_dir / "jobs" / job_id
            if not quarantined_job.is_dir() or quarantined_job.is_symlink():
                raise ConfigurationError(f"quarantined job record is missing or unsafe: {job_id}")
        workspace_batch = self._workspace_trash_batch_dir(record.batch_id)
        for workspace in record.workspaces:
            quarantined = workspace.quarantined_workspace
            if quarantined is None:
                continue
            if workspace_batch.resolve() not in quarantined.resolve().parents:
                raise ConfigurationError("quarantined workspace escaped its batch root")
            if not quarantined.exists() or quarantined.is_symlink():
                raise ConfigurationError(
                    f"quarantined workspace is missing or unsafe: {quarantined}"
                )
        for backup_id in record.backup_ids:
            self.backup_archive_path(backup_id)
        return record

    def list_trash(self) -> tuple[JobTrashRecord, ...]:
        """Strictly reopen recoverable batches newest first."""
        with self._lock:
            if not self.trash_dir.exists():
                return ()
            records = [
                self._verify_trash_record(_read_model(path, JobTrashRecord))
                for path in self.trash_dir.glob("*/trash.json")
            ]
            return tuple(sorted(records, key=lambda item: item.created_at, reverse=True))

    def apply_batch_delete(self, request: JobBatchDeleteApplyRequest) -> JobTrashRecord:
        """Apply one unchanged reviewed plan by moving records and workspaces to trash."""
        with self._lock:
            plan = self.plan_batch_delete(
                JobBatchDeletePlanRequest(job_ids=request.job_ids, mode=request.mode)
            )
            if request.confirm_plan_id != plan.plan_id:
                raise ConfigurationError(
                    "batch deletion plan changed; review the new measured plan before applying"
                )
            if not plan.required_checks_passed:
                raise ConfigurationError(
                    "batch deletion plan has blockers: " + "; ".join(plan.blockers)
                )

            backup_ids = tuple(
                sorted({self.create_backup(job_id).backup_id for job_id in plan.backup_job_ids})
            )
            batch_id = uuid4().hex
            state_temporary = self.trash_dir / f".{batch_id}.creating"
            state_destination = self._trash_batch_dir(batch_id)
            workspace_temporary = self.workspace_trash_dir / f".{batch_id}.creating"
            workspace_destination = self._workspace_trash_batch_dir(batch_id)
            transaction_dir = self._trash_transaction_dir(batch_id)
            if any(
                path.exists() or path.is_symlink()
                for path in (
                    state_temporary,
                    state_destination,
                    workspace_temporary,
                    workspace_destination,
                    transaction_dir,
                )
            ):
                raise ConfigurationError("generated trash batch destination already exists")

            workspace_items: dict[Path, JobBatchDeletePlanItem] = {}
            for item in plan.items:
                workspace_items.setdefault(item.workspace, item)
            workspace_moves: list[JobTrashTransactionMove] = []
            trash_workspaces: list[JobTrashWorkspace] = []
            for index, (workspace, item) in enumerate(sorted(workspace_items.items())):
                existed = workspace.exists()
                size_bytes = _path_size_bytes(workspace) if existed else 0
                quarantined: Path | None = None
                if request.mode is not JobBatchDeleteMode.RECORD_ONLY and existed:
                    name = f"{index:04d}-{workspace.name or 'workspace'}"
                    temporary = workspace_temporary / name
                    quarantined = workspace_destination / name
                    workspace_moves.append(
                        JobTrashTransactionMove(
                            source=workspace,
                            temporary=temporary,
                            destination=quarantined,
                        )
                    )
                trash_workspaces.append(
                    JobTrashWorkspace(
                        original_workspace=workspace,
                        quarantined_workspace=quarantined,
                        workspace_existed=item.workspace_existed,
                        size_bytes=size_bytes,
                    )
                )

            job_moves = tuple(
                JobTrashTransactionMove(
                    source=self._job_dir(job_id),
                    temporary=state_temporary / "jobs" / job_id,
                    destination=state_destination / "jobs" / job_id,
                )
                for job_id in plan.job_ids
            )
            created_at = utc_now()
            trash_record = JobTrashRecord(
                batch_id=batch_id,
                plan_id=plan.plan_id,
                mode=plan.mode,
                created_at=created_at,
                purge_after=created_at + timedelta(days=7),
                job_ids=plan.job_ids,
                job_record_bytes=plan.job_record_bytes,
                workspaces=tuple(trash_workspaces),
                backup_ids=backup_ids,
                total_quarantined_bytes=plan.total_target_bytes,
                backups_preserved=True,
                required_checks_passed=True,
            )
            transaction = self._validate_trash_transaction(
                JobTrashTransaction(
                    batch_id=batch_id,
                    state_temporary=state_temporary,
                    state_destination=state_destination,
                    workspace_temporary=workspace_temporary,
                    workspace_destination=workspace_destination,
                    job_moves=job_moves,
                    workspace_moves=tuple(workspace_moves),
                    trash_record=trash_record,
                )
            )

            self.trash_dir.mkdir(parents=True, exist_ok=True)
            self.workspace_trash_dir.mkdir(parents=True, exist_ok=True)
            self.trash_transactions_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(self._trash_transaction_path(batch_id), transaction)
            state_temporary.mkdir(parents=True)
            (state_temporary / "jobs").mkdir()
            if workspace_moves:
                workspace_temporary.mkdir(parents=True)

            try:
                for move in transaction.workspace_moves:
                    move.source.replace(move.temporary)
                for move in transaction.job_moves:
                    move.source.replace(move.temporary)
                _atomic_write(state_temporary / "trash.json", trash_record)
                published = self._publish_trash_transaction(transaction)
            except BaseException:
                if self._trash_transaction_path(batch_id).is_file():
                    self._rollback_trash_transaction(transaction)
                raise
            for job_id in plan.job_ids:
                self._processes.pop(job_id, None)
            return published

    def _write_trash_audit(
        self,
        record: JobTrashRecord,
        *,
        action: str,
        affected_bytes: int,
    ) -> None:
        self.deletion_audit_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            self.deletion_audit_dir / f"{record.batch_id}-{action}.json",
            {
                "schema_version": "topoforge-web-job-trash-audit-v1",
                "batch_id": record.batch_id,
                "plan_id": record.plan_id,
                "action": action,
                "occurred_at": utc_now().isoformat(),
                "job_ids": record.job_ids,
                "affected_bytes": affected_bytes,
                "backup_ids": record.backup_ids,
                "backups_preserved": True,
                "required_checks_passed": True,
            },
        )

    def restore_trash(
        self,
        batch_id: str,
        request: JobTrashActionRequest,
    ) -> JobTrashActionResult:
        """Restore every job record and quarantined workspace in one batch."""
        with self._lock:
            if request.confirm_batch_id != batch_id:
                raise ConfigurationError(
                    "trash restore confirmation does not match the selected batch"
                )
            record = self._verify_trash_record(self._read_trash_record(batch_id))
            for job_id in record.job_ids:
                if self._job_dir(job_id).exists():
                    raise ConfigurationError(
                        f"job id already exists and blocks trash restore: {job_id}"
                    )
            for workspace in record.workspaces:
                original = workspace.original_workspace
                if workspace.quarantined_workspace is not None and original.exists():
                    raise ConfigurationError(
                        f"workspace destination already exists and blocks restore: {original}"
                    )
                if (
                    workspace.quarantined_workspace is None
                    and workspace.workspace_existed
                    and not original.exists()
                ):
                    raise ConfigurationError(
                        f"retained workspace is missing and blocks job record restore: {original}"
                    )

            workspace_moves: list[tuple[Path, Path]] = []
            job_moves: list[tuple[Path, Path]] = []
            try:
                for workspace in record.workspaces:
                    quarantined = workspace.quarantined_workspace
                    if quarantined is None:
                        continue
                    workspace.original_workspace.parent.mkdir(parents=True, exist_ok=True)
                    quarantined.replace(workspace.original_workspace)
                    workspace_moves.append((workspace.original_workspace, quarantined))
                for job_id in record.job_ids:
                    source = self._trash_batch_dir(batch_id) / "jobs" / job_id
                    destination = self._job_dir(job_id)
                    source.replace(destination)
                    job_moves.append((destination, source))
            except BaseException:
                for destination, source in reversed(job_moves):
                    if destination.exists() and not source.exists():
                        destination.replace(source)
                for destination, source in reversed(workspace_moves):
                    if destination.exists() and not source.exists():
                        destination.replace(source)
                raise

            workspace_batch = self._workspace_trash_batch_dir(batch_id)
            if workspace_batch.exists():
                shutil.rmtree(workspace_batch)
            shutil.rmtree(self._trash_batch_dir(batch_id))
            self._write_trash_audit(
                record,
                action="restored",
                affected_bytes=record.total_quarantined_bytes,
            )
            checks = all(self._job_dir(job_id).is_dir() for job_id in record.job_ids)
            if not checks:
                raise ConfigurationError("trash restore finished without all job records")
            return JobTrashActionResult(
                batch_id=batch_id,
                action="restored",
                job_ids=record.job_ids,
                workspace_count=len(record.workspaces),
                affected_bytes=record.total_quarantined_bytes,
                backups_preserved=True,
                required_checks_passed=True,
            )

    def purge_trash(
        self,
        batch_id: str,
        request: JobTrashActionRequest,
    ) -> JobTrashActionResult:
        """Permanently remove one reviewed trash batch while preserving backups."""
        with self._lock:
            if request.confirm_batch_id != batch_id:
                raise ConfigurationError(
                    "trash purge confirmation does not match the selected batch"
                )
            record = self._verify_trash_record(self._read_trash_record(batch_id))
            state_batch = self._trash_batch_dir(batch_id)
            workspace_batch = self._workspace_trash_batch_dir(batch_id)
            affected_bytes = _path_size_bytes(state_batch) + _path_size_bytes(workspace_batch)
            if workspace_batch.exists():
                shutil.rmtree(workspace_batch)
            shutil.rmtree(state_batch)
            self._write_trash_audit(
                record,
                action="purged",
                affected_bytes=affected_bytes,
            )
            if state_batch.exists() or workspace_batch.exists():
                raise ConfigurationError("trash purge finished without removing every batch path")
            return JobTrashActionResult(
                batch_id=batch_id,
                action="purged",
                job_ids=record.job_ids,
                workspace_count=len(record.workspaces),
                affected_bytes=affected_bytes,
                backups_preserved=True,
                required_checks_passed=True,
            )

    def delete(
        self,
        job_id: str,
        request: JobDeleteRequest,
    ) -> JobDeleteResult:
        """Remove one terminal job record and optionally its unshared workspace."""
        with self._lock:
            self.refresh()
            record = self._read_record(job_id)
            if request.confirm_job_id != job_id:
                raise ConfigurationError(
                    "job deletion confirmation does not match the selected job"
                )
            terminal_states = {
                JobState.CANCELLED,
                JobState.COMPLETED,
                JobState.FAILED,
            }
            if record.state not in terminal_states:
                raise ConfigurationError(
                    "only cancelled, failed, or completed jobs can be deleted; "
                    "cancel the selected job first"
                )

            workspace_path = record.workspace_dir.expanduser()
            workspace = workspace_path.resolve()
            workspace_existed = workspace_path.exists() or workspace_path.is_symlink()
            workspace_bytes = 0
            if request.delete_workspace:
                workspace_root = self.config.workspace_root.resolve()
                if workspace_path.is_symlink():
                    raise ConfigurationError(
                        "job workspace is a symlink and will not be recursively deleted"
                    )
                if workspace == workspace_root or workspace_root not in workspace.parents:
                    raise ConfigurationError(
                        "job workspace is outside the configured workspace root"
                    )
                shared = tuple(
                    item.job_id
                    for item in self._all_records()
                    if item.job_id != job_id
                    and item.workspace_dir.expanduser().resolve() == workspace
                )
                if shared:
                    raise ConfigurationError(
                        "job workspace is still referenced by other jobs: "
                        + ", ".join(shared)
                        + ". Remove only this task record or delete the other task "
                        "records first."
                    )
                workspace_bytes = _path_size_bytes(workspace_path)

            job_dir = self._job_dir(job_id)
            job_record_bytes = _path_size_bytes(job_dir)
            workspace_removed = False
            if request.delete_workspace and workspace_existed:
                if workspace_path.is_dir():
                    shutil.rmtree(workspace_path)
                else:
                    workspace_path.unlink()
                workspace_removed = True
            shutil.rmtree(job_dir)
            self._processes.pop(job_id, None)

            workspace_retained = workspace_path.exists() or workspace_path.is_symlink()
            required_checks_passed = not job_dir.exists() and (
                not request.delete_workspace or not workspace_retained
            )
            if not required_checks_passed:
                raise ConfigurationError(
                    "job deletion finished without satisfying filesystem checks"
                )
            deleted_workspace_bytes = workspace_bytes if workspace_removed else 0
            return JobDeleteResult(
                job_id=job_id,
                previous_state=record.state,
                workspace=workspace,
                workspace_existed=workspace_existed,
                workspace_removed=workspace_removed,
                workspace_retained=workspace_retained,
                deleted_job_record_bytes=job_record_bytes,
                deleted_workspace_bytes=deleted_workspace_bytes,
                reclaimed_bytes=job_record_bytes + deleted_workspace_bytes,
                backups_preserved=True,
                required_checks_passed=True,
            )

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
